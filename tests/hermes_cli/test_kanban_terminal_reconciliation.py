"""Exact SQLite run reconciliation; subprocess exit alone is never completion."""

from __future__ import annotations

import json
import signal
import sqlite3
import threading

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def db(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_recent_worker_exits", {})
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_unlock_task_worktree", lambda *_args: None)
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda *_args: False)
    kb.init_db()
    with kb.connect() as conn:
        yield conn


def claimed(conn, pid=990701):
    tid = kb.create_task(conn, title="Reconcile one attempt", assignee="backend")
    lock = kb._claimer_id().split(":", 1)[0] + ":private-test-claim"
    assert kb.claim_task(conn, tid, claimer=lock)
    kb._set_worker_pid(conn, tid, pid)
    return tid, kb.get_task(conn, tid).current_run_id, pid, lock


def row(conn, tid):
    return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())


def run(conn, rid):
    return dict(conn.execute("SELECT * FROM task_runs WHERE id = ?", (rid,)).fetchone())


def reconciled_events(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == "terminal_reconciled"]


def test_clean_exit_blocks_exact_attempt_once_without_crash_retry(db):
    tid, rid, pid, lock = claimed(db)
    kb._record_worker_exit(pid, 0)
    assert kb.detect_crashed_workers(db) == []
    task, attempt = row(db, tid), run(db, rid)
    assert task["status"] == "blocked"
    assert task["block_kind"] == "capability"
    assert task["current_run_id"] is None
    assert task["worker_pid"] is None and task["claim_lock"] is None
    assert task["consecutive_failures"] == 0
    assert attempt["outcome"] == "blocked" and attempt["ended_at"] is not None
    event, = reconciled_events(db, tid)
    assert event.run_id == rid
    assert event.payload["exit_kind"] == "clean_exit"
    assert event.payload["exit_code"] == 0 and event.payload["pid"] == pid
    assert lock not in json.dumps(event.payload)
    assert pid not in kb._recent_worker_exits
    assert kb.detect_crashed_workers(db) == []
    assert len(reconciled_events(db, tid)) == 1


@pytest.mark.parametrize("raw, outcome", [(256, "crashed"), (signal.SIGKILL, "crashed"), (None, "crashed"), (kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8, "rate_limited")])
def test_real_crash_and_quota_keep_their_existing_retry_paths(db, raw, outcome):
    tid, rid, pid, _ = claimed(db)
    if raw is not None:
        kb._record_worker_exit(pid, raw)
    result = kb.detect_crashed_workers(db)
    assert row(db, tid)["status"] == "ready"
    assert run(db, rid)["outcome"] == outcome
    assert row(db, tid)["consecutive_failures"] == (0 if outcome == "rate_limited" else 1)
    assert result == ([] if outcome == "rate_limited" else [tid])
    assert not reconciled_events(db, tid)
    assert pid not in kb._recent_worker_exits


@pytest.mark.parametrize("terminal", ["complete", "block"])
def test_real_terminal_winner_is_unchanged(db, terminal):
    tid, rid, pid, _ = claimed(db)
    if terminal == "complete":
        assert kb.complete_task(db, tid, summary="Local evidence inspected", expected_run_id=rid)
    else:
        assert kb.block_task(db, tid, reason="Exact owner decision", kind="needs_input", expected_run_id=rid)
    before = row(db, tid), run(db, rid)
    kb._record_worker_exit(pid, 0)
    for _ in range(3):
        assert kb.detect_crashed_workers(db) == []
        assert (row(db, tid), run(db, rid)) == before
        assert not reconciled_events(db, tid)


@pytest.mark.parametrize("field, value", [("worker_pid", 991111), ("claim_lock", "foreign:claim"), ("ended_at", 1), ("outcome", "completed")])
def test_clean_exit_requires_matching_open_joined_run(db, field, value):
    tid, rid, pid, _ = claimed(db)
    db.execute(f"UPDATE task_runs SET {field} = ? WHERE id = ?", (value, rid))
    db.commit()
    before = row(db, tid), run(db, rid)
    kb._record_worker_exit(pid, 0)
    assert kb.detect_crashed_workers(db) == []
    assert (row(db, tid), run(db, rid)) == before
    assert not reconciled_events(db, tid)


def test_changed_current_run_cannot_be_closed_from_the_old_snapshot(db, monkeypatch):
    tid, rid, pid, _ = claimed(db)
    _, successor, _, _ = claimed(db, pid=990702)
    classify = kb._classify_worker_exit

    def move_pointer(process):
        result = classify(process)
        if process == pid:
            db.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (successor, tid))
        return result

    monkeypatch.setattr(kb, "_classify_worker_exit", move_pointer)
    kb._record_worker_exit(pid, 0)
    kb.detect_crashed_workers(db)
    assert row(db, tid)["status"] == "running"
    assert row(db, tid)["current_run_id"] == successor
    assert run(db, rid)["ended_at"] is None
    assert not reconciled_events(db, tid)
    assert pid not in kb._recent_worker_exits


