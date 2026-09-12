"""Real held-child mechanics; these fixtures do not enable protected dispatch."""

import contextlib
import ctypes
import json
import os
from pathlib import Path
import select
import signal
import socket
import sys
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_held_worker as held
from hermes_cli import kanban_policy as policy
from hermes_cli import kanban_worker_supervisor as protocol


pytestmark = pytest.mark.linux_only


class FixtureProvider(policy.RequiredKanbanPolicy):
    """Allow preparation only in this test process; launch gates stay closed."""

    def __init__(self):
        self.hook = lambda request, boundary, observation: None
        self.cancelled = []
        self.closed = []

    def open_workspace_request(self, request):
        provider = self

        class Admission(policy.RequiredWorkspaceAdmission):
            def checkpoint(self, boundary, observation):
                return provider.hook(request, boundary, observation)

            def cancel(self, reason):
                assert request.cancelled
                provider.cancelled.append(request.claim.request_id)

            def close(self):
                provider.closed.append(request.claim.request_id)

        return Admission()


@pytest.fixture
def native(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    monkeypatch.setattr(kb, "_fire_worker_spawned_hook", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", lambda *a, **kw: None)
    registration = SimpleNamespace(
        uid=os.getuid(), generation="held-fixture", provider=FixtureProvider(),
        check_integrity=lambda: None,
    )
    monkeypatch.setattr(policy, "select_required_policy", lambda: registration)
    workspace = tmp_path / "workspace"
    # The held-worker contract rejects group-writable paths, independent of
    # the developer's shell umask (which may be 0002).
    workspace.mkdir(mode=0o700)
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    with contextlib.closing(kb.connect(db_path=tmp_path / "board.db")) as conn:
        yield SimpleNamespace(conn=conn, workspace=workspace, journal=journal,
                              registration=registration)


def claim(native, lane="ready"):
    task_id = kb.create_task(
        native.conn, title="held fixture", assignee="default", workspace_kind="dir",
        workspace_path=str(native.workspace),
    )
    if lane == "review":
        native.conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        native.conn.commit()
    task = (kb.claim_task if lane == "ready" else kb.claim_review_task)(native.conn, task_id)
    assert task is not None
    return task


def request_for(native, task, lane="ready"):
    return kb.required_workspace_request(
        native.conn, task_id=task.id, expected_run_id=task.current_run_id,
        expected_claim_lock=task.claim_lock, lane=lane,
    )


@contextlib.contextmanager
def request_refusal(*, expected_cause=()):
    # The native scope wraps arbitrary exceptions as RequiredPolicyError. A
    # failed fixture assertion, unavailable test API or other unexpected error
    # must NOT masquerade as refusal. Only explicitly requested causes qualify.
    with pytest.raises(policy.RequiredPolicyError) as caught:
        yield caught
    error = caught.value
    while error is not None:
        if not isinstance(error, (policy.RequiredPolicyError, *expected_cause)):
            raise error
        error = error.__cause__


def worker_for(native, request, script, *args, timeout=8):
    return held.HeldWorker(
        request, [sys.executable, "-I", "-S", "-B", "-c", script, *map(str, args)],
        workspace=native.workspace, journal_root=native.journal, timeout=timeout,
    )


def wait_for_file(path, timeout=5):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Harmless fixture child did not create {path.name}")
        time.sleep(0.01)
    return path.read_text()


def rows(native, task):
    return (
        dict(native.conn.execute("SELECT * FROM tasks WHERE id = ?", (task.id,)).fetchone()),
        dict(native.conn.execute("SELECT * FROM task_runs WHERE id = ?", (task.current_run_id,)).fetchone()),
    )


def assert_pending(native, task):
    assert kb._controlled_worker_pending(native.conn, task.id)
    task_row, run_row = rows(native, task)
    assert task_row["status"] == run_row["status"] == "running"
    assert run_row["ended_at"] is None
    assert run_row["outcome"] is None


def assert_gone(*pids):
    # Only inspect children this fixture created, after their owner reported drain.
    for pid in pids:
        assert not Path(f"/proc/{pid}").exists(), f"Fixture child {pid} was not reaped"


@contextlib.contextmanager
def fixture_process_handle(pid):
    # This native Python build omits the os/signal wrappers, but its libc
    # exposes the documented pidfd APIs. These are fixture-only functions,
    # not raw syscall numbers or any new production override.
    libc = ctypes.CDLL(None, use_errno=True)
    open_handle = libc.pidfd_open
    open_handle.argtypes = [ctypes.c_int, ctypes.c_uint]
    open_handle.restype = ctypes.c_int
    send_signal = libc.pidfd_send_signal
    send_signal.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    send_signal.restype = ctypes.c_int
    descriptor = open_handle(pid, 0)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "Fixture could not pin its own child.")
    try:
        def kill_original():
            if send_signal(descriptor, signal.SIGKILL, None, 0) != 0:
                raise OSError(ctypes.get_errno(), "Fixture could not stop its pinned child.")
        yield descriptor, kill_original
    finally:
        os.close(descriptor)


MARKER = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('started')"
BLOCKING_MARKER = """
from pathlib import Path
import sys
Path(sys.argv[1]).write_text('started')
with open(sys.argv[2], 'rb', buffering=0) as stream:
    stream.read(1)
"""


@contextlib.contextmanager
def blocking_gate(native):
    path = native.workspace / "gate"
    os.mkfifo(path, 0o600)
    descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        yield path, descriptor
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_real_claim_records_held_identity_before_release_and_never_completes(native, lane):
    task = claim(native, lane)
    marker = native.workspace / "started"
    with request_for(native, task, lane) as request:
        worker = worker_for(native, request, MARKER, marker).prepare()
        assert worker.request is request
        assert request.claim.lane == lane
        assert not marker.exists()
        task_row, run_row = rows(native, task)
        assert task_row["worker_pid"] == run_row["worker_pid"] == worker.process.pid
        assert worker.child["pid"] != worker.process.pid
        assert worker.child["parent"] == worker.process.pid
        assert worker.child == protocol.process_identity(worker.child["pid"])
        record = json.loads((worker.record_path / "record.json").read_text())
        assert record["child"] == worker.child
        assert record["supervisor"] == worker.supervisor
        assert record["run_id"] == task.current_run_id
        assert record["claim_lock"] == task.claim_lock
        assert record["request_id"] == request.claim.request_id
        recorded = native.conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
            "AND kind = 'controlled_worker_recorded'", (task.id, task.current_run_id),
        ).fetchone()
        assert json.loads(recorded["payload"])["child"] == worker.child
        with pytest.raises(held.HeldWorkerError):
            worker.clear_drained_bookkeeping()
        request.checkpoint("fixture_still_held")
        assert not marker.exists()
        worker.release()
        assert wait_for_file(marker) == "started"
        outcome = worker.wait()
        assert outcome == {"state": "exited", "drained": True, "exit_code": 0}
        with pytest.raises(TypeError):
            outcome["drained"] = False
        with pytest.raises(AttributeError):
            worker.outcome = {"state": "exited", "drained": True, "exit_code": 0}
        assert_gone(worker.process.pid, worker.child["pid"])
        assert_pending(native, task)
        before = rows(native, task)
        assert kb.complete_task(native.conn, task.id, result="process exited zero") is False
        assert rows(native, task) == before
        with pytest.raises(held.HeldWorkerError):
            worker.release()
        worker.clear_drained_bookkeeping()
        assert not kb._controlled_worker_pending(native.conn, task.id)
        task_row, run_row = rows(native, task)
        assert task_row["worker_pid"] is run_row["worker_pid"] is None
        assert task_row["status"] == run_row["status"] == "running"
        assert run_row["outcome"] is run_row["ended_at"] is None
        with pytest.raises(held.HeldWorkerError):
            worker.clear_drained_bookkeeping()


