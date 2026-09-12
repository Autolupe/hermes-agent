"""Native edits and reclaim cannot invalidate held or protected attempts."""

import contextlib
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    effects = []
    terminations = []

    def terminate(pid, claim, **_kwargs):
        terminations.append((pid, claim))
        return {"termination_attempted": False, "terminated": False}

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", terminate)
    for name in (
        "_unlock_task_worktree", "_cleanup_workspace", "_cleanup_worker_tmux",
        "notify_task_updated", "_recompute_ready_after_committed_mutation",
    ):
        monkeypatch.setattr(kb, name, lambda *_a, _name=name, **_kw: effects.append(_name))
    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", lambda *_a, **_kw: None)
    monkeypatch.setattr(kb, "_fire_worker_spawned_hook", lambda *_a, **_kw: None)
    with contextlib.closing(kb.connect(db_path=tmp_path / "board.db")) as conn:
        task_id = kb.create_task(conn, title="native edit fixture", assignee="builder")
        task = kb.claim_task(conn, task_id, claimer="fixture:same-claim")
        other_id = kb.create_task(conn, title="other native fixture", assignee="other")
        other = kb.claim_task(conn, other_id, claimer="fixture:other-claim")
        assert task is not None and other is not None
        effects.clear()
        yield SimpleNamespace(conn=conn, task=task, other=other, effects=effects,
                              terminations=terminations)


def snapshot(board):
    order = {
        "tasks": "id", "task_runs": "id", "task_events": "id",
        "task_comments": "id", "task_links": "parent_id, child_id",
        "delivery_control_operations": "op_id",
        "kanban_notify_subs": "task_id, platform, chat_id, thread_id",
    }
    return {table: [dict(row) for row in board.conn.execute(f"SELECT * FROM {table} ORDER BY {key}")]
            for table, key in order.items()}


def protect(board, task, fence, *, triage=False):
    with kb.write_txn(board.conn):
        if fence == "held":
            if triage:
                # Model an inconsistent old row: planning status did not drain
                # the existing worker or clear its durable ownership evidence.
                board.conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (task.id,))
            kb._append_event(board.conn, task.id, "controlled_worker_held",
                             {"request_id": "native-edit-fixture"}, run_id=task.current_run_id)
        else:
            board.conn.execute("UPDATE tasks SET status = 'shipping' WHERE id = ?", (task.id,))
    assert board.conn.execute("SELECT COUNT(*) FROM delivery_control_operations").fetchone()[0] == 0


def edit(board, operation):
    conn, task_id = board.conn, board.task.id
    return {
        "assign": lambda: kb.assign_task(conn, task_id, "replacement"),
        "model": lambda: kb.set_model_override(conn, task_id, "fixture-model", "fixture-provider"),
        "reasoning": lambda: kb.set_reasoning_effort(conn, task_id, "low"),
        "archive": lambda: kb.archive_task(conn, task_id),
        "specify": lambda: kb.specify_triage_task(
            conn, task_id, title="replacement title", body="replacement instructions", author="fixture"),
        "decompose": lambda: kb.decompose_triage_task(
            conn, task_id, root_assignee="replacement", author="fixture",
            children=[{"title": "must not create child"}]),
    }[operation]()


@pytest.mark.parametrize("fence", ["held", "orphan_shipping"])
@pytest.mark.parametrize("operation", ["assign", "model", "reasoning", "archive", "specify", "decompose"])
def test_native_edits_preserve_protected_rows_and_events(board, fence, operation):
    protect(board, board.task, fence, triage=operation in {"specify", "decompose"})
    before = snapshot(board)
    if fence == "held":
        expected = None if operation == "decompose" else False
        assert edit(board, operation) is expected
    else:
        with pytest.raises(kb.DeliveryOperationInProgressError):
            edit(board, operation)
    assert snapshot(board) == before
    assert board.effects == board.terminations == []


@pytest.mark.parametrize("fence", ["held", "orphan_shipping"])
@pytest.mark.parametrize("endpoint", ["parent", "child"])
@pytest.mark.parametrize("operation", ["link", "unlink"])
def test_dependency_edit_refuses_either_protected_endpoint(board, fence, endpoint, operation):
    parent, child = board.task, board.other
    if operation == "unlink":
        kb.link_tasks(board.conn, parent.id, child.id)
    protect(board, parent if endpoint == "parent" else child, fence)
    before = snapshot(board)
    action = kb.link_tasks if operation == "link" else kb.unlink_tasks
    if fence == "held":
        with pytest.raises(RuntimeError, match="held worker cleanup"):
            action(board.conn, parent.id, child.id)
    else:
        with pytest.raises(kb.DeliveryOperationInProgressError):
            action(board.conn, parent.id, child.id)
    assert snapshot(board) == before
    assert board.effects == board.terminations == []


def forbid_signal(*_args, **_kwargs):
    raise AssertionError("A native edit fixture must never send a process signal")