@pytest.mark.parametrize("failure", ["statement", "commit"])
def test_exit_record_survives_a_rolled_back_reconciliation(db, monkeypatch, failure):
    tid, rid, pid, _ = claimed(db)
    kb._record_worker_exit(pid, 0)
    before = row(db, tid), run(db, rid)
    with monkeypatch.context() as patch:
        if failure == "statement":
            append = kb._append_event

            def fail_event(conn, task, kind, *args, **kwargs):
                if kind == "terminal_reconciled":
                    raise sqlite3.OperationalError("injected statement failure")
                return append(conn, task, kind, *args, **kwargs)

            patch.setattr(kb, "_append_event", fail_event)
        else:
            boundary = kb._execute_boundary_with_retry

            def fail_commit(conn, sql):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("injected commit failure")
                return boundary(conn, sql)

            patch.setattr(kb, "_execute_boundary_with_retry", fail_commit)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            kb.detect_crashed_workers(db)
    assert (row(db, tid), run(db, rid)) == before
    assert pid in kb._recent_worker_exits
    assert not reconciled_events(db, tid)
    kb.detect_crashed_workers(db)
    assert row(db, tid)["block_kind"] == "capability"
    assert len(reconciled_events(db, tid)) == 1
    assert pid not in kb._recent_worker_exits


def test_old_cached_wait_status_is_not_reused_for_a_new_process(db):
    tid, rid, pid, _ = claimed(db)
    kb._recent_worker_exits[pid] = (0, 0.0)
    kb.detect_crashed_workers(db)
    assert run(db, rid)["outcome"] == "crashed"
    assert row(db, tid)["status"] == "ready"
    assert not reconciled_events(db, tid)


def test_dispatch_reconciles_clean_exit_before_expired_claim_cleanup(db, monkeypatch):
    tid, rid, pid, _ = claimed(db)
    db.execute("UPDATE tasks SET claim_expires = 0 WHERE id = ?", (tid,))
    db.commit()
    kb._record_worker_exit(pid, 0)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    result = kb._dispatch_once_locked(db, max_spawn=0)
    assert row(db, tid)["status"] == "blocked"
    assert run(db, rid)["outcome"] == "blocked"
    assert result.reclaimed == 0 and result.crashed == []
    assert len(reconciled_events(db, tid)) == 1


@pytest.mark.parametrize("terminal", ["complete", "block"])
def test_late_terminal_call_and_reconciler_have_one_winner(db, terminal):
    tid, rid, pid, _ = claimed(db)
    kb._record_worker_exit(pid, 0)
    barrier = threading.Barrier(2)
    errors = []

    def act(reconcile):
        try:
            with kb.connect() as conn:
                barrier.wait(timeout=10)
                if reconcile:
                    kb.detect_crashed_workers(conn)
                elif terminal == "complete":
                    kb.complete_task(conn, tid, summary="Inspected", expected_run_id=rid)
                else:
                    kb.block_task(conn, tid, reason="Owner gate", kind="needs_input", expected_run_id=rid)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=act, args=(kind,)) for kind in (False, True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert not errors
    events = [e for e in kb.list_events(db, tid) if e.kind in {"completed", "blocked", "terminal_reconciled"}]
    assert len(events) == 1
    assert run(db, rid)["ended_at"] is not None
    assert row(db, tid)["current_run_id"] is None


def test_worker_exit_hook_uses_the_dispatcher_board(db, monkeypatch):
    tid, _, pid, _ = claimed(db)
    kb._record_worker_exit(pid, 0)
    captured = []
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda *_args: True)
    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", lambda *args, **kwargs: captured.append((args, kwargs)))
    kb.detect_crashed_workers(db, board="another-board")
    assert len(captured) == 1 and captured[0][1]["board"] == "another-board"
    assert captured[0][0][1] == tid


def test_atomic_heartbeat_renews_an_expired_but_current_attempt(db):
    tid, rid, _, lock = claimed(db)
    db.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (tid,))
    db.execute("UPDATE task_runs SET claim_expires = 1 WHERE id = ?", (rid,))
    db.commit()
    assert kb.heartbeat_worker(db, tid, expected_run_id=rid, extend_claim=True, claimer=lock)
    task, attempt = row(db, tid), run(db, rid)
    assert task["claim_expires"] == attempt["claim_expires"] > 1
    assert task["last_heartbeat_at"] == attempt["last_heartbeat_at"]
    event, = [e for e in kb.list_events(db, tid) if e.kind == "heartbeat"]
    assert event.run_id == rid


