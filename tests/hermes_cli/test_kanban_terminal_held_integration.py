"""Terminal reconciliation cannot turn a held child's exit into drain proof."""

import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_held_worker as held_fixture
from tests.hermes_cli.test_kanban_held_worker import native as native


pytestmark = pytest.mark.linux_only


@pytest.fixture(autouse=True)
def isolated_recovery(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_recent_worker_exits", {})
    monkeypatch.setattr(kb, "_unlock_task_worktree", lambda *_args: None)
    monkeypatch.setattr(kb, "_cleanup_worker_tmux", lambda *_args: None)
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda *_args: False)


def snapshot(native, task):
    return held_fixture.rows(native, task), kb.list_events(native.conn, task.id)


def assert_pending_exit_untouched(native, task, worker, before):
    observed = kb._recent_worker_exits[worker.process.pid]
    for _ in range(3):
        assert kb.detect_crashed_workers(native.conn) == []
        assert kb.detect_crashed_workers._last_terminal_reconciled == []
        assert snapshot(native, task) == before
        assert kb._recent_worker_exits[worker.process.pid] == observed
        held_fixture.assert_pending(native, task)


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_clean_held_exit_waits_for_owner_drain_and_explicit_terminal(native, lane):
    task = held_fixture.claim(native, lane)
    with held_fixture.request_for(native, task, lane) as request:
        worker = held_fixture.worker_for(native, request, "pass").prepare()
        worker.release()
        assert worker.wait() == {"state": "exited", "drained": True, "exit_code": 0}
        held_fixture.assert_gone(worker.process.pid, worker.child["pid"])
        kb._record_worker_exit(worker.process.pid, 0)

        before = snapshot(native, task)
        assert_pending_exit_untouched(native, task, worker, before)
        assert not kb.complete_task(native.conn, task.id, result="exit zero alone")
        assert snapshot(native, task) == before

        worker.clear_drained_bookkeeping()
        assert not kb._controlled_worker_pending(native.conn, task.id)
        task_row, run_row = held_fixture.rows(native, task)
        assert task_row["worker_pid"] is run_row["worker_pid"] is None
        assert task_row["status"] == run_row["status"] == "running"
        assert task_row["current_run_id"] == task.current_run_id
        assert task_row["consecutive_failures"] == 0
        assert run_row["ended_at"] is run_row["outcome"] is None

        # The recorded process was a supervisor. Drain does not turn its zero
        # exit into the task's result, or restore a PID to make it eligible.
        drained = snapshot(native, task)
        assert kb.detect_crashed_workers(native.conn) == []
        assert snapshot(native, task) == drained
        assert not any(event.kind == "terminal_reconciled" for event in drained[1])

    assert kb.block_task(native.conn, task.id, kind="capability",
                         reason="Fixture owner explicitly records the missing task result",
                         expected_run_id=task.current_run_id)
    terminal = snapshot(native, task)
    assert terminal[0][0]["status"] == terminal[0][1]["status"] == "blocked"
    assert terminal[0][1]["outcome"] == "blocked"
    assert kb.detect_crashed_workers(native.conn) == []
    assert snapshot(native, task) == terminal


@pytest.mark.parametrize("failure", ["statement", "commit"])
def test_failed_owner_drain_rolls_back_and_preserves_pending_exit(native, monkeypatch, failure):
    task = held_fixture.claim(native)
    with held_fixture.request_for(native, task) as request:
        worker = held_fixture.worker_for(native, request, "pass").prepare()
        worker.release()
        assert worker.wait() == {"state": "exited", "drained": True, "exit_code": 0}
        held_fixture.assert_gone(worker.process.pid, worker.child["pid"])
        kb._record_worker_exit(worker.process.pid, 0)
        before = snapshot(native, task)
        held_event = worker.event_id

        with monkeypatch.context() as patch:
            if failure == "statement":
                append = kb._append_event

                def fail_drain_event(conn, task_id, kind, *args, **kwargs):
                    if kind == "controlled_worker_drained":
                        raise sqlite3.OperationalError("injected drain statement failure")
                    return append(conn, task_id, kind, *args, **kwargs)

                patch.setattr(kb, "_append_event", fail_drain_event)
            else:
                boundary = kb._execute_boundary_with_retry

                def fail_drain_commit(conn, sql):
                    if sql == "COMMIT":
                        raise sqlite3.OperationalError("injected drain commit failure")
                    return boundary(conn, sql)

                patch.setattr(kb, "_execute_boundary_with_retry", fail_drain_commit)

            with pytest.raises(sqlite3.OperationalError, match="injected drain"):
                worker.clear_drained_bookkeeping()

        assert not native.conn.in_transaction
        assert worker.event_id == held_event
        assert_pending_exit_untouched(native, task, worker, before)
        worker.clear_drained_bookkeeping()
        assert worker.event_id is None
        assert not kb._controlled_worker_pending(native.conn, task.id)
        task_row, run_row = held_fixture.rows(native, task)
        assert task_row["worker_pid"] is run_row["worker_pid"] is None
        assert task_row["status"] == run_row["status"] == "running"
        assert run_row["ended_at"] is run_row["outcome"] is None
        assert sum(event.kind == "controlled_worker_drained"
                   for event in kb.list_events(native.conn, task.id)) == 1


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_missing_claim_and_pids_cannot_bypass_pending_held_fence(native, lane):
    task = held_fixture.claim(native, lane)
    # This models the durable negative fence left before a failed PID write.
    # It supplies no positive cleanup evidence and launches no process.
    with kb.write_txn(native.conn):
        kb._append_event(native.conn, task.id, "controlled_worker_held",
                         {"request_id": "pending-fixture"}, run_id=task.current_run_id)
        native.conn.execute("UPDATE tasks SET claim_lock = NULL WHERE id = ?", (task.id,))
    before = snapshot(native, task)
    assert before[0][0]["worker_pid"] is before[0][1]["worker_pid"] is None
    for _ in range(3):
        assert kb.reconcile_orphaned_running(native.conn) == []
        assert snapshot(native, task) == before
        held_fixture.assert_pending(native, task)
