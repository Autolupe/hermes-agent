import builtins
import contextlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import threading

import pytest

from agent import estop
import hermes_cli.kanban_db as kb
from hermes_cli import dispatch_boundary_probe as boundary_probe


def _pause(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    path = tmp_path / "state" / "dispatch_pause.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"reason": "test pause"}), encoding="utf-8")
    return path


def _halt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    path = tmp_path / "state" / "halt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"reason": "test halt"}), encoding="utf-8")
    return path


def _engage_brake(tmp_path, monkeypatch, brake):
    if brake == "dispatch_pause":
        return _pause(tmp_path, monkeypatch)
    if brake == "halt":
        return _halt(tmp_path, monkeypatch)
    path = tmp_path / "ESTOP"
    path.write_text("{}\n", encoding="utf-8")
    return path


def _latest_event_payload(conn, task_id, kind):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    assert row is not None
    return json.loads(row["payload"]) if row["payload"] else {}


def _init_git_repo(path):
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test User"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-qm", "base"],
        check=True,
    )


def test_pause_blocks_ready_and_review_claims_without_run_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    ready_id = kb.create_task(conn, title="ready", assignee="default")
    review_id = kb.create_task(conn, title="review", assignee="default")
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review_id,))
    conn.commit()
    pause = _pause(tmp_path, monkeypatch)

    assert kb.claim_task(conn, ready_id) is None
    assert kb.claim_review_task(conn, review_id) is None
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0

    pause.unlink()
    assert kb.claim_task(conn, ready_id) is not None
    assert kb.claim_review_task(conn, review_id) is not None


def test_pause_lookup_error_fails_closed(monkeypatch):
    def unreadable_pause_path():
        raise OSError("pause state unavailable")

    monkeypatch.setattr(kb, "dispatch_pause_path", unreadable_pause_path)
    assert kb.dispatch_is_paused() is True


def test_halt_blocks_ready_and_review_claims_without_run_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    ready_id = kb.create_task(conn, title="ready", assignee="default")
    review_id = kb.create_task(conn, title="review", assignee="default")
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review_id,))
    conn.commit()
    halt = _halt(tmp_path, monkeypatch)

    assert kb.claim_task(conn, ready_id) is None
    assert kb.claim_review_task(conn, review_id) is None
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0

    halt.unlink()
    assert kb.claim_task(conn, ready_id) is not None
    assert kb.claim_review_task(conn, review_id) is not None


@pytest.mark.parametrize("brake", ["dispatch_pause", "halt", "estop"])
def test_recompute_ready_checks_dispatch_brakes_by_default(
    tmp_path, monkeypatch, brake
):
    """Every ready-promotion caller must opt out explicitly, never by accident."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="parked todo", assignee="default")
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
    conn.commit()
    if brake == "dispatch_pause":
        _pause(tmp_path, monkeypatch)
    elif brake == "halt":
        _halt(tmp_path, monkeypatch)
    else:
        (tmp_path / "ESTOP").write_text("{}\n", encoding="utf-8")

    with pytest.raises(kb.DispatchPausedError):
        kb.recompute_ready(conn)

    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "todo"
    conn.close()


@pytest.mark.parametrize("brake", ["dispatch_pause", "halt", "estop"])
def test_completion_commits_but_keeps_dependents_parked(
    tmp_path, monkeypatch, brake
):
    """An in-flight completion remains valid without creating ready work."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    parent_id = kb.create_task(conn, title="finishing parent")
    child_id = kb.create_task(
        conn,
        title="parked dependent",
        parents=[parent_id],
    )
    if brake == "dispatch_pause":
        _pause(tmp_path, monkeypatch)
    elif brake == "halt":
        _halt(tmp_path, monkeypatch)
    else:
        (tmp_path / "ESTOP").write_text("{}\n", encoding="utf-8")

    assert kb.complete_task(conn, parent_id, summary="finished in flight") is True

    parent = kb.get_task(conn, parent_id)
    child = kb.get_task(conn, child_id)
    assert parent is not None and parent.status == "done"
    assert child is not None and child.status == "todo"
    conn.close()


@pytest.mark.parametrize("brake", ["dispatch_pause", "halt", "estop"])
def test_direct_runnable_transitions_park_or_refuse_while_stopped(
    tmp_path, monkeypatch, brake
):
    """Every public transition into ready/review honors all three brakes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)

    promote_id = kb.create_task(conn, title="manual promotion", triage=True)
    conn.execute(
        "UPDATE tasks SET status = 'todo' WHERE id = ?", (promote_id,)
    )
    conn.commit()
    unblock_id = kb.create_task(
        conn,
        title="manual unblock",
        initial_status="blocked",
    )
    reopen_id = kb.create_task(conn, title="review reopen", assignee="builder")
    assert kb.request_review(conn, reopen_id, reviewer="reviewer")
    review_request_id = kb.create_task(
        conn,
        title="review request",
        assignee="builder",
    )
    changes_id = kb.create_task(conn, title="review changes", assignee="builder")
    implementation = kb.claim_task(conn, changes_id)
    assert implementation is not None
    assert kb.request_review(
        conn,
        changes_id,
        reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )
    review_run = kb.claim_review_task(conn, changes_id)
    assert review_run is not None

    brake_path = _engage_brake(tmp_path, monkeypatch, brake)

    created_id = kb.create_task(conn, title="created while stopped")
    promoted, reason = kb.promote_task(
        conn,
        promote_id,
        actor="test",
    )
    assert promoted is False
    assert reason is not None and "paused" in reason.lower()
    assert kb.unblock_task(conn, unblock_id) is True
    assert kb.reopen_review_task(conn, reopen_id) is True
    assert kb.request_review(
        conn,
        review_request_id,
        reviewer="reviewer",
    ) is True
    assert kb.request_changes(
        conn,
        changes_id,
        reason="fix the stopped handoff",
        expected_run_id=review_run.current_run_id,
    ) == (True, "builder")

    parked_ids = {
        created_id,
        promote_id,
        unblock_id,
        reopen_id,
        review_request_id,
        changes_id,
    }
    statuses = {
        row["id"]: row["status"]
        for row in conn.execute(
            "SELECT id, status FROM tasks WHERE id IN ("
            + ",".join("?" * len(parked_ids))
            + ")",
            tuple(parked_ids),
        ).fetchall()
    }
    assert set(statuses) == parked_ids
    assert set(statuses.values()) == {"todo"}
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events "
        "WHERE task_id = ? AND kind = 'promoted_manual'",
        (promote_id,),
    ).fetchone()[0] == 0

    expected_events = {
        created_id: ("created", "ready"),
        unblock_id: ("unblocked", "ready"),
        reopen_id: ("review_reopened", "ready"),
        review_request_id: ("review_requested", "review"),
        changes_id: ("changes_requested", "ready"),
    }
    for task_id, (kind, resume_status) in expected_events.items():
        payload = _latest_event_payload(conn, task_id, kind)
        assert payload["status"] == "todo"
        assert payload["resume_status"] == resume_status
        assert payload["parked_by_dispatch_brake"] is True

    brake_path.unlink()
    assert kb.recompute_ready(conn) == len(parked_ids)
    assert kb.get_task(conn, review_request_id).status == "review"
    for task_id in parked_ids - {review_request_id}:
        assert kb.get_task(conn, task_id).status == "ready"
    conn.close()


@pytest.mark.parametrize("brake", ["dispatch_pause", "halt", "estop"])
@pytest.mark.parametrize(
    "operation",
    [
        "create",
        "promote",
        "unblock",
        "reopen_review",
        "request_review",
        "request_changes",
    ],
)
def test_stop_engaged_during_direct_runnable_write_rolls_back(
    tmp_path, monkeypatch, brake, operation
):
    """A brake that appears during a direct status write wins before commit."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)

    task_id = None
    expected_status = None
    expected_event = {
        "create": "created",
        "promote": "promoted_manual",
        "unblock": "unblocked",
        "reopen_review": "review_reopened",
        "request_review": "review_requested",
        "request_changes": "changes_requested",
    }[operation]
    expected_write_status = "review" if operation == "request_review" else "ready"
    expected_run_id = None

    if operation == "create":
        expected_status = None
    elif operation == "promote":
        task_id = kb.create_task(conn, title="late promote", triage=True)
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
        conn.commit()
        expected_status = "todo"
    elif operation == "unblock":
        task_id = kb.create_task(
            conn,
            title="late unblock",
            initial_status="blocked",
        )
        expected_status = "blocked"
    elif operation == "reopen_review":
        task_id = kb.create_task(conn, title="late reopen", assignee="builder")
        assert kb.request_review(conn, task_id, reviewer="reviewer")
        expected_status = "review"
    elif operation == "request_review":
        task_id = kb.create_task(conn, title="late review", assignee="builder")
        expected_status = "ready"
    else:
        task_id = kb.create_task(conn, title="late changes", assignee="builder")
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            reviewer="reviewer",
            expected_run_id=implementation.current_run_id,
        )
        review_run = kb.claim_review_task(conn, task_id)
        assert review_run is not None
        expected_run_id = review_run.current_run_id
        expected_status = "running"

    task_count_before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    event_count_before = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind = ?",
        (expected_event,),
    ).fetchone()[0]
    stop_created = False

    def stop_on_runnable_write(statement):
        nonlocal stop_created
        normalized = " ".join(statement.lower().split())
        if stop_created:
            return
        if operation == "create":
            matches = (
                normalized.startswith("insert into tasks")
                and "created during write" in normalized
            )
        else:
            matches = (
                normalized.startswith("update tasks")
                and f"set status = '{expected_write_status}'" in normalized
                and (task_id or "") in normalized
            )
        if matches:
            stop_created = True
            _engage_brake(tmp_path, monkeypatch, brake)

    conn.set_trace_callback(stop_on_runnable_write)
    try:
        if operation == "create":
            with pytest.raises(kb.DispatchPausedError):
                kb.create_task(conn, title="created during write")
        elif operation == "promote":
            ok, reason = kb.promote_task(conn, task_id, actor="test")
            assert ok is False
            assert reason is not None and "paused" in reason.lower()
        elif operation == "unblock":
            with pytest.raises(kb.DispatchPausedError):
                kb.unblock_task(conn, task_id)
        elif operation == "reopen_review":
            with pytest.raises(kb.DispatchPausedError):
                kb.reopen_review_task(conn, task_id)
        elif operation == "request_review":
            with pytest.raises(kb.DispatchPausedError):
                kb.request_review(conn, task_id, reviewer="reviewer")
        else:
            with pytest.raises(kb.DispatchPausedError):
                kb.request_changes(
                    conn,
                    task_id,
                    reason="must roll back",
                    expected_run_id=expected_run_id,
                )
    finally:
        conn.set_trace_callback(None)

    assert stop_created is True
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == task_count_before
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind = ?",
        (expected_event,),
    ).fetchone()[0] == event_count_before
    if task_id is not None:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == expected_status
        if operation == "request_changes":
            assert task.current_run_id == expected_run_id
    conn.close()