@pytest.mark.parametrize("damage", ["stale_token", "foreign_owner", "ended", "pid_mismatch"])
def test_run_specific_reclaim_refuses_stale_or_invalid_run_before_termination(board, damage):
    conn, task = board.conn, board.task
    expected = task.current_run_id
    with kb.write_txn(conn):
        if damage == "stale_token":
            expected = board.other.current_run_id
        elif damage == "foreign_owner":
            conn.execute("UPDATE task_runs SET task_id = ? WHERE id = ?", (board.other.id, expected))
        elif damage == "ended":
            conn.execute("UPDATE task_runs SET ended_at = 1, outcome = 'blocked', status = 'blocked' WHERE id = ?",
                         (expected,))
        else:
            conn.execute("UPDATE task_runs SET worker_pid = 991234 WHERE id = ?", (expected,))
    before = snapshot(board)
    assert kb.reclaim_task(conn, task.id, expected_run_id=expected, signal_fn=forbid_signal) is False
    assert snapshot(board) == before
    assert board.effects == board.terminations == []


@pytest.mark.parametrize("bind_expected_run", [False, True])
@pytest.mark.parametrize("replacement", ["new_run_same_claim", "new_pid_same_run"])
def test_reclaim_compare_update_preserves_newer_identity_after_termination_callback(
    board, monkeypatch, bind_expected_run, replacement,
):
    conn, task = board.conn, board.task
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET consecutive_failures = 3 WHERE id = ?", (task.id,))
    protected = []

    def replace_after_snapshot(pid, claim_lock, **_kwargs):
        board.terminations.append((pid, claim_lock))
        assert pid is None and claim_lock == task.claim_lock
        if replacement == "new_run_same_claim":
            with kb.write_txn(conn):
                kb._end_run(conn, task.id, outcome="superseded", status="superseded")
                conn.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL WHERE id = ?",
                             (task.id,))
            newer = kb.claim_task(conn, task.id, claimer=task.claim_lock)
            assert newer is not None and newer.current_run_id != task.current_run_id
            assert newer.claim_lock == task.claim_lock and newer.worker_pid is None
        else:
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET worker_pid = 991235 WHERE id = ?", (task.id,))
                conn.execute("UPDATE task_runs SET worker_pid = 991235 WHERE id = ?", (task.current_run_id,))
        protected.append(snapshot(board))
        return {"termination_attempted": False, "terminated": False}

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", replace_after_snapshot)
    kwargs = {"expected_run_id": task.current_run_id} if bind_expected_run else {}
    assert kb.reclaim_task(conn, task.id, signal_fn=forbid_signal, **kwargs) is False
    assert len(protected) == len(board.terminations) == 1
    assert snapshot(board) == protected[0]
    current = kb.get_task(conn, task.id)
    assert current.status == "running" and current.claim_lock == task.claim_lock
    assert current.consecutive_failures == 3
    run = kb.get_run(conn, current.current_run_id)
    assert run.ended_at is None and run.outcome is None
    assert not any(event.kind == "reclaimed" for event in kb.list_events(conn, task.id))
    assert board.effects == []


@pytest.mark.parametrize("bind_expected_run", [False, True])
@pytest.mark.parametrize("damage", ["ended", "foreign_owner", "claim", "pid"])
def test_reclaim_rechecks_open_owned_run_after_termination_callback(
    board, monkeypatch, bind_expected_run, damage,
):
    conn, task = board.conn, board.task
    protected = []

    def change_run(pid, claim_lock, **_kwargs):
        board.terminations.append((pid, claim_lock))
        with kb.write_txn(conn):
            if damage == "ended":
                conn.execute(
                    "UPDATE task_runs SET ended_at = 1, status = 'blocked', outcome = 'blocked' WHERE id = ?",
                    (task.current_run_id,),
                )
            elif damage == "foreign_owner":
                conn.execute("UPDATE task_runs SET task_id = ? WHERE id = ?",
                             (board.other.id, task.current_run_id))
            elif damage == "claim":
                conn.execute("UPDATE task_runs SET claim_lock = 'new-run-owner' WHERE id = ?",
                             (task.current_run_id,))
            else:
                conn.execute("UPDATE task_runs SET worker_pid = 991235 WHERE id = ?",
                             (task.current_run_id,))
        protected.append(snapshot(board))
        return {"termination_attempted": False, "terminated": False}

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", change_run)
    kwargs = {"expected_run_id": task.current_run_id} if bind_expected_run else {}
    assert kb.reclaim_task(conn, task.id, signal_fn=forbid_signal, **kwargs) is False
    assert len(protected) == len(board.terminations) == 1
    assert snapshot(board) == protected[0]
    assert kb.get_task(conn, task.id).current_run_id == task.current_run_id
    assert not any(event.kind == "reclaimed" for event in kb.list_events(conn, task.id))
    assert board.effects == []


def test_exact_unchanged_run_can_be_reclaimed_without_real_process_actions(board):
    task = board.task
    assert kb.reclaim_task(board.conn, task.id, expected_run_id=task.current_run_id,
                           signal_fn=forbid_signal)
    current = kb.get_task(board.conn, task.id)
    assert current.status == "ready" and current.current_run_id is None
    run = kb.get_run(board.conn, task.current_run_id)
    assert run.ended_at is not None and run.outcome == "reclaimed"
    assert board.terminations == [(None, task.claim_lock)]
    assert board.effects == ["_unlock_task_worktree"]