def test_cancel_held_never_executes_and_same_request_cannot_reuse_child(native):
    task = claim(native)
    marker = native.workspace / "never-started"
    with request_refusal():
        with request_for(native, task) as request:
            worker = worker_for(native, request, MARKER, marker).prepare()
            child_pid = worker.child["pid"]
            worker.cancel()
            worker.close()
            assert request.cancelled
            assert worker.outcome["drained"] is True
            assert not marker.exists()
            assert_gone(child_pid, worker.process.pid)
            with pytest.raises(held.HeldWorkerError):
                worker.release()
            with pytest.raises(held.HeldWorkerError):
                worker.prepare()
            with request_refusal():
                worker_for(native, request, MARKER, marker).prepare()
    assert_pending(native, task)


def test_deadline_cancels_real_running_child_and_retains_pending_bookkeeping(native):
    task = claim(native)
    marker = native.workspace / "started"
    with blocking_gate(native) as (gate, _descriptor):
        with request_refusal():
            with request_for(native, task) as request:
                worker = worker_for(native, request, BLOCKING_MARKER, marker, gate, timeout=2).prepare()
                worker.release()
                assert wait_for_file(marker) == "started"
                with pytest.raises(TimeoutError):
                    worker.wait()
        assert worker.cancelled
        assert worker.outcome["drained"] is True
        assert_gone(worker.child["pid"], worker.process.pid)
        assert_pending(native, task)