def test_estop_blocks_ready_and_review_claims_without_run_rows(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    ready_id = kb.create_task(conn, title="ready", assignee="default")
    review_id = kb.create_task(conn, title="review", assignee="default")
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review_id,))
    conn.commit()
    estop_path = tmp_path / "ESTOP"
    estop_path.write_text("{}\n", encoding="utf-8")

    assert kb.claim_task(conn, ready_id) is None
    assert kb.claim_review_task(conn, review_id) is None
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0

    estop_path.unlink()
    assert kb.claim_task(conn, ready_id) is not None
    assert kb.claim_review_task(conn, review_id) is not None


@pytest.mark.linux_only
def test_broken_estop_ancestor_blocks_claim_and_final_spawn(tmp_path, monkeypatch):
    """A broken HERMES_HOME ancestor must hold every dispatch edge closed."""
    broken = tmp_path / "broken-home"
    broken.symlink_to(tmp_path / "missing-home", target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(broken / "profiles" / "planner"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="ready", assignee="default")

    assert kb.claim_task(conn, task_id) is None
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0

    task = kb.get_task(conn, task_id)
    with pytest.raises(kb.DispatchPausedError, match="global emergency stop"):
        kb._default_spawn(task, str(tmp_path))

    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0


def test_shared_root_estop_blocks_named_profile_claims(tmp_path, monkeypatch):
    """A named-profile caller must honor the default profile global stop."""
    root = tmp_path / "shared-root"
    profile_home = root / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    (root / "ESTOP").write_text("{}\n", encoding="utf-8")
    db_path = root / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    ready_id = kb.create_task(conn, title="ready", assignee="coder")
    review_id = kb.create_task(conn, title="review", assignee="coder")
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review_id,))
    conn.commit()

    assert kb.claim_task(conn, ready_id) is None
    assert kb.claim_review_task(conn, review_id) is None
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0

    task = kb.get_task(conn, ready_id)
    with pytest.raises(kb.DispatchPausedError, match="global emergency stop"):
        kb._default_spawn(task, str(root))

    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("brake", ["halt", "estop"])
def test_stop_engaged_during_claim_lock_wait_blocks_transaction(
    tmp_path, monkeypatch, lane, brake
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    setup = kb.connect(db_path=db_path)
    task_id = kb.create_task(setup, title=f"{lane} lock wait", assignee="default")
    if lane == "review":
        setup.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        setup.commit()
    setup.close()

    blocker = kb.connect(db_path=db_path)
    blocker.execute("BEGIN IMMEDIATE")
    entering_transaction = threading.Event()
    real_write_txn = kb.write_txn

    @contextlib.contextmanager
    def observed_write_txn(conn, *args, **kwargs):
        entering_transaction.set()
        with real_write_txn(conn, *args, **kwargs):
            yield

    monkeypatch.setattr(kb, "write_txn", observed_write_txn)
    outcome = []
    failures = []

    def claim_after_lock_release():
        conn = kb.connect(db_path=db_path)
        try:
            claim = kb.claim_task if lane == "ready" else kb.claim_review_task
            outcome.append(claim(conn, task_id))
        except Exception as exc:
            failures.append(exc)
        finally:
            conn.close()

    worker = threading.Thread(target=claim_after_lock_release)
    worker.start()
    try:
        assert entering_transaction.wait(timeout=2)
        if brake == "halt":
            _halt(tmp_path, monkeypatch)
        else:
            (tmp_path / "ESTOP").write_text("{}\n", encoding="utf-8")
    finally:
        blocker.rollback()
        blocker.close()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert failures == []
    assert outcome == [None]
    verify = kb.connect(db_path=db_path)
    task = kb.get_task(verify, task_id)
    assert task is not None and task.status == lane
    assert verify.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
    verify.close()


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("brake", ["dispatch_pause", "halt", "estop"])
def test_stop_engaged_during_claim_writes_rolls_back_transaction(
    tmp_path, monkeypatch, lane, brake
):
    """A stop raised from inside the claim transaction wins before commit."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title=f"{lane} late stop", assignee="default")
    if lane == "review":
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        conn.commit()

    stop_created = False

    def stop_on_running_update(statement):
        nonlocal stop_created
        normalized = " ".join(statement.lower().split())
        if (
            not stop_created
            and normalized.startswith("update tasks")
            and "set status = 'running'" in normalized
        ):
            stop_created = True
            _engage_brake(tmp_path, monkeypatch, brake)

    conn.set_trace_callback(stop_on_running_update)
    try:
        claim = kb.claim_task if lane == "ready" else kb.claim_review_task
        outcome = claim(conn, task_id)
    finally:
        conn.set_trace_callback(None)

    task = kb.get_task(conn, task_id)
    assert stop_created is True
    assert outcome is None
    assert task is not None and task.status == lane
    assert task.claim_lock is None
    assert task.current_run_id is None
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'claimed'",
        (task_id,),
    ).fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("operation", ["specify", "decompose"])
@pytest.mark.parametrize("brake", ["halt", "estop"])
def test_stop_engaged_during_planning_lock_wait_rolls_back_transaction(
    tmp_path, monkeypatch, operation, brake
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    setup = kb.connect(db_path=db_path)
    task_id = kb.create_task(setup, title="planning lock wait", triage=True)
    setup.close()

    blocker = kb.connect(db_path=db_path)
    blocker.execute("BEGIN IMMEDIATE")
    entering_transaction = threading.Event()
    real_write_txn = kb.write_txn

    @contextlib.contextmanager
    def observed_write_txn(conn, *args, **kwargs):
        entering_transaction.set()
        with real_write_txn(conn, *args, **kwargs):
            yield

    monkeypatch.setattr(kb, "write_txn", observed_write_txn)
    outcomes = []
    failures = []

    def plan_after_lock_release():
        conn = kb.connect(db_path=db_path)
        try:
            if operation == "specify":
                outcomes.append(
                    kb.specify_triage_task(
                        conn,
                        task_id,
                        title="must not land",
                        body="must remain in triage",
                        author="test",
                    )
                )
            else:
                outcomes.append(
                    kb.decompose_triage_task(
                        conn,
                        task_id,
                        root_assignee="default",
                        children=[
                            {
                                "title": "must not exist",
                                "body": "the stop wins",
                                "assignee": "default",
                                "parents": [],
                            }
                        ],
                        author="test",
                    )
                )
        except Exception as exc:
            failures.append(exc)
        finally:
            conn.close()

    worker = threading.Thread(target=plan_after_lock_release)
    worker.start()
    try:
        assert entering_transaction.wait(timeout=2)
        if brake == "halt":
            _halt(tmp_path, monkeypatch)
        else:
            (tmp_path / "ESTOP").write_text("{}\n", encoding="utf-8")
    finally:
        blocker.rollback()
        blocker.close()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert outcomes == []
    assert len(failures) == 1
    assert isinstance(failures[0], kb.DispatchPausedError)
    verify = kb.connect(db_path=db_path)
    task = kb.get_task(verify, task_id)
    assert task is not None and task.status == "triage"
    assert task.title == "planning lock wait"
    assert verify.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert verify.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] == 0
    verify.close()


@pytest.mark.parametrize("operation", ["specify", "decompose"])
@pytest.mark.parametrize("brake", ["dispatch_pause", "halt", "estop"])
def test_stop_engaged_during_planning_writes_rolls_back_transaction(
    tmp_path, monkeypatch, operation, brake
):
    """A stop raised after planning starts rolls back its whole audit trail."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="planning write", triage=True)
    stop_created = False

    def stop_on_todo_update(statement):
        nonlocal stop_created
        normalized = " ".join(statement.lower().split())
        if (
            not stop_created
            and normalized.startswith("update tasks")
            and "set status = 'todo'" in normalized
        ):
            stop_created = True
            _engage_brake(tmp_path, monkeypatch, brake)

    conn.set_trace_callback(stop_on_todo_update)
    try:
        with pytest.raises(kb.DispatchPausedError):
            if operation == "specify":
                kb.specify_triage_task(
                    conn,
                    task_id,
                    title="must not land",
                    body="must roll back",
                    author="test",
                )
            else:
                kb.decompose_triage_task(
                    conn,
                    task_id,
                    root_assignee="default",
                    children=[
                        {
                            "title": "must not exist",
                            "body": "must roll back",
                            "assignee": "default",
                            "parents": [],
                        }
                    ],
                    author="test",
                )
    finally:
        conn.set_trace_callback(None)

    task = kb.get_task(conn, task_id)
    assert stop_created is True
    assert task is not None and task.status == "triage"
    assert task.title == "planning write"
    assert task.body is None
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind IN ('specified', 'decomposed')"
    ).fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("operation", ["specify", "decompose"])
