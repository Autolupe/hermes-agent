import builtins
import contextlib
import inspect
import json
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


@pytest.mark.linux_only
def test_broken_estop_entry_blocks_dispatch_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    (tmp_path / "ESTOP").symlink_to(tmp_path / "missing-estop-target")

    with pytest.raises(kb.DispatchPausedError, match="emergency stop"):
        kb._raise_if_dispatch_paused()


def test_estop_lstat_error_blocks_dispatch_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    target = tmp_path / "ESTOP"
    real_lstat = estop.os.lstat

    def denied_lstat(path, *args, **kwargs):
        if path == target:
            raise PermissionError("ESTOP lookup denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(estop.os, "lstat", denied_lstat)

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
def test_dispatch_brake_lstat_error_fails_closed(tmp_path, monkeypatch, brake_name):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    target = tmp_path / "state" / brake_name
    real_lstat = kb.os.lstat

    def unreadable_entry(path):
        if path == target:
            raise PermissionError("dispatch state unavailable")
        return real_lstat(path)

    monkeypatch.setattr(kb.os, "lstat", unreadable_entry)

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
    first_lookup = True

    def brake_appears_during_lookup(path):
        nonlocal first_lookup
        if path == target and first_lookup:
            first_lookup = False
            target.mkdir()
            raise FileNotFoundError("raced with brake creation")
        return real_lstat(path)

    monkeypatch.setattr(kb.os, "lstat", brake_appears_during_lookup)

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

    def brake_appears_during_final_snapshot(path):
        nonlocal state_lookups
        if path == state:
            state_lookups += 1
            if state_lookups == 4:
                target.write_text("{}\n", encoding="utf-8")
        return real_lstat(path)

    monkeypatch.setattr(
        kb.os, "lstat", brake_appears_during_final_snapshot
    )

    assert kb.dispatch_is_paused() is True
    assert target.is_file()


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


def test_halt_arriving_after_claim_requeues_without_failure(tmp_path, monkeypatch):
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
    assert task.status == "ready"
    assert task.consecutive_failures == 0
    run = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    assert (run["status"], run["outcome"]) == ("reclaimed", "reclaimed")
    assert run["ended_at"] is not None


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
        kb, "worker_log_rotation_config", lambda *_args, **_kwargs: (2097152, 1)
    )
    monkeypatch.setattr(kb.subprocess, "Popen", forbidden_popen)

    with pytest.raises(kb.DispatchPausedError, match="global emergency stop"):
        kb._default_spawn(task, str(tmp_path))

    assert estop_path.is_file()
    assert popen_calls == []


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
    assert task is not None and task.status == expected_status
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


def test_pause_arriving_after_ready_claim_requeues_without_failure(tmp_path, monkeypatch):
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
    assert task.status == "ready"
    assert task.consecutive_failures == 0
    run = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    assert (run["status"], run["outcome"]) == ("reclaimed", "reclaimed")
    assert run["ended_at"] is not None


def test_pause_arriving_after_review_claim_returns_to_review(tmp_path, monkeypatch):
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
    assert task.status == "review"
    assert task.consecutive_failures == 0