def test_subreaper_drains_double_fork_after_worker_leader_exits(native):
    task = claim(native)
    marker = native.workspace / "descendant.json"
    script = """
import json, os, sys
from pathlib import Path
ready_read, ready_write = os.pipe()
intermediate = os.fork()
if intermediate:
    os.close(ready_write)
    assert os.read(ready_read, 1) == b'R'
    os._exit(0)
os.close(ready_read)
os.setsid()
intermediate = os.getpid()
if os.fork():
    os._exit(0)
Path(sys.argv[1]).write_text(json.dumps({
    'descendant': os.getpid(), 'intermediate': intermediate, 'session': os.getsid(0)
}))
os.write(ready_write, b'R')
os.close(ready_write)
with open(sys.argv[2], 'rb', buffering=0) as stream:
    stream.read(1)
"""
    with blocking_gate(native) as (gate, _descriptor):
        with request_for(native, task) as request:
            worker = worker_for(native, request, script, marker, gate).prepare()
            worker.release()
            descendants = json.loads(wait_for_file(marker))
            assert descendants["session"] == descendants["intermediate"]
            assert descendants["session"] != worker.process.pid
            assert worker.wait() == {"state": "exited", "drained": True, "exit_code": 0}
            assert_gone(worker.process.pid, worker.child["pid"],
                        descendants["intermediate"], descendants["descendant"])
            assert_pending(native, task)
            worker.clear_drained_bookkeeping()


def test_lost_release_ack_keeps_execution_uncertain_and_cannot_clear_fence(native):
    task = claim(native)
    marker = native.workspace / "possibly-started"
    with request_refusal():
        with request_for(native, task) as request:
            worker = worker_for(native, request, MARKER, marker).prepare()
            # A real half-closed socket loses the acknowledgement; no protocol fake.
            worker.channel.shutdown(socket.SHUT_RD)
            with pytest.raises((EOFError, OSError, held.HeldWorkerError)):
                worker.release()
            worker.close()
            worker.process.wait(timeout=7)
            assert worker.outcome is None
            with pytest.raises(AttributeError):
                worker.outcome = {"state": "exited", "drained": True, "exit_code": 0}
            with pytest.raises(held.HeldWorkerError):
                worker.clear_drained_bookkeeping()
    assert_pending(native, task)
    assert json.loads((worker.record_path / "record.json").read_text())["state"] == "uncertain"
    assert_gone(worker.child["pid"], worker.process.pid)