@pytest.mark.parametrize("brake", ["halt", "estop"])
def test_stop_engaged_during_ready_promotion_lock_wait_keeps_todos_parked(
    tmp_path, monkeypatch, operation, brake
):
    """The second planning transaction must recheck a newly engaged stop."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    setup = kb.connect(db_path=db_path)
    task_id = kb.create_task(setup, title="promotion lock wait", triage=True)
    setup.close()

    promotion_called = threading.Event()
    allow_promotion = threading.Event()
    entering_promotion_txn = threading.Event()
    real_recompute_ready = kb.recompute_ready
    real_write_txn = kb.write_txn

    def observed_recompute_ready(conn, *args, **kwargs):
        promotion_called.set()
        if not allow_promotion.wait(timeout=5):
            raise RuntimeError("test did not release promotion")
        return real_recompute_ready(conn, *args, **kwargs)

    monkeypatch.setattr(kb, "recompute_ready", observed_recompute_ready)
    outcomes = []
    failures = []

    def plan_through_promotion():
        conn = kb.connect(db_path=db_path)
        try:
            if operation == "specify":
                outcomes.append(
                    kb.specify_triage_task(
                        conn,
                        task_id,
                        title="specified but parked",
                        author="test",
                    )
                )
            else:
                outcomes.append(
                    kb.decompose_triage_task(
                        conn,
                        task_id,
                        root_assignee="default",
                        children=[
                            {
                                "title": "created but parked",
                                "assignee": "default",
                                "parents": [],
                            }
                        ],
                        author="test",
                    )
                )
        except Exception as exc:
            failures.append(exc)
        finally:
            conn.close()

    worker = threading.Thread(target=plan_through_promotion)
    worker.start()
    assert promotion_called.wait(timeout=2)

    blocker = kb.connect(db_path=db_path)
    blocker.execute("BEGIN IMMEDIATE")

    @contextlib.contextmanager
    def observed_write_txn(conn, *args, **kwargs):
        entering_promotion_txn.set()
        with real_write_txn(conn, *args, **kwargs):
            yield

    monkeypatch.setattr(kb, "write_txn", observed_write_txn)
    allow_promotion.set()
    try:
        assert entering_promotion_txn.wait(timeout=2)
        if brake == "halt":
            _halt(tmp_path, monkeypatch)
        else:
            (tmp_path / "ESTOP").write_text("{}\n", encoding="utf-8")
    finally:
        blocker.rollback()
        blocker.close()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert failures == []
    assert len(outcomes) == 1
    if operation == "specify":
        assert outcomes[0] is True
    else:
        assert isinstance(outcomes[0], list)
        assert len(outcomes[0]) == 1
    verify = kb.connect(db_path=db_path)
    root = kb.get_task(verify, task_id)
    assert root is not None and root.status == "todo"
    assert verify.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'ready'"
    ).fetchone()[0] == 0
    expected_task_count = 1 if operation == "specify" else 2
    assert verify.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == expected_task_count
    verify.close()


@pytest.mark.parametrize("brake", ["dispatch_pause", "halt", "estop"])
def test_stop_engaged_during_ready_promotion_writes_rolls_back_transaction(
    tmp_path, monkeypatch, brake
):
    """A stop raised during todo-to-ready writes prevents their commit."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="promotion write", triage=True)
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
    conn.commit()
    stop_created = False

    def stop_on_ready_update(statement):
        nonlocal stop_created
        normalized = " ".join(statement.lower().split())
        if (
            not stop_created
            and normalized.startswith("update tasks")
            and "set status = 'ready'" in normalized
        ):
            stop_created = True
            _engage_brake(tmp_path, monkeypatch, brake)

    conn.set_trace_callback(stop_on_ready_update)
    try:
        with pytest.raises(kb.DispatchPausedError):
            kb.recompute_ready(conn)
    finally:
        conn.set_trace_callback(None)

    task = kb.get_task(conn, task_id)
    assert stop_created is True
    assert task is not None and task.status == "todo"
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'promoted'",
        (task_id,),
    ).fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("brake", ["halt", "estop"])
