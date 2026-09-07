"""Reader/remover coordination against real temporary files and processes."""

import os
from pathlib import Path
import select
import stat
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import dispatch_boundary_probe as boundary_probe


@pytest.fixture
def shared_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return state


@pytest.mark.linux_only
def test_first_reader_creates_only_private_lock_and_allows_dispatch(shared_state):
    assert kb.dispatch_is_paused() is False
    lock = shared_state / "dispatch_pause.lock"
    info = lock.stat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_uid == os.getuid()
    assert info.st_nlink == 1
    assert list(shared_state.iterdir()) == [lock]
    before = info.st_ino
    assert kb.dispatch_is_paused() is False
    assert lock.stat().st_ino == before


def test_missing_root_or_state_is_not_created(tmp_path, monkeypatch):
    root = tmp_path / "absent-root"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    assert kb.dispatch_is_paused() is False
    assert not root.exists()
    root.mkdir()
    assert kb.dispatch_is_paused() is False
    assert list(root.iterdir()) == []


@pytest.mark.linux_only
@pytest.mark.parametrize("kind", ["regular", "broken-symlink", "directory"])
def test_retiring_pause_blocks_without_removing_it(shared_state, kind):
    retiring = shared_state / "dispatch_pause.retiring.json"
    if kind == "regular":
        retiring.write_bytes(b"original pause bytes\n")
    elif kind == "broken-symlink":
        retiring.symlink_to(shared_state / "missing")
    else:
        retiring.mkdir()
    before = retiring.lstat()
    assert kb.dispatch_is_paused() is True
    assert retiring.lstat().st_ino == before.st_ino
    if kind == "regular":
        assert retiring.read_bytes() == b"original pause bytes\n"


@pytest.mark.linux_only
def test_exclusive_remover_lock_blocks_and_shared_reader_lock_allows(shared_state):
    import fcntl

    lock = shared_state / "dispatch_pause.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert kb.dispatch_is_paused() is True
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        assert kb.dispatch_is_paused() is False
    finally:
        os.close(descriptor)
    assert lock.is_file()
    assert kb.dispatch_is_paused() is False


@pytest.mark.linux_only
@pytest.mark.parametrize(
    "kind", ["symlink", "directory", "fifo", "hardlink", "public", "wrong-owner"]
)
def test_unsafe_admission_lock_fails_closed_without_repair(
    shared_state, monkeypatch, kind
):
    lock = shared_state / "dispatch_pause.lock"
    if kind == "symlink":
        lock.symlink_to(shared_state / "missing")
    elif kind == "directory":
        lock.mkdir()
    elif kind == "fifo":
        os.mkfifo(lock, 0o600)
    else:
        lock.touch(mode=0o600)
        if kind == "hardlink":
            os.link(lock, shared_state / "extra-link")
        elif kind == "public":
            lock.chmod(0o644)
        else:
            actual_uid = os.getuid()
            monkeypatch.setattr(kb.os, "getuid", lambda: actual_uid + 1)
    before = lock.lstat()
    assert kb.dispatch_is_paused() is True
    after = lock.lstat()
    assert (after.st_ino, after.st_mode, after.st_nlink) == (
        before.st_ino, before.st_mode, before.st_nlink
    )