def test_control_channel_loss_drains_real_child_but_preserves_unknown_result(native):
    task = claim(native)
    marker = native.workspace / "started"
    with blocking_gate(native) as (gate, _descriptor):
        with request_refusal():
            with request_for(native, task) as request:
                worker = worker_for(native, request, BLOCKING_MARKER, marker, gate).prepare()
                worker.release()
                assert wait_for_file(marker) == "started"
                worker.channel.close()
                worker.close()
                worker.process.wait(timeout=7)
                assert worker.outcome is None
                with pytest.raises(held.HeldWorkerError):
                    worker.clear_drained_bookkeeping()
        assert_pending(native, task)
        assert_gone(worker.child["pid"], worker.process.pid)


@pytest.mark.parametrize("field", ["start", "boot"])
def test_changed_held_identity_cannot_release_and_retains_pending_bookkeeping(native, field):
    task = claim(native)
    marker = native.workspace / "never-started"
    with request_refusal():
        with request_for(native, task) as request:
            worker = worker_for(native, request, MARKER, marker).prepare()
            original_child = dict(worker.child)
            worker.child = {**worker.child, field: (
                worker.child[field] + 1 if field == "start" else "different-boot"
            )}
            with pytest.raises(held.HeldWorkerError, match="identity"):
                worker.release()
            worker.close()
            assert not marker.exists()
            assert_gone(original_child["pid"], worker.process.pid)
    assert_pending(native, task)


@pytest.mark.parametrize("change", ["replacement_claim", "lease_expiry"])
def test_changed_claim_or_expired_lease_cancels_held_release(native, change):
    task = claim(native)
    marker = native.workspace / "never-started"
    with request_refusal():
        with request_for(native, task) as request:
            worker = worker_for(native, request, MARKER, marker).prepare()
            field, value = ("claim_lock", "replacement-claim") if change == "replacement_claim" else (
                "claim_expires", int(time.time()) - 1
            )
            with kb.write_txn(native.conn):
                native.conn.execute(f"UPDATE tasks SET {field} = ? WHERE id = ?", (value, task.id))
                native.conn.execute(f"UPDATE task_runs SET {field} = ? WHERE id = ?",
                                    (value, task.current_run_id))
            with request_refusal():
                worker.release()
            worker.close()
            assert not marker.exists()
            assert_gone(worker.child["pid"], worker.process.pid)
            before = rows(native, task)
            if change == "replacement_claim":
                with pytest.raises(held.HeldWorkerError):
                    worker.clear_drained_bookkeeping()
                assert rows(native, task) == before
    assert_pending(native, task)


@pytest.mark.parametrize("pid_kind", ["missing", "dead", "reused"])
def test_ordinary_reclaimers_preserve_pending_fence_without_signaling_stale_pid(native, pid_kind):
    task = claim(native)
    marker = native.workspace / "never-started"
    with request_refusal():
        with request_for(native, task) as request:
            worker = worker_for(native, request, MARKER, marker).prepare()
            worker.cancel()
            worker.close()
    assert_gone(worker.process.pid, worker.child["pid"])
    # The owner is already drained. Simulate stale/overwritten PID bookkeeping;
    # no positive cleanup evidence is supplied to any ordinary reclaimer.
    pid = {"missing": None, "dead": worker.process.pid, "reused": os.getpid()}[pid_kind]
    with kb.write_txn(native.conn):
        native.conn.execute("UPDATE tasks SET worker_pid = ?, claim_expires = 1, "
                            "max_runtime_seconds = 1, started_at = 1 WHERE id = ?",
                            (pid, task.id))
        native.conn.execute("UPDATE task_runs SET worker_pid = ?, claim_expires = 1, "
                            "started_at = 1 WHERE id = ?",
                            (pid, task.current_run_id))
    before = rows(native, task)
    def must_not_signal(*args, **kwargs):
        raise AssertionError("A stored PID cannot authorize controlled-worker cleanup")

    assert kb.release_stale_claims(native.conn, signal_fn=must_not_signal) == 0
    assert kb.detect_stale_running(native.conn, stale_timeout_seconds=1,
                                   signal_fn=must_not_signal) == []
    assert task.id not in kb.detect_crashed_workers(native.conn)
    assert task.id not in kb.enforce_max_runtime(native.conn, signal_fn=must_not_signal)
    assert kb.complete_task(native.conn, task.id, result="unproven cleanup") is False
    assert kb._restore_required_workspace(native.conn, request) is False
    kb._release_required_claim(native.conn, task.id, task.current_run_id, task.claim_lock)
    assert rows(native, task) == before
    assert kb.reclaim_task(native.conn, task.id, signal_fn=must_not_signal) is False
    assert rows(native, task) == before
    assert_pending(native, task)
    # The fence also blocks a fresh claim even if another writer presents ready.
    native.conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task.id,))
    native.conn.commit()
    assert kb.claim_task(native.conn, task.id) is None
    native.conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task.id,))
    native.conn.commit()
    assert kb.claim_review_task(native.conn, task.id) is None
    assert kb._controlled_worker_pending(native.conn, task.id)


