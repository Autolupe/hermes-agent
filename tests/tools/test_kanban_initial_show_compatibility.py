"""Preserve the real initial kanban_show response during collection extraction.

The fixture contains exact strings captured from the actual _handle_show at
64304ef6cfdda67abf3facd1f67c5cb698e3b1f0, using only temporary SQLite boards
and a fixed clock. The additional checks explain the ordering, filtering,
fallback, and error contracts rather than duplicating the old formatter.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt


NOW = 1_800_000_000
LEGACY = Path(__file__).with_name("initial_show_legacy.json")


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "initial-show.db"
    with kb.connect_closing(path):
        pass
    return path


def _insert(conn, table, values):
    columns = ", ".join(values)
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


def _task(conn, task_id="task-main", **overrides):
    values = {
        "id": task_id, "title": "Initial task view", "status": "running",
        "created_at": NOW - 86_400,
    }
    values.update(overrides)
    _insert(conn, "tasks", values)


def _run(conn, **overrides):
    values = {
        "task_id": "task-main", "profile": "writer", "status": "done",
        "outcome": "completed", "started_at": NOW - 3_600,
        "ended_at": NOW - 3_000, "summary": "Earlier result.",
    }
    values.update(overrides)
    _insert(conn, "task_runs", values)


def _comment(conn, **overrides):
    values = {
        "task_id": "task-main", "author": "writer", "body": "A comment.",
        "created_at": NOW - 600,
    }
    values.update(overrides)
    _insert(conn, "task_comments", values)


def _event(conn, **overrides):
    values = {
        "task_id": "task-main", "kind": "fixture-event",
        "created_at": NOW - 600, "payload": None, "run_id": None,
    }
    values.update(overrides)
    _insert(conn, "task_events", values)


def _seed_rich(conn):
    _task(
        conn, title="Résumé: keep the initial view", body="  Body\nwith café.  ",
        assignee="writer", tenant="fixture-tenant", priority=7,
        workspace_kind="worktree", workspace_path="/fixture/workspace",
        created_by="operator", started_at=NOW - 120, completed_at=NOW - 60,
        result="A stored task result.", current_run_id=11,
        model_override="fixture-model", provider_override="fixture-provider",
        branch_name="fixture/initial-view", max_runtime_seconds=90,
    )
    _task(conn, "parent-z", status="todo")
    _task(conn, "parent-a", status="done", result="Legacy parent handoff.")
    _task(conn, "child-z", status="todo")
    _task(conn, "child-a", status="triage")
    for parent in ("parent-z", "parent-a", "parent-missing"):
        _insert(conn, "task_links", {"parent_id": parent, "child_id": "task-main"})
    for child in ("child-z", "child-a", "child-missing"):
        _insert(conn, "task_links", {"parent_id": "task-main", "child_id": child})
    _run(
        conn, id=11, started_at=NOW - 120, ended_at=None, status="running",
        outcome=None, summary=None, profile=None,
    )
    _run(
        conn, id=9, started_at=NOW - 7_200, ended_at=NOW - 6_000,
        status="crashed", outcome="crashed", summary="  Earlier résumé.  ",
        error="  Previous error.  ", metadata='{"z": [true, null], "a": {"z": "café", "a": 2}}',
    )
    _run(
        conn, id=5, started_at=NOW - 7_200, ended_at=NOW - 6_600,
        summary="Same start; lower ID first.", metadata="invalid JSON",
    )
    _comment(conn, id=10, created_at=NOW - 600, author="`operator`", body="Latest café.")
    _comment(conn, id=2, created_at=NOW - 1_200, body="  Earlier comment.  ")
    _event(conn, id=10, created_at=NOW - 100, payload='{"z": [1, null], "a": "café"}')
    _event(conn, id=4, created_at=NOW - 600, kind="created")
    _event(conn, id=7, created_at=NOW - 100, kind="attempt", run_id=9, payload="broken")
    _insert(conn, "task_attachments", {
        "task_id": "task-main", "filename": "résumé.txt",
        "stored_path": "/fixture/attachments/resume.txt", "size": 1025,
        "content_type": "text/plain", "created_at": NOW - 100,
    })


def _show(path, args=None):
    """Only replace connection selection; run the actual handler and database queries."""
    def connect(*, board=None):
        return kb, kb.connect(path)

    with mock.patch.object(kt, "_connect", side_effect=connect), mock.patch.object(
        kb.time, "time", return_value=NOW,
    ), mock.patch.dict("os.environ", {"TERMINAL_TIMEOUT": "10"}):
        return kt._handle_show({"task_id": "task-main"} if args is None else args)


@pytest.mark.parametrize("scenario", ["minimal", "rich"])
def test_initial_tool_response_matches_pre_extraction_bytes(board, scenario):
    with kb.connect_closing(board) as conn:
        (_seed_rich if scenario == "rich" else _task)(conn)
        conn.commit()
    expected = json.loads(LEGACY.read_text(encoding="utf-8"))[scenario]
    assert _show(board) == expected


def test_null_task_fields_and_empty_collections_remain_present(board):
    with kb.connect_closing(board) as conn:
        _task(conn)
        conn.commit()
    response = json.loads(_show(board))
    for key in ("body", "assignee", "tenant", "workspace_path", "created_by",
                "started_at", "completed_at", "result", "current_run_id",
                "model_override", "provider_override"):
        assert key in response["task"] and response["task"][key] is None
    for key in ("parents", "children", "comments", "events", "runs"):
        assert response[key] == []
    assert response["task"]["priority"] == 0
    assert response["task"]["workspace_kind"] == "scratch"


def test_links_are_sorted_and_keep_unresolved_parent_and_child_ids(board):
    with kb.connect_closing(board) as conn:
        _seed_rich(conn)
        conn.commit()
    response = json.loads(_show(board))
    assert response["parents"] == ["parent-a", "parent-missing", "parent-z"]
    assert response["children"] == ["child-a", "child-missing", "child-z"]
    assert "Legacy parent handoff." in response["worker_context"]


def test_structured_metadata_keeps_insertion_order_and_unicode_escapes(board):
    with kb.connect_closing(board) as conn:
        _seed_rich(conn)
        conn.commit()
    raw = _show(board)
    response = json.loads(raw)
    metadata = response["runs"][1]["metadata"]
    assert list(metadata) == ["z", "a"]
    assert list(metadata["a"]) == ["z", "a"]
    assert metadata["a"]["z"] == "café"
    assert "café" not in raw and "caf\\u00e9" in raw
    assert response["events"][-1]["payload"] == {"z": [1, None], "a": "café"}
    assert list(response["events"][-1]["payload"]) == ["z", "a"]


@pytest.mark.parametrize("metadata", [None, "", "invalid JSON", "null", "[]", "0"])
def test_run_metadata_legacy_parsing_fallbacks(board, metadata):
    with kb.connect_closing(board) as conn:
        _task(conn)
        _run(conn, metadata=metadata)
        conn.commit()
    response = json.loads(_show(board))
    expected = [] if metadata == "[]" else 0 if metadata == "0" else None
    assert response["runs"][0]["metadata"] == expected
    assert response["runs"][0]["summary"] == "Earlier result."


def test_full_comments_keep_time_order_and_raw_body_outside_capped_context(board):
    count = kb._CTX_MAX_COMMENTS + 3
    with kb.connect_closing(board) as conn:
        _task(conn)
        for index in reversed(range(count)):
            _comment(
                conn, body=f"  comment-{index}  ", author="`operator`",
                created_at=NOW - 1_000 + index,
            )
        conn.commit()
    response = json.loads(_show(board))
    assert [c["body"] for c in response["comments"]] == [
        f"  comment-{index}  " for index in range(count)
    ]
    assert all(c["author"] == "`operator`" for c in response["comments"])
    assert "3 earlier comments omitted" in response["worker_context"]
    assert "comment-0\n" not in response["worker_context"]
    assert "comment from worker `operator`" in response["worker_context"]


def test_comments_with_equal_timestamps_keep_existing_id_order(board):
    with kb.connect_closing(board) as conn:
        _task(conn)
        for comment_id in (20, 3, 11):
            _comment(conn, id=comment_id, body=f"comment-{comment_id}")
        conn.commit()
    response = json.loads(_show(board))
    assert [c["body"] for c in response["comments"]] == [
        "comment-3", "comment-11", "comment-20",
    ]


def test_full_run_history_keeps_active_and_all_closed_runs_with_start_id_order(board):
    count = kb._CTX_MAX_PRIOR_ATTEMPTS + 3
    with kb.connect_closing(board) as conn:
        _task(conn)
        for index in reversed(range(count)):
            _run(conn, id=index + 1, started_at=NOW - 1_000 + index // 2)
        _run(conn, id=100, started_at=NOW - 1, ended_at=None, outcome=None, status="running")
        conn.commit()
    response = json.loads(_show(board))
    assert [r["id"] for r in response["runs"]] == list(range(1, count + 1)) + [100]
    assert response["runs"][-1]["ended_at"] is None
    assert response["runs"][-1]["outcome"] is None
    assert response["runs"][-1]["status"] == "running"
    assert "3 earlier attempts omitted" in response["worker_context"]


@pytest.mark.parametrize("count", [0, 1, 49, 50, 51, 62])
def test_event_tail_is_latest_fifty_in_timestamp_and_id_order(board, count):
    with kb.connect_closing(board) as conn:
        _task(conn)
        for index in reversed(range(count)):
            _event(
                conn, id=index + 1, kind=f"event-{index}",
                created_at=NOW - 1_000 + index // 2, run_id=index + 500,
                payload=json.dumps({"index": index}),
            )
        conn.commit()
    events = json.loads(_show(board))["events"]
    expected_indices = list(range(max(0, count - 50), count))
    assert [e["kind"] for e in events] == [f"event-{i}" for i in expected_indices]
    assert [e["run_id"] for e in events] == [i + 500 for i in expected_indices]
    assert [e["payload"]["index"] for e in events] == expected_indices


def test_invalid_event_payload_keeps_event_and_null_run_id(board):
    with kb.connect_closing(board) as conn:
        _task(conn)
        _event(conn, payload="invalid JSON")
        conn.commit()
    event = json.loads(_show(board))["events"][0]
    assert event["kind"] == "fixture-event"
    assert event["payload"] is None and event["run_id"] is None


def test_missing_task_returns_existing_not_found_error_and_closes_connection(board):
    conn = kb.connect(board)
    with mock.patch.object(kt, "_connect", return_value=(kb, conn)):
        assert kt._handle_show({"task_id": "missing"}) == '{"error": "task missing not found"}'
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_missing_task_id_does_not_connect(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with mock.patch.object(kt, "_connect", side_effect=AssertionError("unexpected connection")):
        response = json.loads(kt._handle_show({}))
    assert response == {"error": "task_id is required (or set HERMES_KANBAN_TASK in the env)"}


def test_invalid_board_uses_real_normalization_and_existing_error_prefix():
    response = json.loads(kt._handle_show({"task_id": "task-main", "board": "../invalid"}))
    assert response["error"].startswith("kanban_show: invalid board slug '../invalid':")


@pytest.mark.parametrize(
    "error", [ValueError("fixture invalid board"), OSError("fixture unavailable")],
)
def test_connection_failure_keeps_structured_error(error):
    with mock.patch.object(kt, "_connect", side_effect=error):
        response = json.loads(kt._handle_show({"task_id": "task-main"}))
    assert response == {"error": "kanban_show: " + str(error)}


def test_success_forwards_explicit_board_and_closes_actual_connection(board):
    with kb.connect_closing(board) as setup:
        _task(setup)
        setup.commit()
    conn = kb.connect(board)
    with mock.patch.object(kt, "_connect", return_value=(kb, conn)) as connect:
        response = json.loads(kt._handle_show({"task_id": "task-main", "board": "fixture-board"}))
    connect.assert_called_once_with(board="fixture-board")
    assert response["task"]["id"] == "task-main"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_database_read_failure_returns_error_and_still_closes_connection(board):
    conn = kb.connect(board)

    def deny_select(action, _arg1, _arg2, _database, _source):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK

    conn.set_authorizer(deny_select)
    with mock.patch.object(kt, "_connect", return_value=(kb, conn)):
        response = json.loads(kt._handle_show({"task_id": "task-main"}))
    assert response["error"] == "kanban_show: not authorized"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
