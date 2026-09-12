"""Native lifecycle writes retain unresolved controller and worker ownership."""

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.delivery_control import DeliveryControl, DeliveryControlError
from tests.hermes_cli import test_delivery_control as fixtures


board = fixtures.board


def _snapshot(conn):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in (
            "tasks", "task_runs", "task_events", "task_comments", "task_links",
            "delivery_control_operations",
        )
    }


def _hold(conn, task_id, run_id):
    kb._append_event(
        conn, task_id, "controlled_worker_held", {"request_id": "unresolved-owner"},
        run_id=run_id,
    )


@pytest.mark.parametrize("drift", ["held_worker", "creation_base"])
def test_acceptance_failure_cannot_release_changed_worker(board, drift):
    db_path, root = board
    task_id, request = fixtures._claimed_builder(db_path, root)
    DeliveryControl(db_path, fixtures.FakeGitHub(), allowed_worker_uid=fixtures.UID).handle(
        request, peer_pid=fixtures.PID, peer_uid=fixtures.UID,
    )
    review = fixtures._claim_review(db_path, task_id)
    backend = fixtures.AcceptanceRejectingGitHub()
    before = {}

    def change_after_receipt(task, _submission):
        with kb.connect_closing(db_path) as conn, kb.write_txn(conn):
            if drift == "held_worker":
                _hold(conn, task_id, task.current_run_id)
            else:
                conn.execute(
                    "UPDATE tasks SET worktree_base_sha = ? WHERE id = ?",
                    ("c" * 40, task_id),
                )
            before.update(_snapshot(conn))

    backend.mutate_after_receipt = change_after_receipt
    with pytest.raises(DeliveryControlError) as refused:
        DeliveryControl(db_path, backend, allowed_worker_uid=fixtures.UID).handle(
            review, peer_pid=fixtures.PID, peer_uid=fixtures.UID,
        )
    assert refused.value.code == "task_drifted"
    with kb.connect_closing(db_path) as conn:
        assert _snapshot(conn) == before
        assert kb.get_task(conn, task_id).status == "shipping"


@pytest.mark.parametrize("archived", [False, True])
def test_delete_preserves_held_worker_and_its_evidence(board, archived):
    db_path, root = board
    task_id, request = fixtures._claimed_builder(db_path, root)
    with kb.connect_closing(db_path) as conn:
        with kb.write_txn(conn):
            _hold(conn, task_id, request.run_id)
            if archived:
                conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (task_id,))
        before = _snapshot(conn)
        delete = kb.delete_archived_task if archived else kb.delete_task
        assert delete(conn, task_id) is False
        assert _snapshot(conn) == before


@pytest.mark.parametrize("status", ["blocked", "scheduled", "review"])
@pytest.mark.parametrize("drift", ["held_worker", "foreign_run", "ended_run", "run_outcome", "claim", "pid"])
def test_reopen_preserves_inconsistent_or_held_attempt(board, status, drift):
    db_path, root = board
    task_id, request = fixtures._claimed_builder(db_path, root)
    with kb.connect_closing(db_path) as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
            if drift == "held_worker":
                _hold(conn, task_id, request.run_id)
            elif drift == "foreign_run":
                conn.execute("UPDATE task_runs SET task_id = ? WHERE id = ?", ("t_bad0cafe", request.run_id))
            elif drift == "ended_run":
                conn.execute("UPDATE task_runs SET ended_at = 1 WHERE id = ?", (request.run_id,))
            elif drift == "run_outcome":
                conn.execute("UPDATE task_runs SET outcome = 'completed' WHERE id = ?", (request.run_id,))
            elif drift == "claim":
                conn.execute("UPDATE task_runs SET claim_lock = 'different-owner' WHERE id = ?", (request.run_id,))
            else:
                conn.execute("UPDATE task_runs SET worker_pid = ? WHERE id = ?", (fixtures.PID + 1, request.run_id))
        before = _snapshot(conn)
        reopen = kb.reopen_review_task if status == "review" else kb.unblock_task
        assert reopen(conn, task_id) is False
        assert _snapshot(conn) == before