@pytest.mark.parametrize("drift", ["generation", "integrity"])
def test_late_enrollment_drift_cancels_before_held_child_exec(native, drift):
    task = claim(native)
    marker = native.workspace / "never-started"
    with request_refusal():
        with request_for(native, task) as request:
            worker = worker_for(native, request, MARKER, marker).prepare()
            if drift == "generation":
                native.registration.generation = "replacement-generation"
            else:
                def integrity_changed():
                    raise policy.RequiredPolicyError("Fixture enrollment integrity changed")

                native.registration.check_integrity = integrity_changed
            with request_refusal():
                worker.release()
            worker.close()
            assert request.cancelled
            assert not marker.exists()
            assert_gone(worker.child["pid"], worker.process.pid)
    assert_pending(native, task)


def test_original_child_death_while_held_refuses_release(native):
    task = claim(native)
    marker = native.workspace / "never-started"
    with request_refusal():
        with request_for(native, task) as request:
            worker = worker_for(native, request, MARKER, marker).prepare()
            # Pin this fixture's original child before signaling. A pidfd is a
            # kernel handle which cannot switch to a recycled process number.
            with fixture_process_handle(worker.child["pid"]) as (descriptor, kill_original):
                assert protocol.process_identity(worker.child["pid"]) == worker.child
                kill_original()
                assert select.select([descriptor], [], [], 3)[0]
                with pytest.raises((held.HeldWorkerError, OSError)):
                    worker.release()
            worker.close()
            assert not marker.exists()
            assert_gone(worker.child["pid"], worker.process.pid)
    assert_pending(native, task)


def test_commit_edge_refusal_keeps_fence_when_child_pid_transaction_rolls_back(native):
    task = claim(native)
    marker = native.workspace / "never-started"
    observed = []
    with request_refusal():
        with request_for(native, task) as request:
            worker = worker_for(native, request, MARKER, marker)

            def refuse_commit(original, boundary, observation):
                if boundary == "before_held_commit":
                    assert original is request
                    assert worker.child == protocol.process_identity(worker.child["pid"])
                    assert rows(native, task)[0]["worker_pid"] == worker.process.pid
                    assert not marker.exists()
                    observed.append(worker.child["pid"])
                    raise policy.RequiredPolicyError("Fixture refuses held PID commit")

            native.registration.provider.hook = refuse_commit
            worker.prepare()
    assert observed == [worker.child["pid"]]
    assert_gone(worker.child["pid"], worker.process.pid)
    task_row, run_row = rows(native, task)
    assert task_row["worker_pid"] is run_row["worker_pid"] is None
    assert_pending(native, task)
    assert not marker.exists()
    assert native.workspace.is_dir()
    assert native.conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'controlled_worker_recorded'",
        (task.id,),
    ).fetchone() is None


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_ordinary_task_without_controlled_fence_still_reclaims_expired_claim(native, lane):
    task = claim(native, lane)
    assert not kb._controlled_worker_pending(native.conn, task.id)
    with kb.write_txn(native.conn):
        native.conn.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (task.id,))
        native.conn.execute("UPDATE task_runs SET claim_expires = 1 WHERE id = ?",
                            (task.current_run_id,))
    assert kb.release_stale_claims(native.conn) == 1
    task_row, run_row = rows(native, task)
    assert task_row["status"] == lane
    assert task_row["worker_pid"] is None
    assert task_row["claim_lock"] is None
    assert run_row["outcome"] == "reclaimed"
    assert run_row["ended_at"] is not None


