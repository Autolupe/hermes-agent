"""Internal Linux held-child mechanics, not a worker admission endpoint.

The only entry is an inherited private socket. The single-threaded supervisor
is a Linux child subreaper. It signals only its own unreaped direct children;
detached grandchildren become such children when their parent is killed/exits.
No stored PID, process-group scan or saved JSON authorizes a signal.
"""

from __future__ import annotations

import ctypes
import json
import os
import select
import signal
import socket
import sys
import time


MAX_FRAME = 256 * 1024
_CANCELLED = False


def send(channel, message):
    raw = json.dumps(message, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > MAX_FRAME:
        raise ValueError("Worker protocol frame is too large.")
    channel.sendall(len(raw).to_bytes(4, "big") + raw)


def receive(channel, deadline):
    def read_exact(size):
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([channel], [], [], remaining)[0]:
                raise TimeoutError("Worker protocol deadline expired.")
            chunk = channel.recv(size - len(data))
            if not chunk:
                raise EOFError("Worker control channel closed.")
            data.extend(chunk)
        return data

    size = int.from_bytes(read_exact(4), "big")
    if not 0 < size <= MAX_FRAME:
        raise ValueError("Invalid worker protocol frame.")
    return json.loads(read_exact(size))


def process_identity(pid):
    # The comm field can contain spaces/parentheses. Its last ')' precedes
    # the fixed numeric fields; starttime is stat field 22.
    with open(f"/proc/{pid}/stat", encoding="ascii") as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise ValueError("Oversized process identity.")
    fields = raw[raw.rindex(")") + 2:].split()
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as stream:
        boot = stream.read(37).strip()
    metadata = os.stat(f"/proc/{pid}")
    return {"pid": pid, "parent": int(fields[1]), "start": int(fields[19]),
            "boot": boot, "uid": metadata.st_uid, "gid": metadata.st_gid}


def _subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    # Documented prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0), not an opaque
    # runtime pointer or architecture-dependent raw syscall number.
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                      ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Child ownership is unavailable.")


def _cancel(_signum, _frame):
    global _CANCELLED
    _CANCELLED = True


def _direct_children():
    # No threads or other waiters exist in this supervisor. A listed child
    # cannot have its PID recycled until this process explicitly reaps it.
    with open(f"/proc/self/task/{os.getpid()}/children", encoding="ascii") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Too many worker children to inspect safely.")
    children = [int(value) for value in raw.split()]
    if len(children) > 4096:
        raise ValueError("Too many worker children to inspect safely.")
    return children


def _drain(leader, deadline):
    """Return only after ECHILD proves the owned descendant tree is empty."""
    leader_status = None
    while time.monotonic() < deadline:
        for pid in _direct_children():
            # A current direct child, not a previously remembered process.
            # It stays unreaped throughout the identity check and signal.
            if process_identity(pid)["parent"] != os.getpid():
                raise RuntimeError("Worker child ownership changed.")
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return leader_status
            if pid == 0:
                break
            if pid == leader:
                leader_status = os.waitstatus_to_exitcode(status)
        # Killing an intermediate parent adopts its detached descendants.
        # A deadline failure never becomes an empty-tree result.
        select.select([], [], [], 0.01)
    raise TimeoutError("Worker descendant cleanup is uncertain.")


def _child(control, ready, command, workspace_fd, deadline):
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.write(ready, b"H")
        os.close(ready)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([control], [], [], remaining)[0]:
            os._exit(124)
        if os.read(control, 2) != b"G":
            os._exit(125)
        os.close(control)
        os.fchdir(workspace_fd)
        os.close(workspace_fd)
        # The exec'd task receives no control socket or release descriptor.
        os.execve(command[0], command, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    except BaseException:
        os._exit(127)


def supervise(channel):
    global _CANCELLED
    _subreaper()
    signal.signal(signal.SIGTERM, _cancel)
    signal.signal(signal.SIGINT, _cancel)
    leader = None
    release_fd = None
    child_read = ready_read = ready_write = workspace_fd = None
    state = "held"
    leader_status = None
    try:
        plan = receive(channel, time.monotonic() + 10)
        if (type(plan) is not dict or set(plan) != {"command", "workspace", "duration", "nonce"}
                or type(plan["command"]) is not list or not plan["command"]
                or any(type(arg) is not str or "\0" in arg for arg in plan["command"])
                or not os.path.isabs(plan["command"][0])
                or type(plan["duration"]) not in (int, float)
                or not 0 < plan["duration"] <= 300
                or type(plan["nonce"]) is not str or len(plan["nonce"]) != 32):
            raise ValueError("Invalid held-worker plan.")
        deadline = time.monotonic() + plan["duration"]
        workspace_fd = os.open(plan["workspace"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        workspace_stat = os.fstat(workspace_fd)
        workspace_identity = {"device": workspace_stat.st_dev, "inode": workspace_stat.st_ino}
        child_read, release_fd = os.pipe2(os.O_CLOEXEC)
        ready_read, ready_write = os.pipe2(os.O_CLOEXEC)
        leader = os.fork()
        if leader == 0:
            channel.close()
            os.close(release_fd)
            os.close(ready_read)
            _child(child_read, ready_write, plan["command"], workspace_fd, deadline)
        os.close(child_read)
        child_read = None
        os.close(ready_write)
        ready_write = None
        os.close(workspace_fd)
        workspace_fd = None
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([ready_read], [], [], remaining)[0]:
            raise TimeoutError("Held child did not become ready.")
        if os.read(ready_read, 2) != b"H":
            raise RuntimeError("Held child exited before readiness.")
        os.close(ready_read)
        ready_read = None
        identity = process_identity(leader)
        send(channel, {"state": "held", "child": identity, "nonce": plan["nonce"],
                       "workspace": workspace_identity})
        while not _CANCELLED and time.monotonic() < deadline:
            exited = os.waitid(os.P_PID, leader, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited is not None:
                state = "exited" if state == "released" else "cancelled"
                break
            if not select.select([channel], [], [], 0.02)[0]:
                continue
            message = receive(channel, deadline)
            if message == {"action": "cancel", "nonce": plan["nonce"]}:
                state = "cancelled"
                break
            if (state != "held" or message != {
                    "action": "release", "nonce": plan["nonce"], "child": identity}):
                raise ValueError("Worker release is not for this held child.")
            if process_identity(leader) != identity or _CANCELLED:
                raise RuntimeError("Held child identity changed before release.")
            os.write(release_fd, b"G")
            os.close(release_fd)
            release_fd = None
            state = "released"
            # This acknowledges release, not exec success or task completion.
            send(channel, {"state": state, "nonce": plan["nonce"]})
        else:
            state = "cancelled"
    except BaseException:
        state = "cancelled"
    finally:
        for descriptor in (release_fd, child_read, ready_read, ready_write, workspace_fd):
            if descriptor is not None:
                os.close(descriptor)
        try:
            leader_status = _drain(leader, time.monotonic() + 5)
            result = {"state": state if state in ("exited", "cancelled") else "cancelled",
                      "drained": True, "exit_code": leader_status}
        except BaseException:
            result = {"state": "uncertain", "drained": False, "exit_code": None}
        try:
            send(channel, result)
        except (OSError, ValueError):
            pass
        channel.close()


if __name__ == "__main__":
    if sys.platform != "linux" or len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit("An inherited worker-control socket is required.")
    with socket.socket(fileno=int(sys.argv[1])) as inherited:
        supervise(inherited)
