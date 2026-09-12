"""Late spawn results cannot rewrite another attempt or protected operation."""
import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_delivery_modern_fences import (
    add_active_operation, review as review, snapshot,
)
from tests.hermes_cli.test_kanban_delivery_gate import delivery_board as delivery_board


@pytest.mark.parametrize("damage", ["ended", "foreign", "claim", "pid", "held", "shipping"])
def test_pid_bookkeeping_preserves_invalid_or_protected_attempt(review, damage):
    conn = review.conn
    original = kb.get_task(conn, review.task_id)
    with kb.write_txn(conn):
        if damage == "ended":
            conn.execute("UPDATE task_runs SET ended_at = 1 WHERE id = ?", (review.run_id,))
        elif damage == "foreign":
            conn.execute("UPDATE task_runs SET task_id = 'different-owner' WHERE id = ?", (review.run_id,))
        elif damage == "claim":
            conn.execute("UPDATE task_runs SET claim_lock = 'different-claim' WHERE id = ?", (review.run_id,))
        elif damage == "pid":
            conn.execute("UPDATE task_runs SET worker_pid = 999 WHERE id = ?", (review.run_id,))
        elif damage == "held":
            kb._append_event(conn, review.task_id, "controlled_worker_held", {}, run_id=review.run_id)
    if damage == "shipping":
        add_active_operation(review, "in_progress")
    before = snapshot(review)
    assert not kb._set_worker_pid(conn, review.task_id, 12345,
                                  expected_run_id=original.current_run_id,
                                  expected_claim_lock=original.claim_lock)
    assert snapshot(review) == before


def test_pid_bookkeeping_accepts_exact_current_attempt(review):
    original = kb.get_task(review.conn, review.task_id)
    assert kb._set_worker_pid(review.conn, review.task_id, 12345,
                              expected_run_id=original.current_run_id,
                              expected_claim_lock=original.claim_lock)
    assert kb.get_task(review.conn, review.task_id).worker_pid == 12345
    assert kb.get_run(review.conn, review.run_id).worker_pid == 12345


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_dispatch_does_not_record_stale_spawn_on_successor(delivery_board, monkeypatch, lane):
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="temporary spawn callback fixture", assignee="builder")
        if lane == "review":
            assert kb.request_review(conn, task_id, reviewer="reviewer")
        successor = []

        def delayed_spawn(task, workspace, **kwargs):
            with kb.write_txn(conn):
                kb._end_run(conn, task_id, outcome="reclaimed", status="reclaimed")
                conn.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                             "claim_expires = NULL, worker_pid = NULL WHERE id = ?", (task_id,))
            successor.append(kb.claim_task(conn, task_id, claimer="successor-owner"))
            return 12345

        result = kb.dispatch_once(conn, spawn_fn=delayed_spawn, max_spawn=1)
        assert len(successor) == 1 and successor[0] is not None
        assert result.spawned == []
        assert (task_id, "spawn_identity_changed") in result.claim_guarded
        current = kb.get_task(conn, task_id)
        assert current.current_run_id == successor[0].current_run_id
        assert current.claim_lock == "successor-owner" and current.worker_pid is None
        assert current.consecutive_failures == 0
        assert kb.get_run(conn, current.current_run_id).worker_pid is None
        assert not any(event.kind == "spawned" for event in kb.list_events(conn, task_id))