@pytest.mark.parametrize("primary_failure", [True, False])
def test_journal_close_failure_attempts_both_owners_and_preserves_primary(native, primary_failure):
    task = claim(native)
    marker = native.workspace / "never-started"
    with request_refusal(expected_cause=(ValueError, OSError)) as caught:
        with request_for(native, task) as request:
            worker = worker_for(native, request, MARKER, marker).prepare()
            # Real exclusive-create failure, not a fake positive cleanup result.
            (worker.record_path / "record.next").write_bytes(b"interrupted journal update")
            if primary_failure:
                raise ValueError("original body failure")
    if primary_failure:
        assert isinstance(caught.value.__cause__, ValueError)
        assert str(caught.value.__cause__) == "original body failure"
    else:
        assert isinstance(caught.value.__cause__, FileExistsError)
        assert "worker cleanup failed" in str(caught.value)
    assert request.cancelled
    assert_gone(worker.child["pid"], worker.process.pid)
    assert not marker.exists()
    assert_pending(native, task)
    assert native.registration.provider.closed == [request.claim.request_id]


def test_held_cancel_exception_still_attempts_admission_cancel(native, monkeypatch):
    task = claim(native)
    marker = native.workspace / "never-started"
    with request_refusal():
        with request_for(native, task) as request:
            worker = worker_for(native, request, MARKER, marker).prepare()
            escaped = None
            try:
                with monkeypatch.context() as patcher:
                    def failed_cancel(_reason):
                        raise ValueError("held cancellation cleanup failed")
                    patcher.setattr(worker, "cancel", failed_cancel)
                    try:
                        request.cancel("fixture cancellation")
                    except ValueError as error:
                        escaped = error
            finally:
                # The real owner drains its real child after the fault seam is
                # removed, even when the assertion below reproduces a failure.
                worker.close()
            assert request.cancelled
            assert_gone(worker.child["pid"], worker.process.pid)
            assert not marker.exists()
            assert native.registration.provider.cancelled == [request.claim.request_id]
            assert escaped is None
    assert_pending(native, task)


@pytest.mark.parametrize("error_type", [AssertionError, AttributeError])
def test_refusal_fixture_never_accepts_wrapped_assertions_or_missing_apis(native, error_type):
    task = claim(native)
    with pytest.raises(error_type, match="fixture failure"):
        with request_refusal():
            with request_for(native, task):
                raise error_type("fixture failure")


