"""Completion/reclaim reset only the attempt whose transition commits."""

import contextlib
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.delivery_fixtures import ARTIFACT_CONTRACT, ARTIFACT_DELIVERY


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    kb.init_db()
    with contextlib.closing(kb.connect()) as conn:
        task_id = kb.create_task(
            conn, title="Temporary evidence task", body=ARTIFACT_CONTRACT,
            assignee="backend", workspace_kind="dir", workspace_path=str(home),
        )
        claimed = kb.claim_task(conn, task_id, claimer="old-fixture-claim")
        assert claimed is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET consecutive_failures=3, last_failure_error='old failure' WHERE id=?",
                (task_id,),
            )
        yield conn, task_id, claimed.current_run_id


def task_row(conn, task_id):
    return tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def complete(board):
    conn, task_id, run_id = board
    return kb.complete_task(
        conn, task_id, summary="Evidence recorded", delivery=ARTIFACT_DELIVERY,
        expected_run_id=run_id, fire_lifecycle_hook=False,
    )


@pytest.mark.parametrize("action", ["complete", "reclaim"])
def test_successor_failure_survives_old_transition_followup(board, monkeypatch, action):
    conn, task_id, run_id = board
    captured = []

    def begin_successor(*_args, **_kwargs):
        # This callback runs after the old transition has committed. A second
        # real connection models an independent operator/dispatcher writer.
        assert not conn.in_transaction
        with contextlib.closing(kb.connect()) as other:
            old = kb.get_task(other, task_id)
            assert old.consecutive_failures == 0
            assert old.last_failure_error is None
            assert old.status == ("done" if action == "complete" else "ready")
            if action == "complete":
                with kb.write_txn(other):
                    other.execute(
                        "UPDATE tasks SET status='ready', completed_at=NULL WHERE id=?",
                        (task_id,),
                    )
            successor = kb.claim_task(other, task_id, claimer="new-fixture-claim")
            assert successor is not None and successor.current_run_id != run_id
            with kb.write_txn(other):
                other.execute(
                    "UPDATE tasks SET consecutive_failures=1, last_failure_error='successor failure' WHERE id=?",
                    (task_id,),
                )
            captured.append(task_row(other, task_id))
        return []

    if action == "complete":
        monkeypatch.setattr(kb, "_scan_prose_for_phantom_ids", begin_successor)
        assert complete(board)
    else:
        monkeypatch.setattr(kb, "_park_retry_transitions_after_commit", begin_successor)
        assert kb.reclaim_task(conn, task_id, expected_run_id=run_id)
    assert len(captured) == 1
    assert task_row(conn, task_id) == captured[0]
    assert kb.get_run(conn, run_id).outcome == ("completed" if action == "complete" else "reclaimed")


@pytest.mark.parametrize("action", ["complete", "reclaim"])
def test_postcommit_error_does_not_leave_old_failure_budget(board, monkeypatch, action):
    conn, task_id, run_id = board

    def fail_after_commit(*_args, **_kwargs):
        assert not conn.in_transaction
        raise RuntimeError("fixture postcommit failure")

    target = "_scan_prose_for_phantom_ids" if action == "complete" else "_park_retry_transitions_after_commit"
    monkeypatch.setattr(kb, target, fail_after_commit)
    with pytest.raises(RuntimeError, match="fixture postcommit"):
        if action == "complete":
            complete(board)
        else:
            kb.reclaim_task(conn, task_id, expected_run_id=run_id)
    task = kb.get_task(conn, task_id)
    assert task.status == ("done" if action == "complete" else "ready")
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
    assert kb.get_run(conn, run_id).ended_at is not None


@pytest.mark.parametrize("action", ["complete", "reclaim"])
def test_failed_transaction_preserves_old_failure_budget(board, monkeypatch, action):
    conn, task_id, run_id = board
    before = task_row(conn, task_id)
    original = kb._append_event

    def fail_terminal_event(connection, tid, kind, *args, **kwargs):
        if kind == ("completed" if action == "complete" else "reclaimed"):
            raise sqlite3.OperationalError("fixture transaction failure")
        return original(connection, tid, kind, *args, **kwargs)

    monkeypatch.setattr(kb, "_append_event", fail_terminal_event)
    with pytest.raises(sqlite3.OperationalError, match="fixture transaction"):
        if action == "complete":
            complete(board)
        else:
            kb.reclaim_task(conn, task_id, expected_run_id=run_id)
    assert task_row(conn, task_id) == before
    assert kb.get_run(conn, run_id).ended_at is None
    assert not conn.in_transaction
