"""Source-only held-worker mechanics for a future protected launch adapter.

Nothing calls this from native dispatch. Both required launch gates still
refuse. A live WorkspaceRequest is required; its preparation admission is NOT
production launch admission. Saved journals are never loaded as permission.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import MappingProxyType

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_policy as policy
from hermes_cli import kanban_workspace_policy as workspace_policy
from hermes_cli import kanban_worker_supervisor as protocol


class HeldWorkerError(policy.RequiredPolicyError):
    """A child was refused or its cleanup cannot be proved by its live owner."""


def _directory(path, *, private=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise HeldWorkerError("Worker directory must be an absolute safe path.")
    # Check each named component without following a symlink. Public root-owned
    # ancestry is allowed; the supplied leaf must belong to the native account.
    for parent in (*reversed(path.parents), path):
        metadata = parent.lstat()
        sticky_root = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
        if (not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid not in (0, os.getuid())
                or (metadata.st_mode & 0o022 and not sticky_root)):
            raise HeldWorkerError("Unsafe worker directory ancestry.")
    if metadata.st_uid != os.getuid() or (private and metadata.st_mode & 0o077):
        raise HeldWorkerError("Worker journal directory is not private.")
    return {"device": metadata.st_dev, "inode": metadata.st_ino}


class HeldWorker:
    """One in-process, non-transferable handle to one held descendant tree.

    prepare/release/wait are synchronous and must run in the original request
    context. cancel is one-way and may be requested from another thread. Only
    a fresh drained response from the still-owned supervisor can clear its two
    PID markers and durable negative fence. Journal contents cannot do that.
    """

    def __init__(self, request, command, *, workspace, journal_root, timeout=10):
        self.request = request
        self.command = tuple(command)
        self.workspace = str(workspace)
        self.journal_root = Path(journal_root)
        self.timeout = timeout
        self.process = None
        self.channel = None
        self.child = None
        self.supervisor = None
        self.record_fd = None
        self.record_path = None
        self.event_id = None
        self.cancelled = False
        self.released = False
        self._outcome = None
        self._closed = False
        self._send_lock = threading.RLock()

    @property
    def outcome(self):
        return None if self._outcome is None else MappingProxyType(self._outcome)

    def _check(self, boundary):
        if self.cancelled or self._closed:
            raise HeldWorkerError("Held worker was cancelled or closed.")
        if not isinstance(self.request, workspace_policy.WorkspaceRequest):
            raise HeldWorkerError("The original native request is required.")
        if workspace_policy.current_request(
                conn=self.request.connection, task_id=self.request.claim.task_id,
                board=self.request.claim.board, boundary=boundary) is not self.request:
            raise HeldWorkerError("Held worker request changed.")
        if self.request.stored_workspace[0] != self.workspace:
            raise HeldWorkerError("Held worker workspace does not match the claim.")
        if _directory(self.workspace) != self.workspace_identity:
            raise HeldWorkerError("Held worker workspace identity changed.")

    def _journal(self, state):
        claim = self.request.claim
        payload = {"version": 1, "state": state, "request_id": claim.request_id,
                   "task_id": claim.task_id, "run_id": claim.run_id,
                   "claim_lock": claim.claim_lock, "board": claim.board,
                   "lane": claim.lane, "account": claim.account,
                   "policy_generation": claim.policy_generation,
                   "workspace": self.workspace_identity, "supervisor": self.supervisor,
                   "child": self.child, "command_sha256": hashlib.sha256(
                       json.dumps(self.command, ensure_ascii=True).encode()).hexdigest()}
        raw = json.dumps(payload, allow_nan=False, sort_keys=True).encode()
        descriptor = os.open("record.next", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.record_fd)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(raw)
                output.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace("record.next", "record.json", src_dir_fd=self.record_fd,
                   dst_dir_fd=self.record_fd)
        os.fsync(self.record_fd)

    def prepare(self):
        if sys.platform != "linux":
            raise HeldWorkerError("Held worker mechanics require Linux child ownership.")
        if (self.process is not None or self.record_fd is not None
                or type(self.timeout) not in (int, float) or not 0 < self.timeout <= 300
                or not self.command or len(self.command) > 64
                or any(type(arg) is not str or "\0" in arg or len(arg) > 4096 for arg in self.command)
                or not os.path.isabs(self.command[0])):
            raise HeldWorkerError("Invalid or repeated held-worker preparation.")
        self.workspace_identity = _directory(self.workspace)
        _directory(self.journal_root, private=True)
        self._check("held_prepare")
        if self.request._held_worker is not None:
            raise HeldWorkerError("This native request already owns a held worker.")
        self.request._held_worker = self
        self.deadline = time.monotonic() + self.timeout
        try:
            self.record_path = Path(tempfile.mkdtemp(prefix="worker-", dir=self.journal_root))
            self.record_fd = os.open(self.record_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except BaseException:
            self.request.cancel("preparation_failed")
            with contextlib.suppress(BaseException):
                self.close()
            raise
        try:
            parent_fd = os.open(self.journal_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            self._journal("preparing")
            # Commit a negative fence BEFORE creating any child. If creation or
            # bookkeeping later fails and cleanup is uncertain, a rolled-back
            # PID write must not look like proof that no worker exists.
            with kb.write_txn(self.request.connection):
                self._check("held_fence_locked")
                if kb._controlled_worker_pending(self.request.connection, self.request.claim.task_id):
                    raise HeldWorkerError("An earlier worker still has unresolved ownership.")
                claim = self.request.claim
                pids = self.request.connection.execute(
                    "SELECT t.worker_pid, r.worker_pid FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
                    "WHERE t.id = ? AND r.id = ?", (claim.task_id, claim.run_id)).fetchone()
                if pids is None or any(value is not None for value in pids):
                    raise HeldWorkerError("The claim already records a worker.")
                kb._append_event(self.request.connection, self.request.claim.task_id,
                                 "controlled_worker_held", {"request_id": self.request.claim.request_id},
                                 run_id=self.request.claim.run_id)
                self.event_id = self.request.connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            # The original SQLite write lock covers child creation and both PID
            # records. No cooperating reclaimer can slip between those steps.
            with kb.write_txn(self.request.connection):
                self._check("held_prepare_locked")
                self.channel, child_channel = socket.socketpair()
                self.channel.settimeout(2)
                try:
                    self.process = subprocess.Popen(
                        [sys.executable, "-I", "-S", "-B", str(Path(protocol.__file__).resolve()),
                         str(child_channel.fileno())],
                        pass_fds=(child_channel.fileno(),), stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, start_new_session=True,
                    )
                finally:
                    child_channel.close()
                self.supervisor = protocol.process_identity(self.process.pid)
                protocol.send(self.channel, {"command": list(self.command), "workspace": self.workspace,
                                            "duration": self.timeout,
                                            "nonce": self.request.claim.request_id})
                held = protocol.receive(self.channel, min(self.deadline, time.monotonic() + 5))
                if (type(held) is not dict or set(held) != {"state", "child", "nonce", "workspace"}
                        or held["state"] != "held" or held["nonce"] != self.request.claim.request_id
                        or held["workspace"] != self.workspace_identity):
                    raise HeldWorkerError("The original child did not acknowledge its held workspace.")
                self.child = held["child"]
                self._live_identity()
                self._journal("held_uncommitted")
                claim = self.request.claim
                conn = self.request.connection
                task_count = conn.execute(
                    "UPDATE tasks SET worker_pid = ? WHERE id = ? AND status = 'running' "
                    "AND current_run_id = ? AND claim_lock = ? AND worker_pid IS NULL",
                    (self.process.pid, claim.task_id, claim.run_id, claim.claim_lock)).rowcount
                run_count = conn.execute(
                    "UPDATE task_runs SET worker_pid = ? WHERE id = ? AND task_id = ? "
                    "AND claim_lock = ? AND status = 'running' AND ended_at IS NULL AND worker_pid IS NULL",
                    (self.process.pid, claim.run_id, claim.task_id, claim.claim_lock)).rowcount
                if task_count != 1 or run_count != 1:
                    raise HeldWorkerError("The exact held-worker claim changed.")
                kb._append_event(conn, claim.task_id, "controlled_worker_recorded",
                                 {"request_id": claim.request_id, "supervisor": self.supervisor,
                                  "child": self.child, "workspace": self.workspace_identity}, run_id=claim.run_id)
                self._check("before_held_commit")
                self._live_identity()
            self._journal("held_recorded")
            self._check("held_recorded")
            return self
        except BaseException:
            self.request.cancel("preparation_failed")
            # Keep the original refusal/interrupt if journal cleanup also
            # fails. The negative fence remains; this does not approve drain.
            with contextlib.suppress(BaseException):
                self.close()
            raise

    def _live_identity(self):
        if (self.process is None or self.child is None
                or protocol.process_identity(self.process.pid) != self.supervisor
                or protocol.process_identity(self.child["pid"]) != self.child
                or self.child["parent"] != self.process.pid
                or os.waitid(os.P_PID, self.process.pid,
                             os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None):
            raise HeldWorkerError("The original held process identity is no longer live.")

    @staticmethod
    def _outcome_message(result):
        if (type(result) is not dict or set(result) != {"state", "drained", "exit_code"}
                or result["state"] not in ("exited", "cancelled", "uncertain")
                or type(result["drained"]) is not bool
                or result["drained"] != (result["state"] != "uncertain")
                or (result["exit_code"] is not None and type(result["exit_code"]) is not int)):
            raise HeldWorkerError("Invalid worker cleanup response.")
        return result

    def _recorded(self):
        claim = self.request.claim
        row = self.request.connection.execute(
            "SELECT 1 FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
            "WHERE t.id = ? AND t.status = 'running' AND t.current_run_id = ? "
            "AND t.claim_lock = ? AND t.worker_pid = ? AND r.task_id = t.id "
            "AND r.claim_lock = ? AND r.worker_pid = ? AND r.status = 'running' AND r.ended_at IS NULL",
            (claim.task_id, claim.run_id, claim.claim_lock, self.process.pid,
             claim.claim_lock, self.process.pid)).fetchone()
        if row is None:
            raise HeldWorkerError("Held-worker bookkeeping no longer belongs to this request.")

    def release(self):
        if self.released or self.event_id is None:
            raise HeldWorkerError("Held-worker release is single use and requires recorded ownership.")
        try:
            with kb.write_txn(self.request.connection):
                self._check("held_release_locked")
                self._recorded()
                self._live_identity()
                self._journal("release_uncertain")
                self._check("before_held_release")
                self._recorded()
                self._live_identity()
                with self._send_lock:
                    if self.cancelled or self.request.cancelled:
                        raise HeldWorkerError("The request was cancelled before release.")
                    protocol.send(self.channel, {"action": "release", "nonce": self.request.claim.request_id,
                                                "child": self.child})
                    # From this point an absent ACK is uncertain execution,
                    # never permission to erase a claim or workspace.
                    self.released = True
                reply = protocol.receive(self.channel, min(self.deadline, time.monotonic() + 5))
                if reply != {"state": "released", "nonce": self.request.claim.request_id}:
                    raise HeldWorkerError("Held-worker release was not acknowledged.")
            self._journal("released")
        except BaseException:
            self.request.cancel("release_failed")
            raise

    def cancel(self, reason="cancelled"):
        if self.cancelled:
            return
        self.cancelled = True
        self.request.cancel(reason)
        if self.channel is not None:
            try:
                with self._send_lock:
                    protocol.send(self.channel, {"action": "cancel", "nonce": self.request.claim.request_id})
            except OSError:
                pass

    def wait(self):
        try:
            while self.outcome is None:
                if time.monotonic() >= self.deadline:
                    raise TimeoutError("Held worker timed out.")
                if select.select([self.channel], [], [], 0.02)[0]:
                    result = protocol.receive(self.channel, self.deadline)
                    self._outcome = self._outcome_message(result)
                    break
                self._check("held_running")
                self._recorded()
            self.process.wait(timeout=2)
            self._journal("drained" if self.outcome["drained"] else "uncertain")
            return self.outcome
        except BaseException:
            self.request.cancel("worker_wait_failed")
            raise

    def clear_drained_bookkeeping(self):
        """Clear only this live handle's exact markers; never load a receipt."""
        if (self.outcome is None or self.outcome["drained"] is not True
                or self.process.returncode != 0 or self.event_id is None):
            raise HeldWorkerError("Worker descendants have not been proved drained.")
        if workspace_policy._account() != self.request.claim.account:
            raise HeldWorkerError("Held-worker cleanup changed native execution context.")
        claim = self.request.claim
        with kb.write_txn(self.request.connection):
            self._recorded()
            conn = self.request.connection
            conn.execute("UPDATE tasks SET worker_pid = NULL WHERE id = ?", (claim.task_id,))
            conn.execute("UPDATE task_runs SET worker_pid = NULL WHERE id = ?", (claim.run_id,))
            kb._append_event(conn, claim.task_id, "controlled_worker_drained",
                             {"held_event_id": self.event_id, "request_id": claim.request_id}, run_id=claim.run_id)
        self.event_id = None

    def close(self):
        if self._closed:
            return
        try:
            if self.process is not None and self.outcome is None:
                self.cancel("worker_scope_closed")
                # Do not use ordinary wait(): cancellation intentionally makes
                # request checks fail, but the owner still must drain children.
                cleanup_deadline = time.monotonic() + 7
                try:
                    while True:
                        result = protocol.receive(self.channel, cleanup_deadline)
                        if type(result) is dict and "drained" in result:
                            self._outcome = self._outcome_message(result)
                            break
                    self.process.wait(timeout=max(0.1, cleanup_deadline - time.monotonic()))
                except (OSError, EOFError, ValueError, HeldWorkerError, TimeoutError, subprocess.TimeoutExpired):
                    # EOF itself grants no drained proof. Closing this private
                    # channel asks the still-owned supervisor to drain anyway.
                    self.channel.close()
                    try:
                        self.process.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        pass  # Retain the handle and unresolved negative fence.
            if self.record_fd is not None:
                self._journal("drained" if self.outcome and self.outcome.get("drained") is True
                              else "uncertain")
        finally:
            self._closed = True
            if self.channel is not None:
                self.channel.close()
            if self.record_fd is not None:
                os.close(self.record_fd)
                self.record_fd = None

    def __enter__(self):
        return self.prepare()

    def __exit__(self, *_exc):
        self.close()