def test_manual_reclaim_rechecks_fence_after_reading_new_worker_pid(native, monkeypatch):
    task = claim(native)
    marker = native.workspace / "never-started"
    original_pending = kb._controlled_worker_pending
    published = False
    with request_refusal():
        with request_for(native, task) as request:
            worker = worker_for(native, request, MARKER, marker)

            def publish_between_guard_and_pid_read(conn, task_id):
                nonlocal published
                observed = original_pending(conn, task_id)
                if not published:
                    published = True
                    # A real negative fence and real held child become visible
                    # after the first guard observed no worker. No fake PID or
                    # cleanup success is supplied to the reclaim path.
                    worker.prepare()
                return observed

            def must_not_signal(*args):
                raise AssertionError("A newly recorded controlled worker cannot use legacy signaling")

            monkeypatch.setattr(kb, "_controlled_worker_pending", publish_between_guard_and_pid_read)
            try:
                assert kb.reclaim_task(native.conn, task.id, signal_fn=must_not_signal) is False
            finally:
                worker.cancel()
                worker.close()
    assert published
    assert not marker.exists()
    assert_pending(native, task)


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("recovery", ["stale", "orphan"])
def test_stale_and_orphan_passes_preserve_held_task_and_continue_ordinary_task(native, lane, recovery):
    task = claim(native, lane)
    marker = native.workspace / "never-started"
    with request_refusal():
        with request_for(native, task, lane) as request:
            worker = worker_for(native, request, MARKER, marker).prepare()
            worker.cancel()
            worker.close()
    assert_gone(worker.process.pid, worker.child["pid"])
    ordinary = claim(native, lane)
    with kb.write_txn(native.conn):
        for current in (task, ordinary):
            native.conn.execute(
                "UPDATE tasks SET worker_pid = NULL, claim_expires = NULL, "
                "started_at = 1, last_heartbeat_at = NULL WHERE id = ?", (current.id,),
            )
            native.conn.execute(
                "UPDATE task_runs SET worker_pid = NULL, claim_expires = NULL, "
                "started_at = 1 WHERE id = ?", (current.current_run_id,),
            )
    before = rows(native, task)
    if recovery == "stale":
        result = kb.detect_stale_running(native.conn, stale_timeout_seconds=1)
    else:
        result = kb.reconcile_orphaned_running(native.conn)
    assert result == [ordinary.id]
    assert rows(native, task) == before
    assert_pending(native, task)
    task_row, run_row = rows(native, ordinary)
    assert task_row["status"] == lane
    assert task_row["worker_pid"] is task_row["claim_lock"] is None
    assert run_row["outcome"] == ("stale" if recovery == "stale" else "reclaimed")
    assert run_row["ended_at"] is not None
    assert not marker.exists()


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_held_pid_write_interrupt_rolls_back_original_transaction(native, lane, error_type):
    task = claim(native, lane)
    marker = native.workspace / "never-started"
    primary = error_type("fixture interrupts held PID commit")
    observed = []
    with pytest.raises(error_type) as caught:
        with request_for(native, task, lane) as request:
            worker = worker_for(native, request, MARKER, marker)

            def interrupt_commit(original, boundary, observation):
                if boundary == "before_held_commit":
                    assert original is request
                    assert native.conn.in_transaction
                    assert rows(native, task)[0]["worker_pid"] == worker.process.pid
                    observed.append(worker.child["pid"])
                    raise primary

            native.registration.provider.hook = interrupt_commit
            worker.prepare()
    assert caught.value is primary
    assert observed == [worker.child["pid"]]
    assert request.cancelled
    assert not native.conn.in_transaction
    assert_gone(worker.child["pid"], worker.process.pid)
    task_row, run_row = rows(native, task)
    assert task_row["worker_pid"] is run_row["worker_pid"] is None
    assert_pending(native, task)
    assert not marker.exists()
    assert native.workspace.is_dir()
    assert native.conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'controlled_worker_recorded'",
        (task.id,),
    ).fetchone() is None