def test_stop_engaged_during_dispatch_reclaim_skips_promotion_and_spawn(
    tmp_path, monkeypatch, brake
):
    """A stop arriving after tick entry must end the tick before ready work."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="stay parked", triage=True)
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
    conn.commit()

    def stop_during_reclaim(_conn):
        if brake == "halt":
            _halt(tmp_path, monkeypatch)
        else:
            (tmp_path / "ESTOP").write_text("{}\n", encoding="utf-8")
        return []

    spawn_calls = []
    monkeypatch.setattr(kb, "release_stale_claims", stop_during_reclaim)

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *args: spawn_calls.append(args),
        max_spawn=1,
    )

    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "todo"
    assert result.promoted == 0
    assert result.spawned == []
    assert spawn_calls == []
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("brake", ["dispatch_pause", "halt", "estop"])
@pytest.mark.parametrize("source_lane", ["ready", "review"])
@pytest.mark.parametrize("arrival", ["retry_write", "commit"])
@pytest.mark.parametrize(
    "recovery",
    [
        "expired_claim",
        "manual_reclaim",
        "stale_heartbeat",
        "timed_out",
        "crashed",
        "orphaned",
        "spawn_failed",
    ],
)
def test_stop_during_retry_transition_parks_and_restores_source_lane(
    tmp_path, monkeypatch, recovery, arrival, source_lane, brake
):
    """A brake arriving during a retry write or COMMIT stays authoritative."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        kb,
        "_classify_worker_exit",
        lambda _pid: ("nonzero_exit", 1),
    )
    monkeypatch.setattr(kb, "_cleanup_worker_tmux", lambda *_args: None)
    monkeypatch.setattr(kb, "_unlock_task_worktree", lambda *_args: None)

    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(
        conn,
        title=f"{recovery} from {source_lane}",
        assignee="default",
        max_runtime_seconds=1 if recovery == "timed_out" else None,
    )
    if source_lane == "review":
        conn.execute(
            "UPDATE tasks SET status = 'review' WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        claimed = kb.claim_review_task(
            conn,
            task_id,
            claimer=f"{kb._claimer_id().split(':', 1)[0]}:test",
        )
    else:
        claimed = kb.claim_task(
            conn,
            task_id,
            claimer=f"{kb._claimer_id().split(':', 1)[0]}:test",
        )
    assert claimed is not None

    old = int(kb.time.time()) - 10_000
    if recovery == "expired_claim":
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (old, task_id),
        )
    elif recovery == "stale_heartbeat":
        conn.execute(
            "UPDATE tasks SET started_at = ?, last_heartbeat_at = NULL "
            "WHERE id = ?",
            (old, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?",
            (old, claimed.current_run_id),
        )
    elif recovery in {"timed_out", "crashed"}:
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, started_at = ? WHERE id = ?",
            (999_999, old, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ?, started_at = ? WHERE id = ?",
            (999_999, old, claimed.current_run_id),
        )
    elif recovery == "orphaned":
        conn.execute(
            "UPDATE tasks SET claim_lock = NULL WHERE id = ?",
            (task_id,),
        )
    conn.commit()

    stop_created = False
    brake_path = None

    def stop_on_retry_update(statement):
        nonlocal stop_created, brake_path
        normalized = " ".join(statement.lower().split())
        if (
            not stop_created
            and (
                (
                    arrival == "retry_write"
                    and normalized.startswith("update tasks")
                    and "set status =" in normalized
                )
                or (arrival == "commit" and normalized == "commit")
            )
        ):
            stop_created = True
            brake_path = _engage_brake(tmp_path, monkeypatch, brake)

    conn.set_trace_callback(stop_on_retry_update)
    try:
        if recovery == "expired_claim":
            outcome = kb.release_stale_claims(
                conn,
                signal_fn=lambda *_args: None,
            )
            assert outcome == 1
        elif recovery == "manual_reclaim":
            assert kb.reclaim_task(
                conn,
                task_id,
                reason="test retry",
                signal_fn=lambda *_args: None,
            )
        elif recovery == "stale_heartbeat":
            assert kb.detect_stale_running(
                conn,
                stale_timeout_seconds=1,
                signal_fn=lambda *_args: None,
            ) == [task_id]
        elif recovery == "timed_out":
            assert kb.enforce_max_runtime(
                conn,
                signal_fn=lambda *_args: None,
            ) == [task_id]
        elif recovery == "crashed":
            assert kb.detect_crashed_workers(conn) == [task_id]
        elif recovery == "orphaned":
            assert kb.reconcile_orphaned_running(conn) == [task_id]
        else:
            assert not kb._record_spawn_failure(
                conn,
                task_id,
                "spawn failed during stop race",
                failure_limit=3,
            )
    finally:
        conn.set_trace_callback(None)

    assert stop_created is True
    assert brake_path is not None and brake_path.exists()
    parked = kb.get_task(conn, task_id)
    assert parked is not None
    assert parked.status == "todo"
    assert parked.current_run_id is None
    assert conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('ready', 'review')"
    ).fetchone()[0] == 0
    parked_event = kb.list_events(conn, task_id)[-1]
    assert parked_event.kind == "dispatch_parked"
    assert parked_event.payload is not None
    assert parked_event.payload["resume_status"] == source_lane
    assert parked_event.payload["parked_by_dispatch_brake"] is True

    brake_path.unlink()
    estop._reset_log_state_for_tests()
    assert kb.recompute_ready(conn) == 1
    resumed = kb.get_task(conn, task_id)
    assert resumed is not None and resumed.status == source_lane
    conn.close()