@pytest.mark.parametrize("mismatch", ["run", "claim", "ended", "pid"])
def test_atomic_heartbeat_rejects_stale_or_inconsistent_identity_without_writes(db, mismatch):
    tid, rid, _, lock = claimed(db)
    expected = rid + 1 if mismatch == "run" else rid
    if mismatch == "claim":
        lock = "foreign:owner"
    elif mismatch == "ended":
        db.execute("UPDATE task_runs SET ended_at = 1 WHERE id = ?", (rid,))
    elif mismatch == "pid":
        db.execute("UPDATE task_runs SET worker_pid = 990799 WHERE id = ?", (rid,))
    db.commit()
    before = row(db, tid), run(db, rid), kb.list_events(db, tid)
    assert not kb.heartbeat_worker(db, tid, expected_run_id=expected, extend_claim=True, claimer=lock)
    assert (row(db, tid), run(db, rid), kb.list_events(db, tid)) == before


def test_atomic_heartbeat_rolls_back_both_expiries_if_event_write_fails(db, monkeypatch):
    tid, rid, _, lock = claimed(db)
    before = row(db, tid), run(db, rid)

    def fail_event(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected heartbeat event failure")

    monkeypatch.setattr(kb, "_append_event", fail_event)
    with pytest.raises(sqlite3.OperationalError, match="injected heartbeat"):
        kb.heartbeat_worker(db, tid, expected_run_id=rid, extend_claim=True, claimer=lock)
    assert (row(db, tid), run(db, rid)) == before


def test_stale_phantom_completion_does_not_append_a_rejection(db):
    tid, rid, _, _ = claimed(db)
    before = row(db, tid), run(db, rid), kb.list_events(db, tid)
    assert not kb.complete_task(db, tid, created_cards=["t_missing"], expected_run_id=rid + 1)
    assert (row(db, tid), run(db, rid), kb.list_events(db, tid)) == before


def test_current_phantom_completion_still_records_the_existing_rejection(db):
    tid, rid, _, _ = claimed(db)
    with pytest.raises(kb.HallucinatedCardsError):
        kb.complete_task(db, tid, created_cards=["t_missing"], expected_run_id=rid)
    event, = [e for e in kb.list_events(db, tid) if e.kind == "completion_blocked_hallucination"]
    assert event.run_id == rid
    assert row(db, tid)["status"] == "running" and run(db, rid)["ended_at"] is None


def test_new_wait_observation_survives_older_reconciliation_commit(db, monkeypatch):
    tid, _, pid, _ = claimed(db)
    kb._record_worker_exit(pid, 0)
    boundary = kb._execute_boundary_with_retry
    replacement = (256, kb.time.time() + 1)

    def newer_exit_on_commit(conn, sql):
        result = boundary(conn, sql)
        if sql == "COMMIT":
            kb._recent_worker_exits[pid] = replacement
        return result

    monkeypatch.setattr(kb, "_execute_boundary_with_retry", newer_exit_on_commit)
    kb.detect_crashed_workers(db)
    assert row(db, tid)["block_kind"] == "capability"
    assert kb._recent_worker_exits[pid] == replacement


@pytest.mark.parametrize("expiry", [0, None])
@pytest.mark.parametrize("mismatch", ["ended", "pid", "claim", "foreign_pointer", "missing_pointer", "foreign_task"])
def test_repeated_dispatch_cannot_erase_a_refused_run_identity(db, monkeypatch, mismatch, expiry):
    tid, rid, pid, _ = claimed(db)
    old = int(kb.time.time()) - 10_000
    db.execute(
        "UPDATE tasks SET claim_expires = ?, started_at = ?, last_heartbeat_at = ?, "
        "max_runtime_seconds = 1 WHERE id = ?", (expiry, old, old, tid),
    )
    db.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (old, rid))
    if mismatch == "ended":
        db.execute("UPDATE task_runs SET ended_at = 1, outcome = 'completed' WHERE id = ?", (rid,))
    elif mismatch == "pid":
        db.execute("UPDATE task_runs SET worker_pid = 999111 WHERE id = ?", (rid,))
    elif mismatch == "claim":
        db.execute("UPDATE task_runs SET claim_lock = 'foreign:claim' WHERE id = ?", (rid,))
    elif mismatch == "missing_pointer":
        db.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (tid,))
    else:
        foreign = kb.create_task(db, title="Unrelated owner", assignee="backend")
        if mismatch == "foreign_task":
            db.execute("UPDATE task_runs SET task_id = ? WHERE id = ?", (foreign, rid))
        else:
            assert kb.claim_task(db, foreign)
            other_run = kb.get_task(db, foreign).current_run_id
            # The foreign task is outside recovery; its open run must remain intact.
            db.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (foreign,))
            db.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (other_run, tid))
    db.commit()
    before_task = row(db, tid)
    before_runs = [dict(r) for r in db.execute("SELECT * FROM task_runs ORDER BY id")]
    before_events = kb.list_events(db, tid)
    kb._record_worker_exit(pid, 0)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    for _ in range(3):
        assert kb.detect_crashed_workers(db) == []
        result = kb._dispatch_once_locked(db, max_spawn=0, stale_timeout_seconds=1)
        assert row(db, tid) == before_task
        assert [dict(r) for r in db.execute("SELECT * FROM task_runs ORDER BY id")] == before_runs
        assert kb.list_events(db, tid) == before_events
        assert not result.crashed and not result.terminal_reconciled
        assert result.reclaimed == 0 and not result.stale and not result.timed_out
    assert pid not in kb._recent_worker_exits