def test_preparation_cleanup_journal_failure_preserves_original_refusal(native):
    task = claim(native)
    marker = native.workspace / "never-started"
    primary = policy.RequiredPolicyError("primary held failure")
    with pytest.raises(policy.RequiredPolicyError) as caught:
        with request_for(native, task) as request:
            worker = worker_for(native, request, MARKER, marker)

            def refuse_commit(original, boundary, observation):
                if boundary == "before_held_commit":
                    (worker.record_path / "record.next").write_bytes(b"interrupted journal update")
                    raise primary

            native.registration.provider.hook = refuse_commit
            worker.prepare()
    assert caught.value is primary
    assert request.cancelled
    assert not native.conn.in_transaction
    assert_gone(worker.child["pid"], worker.process.pid)
    assert native.registration.provider.closed == [request.claim.request_id]
    assert_pending(native, task)
    assert not marker.exists()


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("boundary", ["begin", "body", "commit", "nested_begin", "nested_body", "nested_release"])
def test_ordinary_write_transaction_interrupt_rolls_back_without_replaying(native, monkeypatch, error_type, boundary):
    task = claim(native)
    primary = error_type("fixture transaction interrupt")
    original_execute = native.conn.execute
    writes = 0
    injected = False
    statements = []

    def execute(sql, *args, **kwargs):
        nonlocal injected
        statements.append(sql)
        if not injected and ((boundary == "begin" and sql == "BEGIN IMMEDIATE") or
                             (boundary == "nested_begin" and sql.startswith("SAVEPOINT "))):
            original_execute(sql, *args, **kwargs)
            injected = True
            raise primary
        if not injected and ((boundary == "commit" and sql == "COMMIT") or
                             (boundary == "nested_release" and sql.startswith("RELEASE "))):
            injected = True
            raise primary
        return original_execute(sql, *args, **kwargs)

    monkeypatch.setattr(native.conn, "execute", execute)

    def write_body():
        nonlocal writes
        writes += 1
        native.conn.execute("UPDATE tasks SET title = 'inner change' WHERE id = ?", (task.id,))
        if boundary.endswith("body"):
            raise primary

    if boundary.startswith("nested_"):
        with kb.write_txn(native.conn):
            native.conn.execute("UPDATE tasks SET title = 'outer change' WHERE id = ?", (task.id,))
            with pytest.raises(error_type) as caught:
                with kb.write_txn(native.conn, allow_nested=True):
                    write_body()
            assert caught.value is primary
            assert native.conn.in_transaction
            assert any(sql.startswith("ROLLBACK TO ") for sql in statements)
            assert any(sql.startswith("RELEASE ") for sql in statements)
            assert native.conn.execute("SELECT title FROM tasks WHERE id = ?", (task.id,)).fetchone()[0] == "outer change"
        expected = "outer change"
    else:
        with pytest.raises(error_type) as caught:
            with kb.write_txn(native.conn):
                write_body()
        assert caught.value is primary
        expected = task.title
    assert writes == (0 if boundary.endswith("begin") else 1)
    assert not native.conn.in_transaction
    assert native.conn.execute("SELECT title FROM tasks WHERE id = ?", (task.id,)).fetchone()[0] == expected


@pytest.mark.parametrize("nested", [False, True])
def test_transaction_cleanup_error_cannot_replace_original_interrupt(native, monkeypatch, nested):
    task = claim(native)
    primary = KeyboardInterrupt("original transaction interrupt")
    original_execute = native.conn.execute
    cleanup_attempted = []

    def execute(sql, *args, **kwargs):
        result = original_execute(sql, *args, **kwargs)
        if sql.startswith("ROLLBACK"):
            cleanup_attempted.append(sql)
            raise ValueError("secondary rollback cleanup failure")
        return result

    monkeypatch.setattr(native.conn, "execute", execute)
    outer = kb.write_txn(native.conn) if nested else contextlib.nullcontext()
    with outer:
        with pytest.raises(KeyboardInterrupt) as caught:
            with kb.write_txn(native.conn, allow_nested=nested):
                native.conn.execute("UPDATE tasks SET title = 'never retained' WHERE id = ?", (task.id,))
                raise primary
        assert caught.value is primary
        assert bool(native.conn.in_transaction) is nested
        assert native.conn.execute("SELECT title FROM tasks WHERE id = ?", (task.id,)).fetchone()[0] == task.title
    assert len(cleanup_attempted) == 1
    assert not native.conn.in_transaction