@pytest.mark.parametrize("brake", ["dispatch_pause", "halt", "estop"])
@pytest.mark.parametrize(
    "arrival", ["after_promotion", "after_write_lock", "during_assignment"]
)
def test_stop_prevents_default_assignment_before_or_inside_transaction(
    tmp_path, monkeypatch, brake, arrival
):
    """A stopped tick cannot assign or audit an unassigned ready task."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="remain unassigned", assignee=None)
    real_recompute_ready = kb.recompute_ready
    stop_created = False

    def stop_during_assignment_transaction(statement):
        nonlocal stop_created
        normalized = " ".join(statement.lower().split())
        if (
            not stop_created
            and (
                (arrival == "after_write_lock" and normalized == "begin immediate")
                or (
                    arrival == "during_assignment"
                    and normalized.startswith("update tasks")
                    and "set assignee =" in normalized
                )
            )
        ):
            stop_created = True
            _engage_brake(tmp_path, monkeypatch, brake)

    def observed_recompute_ready(connection, *args, **kwargs):
        nonlocal stop_created
        promoted = real_recompute_ready(connection, *args, **kwargs)
        if arrival == "after_promotion":
            stop_created = True
            _engage_brake(tmp_path, monkeypatch, brake)
        else:
            connection.set_trace_callback(stop_during_assignment_transaction)
        return promoted

    monkeypatch.setattr(kb, "recompute_ready", observed_recompute_ready)
    spawn_calls = []
    try:
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *args: spawn_calls.append(args),
            default_assignee="default",
            max_spawn=1,
        )
    finally:
        conn.set_trace_callback(None)

    task = kb.get_task(conn, task_id)
    assert stop_created is True
    assert task is not None and task.status == "ready"
    assert task.assignee is None
    assert result.auto_assigned_default == []
    assert result.spawned == []
    assert spawn_calls == []
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'assigned'",
        (task_id,),
    ).fetchone()[0] == 0
    conn.close()


@pytest.mark.linux_only
def test_broken_estop_entry_blocks_dispatch_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    (tmp_path / "ESTOP").symlink_to(tmp_path / "missing-estop-target")

    with pytest.raises(kb.DispatchPausedError, match="emergency stop"):
        kb._raise_if_dispatch_paused()


def test_estop_lookup_error_blocks_dispatch_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    target = tmp_path / "ESTOP"
    real_lstat = estop.os.lstat
    real_optional_entry_info = estop._optional_entry_info

    def denied_lstat(path, *args, **kwargs):
        if path == target:
            raise PermissionError("ESTOP lookup denied")
        return real_lstat(path, *args, **kwargs)

    def denied_anchored_stat(parent_fd, name):
        if name == estop.SENTINEL_NAME:
            raise PermissionError("ESTOP lookup denied")
        return real_optional_entry_info(parent_fd, name)

    monkeypatch.setattr(estop.os, "lstat", denied_lstat)
    monkeypatch.setattr(estop, "_optional_entry_info", denied_anchored_stat)

    with pytest.raises(kb.DispatchPausedError, match="emergency stop"):
        kb._raise_if_dispatch_paused()


@pytest.mark.parametrize("brake_name", ["dispatch_pause.json", "halt.json"])
def test_dispatch_brake_directory_entry_blocks(tmp_path, monkeypatch, brake_name):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    (tmp_path / "state" / brake_name).mkdir(parents=True)

    assert kb.dispatch_is_paused() is True


@pytest.mark.linux_only
@pytest.mark.parametrize("brake_name", ["dispatch_pause.json", "halt.json"])
def test_dispatch_brake_broken_symlink_entry_blocks(tmp_path, monkeypatch, brake_name):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    path = tmp_path / "state" / brake_name
    path.parent.mkdir(parents=True)
    path.symlink_to(tmp_path / "does-not-exist")

    assert kb.dispatch_is_paused() is True


@pytest.mark.linux_only
@pytest.mark.parametrize("brake_name", ["dispatch_pause.json", "halt.json"])
def test_dispatch_brake_intact_symlink_entry_blocks(tmp_path, monkeypatch, brake_name):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    target = tmp_path / "target"
    target.write_text("not a brake\n", encoding="utf-8")
    path = tmp_path / "state" / brake_name
    path.parent.mkdir(parents=True)
    path.symlink_to(target)

    assert kb.dispatch_is_paused() is True


@pytest.mark.parametrize("brake_name", ["dispatch_pause.json", "halt.json"])
def test_dispatch_brake_lookup_error_fails_closed(
    tmp_path, monkeypatch, brake_name
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    target = tmp_path / "state" / brake_name
    real_lstat = kb.os.lstat
    real_stat = kb.os.stat

    def unreadable_entry(path):
        if path == target:
            raise PermissionError("dispatch state unavailable")
        return real_lstat(path)

    def unreadable_anchored_entry(path, *args, **kwargs):
        if (
            kwargs.get("dir_fd") is not None
            and os.fspath(path) == brake_name
        ):
            raise PermissionError("dispatch state unavailable")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(kb.os, "lstat", unreadable_entry)
    monkeypatch.setattr(kb.os, "stat", unreadable_anchored_entry)

    assert kb.dispatch_is_paused() is True


def test_absent_shared_root_and_state_directory_allow_dispatch(tmp_path, monkeypatch):
    root = tmp_path / "missing-root"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))

    assert kb.dispatch_is_paused() is False
    root.mkdir()
    assert kb.dispatch_is_paused() is False
    (root / "state").mkdir()
    assert kb.dispatch_is_paused() is False


def test_non_directory_state_entry_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    (tmp_path / "state").write_text("not a directory\n", encoding="utf-8")

    assert kb.dispatch_is_paused() is True


@pytest.mark.linux_only
@pytest.mark.parametrize("broken", [False, True])
def test_state_symlink_entry_blocks(tmp_path, monkeypatch, broken):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "state-target"
    if not broken:
        target.mkdir()
    (root / "state").symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))

    assert kb.dispatch_is_paused() is True


@pytest.mark.linux_only
def test_broken_shared_root_symlink_blocks(tmp_path, monkeypatch):
    root = tmp_path / "shared-root"
    root.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))

    assert kb.dispatch_is_paused() is True


@pytest.mark.linux_only
def test_intact_shared_root_symlink_preserves_shared_board_semantics(
    tmp_path, monkeypatch
):
    target = tmp_path / "actual-shared-root"
    target.mkdir()
    root = tmp_path / "shared-root-link"
    root.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))

    assert kb.dispatch_is_paused() is False
    halt = target / "state" / "halt.json"
    halt.parent.mkdir()
    halt.write_text("{}\n", encoding="utf-8")
    assert kb.dispatch_is_paused() is True


@pytest.mark.parametrize("component", ["root", "state"])
def test_dispatch_ancestor_lstat_error_fails_closed(tmp_path, monkeypatch, component):
    root = tmp_path / "root"
    state = root / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    target = root if component == "root" else state
    real_lstat = kb.os.lstat

    def unreadable_ancestor(path):
        if path == target:
            raise PermissionError("dispatch ancestor unavailable")
        return real_lstat(path)

    monkeypatch.setattr(kb.os, "lstat", unreadable_ancestor)

    assert kb.dispatch_is_paused() is True


def test_brake_entry_appearing_after_initial_enoent_still_blocks(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    state.mkdir()
    target = state / "dispatch_pause.json"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    real_lstat = kb.os.lstat
    real_stat = kb.os.stat
    first_lookup = True

    def brake_appears_during_lookup(path):
        nonlocal first_lookup
        if path == target and first_lookup:
            first_lookup = False
            target.mkdir()
            raise FileNotFoundError("raced with brake creation")
        return real_lstat(path)

    def brake_appears_during_anchored_lookup(path, *args, **kwargs):
        nonlocal first_lookup
        if (
            kwargs.get("dir_fd") is not None
            and os.fspath(path) == target.name
            and first_lookup
        ):
            first_lookup = False
            target.mkdir()
            raise FileNotFoundError("raced with brake creation")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(kb.os, "lstat", brake_appears_during_lookup)
    monkeypatch.setattr(kb.os, "stat", brake_appears_during_anchored_lookup)

    assert kb.dispatch_is_paused() is True


def test_brake_created_during_final_state_snapshot_still_blocks(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    state.mkdir()
    target = state / "dispatch_pause.json"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    real_lstat = kb.os.lstat
    state_lookups = 0
    final_state_lookup = (
        3 if kb._supports_anchored_dispatch_state_read() else 4
    )

    def brake_appears_during_final_snapshot(path):
        nonlocal state_lookups
        if path == state:
            state_lookups += 1
            if state_lookups == final_state_lookup:
                target.write_text("{}\n", encoding="utf-8")
        return real_lstat(path)

    monkeypatch.setattr(
        kb.os, "lstat", brake_appears_during_final_snapshot
    )

    assert kb.dispatch_is_paused() is True
    assert target.is_file()


@pytest.mark.linux_only
def test_dispatch_state_read_anchors_real_bind_mount_swaps(tmp_path):
    """A real temporary state mount cannot hide either dispatch brake."""
    unshare = shutil.which("unshare")
    mount = shutil.which("mount")
    umount = shutil.which("umount")
    if not unshare or not mount or not umount:
        pytest.skip("unshare, mount, and umount are required")

    root = tmp_path / "shared-root"
    state = root / "state"
    state.mkdir(parents=True)
    halt = state / "halt.json"
    halt.write_text("{}\n", encoding="utf-8")
    empty_state = tmp_path / "empty-state"
    empty_state.mkdir()
    child = r'''
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1])
state = root / "state"
empty_state = Path(sys.argv[2])
mount = sys.argv[3]
umount = sys.argv[4]
pause = state / "dispatch_pause.json"
halt = state / "halt.json"
subprocess.run([mount, "--make-rprivate", "/"], check=True)
os.environ["HERMES_KANBAN_HOME"] = str(root)

from hermes_cli import kanban_db as kb

# Reproduce the former path race with real mounts: every leaf lookup sees the
# empty mount, while both directory snapshots see the restored original.
before = kb._dispatch_state_snapshot(state)
real_lstat = os.lstat
path_swaps = 0

def lstat_through_temporary_mount(path, *args, **kwargs):
    global path_swaps
    if Path(path) not in (pause, halt):
        return real_lstat(path, *args, **kwargs)
    subprocess.run([mount, "--bind", str(empty_state), str(state)], check=True)
    try:
        return real_lstat(path, *args, **kwargs)
    finally:
        subprocess.run([umount, str(state)], check=True)
        path_swaps += 1

os.lstat = lstat_through_temporary_mount
try:
    old_paused = (
        before is None
        or kb._dispatch_brake_entry_exists(pause)
        or kb._dispatch_brake_entry_exists(halt)
        or kb._dispatch_state_snapshot(state) != before
    )
finally:
    os.lstat = real_lstat

# A mount already present when the state directory is opened has a different
# Linux mount identity and must fail closed even though it hides the brake.
subprocess.run([mount, "--bind", str(empty_state), str(state)], check=True)
try:
    mounted_paused = kb.dispatch_is_paused()
finally:
    subprocess.run([umount, str(state)], check=True)

# If the mount arrives only around each leaf read, the retained state handle
# still names the original directory and therefore still sees halt.json.
real_optional_entry_info = kb._dispatch_optional_entry_info
anchored_swaps = 0

def anchored_read_through_temporary_mount(parent_fd, name):
    global anchored_swaps
    if name not in (pause.name, halt.name):
        return real_optional_entry_info(parent_fd, name)
    subprocess.run([mount, "--bind", str(empty_state), str(state)], check=True)
    try:
        return real_optional_entry_info(parent_fd, name)
    finally:
        subprocess.run([umount, str(state)], check=True)
        anchored_swaps += 1

kb._dispatch_optional_entry_info = anchored_read_through_temporary_mount
anchored_paused = kb.dispatch_is_paused()

print(json.dumps({
    "old_paused": old_paused,
    "mounted_paused": mounted_paused,
    "anchored_paused": anchored_paused,
    "path_swaps": path_swaps,
    "anchored_swaps": anchored_swaps,
    "halt_present": halt.is_file(),
}))
'''
    result = subprocess.run(
        [
            unshare,
            "-Ur",
            "-m",
            sys.executable,
            "-c",
            child,
            str(root),
            str(empty_state),
            mount,
            umount,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "Operation not permitted" in result.stderr:
        pytest.skip("this Linux host disables unprivileged mount namespaces")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])

    assert payload == {
        "old_paused": False,
        "mounted_paused": True,
        "anchored_paused": True,
        "path_swaps": 4,
        "anchored_swaps": 2,
        "halt_present": True,
    }
    assert halt.is_file()


@pytest.mark.windows_only
@pytest.mark.parametrize("root_kind", ["directory", "junction"])
def test_dispatch_state_read_anchors_configured_root_on_windows(
    tmp_path, monkeypatch, root_kind
):
    """A real Windows root replacement or redirect cannot hide a halt."""
    configured_root = tmp_path / "configured-root"
    physical_root = (
        configured_root
        if root_kind == "directory"
        else tmp_path / "physical-root"
    )
    physical_state = physical_root / "state"
    physical_state.mkdir(parents=True)
    empty_root = tmp_path / "empty-root"
    (empty_root / "state").mkdir(parents=True)

    def create_junction(path, target):
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(path), str(target)],
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr.decode(
            "utf-8",
            errors="replace",
        )

    def remove_junction(path):
        result = subprocess.run(
            ["cmd", "/c", "rmdir", str(path)],
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr.decode(
            "utf-8",
            errors="replace",
        )

    if root_kind == "junction":
        create_junction(configured_root, physical_root)

    physical_halt = physical_state / "halt.json"
    physical_halt.write_text("{}\n", encoding="utf-8")
    logical_state = configured_root / "state"
    logical_pause = logical_state / "dispatch_pause.json"
    logical_halt = logical_state / "halt.json"
    physical_pause = physical_state / "dispatch_pause.json"
    moved_root = tmp_path / "configured-root-original"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(configured_root))

    real_lstat = kb.os.lstat
    brake_keys = {
        os.path.normcase(os.path.abspath(os.fspath(path)))
        for path in (
            logical_pause,
            logical_halt,
            physical_pause,
            physical_halt,
        )
    }
    replacement_attempts = 0
    completed_swaps = 0
    locked_attempts = 0
    expect_locked = False

    def lstat_during_temporary_root_replacement(path, *args, **kwargs):
        nonlocal replacement_attempts, completed_swaps, locked_attempts
        path_key = os.path.normcase(os.path.abspath(os.fspath(path)))
        if path_key not in brake_keys:
            return real_lstat(path, *args, **kwargs)
        replacement_attempts += 1
        try:
            configured_root.rename(moved_root)
        except OSError as exc:
            if (
                not expect_locked
                or getattr(exc, "winerror", None) not in {5, 32, 33}
            ):
                raise
            locked_attempts += 1
            return real_lstat(path, *args, **kwargs)
        if root_kind == "directory":
            empty_root.rename(configured_root)
        else:
            create_junction(configured_root, empty_root)
        try:
            return real_lstat(path, *args, **kwargs)
        finally:
            if root_kind == "directory":
                configured_root.rename(empty_root)
            else:
                remove_junction(configured_root)
            moved_root.rename(configured_root)
            completed_swaps += 1

    monkeypatch.setattr(
        kb.os,
        "lstat",
        lstat_during_temporary_root_replacement,
    )

    # Reproduce the former path race before either directory is locked: both
    # brake lookups see the empty replacement and both snapshots see the
    # restored original root.
    before = kb._dispatch_state_snapshot(logical_state)
    old_paused = (
        before is None
        or kb._dispatch_brake_entry_exists(logical_pause)
        or kb._dispatch_brake_entry_exists(logical_halt)
        or kb._dispatch_state_snapshot(logical_state) != before
    )
    assert old_paused is False
    assert replacement_attempts == 4
    assert completed_swaps == 4

    replacement_attempts = 0
    completed_swaps = 0
    expect_locked = True

    # A real root is locked against replacement. A root junction may still
    # move, but the public read stays on its locked physical root and state.
    assert kb.dispatch_is_paused() is True
    assert replacement_attempts == 3
    assert completed_swaps == (0 if root_kind == "directory" else 3)
    assert locked_attempts == (3 if root_kind == "directory" else 0)
    assert logical_halt.is_file()
    assert physical_halt.is_file()


def test_halt_uses_shared_root_from_profile_home(tmp_path, monkeypatch):
    root = tmp_path / "shared"
    profile_home = root / "profiles" / "planner"
    profile_home.mkdir(parents=True)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    halt = root / "state" / "halt.json"
    halt.parent.mkdir(parents=True)
    halt.write_text("{}", encoding="utf-8")

    assert kb.dispatch_halt_path() == halt
    assert kb.dispatch_is_paused() is True


def test_pause_blocks_dispatch_and_final_spawn_edge(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="ready", assignee="default")
    pause = _pause(tmp_path, monkeypatch)
    spawned = []

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *args, **kwargs: spawned.append((args, kwargs)),
        max_spawn=1,
    )
    assert result.spawned == []
    assert spawned == []
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0

    task = kb.get_task(conn, task_id)
    try:
        kb._default_spawn(task, str(tmp_path))
    except RuntimeError as exc:
        assert str(pause) in str(exc)
    else:
        raise AssertionError("paused final spawn edge did not fail closed")

    pause.unlink()
    resumed = kb.dispatch_once(
        conn,
        spawn_fn=lambda *args, **kwargs: 4242,
        max_spawn=1,
    )
    assert [item[0] for item in resumed.spawned] == [task_id]


def test_halt_blocks_dispatch_and_final_spawn_edge(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="ready", assignee="default")
    halt = _halt(tmp_path, monkeypatch)
    spawned = []

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *args, **kwargs: spawned.append((args, kwargs)),
        max_spawn=1,
    )
    assert result.spawned == []
    assert spawned == []
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0

    task = kb.get_task(conn, task_id)
    with pytest.raises(kb.DispatchPausedError, match=str(halt)):
        kb._default_spawn(task, str(tmp_path))


def test_halt_lookup_error_blocks_claim_dispatch_and_final_spawn(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="ready", assignee="default")
    spawned = []

    def unreadable_halt_path():
        raise OSError("halt state unavailable")

    monkeypatch.setattr(kb, "dispatch_halt_path", unreadable_halt_path)

    assert kb.dispatch_is_paused() is True
    assert kb.claim_task(conn, task_id) is None
    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *args, **kwargs: spawned.append((args, kwargs)),
        max_spawn=1,
    )
    assert result.spawned == []
    assert spawned == []
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0

    task = kb.get_task(conn, task_id)
    with pytest.raises(kb.DispatchPausedError, match="unreadable shared-root"):
        kb._default_spawn(task, str(tmp_path))


def test_halt_arriving_after_claim_parks_without_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="ready", assignee="default")

    def halt_at_final_spawn(task, workspace, *, board=None):
        _halt(tmp_path, monkeypatch)
        return kb._default_spawn(task, workspace, board=board)

    result = kb.dispatch_once(conn, spawn_fn=halt_at_final_spawn, max_spawn=1)
    task = kb.get_task(conn, task_id)
    assert result.spawned == []
    assert task.status == "todo"
    assert task.consecutive_failures == 0
    payload = _latest_event_payload(conn, task_id, "dispatch_paused")
    assert payload["resume_status"] == "ready"
    assert payload["parked_by_dispatch_brake"] is True
    run = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    assert (run["status"], run["outcome"]) == ("reclaimed", "reclaimed")
    assert run["ended_at"] is not None


@pytest.mark.parametrize(
    ("lane", "expected_status"),
    [("ready", "ready"), ("review", "review")],
)
@pytest.mark.parametrize("workspace_kind", ["scratch", "worktree"])
@pytest.mark.parametrize("brake", ["dispatch_pause", "halt", "estop"])
def test_stop_after_claim_blocks_workspace_materialization(
    tmp_path, monkeypatch, lane, expected_status, workspace_kind, brake
):
    """A claimed card must be parked before any directory or branch is made."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(
        conn,
        title=f"{lane} {workspace_kind}",
        assignee="default",
        workspace_kind=workspace_kind,
    )
    if lane == "review":
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        conn.commit()
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)

    materialized = tmp_path / "materialized-workspace"
    resolver_calls = []

    def materialize_workspace():
        resolver_calls.append(True)
        materialized.mkdir()
        return materialized

    def resolve_scratch(_task, *, board=None, materialization=None):
        return materialize_workspace()

    def resolve_worktree(
        _task, *, board=None, conn=None, materialization=None
    ):
        return materialize_workspace(), "hermes/test/materialized"

    monkeypatch.setattr(kb, "resolve_workspace", resolve_scratch)
    monkeypatch.setattr(kb, "_resolve_worktree_workspace", resolve_worktree)

    claim_name = "claim_task" if lane == "ready" else "claim_review_task"
    real_claim = getattr(kb, claim_name)

    def claim_then_stop(*args, **kwargs):
        claimed = real_claim(*args, **kwargs)
        if claimed is not None:
            if brake == "dispatch_pause":
                _pause(tmp_path, monkeypatch)
            elif brake == "halt":
                _halt(tmp_path, monkeypatch)
            else:
                (tmp_path / "ESTOP").write_text("{}\n", encoding="utf-8")
        return claimed

    monkeypatch.setattr(kb, claim_name, claim_then_stop)
    spawn_calls = []

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *args, **kwargs: spawn_calls.append((args, kwargs)),
        max_spawn=1,
    )

    task = kb.get_task(conn, task_id)
    run = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    assert resolver_calls == []
    assert not materialized.exists()
    assert spawn_calls == []
    assert result.spawned == []
    assert task is not None and task.status == "todo"
    payload = _latest_event_payload(conn, task_id, "dispatch_paused")
    assert payload["resume_status"] == expected_status
    assert task.workspace_path is None
    assert task.branch_name is None
    assert task.consecutive_failures == 0
    assert run is not None
    assert (run["status"], run["outcome"]) == ("reclaimed", "reclaimed")
    assert run["ended_at"] is not None
    conn.close()