def test_exit_observation_must_be_newer_than_the_active_attempt(db):
    tid, rid, pid, _ = claimed(db)
    now = int(kb.time.time())
    db.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (now - 1000, tid))
    db.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (now, rid))
    db.commit()
    kb._recent_worker_exits[pid] = (0, now - 5)
    assert kb.detect_crashed_workers(db) == [tid]
    assert run(db, rid)["outcome"] == "crashed"
    assert not reconciled_events(db, tid)


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_missing_claim_without_recorded_pid_retains_orphan_recovery(db, lane):
    tid = kb.create_task(db, title="Recover missing claim", assignee="backend")
    if lane == "review":
        db.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        db.commit()
        assert kb.claim_review_task(db, tid)
    else:
        assert kb.claim_task(db, tid)
    rid = kb.get_task(db, tid).current_run_id
    assert row(db, tid)["worker_pid"] is None and run(db, rid)["worker_pid"] is None
    db.execute("UPDATE tasks SET claim_lock=NULL WHERE id=?", (tid,))
    db.commit()
    assert kb.reconcile_orphaned_running(db) == [tid]
    assert row(db, tid)["status"] == lane
    assert run(db, rid)["ended_at"] is not None
    assert kb.reconcile_orphaned_running(db) == []


@pytest.mark.parametrize("damage", ["foreign_pointer", "ended", "missing", "run_pid", "task_pid", "foreign_task"])
def test_missing_claim_does_not_relax_other_run_identity_checks(db, damage):
    tid = kb.create_task(db, title="Preserve damaged orphan", assignee="backend")
    assert kb.claim_task(db, tid)
    rid = kb.get_task(db, tid).current_run_id
    other = kb.create_task(db, title="Other task", assignee="backend")
    assert kb.claim_task(db, other)
    other_rid = kb.get_task(db, other).current_run_id
    db.execute("UPDATE tasks SET claim_lock=NULL WHERE id=?", (tid,))
    if damage == "foreign_pointer":
        db.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (other_rid, tid))
    elif damage == "ended":
        db.execute("UPDATE task_runs SET ended_at=1, outcome='completed' WHERE id=?", (rid,))
    elif damage == "missing":
        db.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid + 99999, tid))
    elif damage == "run_pid":
        db.execute("UPDATE task_runs SET worker_pid=990801 WHERE id=?", (rid,))
    elif damage == "task_pid":
        db.execute("UPDATE tasks SET worker_pid=990801 WHERE id=?", (tid,))
    else:
        db.execute("UPDATE task_runs SET task_id=? WHERE id=?", (other, rid))
    db.commit()
    before = (row(db, tid), run(db, rid), run(db, other_rid))
    for _ in range(3):
        assert kb.reconcile_orphaned_running(db) == []
        assert (row(db, tid), run(db, rid), run(db, other_rid)) == before


def test_pidless_orphan_refuses_a_changed_claim_snapshot(db, monkeypatch):
    from contextlib import contextmanager

    tid = kb.create_task(db, title="Preserve changed claim", assignee="backend")
    assert kb.claim_task(db, tid)
    rid = kb.get_task(db, tid).current_run_id
    db.execute("UPDATE tasks SET claim_lock=NULL WHERE id=?", (tid,))
    db.commit()
    prior_run = run(db, rid)
    prior_events = kb.list_events(db, tid)
    original = kb.write_txn

    @contextmanager
    def replace_claim(conn, *args, **kwargs):
        with original(conn, *args, **kwargs):
            conn.execute("UPDATE tasks SET claim_lock='replacement-claim' WHERE id=?", (tid,))
            yield

    monkeypatch.setattr(kb, "write_txn", replace_claim)
    assert kb.reconcile_orphaned_running(db) == []
    task = row(db, tid)
    assert task["status"] == "running" and task["claim_lock"] == "replacement-claim"
    assert task["current_run_id"] == rid
    assert run(db, rid) == prior_run
    assert kb.list_events(db, tid) == prior_events