@pytest.mark.linux_only
def test_reader_holds_shared_lock_across_all_brake_reads(shared_state, monkeypatch):
    import fcntl

    original = kb._dispatch_optional_entry_info
    observed = []

    def try_remover_between_reads(parent_fd, name):
        if name in ("dispatch_pause.json", "halt.json", "dispatch_pause.retiring.json"):
            contender = os.open(shared_state / "dispatch_pause.lock", os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed.append(name)
            finally:
                os.close(contender)
        return original(parent_fd, name)

    monkeypatch.setattr(kb, "_dispatch_optional_entry_info", try_remover_between_reads)
    assert kb.dispatch_is_paused() is False
    assert observed == ["dispatch_pause.json", "halt.json", "dispatch_pause.retiring.json"]


@pytest.mark.linux_only
def test_lock_replacement_during_read_fails_closed(shared_state, monkeypatch):
    original = kb._dispatch_optional_entry_info
    replacement = shared_state / "replacement-lock"
    replacement.touch(mode=0o600)

    def replace_lock(parent_fd, name):
        if name == "dispatch_pause.retiring.json":
            os.replace(replacement, shared_state / "dispatch_pause.lock")
        return original(parent_fd, name)

    monkeypatch.setattr(kb, "_dispatch_optional_entry_info", replace_lock)
    assert kb.dispatch_is_paused() is True


@pytest.mark.linux_only
def test_profile_uses_shared_retiring_pause_and_admission_lock(tmp_path, monkeypatch):
    import fcntl

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "planner"
    profile.mkdir(parents=True)
    state = shared / "state"
    state.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    assert kb.dispatch_pause_lock_path() == state / "dispatch_pause.lock"
    assert kb.dispatch_retiring_pause_path() == state / "dispatch_pause.retiring.json"
    assert kb.dispatch_is_paused() is False
    descriptor = os.open(state / "dispatch_pause.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert kb.dispatch_is_paused() is True
    finally:
        os.close(descriptor)
    kb.dispatch_retiring_pause_path().write_bytes(b"retained shared brake\n")
    assert kb.dispatch_is_paused() is True
    assert not (profile / "state").exists()


@pytest.mark.linux_only
def test_retiring_brake_survives_remover_process_death(shared_state):
    pause = shared_state / "dispatch_pause.json"
    pause.write_bytes(b"pause before controller crash\n")
    retiring = shared_state / "dispatch_pause.retiring.json"
    child_code = """
import fcntl, os, sys
from pathlib import Path
state = Path(sys.argv[1])
fd = os.open(state / 'dispatch_pause.lock', os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.rename(state / 'dispatch_pause.json', state / 'dispatch_pause.retiring.json')
print('retiring', flush=True)
sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(shared_state)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        readable, _, _ = select.select([process.stdout], [], [], 10)
        assert readable, "remover did not reach retirement"
        assert process.stdout.readline().strip() == "retiring"
        assert not pause.exists()
        assert kb.dispatch_is_paused() is True
        process.kill()
        process.communicate(timeout=10)
        assert kb.dispatch_is_paused() is True
        assert retiring.read_bytes() == b"pause before controller crash\n"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


@pytest.mark.linux_only
@pytest.mark.parametrize("brake", ["retiring", "exclusive-lock"])
def test_retirement_blocks_claims_without_counting_a_worker_failure(shared_state, brake):
    import fcntl

    db_path = shared_state.parent / "kanban.db"
    kb.init_db(db_path=db_path)
    conn = kb.connect(db_path=db_path)
    descriptor = None
    try:
        task_id = kb.create_task(conn, title="retirement must defer", assignee="default")
        if brake == "retiring":
            (shared_state / "dispatch_pause.retiring.json").write_bytes(b"{}\n")
        else:
            descriptor = os.open(
                shared_state / "dispatch_pause.lock", os.O_RDWR | os.O_CREAT, 0o600
            )
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert kb.claim_task(conn, task_id) is None
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
        assert kb.get_task(conn, task_id).consecutive_failures == 0
    finally:
        if descriptor is not None:
            os.close(descriptor)
        conn.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 1),
        ("retiring_pause_path", "state/another-retiring-pause.json"),
        ("admission_lock_path", "state/another-lock"),
        ("admission_lock_path", None),
    ],
)
def test_probe_rejects_incompatible_retirement_contract(field, value):
    candidate = boundary_probe._failed_payload()
    candidate["state"] = "verified"
    candidate["checks"] = {name: True for name in kb.DISPATCH_BOUNDARY_CHECKS}
    assert kb.normalize_dispatch_boundary_self_test(candidate)[1] is True
    candidate[field] = value
    normalized, verified = kb.normalize_dispatch_boundary_self_test(candidate)
    assert verified is False
    assert normalized == boundary_probe._failed_payload()


@pytest.mark.windows_only
def test_native_windows_refuses_existing_retirement_lock(shared_state):
    assert kb.dispatch_is_paused() is False
    (shared_state / "dispatch_pause.lock").touch()
    assert kb.dispatch_is_paused() is True
