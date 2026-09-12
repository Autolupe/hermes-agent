"""Hermetic delivery command wiring and worker capability boundaries."""

import argparse
import contextlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban as cli


def _args(*words):
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["kanban", *words])


@pytest.fixture
def board(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "interface.db")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tasks (id TEXT, status TEXT, current_run_id INTEGER);
        CREATE TABLE task_runs (
            id INTEGER, task_id TEXT, status TEXT, ended_at INTEGER, outcome TEXT
        );
        INSERT INTO tasks VALUES ('t_owned', 'running', 7);
        INSERT INTO task_runs VALUES (7, 't_owned', 'running', NULL, NULL);
    """)
    task = SimpleNamespace(
        id="t_owned", status="running", current_run_id=7, goal_mode=False,
        body="", workspace_kind="worktree", branch_name="task/t_owned",
    )
    calls = []

    @contextlib.contextmanager
    def connect():
        yield conn

    def record(name, result):
        def call(_conn, task_id, **kwargs):
            calls.append((name, task_id, kwargs))
            return result
        return call

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_owned")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.delenv("HERMES_KANBAN_TERMINAL_SANDBOX", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DELIVERY_CONTROL", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(cli.kb, "connect_closing", connect)
    monkeypatch.setattr(cli.kb, "init_db", lambda: None)
    monkeypatch.setattr(cli.kb, "get_task", lambda _conn, tid: task if tid == task.id else None)
    monkeypatch.setattr(cli, "_shared_delivery_config", lambda: {"reviewer_profile": "reviewer", "max_review_rounds": 3})
    monkeypatch.setattr(cli, "_goal_mode_handoff_rejection", lambda task, summary: None)
    monkeypatch.setattr(cli.kb, "complete_task", record("complete", True))
    monkeypatch.setattr(cli.kb, "submit_task_for_review", record("submit", True))
    monkeypatch.setattr(cli.kb, "request_task_changes", record("changes", "ready"))
    monkeypatch.setattr(cli.kb, "request_changes", record("legacy_changes", (True, "builder")))
    yield SimpleNamespace(conn=conn, task=task, calls=calls)
    conn.close()


def test_complete_passes_structured_evidence_with_captured_worker_run(board):
    delivery = {"classification": "merged_pr", "head_sha": "a" * 40, "merge_sha": "b" * 40}
    assert cli.kanban_command(_args("complete", "t_owned", "--summary", "checked", "--delivery", json.dumps(delivery))) == 0
    assert board.calls == [("complete", "t_owned", {
        "result": None, "summary": "checked", "metadata": None,
        "delivery": delivery, "expected_run_id": 7,
    })]


@pytest.mark.parametrize("raw", ["null", "[]", '"text"', "bad-json", ""])
def test_invalid_delivery_json_refuses_without_mutation(board, raw):
    assert cli.kanban_command(_args("complete", "t_owned", "--summary", "checked", "--delivery", raw)) != 0
    assert board.calls == []


def test_bulk_delivery_evidence_is_refused(board):
    assert cli.kanban_command(_args("complete", "t_owned", "t_other", "--delivery", "{}")) != 0
    assert board.calls == []


def test_submit_routes_exact_candidate_and_shared_reviewer(board):
    candidate = {"pr_url": "https://github.com/acme/widgets/pull/3", "pr_number": 3,
                 "head_sha": "a" * 40, "candidate_ref": "task/t_owned"}
    assert cli.kanban_command(_args("submit-for-review", "t_owned", "--summary", "checked", "--pull-request", json.dumps(candidate))) == 0
    assert board.calls == [("submit", "t_owned", {
        "pull_request": candidate, "summary": "checked", "metadata": None,
        "reviewer_assignee": "reviewer", "expected_run_id": 7,
    })]


@pytest.mark.parametrize("command,tail", [
    ("complete", ["--summary", "checked"]),
    ("submit-for-review", ["--pull-request", "{}"]),
    ("request-changes", ["fix", "--reviewed-head-sha", "a" * 40]),
])
@pytest.mark.parametrize("run_id", ["", "0", "-1", "bad", "７", "8"])
def test_invalid_worker_identity_refuses_before_judge_or_mutation(board, monkeypatch, command, tail, run_id):
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    def no_judge(*args):
        pytest.fail("invalid worker reached judge")
    monkeypatch.setattr(cli, "_goal_mode_handoff_rejection", no_judge)
    assert cli.kanban_command(_args(command, "t_owned", *tail)) != 0
    assert board.calls == []


@pytest.mark.parametrize("change", [
    "UPDATE task_runs SET status='done'",
    "UPDATE task_runs SET outcome='completed'",
    "UPDATE task_runs SET ended_at=1",
    "UPDATE task_runs SET task_id='t_other'",
    "UPDATE tasks SET status='shipping'",
])
def test_non_open_attempt_cannot_complete(board, change):
    board.conn.execute(change)
    assert cli.kanban_command(_args("complete", "t_owned", "--summary", "checked")) != 0
    assert board.calls == []


def test_foreign_task_refuses_before_mutation(board):
    assert cli.kanban_command(_args("submit-for-review", "t_other", "--pull-request", "{}")) != 0
    assert board.calls == []


def test_code_request_review_cannot_skip_candidate_submission(board):
    assert cli.kanban_command(_args("request-review", "t_owned", "--summary", "checked")) != 0
    assert board.calls == []


def test_code_changes_route_exact_head_and_round_limit(board):
    assert cli.kanban_command(_args("request-changes", "t_owned", "fix", "--reviewed-head-sha", "a" * 40)) == 0
    assert board.calls == [("changes", "t_owned", {
        "reason": "fix", "reviewed_head_sha": "a" * 40,
        "max_review_rounds": 3, "expected_run_id": 7,
    })]


def test_non_code_changes_preserve_modern_review_route(board):
    board.task.workspace_kind = "scratch"
    board.task.branch_name = None
    monkey_run = SimpleNamespace(summary="checked")
    from unittest.mock import patch
    with patch.object(cli.kb, "latest_run", return_value=monkey_run):
        assert cli.kanban_command(_args("request-changes", "t_owned", "fix")) == 0
    assert board.calls == [("legacy_changes", "t_owned", {"reason": "fix", "expected_run_id": 7})]


@pytest.mark.parametrize("command,tail", [
    ("complete", ["--summary", "checked"]),
    ("submit-for-review", ["--pull-request", "{}"]),
    ("request-review", ["--summary", "checked"]),
    ("request-changes", ["fix"]),
])
@pytest.mark.parametrize("marker,value", [
    ("HERMES_KANBAN_DELIVERY_CONTROL", "clauseye-v1"),
    ("HERMES_KANBAN_TERMINAL_SANDBOX", "systemd-v1"),
    ("HERMES_DELEGATED_CHILD_CONTEXT", "1"),
])
def test_child_or_sandbox_cli_cannot_take_delivery_capability(board, monkeypatch, command, tail, marker, value):
    monkeypatch.setenv(marker, value)
    def no_init():
        pytest.fail("restricted CLI reached board initialization")
    monkeypatch.setattr(cli.kb, "init_db", no_init)
    assert cli.kanban_command(_args(command, "t_owned", *tail)) != 0
    assert board.calls == []


def test_delivery_evidence_rejection_is_visible(board, monkeypatch, capsys):
    def reject(*args, **kwargs):
        raise cli.kb.DeliveryEvidenceError("candidate changed", code="candidate_mismatch", policy={})
    monkeypatch.setattr(cli.kb, "complete_task", reject)
    assert cli.kanban_command(_args("complete", "t_owned", "--summary", "checked")) == 1
    assert "candidate_mismatch" in capsys.readouterr().err
    assert board.conn.execute("SELECT status FROM tasks").fetchone()[0] == "running"