@pytest.mark.parametrize("status", ["blocked", "scheduled", "review"])
def test_reopen_closes_only_the_matching_leaked_open_attempt(board, status):
    db_path, root = board
    task_id, request = fixtures._claimed_builder(db_path, root)
    with kb.connect_closing(db_path) as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        reopen = kb.reopen_review_task if status == "review" else kb.unblock_task
        assert reopen(conn, task_id) is True
        assert kb.get_task(conn, task_id).current_run_id is None
        run = kb.get_run(conn, request.run_id)
        assert run.outcome == "reclaimed"
        assert run.ended_at is not None


@pytest.mark.parametrize("status", ["todo", "ready", "review", "running", "done", "shipping"])
def test_descendant_invalidation_preserves_every_held_status(board, status, monkeypatch):
    db_path, root = board
    task_id, request = fixtures._claimed_builder(db_path, root)
    with kb.connect_closing(db_path) as conn:
        ancestor = kb.create_task(conn, title="reopened ancestor")
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (ancestor, task_id))
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
            _hold(conn, task_id, request.run_id)
        before = _snapshot(conn)
        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *_a: pytest.fail("guessed worker cleanup"))
        with pytest.raises(kb._required_policy.RequiredPolicyError):
            kb.invalidate_descendants_for_parent_reopen(conn, ancestor, author="fixture")
        assert _snapshot(conn) == before


def test_descendant_invalidation_refuses_shipping_before_any_child_change(board):
    db_path, root = board
    task_id, request = fixtures._claimed_builder(db_path, root)
    with kb.connect_closing(db_path) as conn:
        ancestor = kb.create_task(conn, title="reopened ancestor")
        sibling = kb.create_task(conn, title="ordinary dependent")
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (ancestor, task_id))
            conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (ancestor, sibling))
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (sibling,))
    backend = fixtures.FakeGitHub()
    before = {}

    def inspect_during_publish():
        with kb.connect_closing(db_path) as conn:
            before.update(_snapshot(conn))
            with pytest.raises(kb.DeliveryOperationInProgressError):
                kb.invalidate_descendants_for_parent_reopen(conn, ancestor, author="fixture")
            assert _snapshot(conn) == before

    backend.mutate_on_publish = inspect_during_publish
    # Submission cannot land while its ancestor is unfinished, but the
    # invalidation attempt itself must preserve both child rows and receipts.
    with pytest.raises(DeliveryControlError):
        DeliveryControl(db_path, backend, allowed_worker_uid=fixtures.UID).handle(
            request, peer_pid=fixtures.PID, peer_uid=fixtures.UID,
        )
    assert before


@pytest.mark.parametrize("drift", ["foreign", "ended", "outcome", "claim", "pid", "missing"])
def test_descendant_invalidation_preserves_uncertain_running_identity(board, drift, monkeypatch):
    db_path, root = board
    task_id, request = fixtures._claimed_builder(db_path, root)
    with kb.connect_closing(db_path) as conn:
        ancestor = kb.create_task(conn, title="reopened ancestor")
        sibling = kb.create_task(conn, title="ordinary dependent")
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (ancestor, task_id))
            conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (ancestor, sibling))
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (sibling,))
            if drift == "foreign":
                conn.execute("UPDATE task_runs SET task_id = ? WHERE id = ?", (sibling, request.run_id))
            elif drift == "ended":
                conn.execute("UPDATE task_runs SET ended_at = 1 WHERE id = ?", (request.run_id,))
            elif drift == "outcome":
                conn.execute("UPDATE task_runs SET outcome = 'completed' WHERE id = ?", (request.run_id,))
            elif drift == "claim":
                conn.execute("UPDATE task_runs SET claim_lock = 'different-owner' WHERE id = ?", (request.run_id,))
            elif drift == "pid":
                conn.execute("UPDATE task_runs SET worker_pid = ? WHERE id = ?", (fixtures.PID + 1, request.run_id))
            else:
                conn.execute("DELETE FROM task_runs WHERE id = ?", (request.run_id,))
        before = _snapshot(conn)
        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *_a: pytest.fail("guessed worker cleanup"))
        with pytest.raises(RuntimeError, match="identity"):
            kb.invalidate_descendants_for_parent_reopen(conn, ancestor, author="fixture")
        assert _snapshot(conn) == before
