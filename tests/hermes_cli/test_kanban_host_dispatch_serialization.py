"""Shared worker limits must survive simultaneous ticks on different boards."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import threading

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db(board="default")
    return home


def test_simultaneous_boards_cannot_both_spend_last_host_slot(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """Pause a real zero-worker snapshot while a second board tries to dispatch.

    No counts, claims, database writes or dispatch locks are mocked. Only the
    scheduling boundary after the first actual count is controlled. Without
    shared admission serialization, the second board spends the last slot and
    the first then spawns from its now-stale snapshot, exceeding the host cap.
    """
    kb.create_board("second")

    task_ids = {}
    for board in ("default", "second"):
        with kb.connect(board=board) as conn:
            task_ids[board] = kb.create_task(
                conn, title=f"work on {board}", assignee="alice",
            )
            kb.recompute_ready(conn)

    snapshot_taken = threading.Event()
    release_snapshot = threading.Event()
    actual_count = kb.count_running_tasks_other_boards
    observed_counts = []

    def pause_first_snapshot(board=None):
        count = actual_count(board)
        if board == "default":
            observed_counts.append(count)
            snapshot_taken.set()
            assert release_snapshot.wait(15), "test did not release the first tick"
        return count

    monkeypatch.setattr(kb, "count_running_tasks_other_boards", pause_first_snapshot)
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((board, task.id))
        return None  # Record the running claim without starting an OS worker.

    def tick(board):
        # SQLite connections are created and used on their own dispatch thread.
        with kb.connect(board=board) as conn:
            return kb.dispatch_once(
                conn, board=board, spawn_fn=fake_spawn, max_in_progress=1,
                reconcile_orphans=False, _emit_tick_hook=False,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(tick, "default")
        try:
            assert snapshot_taken.wait(10), "first board never reached host accounting"
            # Like the existing board lock, a contended host admission must not
            # block a gateway tick while another dispatcher is still working.
            second_result = pool.submit(tick, "second").result(timeout=10)
        finally:
            release_snapshot.set()
        first_result = first.result(timeout=10)

    assert observed_counts == [0]
    assert len(spawns) == 1, f"host cap=1 admitted both boards: {spawns!r}"
    assert len(first_result.spawned) + len(second_result.spawned) == 1

    statuses = []
    for board, task_id in task_ids.items():
        with kb.connect(board=board) as conn:
            statuses.append(kb.get_task(conn, task_id).status)
    assert statuses.count("running") == 1
    assert all(status in {"ready", "running"} for status in statuses)


@pytest.mark.parametrize("failure", ["host_lock_open", "board_lock_open", "board_path"])
def test_unavailable_dispatch_guard_refuses_admission_then_recovers(
    kanban_home, monkeypatch, all_assignees_spawnable, failure,
):
    """A guard error must leave ready work untouched, and recovery may retry it."""
    db_path = kb.kanban_db_path(board="default")
    failed_path = (
        kanban_home / "host-admission.dispatch.lock"
        if failure == "host_lock_open"
        else db_path.with_name(db_path.name + ".dispatch.lock")
    )
    actual_open = Path.open
    failures_seen = []
    spawns = []

    def fail_lock_open(path, *args, **kwargs):
        if path == failed_path:
            failures_seen.append(path)
            raise PermissionError("temporary lock-directory permissions failure")
        return actual_open(path, *args, **kwargs)

    def fail_board_path(*args, **kwargs):
        failures_seen.append("board_path")
        raise OSError("temporary board-path resolution failure")

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return None

    with kb.connect(board="default") as conn:
        task_id = kb.create_task(conn, title="waiting for safe admission", assignee="alice")
        kb.recompute_ready(conn)
        before_changes = conn.total_changes
        with monkeypatch.context() as faults:
            if failure == "board_path":
                faults.setattr(kb, "kanban_db_path", fail_board_path)
            else:
                faults.setattr(Path, "open", fail_lock_open)
            refused = kb.dispatch_once(
                conn, board="default", spawn_fn=fake_spawn, max_in_progress=1,
                reconcile_orphans=False, _emit_tick_hook=False,
            )

        assert failures_seen, "the requested filesystem/path fault was not exercised"
        assert refused.skipped_locked is True
        assert refused.spawned == []
        assert spawns == []
        assert conn.total_changes == before_changes
        assert kb.get_task(conn, task_id).status == "ready"

        recovered = kb.dispatch_once(
            conn, board="default", spawn_fn=fake_spawn, max_in_progress=1,
            reconcile_orphans=False, _emit_tick_hook=False,
        )
        assert recovered.skipped_locked is False
        assert spawns == [task_id]
        assert kb.get_task(conn, task_id).status == "running"


def test_corrupt_other_board_refuses_admission_until_repaired(
    kanban_home, all_assignees_spawnable,
):
    """Unreadable real board data is unknown capacity, not zero workers."""
    kb.create_board("damaged")
    damaged_path = kb.kanban_db_path(board="damaged")
    with closing(kb.connect(board="damaged")) as other:
        other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    healthy_database = damaged_path.read_bytes()
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return None

    with closing(kb.connect(board="default")) as conn:
        task_id = kb.create_task(conn, title="wait for a trustworthy count", assignee="alice")
        kb.recompute_ready(conn)
        damaged_path.write_bytes(b"corrupted temporary board database\n")
        try:
            with pytest.raises(RuntimeError, match="host_capacity_unavailable"):
                kb.dispatch_once(
                    conn, board="default", spawn_fn=fake_spawn, max_in_progress=1,
                    reconcile_orphans=False, _emit_tick_hook=False,
                )
            assert spawns == []
            assert kb.get_task(conn, task_id).status == "ready"
            assert kb.count_running_tasks(conn) == 0
        finally:
            damaged_path.write_bytes(healthy_database)

        # Restoring the actual database also proves the exception released
        # admission locks: the same ready task can run on the next tick.
        recovered = kb.dispatch_once(
            conn, board="default", spawn_fn=fake_spawn, max_in_progress=1,
            reconcile_orphans=False, _emit_tick_hook=False,
        )
        assert recovered.skipped_locked is False
        assert spawns == [task_id]
        assert kb.get_task(conn, task_id).status == "running"