@pytest.mark.parametrize(
    ("lane", "expected_status"),
    [("ready", "ready"), ("review", "review")],
)
@pytest.mark.parametrize("workspace_kind", ["scratch", "dir"])
@pytest.mark.parametrize("brake", ["dispatch_pause", "halt", "estop"])
def test_stop_during_directory_resolution_rolls_back_new_workspace(
    tmp_path, monkeypatch, lane, expected_status, workspace_kind, brake
):
    """A stop won during mkdir removes only the empty directory just made."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    explicit_path = tmp_path / "new-dir-workspace"
    task_id = kb.create_task(
        conn,
        title=f"{lane} {workspace_kind}",
        assignee="default",
        workspace_kind=workspace_kind,
        workspace_path=str(explicit_path) if workspace_kind == "dir" else None,
    )
    if lane == "review":
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        conn.commit()
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)

    expected_path = (
        explicit_path if workspace_kind == "dir" else kb.workspaces_root() / task_id
    )
    real_resolve = kb.resolve_workspace

    def resolve_then_stop(task, *, board=None, materialization=None):
        workspace = real_resolve(
            task, board=board, materialization=materialization
        )
        assert workspace == expected_path
        assert workspace.is_dir()
        _engage_brake(tmp_path, monkeypatch, brake)
        return workspace

    monkeypatch.setattr(kb, "resolve_workspace", resolve_then_stop)
    spawn_calls = []
    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *args, **kwargs: spawn_calls.append((args, kwargs)),
        max_spawn=1,
    )

    task = kb.get_task(conn, task_id)
    run = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    assert not expected_path.exists()
    assert spawn_calls == []
    assert result.spawned == []
    assert task is not None and task.status == "todo"
    payload = _latest_event_payload(conn, task_id, "dispatch_paused")
    assert payload["resume_status"] == expected_status
    expected_stored_path = str(explicit_path) if workspace_kind == "dir" else None
    assert task.workspace_path == expected_stored_path
    assert task.branch_name is None
    assert task.consecutive_failures == 0
    assert run is not None
    assert (run["status"], run["outcome"]) == ("reclaimed", "reclaimed")
    assert run["ended_at"] is not None
    conn.close()


@pytest.mark.parametrize(
    ("lane", "expected_status"),
    [("ready", "ready"), ("review", "review")],
)
@pytest.mark.parametrize("brake", ["dispatch_pause", "halt", "estop"])
def test_stop_during_worktree_resolution_removes_new_worktree_and_branch(
    tmp_path, monkeypatch, lane, expected_status, brake
):
    """A stop won during git worktree add rolls back its exact new artifacts."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "hermes-home"))
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(
        kb, "read_board_metadata", lambda _board: {"default_workdir": str(repo)}
    )
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(
        conn,
        title=f"{lane} worktree",
        assignee="default",
        workspace_kind="worktree",
    )
    original_workspace_path = kb.get_task(conn, task_id).workspace_path
    if lane == "review":
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        conn.commit()
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)

    expected_path = repo / ".worktrees" / task_id
    branch_name = kb.default_task_branch_name(task_id)
    real_resolve = kb._resolve_worktree_workspace

    def resolve_then_stop(
        task, *, board=None, conn=None, materialization=None
    ):
        outcome = real_resolve(
            task,
            board=board,
            conn=conn,
            materialization=materialization,
        )
        assert outcome == (expected_path, branch_name)
        assert expected_path.is_dir()
        _engage_brake(tmp_path / "hermes-home", monkeypatch, brake)
        return outcome

    monkeypatch.setattr(kb, "_resolve_worktree_workspace", resolve_then_stop)
    spawn_calls = []
    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *args, **kwargs: spawn_calls.append((args, kwargs)),
        max_spawn=1,
    )

    task = kb.get_task(conn, task_id)
    run = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    branch = subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", f"refs/heads/{branch_name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert not expected_path.exists()
    assert f"worktree {expected_path}" not in listed
    assert branch.returncode != 0
    assert spawn_calls == []
    assert result.spawned == []
    assert task is not None and task.status == "todo"
    payload = _latest_event_payload(conn, task_id, "dispatch_paused")
    assert payload["resume_status"] == expected_status
    assert task.workspace_path == original_workspace_path
    assert task.branch_name is None
    assert task.consecutive_failures == 0
    assert run is not None
    assert (run["status"], run["outcome"]) == ("reclaimed", "reclaimed")
    assert run["ended_at"] is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
        "AND kind = 'worktree_base_fallback'",
        (task_id,),
    ).fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize(
    ("lane", "expected_status"),
    [("ready", "ready"), ("review", "review")],
)
def test_halt_from_failing_checkout_hook_rolls_back_partial_worktree(
    tmp_path, monkeypatch, lane, expected_status
):
    """A failing Git hook cannot hide the worktree it created before stopping."""
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(hermes_home))
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(
        "#!/bin/sh\n"
        'mkdir -p "$HERMES_KANBAN_HOME/state"\n'
        'printf "{}\\n" > "$HERMES_KANBAN_HOME/state/halt.json"\n'
        "exit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    monkeypatch.setattr(
        kb, "read_board_metadata", lambda _board: {"default_workdir": str(repo)}
    )
    db_path = hermes_home / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(
        conn,
        title=f"{lane} failing hook",
        assignee="default",
        workspace_kind="worktree",
    )
    original_workspace_path = kb.get_task(conn, task_id).workspace_path
    if lane == "review":
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        conn.commit()
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)

    expected_path = repo / ".worktrees" / task_id
    branch_name = kb.default_task_branch_name(task_id)
    spawn_calls = []
    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *args, **kwargs: spawn_calls.append((args, kwargs)),
        max_spawn=1,
    )

    task = kb.get_task(conn, task_id)
    run = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    branch = subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", f"refs/heads/{branch_name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert (hermes_home / "state" / "halt.json").is_file()
    assert not expected_path.exists()
    assert branch.returncode != 0
    assert spawn_calls == []
    assert result.spawned == []
    assert task is not None and task.status == "todo"
    payload = _latest_event_payload(conn, task_id, "dispatch_paused")
    assert payload["resume_status"] == expected_status
    assert task.workspace_path == original_workspace_path
    assert task.branch_name is None
    assert task.consecutive_failures == 0
    assert run is not None
    assert (run["status"], run["outcome"]) == ("reclaimed", "reclaimed")
    assert run["ended_at"] is not None
    conn.close()


def test_stop_during_resolution_preserves_preexisting_workspace(
    tmp_path, monkeypatch
):
    """Rollback never removes a workspace that existed before this claim."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="existing scratch", assignee="default")
    existing = kb.workspaces_root() / task_id
    existing.mkdir(parents=True)
    marker = existing / "owned-before-dispatch.txt"
    marker.write_text("preserve me", encoding="utf-8")
    real_resolve = kb.resolve_workspace

    def resolve_then_stop(task, *, board=None, materialization=None):
        workspace = real_resolve(
            task, board=board, materialization=materialization
        )
        _halt(tmp_path, monkeypatch)
        return workspace

    monkeypatch.setattr(kb, "resolve_workspace", resolve_then_stop)

    result = kb.dispatch_once(conn, spawn_fn=lambda *_args: 4242, max_spawn=1)

    task = kb.get_task(conn, task_id)
    assert result.spawned == []
    assert task is not None and task.status == "todo"
    payload = _latest_event_payload(conn, task_id, "dispatch_paused")
    assert payload["resume_status"] == "ready"
    assert task.workspace_path is None
    assert marker.read_text(encoding="utf-8") == "preserve me"
    conn.close()


def test_estop_arriving_during_default_spawn_setup_blocks_popen(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="ready", assignee="default")
    task = kb.get_task(conn, task_id)
    estop_path = tmp_path / "ESTOP"
    popen_calls = []
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True)
    log_path = log_dir / f"{task_id}.log"
    backup_path = log_dir / f"{task_id}.log.1"
    log_path.write_bytes(b"current worker log\n")
    backup_path.write_bytes(b"previous backup\n")

    def engage_during_setup(*_args, **_kwargs):
        estop_path.write_text("{}\n", encoding="utf-8")
        return None

    def forbidden_popen(*_args, **_kwargs):
        popen_calls.append(True)
        return type("Proc", (), {"pid": 4242})()

    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", engage_during_setup)
    monkeypatch.setattr(
        kb, "_retag_legacy_worker_sessions", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(
        kb, "worker_log_rotation_config", lambda *_args, **_kwargs: (1, 1)
    )
    monkeypatch.setattr(kb.subprocess, "Popen", forbidden_popen)

    with pytest.raises(kb.DispatchPausedError, match="global emergency stop"):
        kb._default_spawn(task, str(tmp_path))

    assert estop_path.is_file()
    assert popen_calls == []
    assert log_path.read_bytes() == b"current worker log\n"
    assert backup_path.read_bytes() == b"previous backup\n"


@pytest.mark.parametrize(
    ("lane", "expected_status"),
    [("ready", "ready"), ("review", "review")],
)
def test_estop_arriving_at_injected_spawn_guard_releases_claim(
    tmp_path, monkeypatch, lane, expected_status
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title=lane, assignee="default")
    if lane == "review":
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        conn.commit()
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)

    estop_path = tmp_path / "ESTOP"
    spawn_calls = []

    def injected_spawn(*_args, **_kwargs):
        spawn_calls.append(True)
        return 4242

    real_signature = inspect.signature

    def engage_before_final_guard(callable_obj):
        signature = real_signature(callable_obj)
        if callable_obj is injected_spawn:
            estop_path.write_text("{}\n", encoding="utf-8")
        return signature

    monkeypatch.setattr(inspect, "signature", engage_before_final_guard)

    result = kb.dispatch_once(conn, spawn_fn=injected_spawn, max_spawn=1)

    task = kb.get_task(conn, task_id)
    run = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    assert estop_path.is_file()
    assert spawn_calls == []
    assert result.spawned == []
    assert task is not None and task.status == "todo"
    payload = _latest_event_payload(conn, task_id, "dispatch_paused")
    assert payload["resume_status"] == expected_status
    assert task.consecutive_failures == 0
    assert run is not None
    assert (run["status"], run["outcome"]) == ("reclaimed", "reclaimed")
    assert run["ended_at"] is not None


@pytest.mark.linux_only
def test_dispatch_boundary_probe_emits_exact_contract_without_live_writes(
    tmp_path, monkeypatch, capsys
):
    live_root = tmp_path / "live-root"
    live_halt = live_root / "state" / "halt.json"
    live_halt.parent.mkdir(parents=True)
    live_halt.write_bytes(b"live halt must stay untouched\n")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(live_root))

    assert boundary_probe.run_probe() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload == {
        "schema_version": 1,
        "contract": "hermes-kanban-dispatch-boundary",
        "state": "verified",
        "probe_scope": "temporary_shared_root",
        "shared_halt_path": "state/halt.json",
        "live_writes_performed": False,
        "checks": {
            "absent_brakes_allow": True,
            "dispatch_pause_regular_blocks": True,
            "dispatch_pause_broken_symlink_blocks": True,
            "halt_regular_blocks": True,
            "halt_broken_symlink_blocks": True,
            "profile_shared_root_halt_blocks": True,
            "lookup_errors_fail_closed": True,
            "halt_blocks_final_spawn_edge": True,
            "estop_blocks_final_spawn_edge": True,
            "halt_blocks_gateway_auto_decompose_edge": True,
        },
    }
    assert live_halt.read_bytes() == b"live halt must stay untouched\n"
    assert not (live_root / "kanban.db").exists()


def test_dispatch_boundary_probe_fails_closed_when_symlinks_cannot_be_created(
    monkeypatch, capsys
):
    def symlink_unavailable(*_args, **_kwargs):
        raise PermissionError("symlink creation unavailable")

    monkeypatch.setattr(kb.Path, "symlink_to", symlink_unavailable)

    assert boundary_probe.run_probe() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload["state"] == "failed"
    assert payload["checks"]["dispatch_pause_broken_symlink_blocks"] is False
    assert payload["checks"]["halt_broken_symlink_blocks"] is False


def test_dispatch_boundary_probe_accepts_windows_symlink_privilege_limit(
    monkeypatch, capsys
):
    class WindowsSymlinkPrivilegeError(PermissionError):
        winerror = 1314

    def symlink_privilege_unavailable(*_args, **_kwargs):
        raise WindowsSymlinkPrivilegeError("symlink privilege unavailable")

    monkeypatch.setattr(kb.Path, "symlink_to", symlink_privilege_unavailable)

    assert boundary_probe.run_probe() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload["state"] == "verified"
    assert payload["checks"]["dispatch_pause_broken_symlink_blocks"] is True
    assert payload["checks"]["halt_broken_symlink_blocks"] is True
    assert payload["checks"]["lookup_errors_fail_closed"] is True


def test_dispatch_boundary_probe_returns_nonzero_when_any_check_fails(
    monkeypatch, capsys
):
    payload = {
        "schema_version": 1,
        "contract": "hermes-kanban-dispatch-boundary",
        "state": "failed",
        "probe_scope": "temporary_shared_root",
        "shared_halt_path": "state/halt.json",
        "live_writes_performed": False,
        "checks": {name: True for name in kb.DISPATCH_BOUNDARY_CHECKS},
    }
    payload["checks"]["halt_regular_blocks"] = False
    monkeypatch.setattr(kb, "dispatch_boundary_self_test", lambda: payload)

    assert boundary_probe.run_probe() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == payload


def test_dispatch_boundary_probe_exception_still_emits_strict_failure_json(
    monkeypatch, capsys
):
    def failed_probe():
        raise RuntimeError("self-test crashed")

    monkeypatch.setattr(kb, "dispatch_boundary_self_test", failed_probe)

    assert boundary_probe.run_probe() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["contract"] == "hermes-kanban-dispatch-boundary"
    assert payload["state"] == "failed"
    assert payload["probe_scope"] == "temporary_shared_root"
    assert payload["shared_halt_path"] == "state/halt.json"
    assert payload["live_writes_performed"] is False
    assert set(payload["checks"]) == set(kb.DISPATCH_BOUNDARY_CHECKS)
    assert all(value is False for value in payload["checks"].values())


def test_pause_arriving_after_ready_claim_parks_without_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="ready", assignee="default")

    def pause_at_spawn(*_args, **_kwargs):
        _pause(tmp_path, monkeypatch)
        raise kb.DispatchPausedError("paused at final edge")

    result = kb.dispatch_once(conn, spawn_fn=pause_at_spawn, max_spawn=1)
    task = kb.get_task(conn, task_id)
    assert result.spawned == []
    assert task.status == "todo"
    assert task.consecutive_failures == 0
    payload = _latest_event_payload(conn, task_id, "dispatch_paused")
    assert payload["resume_status"] == "ready"
    run = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    assert (run["status"], run["outcome"]) == ("reclaimed", "reclaimed")
    assert run["ended_at"] is not None


def test_pause_arriving_after_review_claim_parks_for_review(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="review", assignee="default")
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
    conn.commit()
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)

    def pause_at_spawn(*_args, **_kwargs):
        _pause(tmp_path, monkeypatch)
        raise kb.DispatchPausedError("paused at final edge")

    result = kb.dispatch_once(conn, spawn_fn=pause_at_spawn, max_spawn=1)
    task = kb.get_task(conn, task_id)
    assert result.spawned == []
    assert task.status == "todo"
    assert task.consecutive_failures == 0
    payload = _latest_event_payload(conn, task_id, "dispatch_paused")
    assert payload["resume_status"] == "review"
