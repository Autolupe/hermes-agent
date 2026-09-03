"""Global emergency stop (ESTOP) — a resumable pause for NEW work only.

``hermes pause`` writes a sentinel file at ``<shared-root>/ESTOP``;
``hermes resume`` removes it. Named profiles use the same shared sentinel.
Profile-local sentinels created by older versions remain authoritative across
all named profiles and are removed by ``hermes resume`` so an upgrade can
never lift an existing stop.
While a sentinel exists:

* the cron scheduler skips dispatching due jobs (``cron/scheduler.py:tick``),
* the embedded kanban dispatcher skips spawning workers
  (``gateway/kanban_watchers.py``),
* new gateway turns get a brief "Hermes is paused" reply instead of an
  agent run (``gateway/run.py:_handle_message``).

In-flight work is NEVER killed — this is pause-new-work, not panic/exit.
The check uses ``os.lstat`` so every directory entry, including a broken
symlink, holds the stop. A missing entry also gets an ancestor check so a
broken parent cannot look like an absent stop. Callers may run it every tick;
no caching beyond the OS is performed, so engaging/disengaging takes effect on
the next check.

The sentinel body is optional JSON ``{"reason": ..., "engaged_at": ...}``.
A corrupt or empty file still counts as engaged (fail safe): the pause must
hold even if the file was created by ``touch ~/.hermes/ESTOP``.

Ported from: gastownhall/gastown estop.go (MIT). Related prior art:
#26778 (/panic — kill/exit semantics; deliberately different, ours is
resumable) and #44617 (interrupting in-flight cron; deliberately out of
scope here).
"""

from __future__ import annotations

import json
import logging
import os
import stat as stat_module
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

SENTINEL_NAME = "ESTOP"

# Per-component "logged already for this engagement" flags so a paused
# dispatch loop logs once per engagement instead of once per tick.
_log_lock = threading.Lock()
_logged_components: set[str] = set()


class DisengageError(RuntimeError):
    """The emergency stop could not be removed or verified safely."""


class EngageError(RuntimeError):
    """The emergency stop could not be created or verified safely."""


class _LegacySentinelEntry(NamedTuple):
    path: Path
    parent_identity: tuple[object, ...]


class _SentinelReadEntry(NamedTuple):
    path: Path
    parent_identity: tuple[object, ...] | None


class _CleanupTarget(NamedTuple):
    logical_path: Path
    physical_parent: Path
    parent_identity: tuple[object, ...]
    parent_fd: int | None = None


def _hermes_home() -> Path:
    """Resolve the shared Hermes root at call time, folding named profiles."""
    try:
        from hermes_constants import get_default_hermes_root
        return get_default_hermes_root()
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def _active_hermes_home() -> Path:
    """Resolve the active profile home for legacy-sentinel compatibility."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def sentinel_path() -> Path:
    """Path of the global ESTOP sentinel under the shared Hermes root."""
    return _hermes_home() / SENTINEL_NAME


def _sentinel_read_entries() -> tuple[_SentinelReadEntry, ...]:
    """Return every stop path with its discovered profile identity.

    Older Hermes releases wrote ESTOP inside only the active profile. A
    default or sibling-profile gateway must therefore scan the shared
    ``profiles`` directory, or an already-engaged stop could disappear during
    upgrade. Enumeration errors fail closed through the public callers.
    """
    shared = sentinel_path()
    legacy = _active_hermes_home() / SENTINEL_NAME
    entries: dict[Path, _SentinelReadEntry] = {
        shared: _SentinelReadEntry(shared, None)
    }
    if legacy != shared:
        entries[legacy] = _SentinelReadEntry(legacy, None)
    for entry in _legacy_profile_sentinel_entries(shared.parent):
        # Replacing an existing key retains its original ordering while adding
        # the directory identity captured during the profile scan.
        entries[entry.path] = _SentinelReadEntry(
            entry.path,
            entry.parent_identity,
        )
    return tuple(entries.values())


def _legacy_profile_sentinel_entries(
    shared_root: Path,
) -> tuple[_LegacySentinelEntry, ...]:
    """Discover legacy ESTOP paths without following profile-dir symlinks.

    A symlinked or unreadable profile directory is not safe to scan or clean:
    following it during ``hermes resume`` could unlink a file outside the
    shared Hermes root. Treat that uncertain layout as engaged instead.
    """
    profiles_root = shared_root / "profiles"
    try:
        root_info = os.lstat(profiles_root)
    except FileNotFoundError:
        if not _missing_path_has_usable_ancestor(profiles_root.parent):
            raise OSError("profiles directory ancestry is not usable")
        try:
            root_info = os.lstat(profiles_root)
        except FileNotFoundError:
            return ()
    if (
        not stat_module.S_ISDIR(root_info.st_mode)
        or _is_unsafe_profile_redirect(root_info)
    ):
        raise OSError("profiles path is not a real, unredirected directory")
    linux_mount_points = _linux_mount_points()
    if _is_profile_mount_point(profiles_root, linux_mount_points):
        raise OSError("profiles path is a mounted directory")

    discovered: list[_LegacySentinelEntry] = []
    with os.scandir(profiles_root) as entries:
        for entry in entries:
            entry_info = entry.stat(follow_symlinks=False)
            if _is_unsafe_profile_redirect(entry_info):
                raise OSError("redirected profile directory is unsafe")
            if stat_module.S_ISDIR(entry_info.st_mode):
                entry_path = Path(entry.path)
                if _is_profile_mount_point(entry_path, linux_mount_points):
                    raise OSError("mounted profile directory is unsafe")
                discovered.append(
                    _LegacySentinelEntry(
                        entry_path / SENTINEL_NAME,
                        _stat_identity(entry_info),
                    )
                )
    return tuple(sorted(discovered, key=lambda item: str(item.path)))


def _is_unsafe_profile_redirect(info: object) -> bool:
    """Reject symlinks and Windows directory reparse points.

    Python 3.11 can report a Windows junction as a directory even when the
    caller asks not to follow links. The Windows file-attribute bit remains
    authoritative, so reject every reparse point before collecting a legacy
    ESTOP path that ``disengage`` might unlink.
    """
    mode = getattr(info, "st_mode", 0)
    reparse_flag = getattr(
        stat_module,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        0x400,
    )
    file_attributes = getattr(info, "st_file_attributes", 0)
    return stat_module.S_ISLNK(mode) or bool(file_attributes & reparse_flag)


def _decode_mountinfo_path(value: str) -> str:
    """Decode the four pathname escapes used by Linux mountinfo."""
    return (
        value.replace(r"\040", " ")
        .replace(r"\011", "\t")
        .replace(r"\012", "\n")
        .replace(r"\134", "\\")
    )


def _linux_mount_points() -> frozenset[str]:
    """Read exact Linux mount points, including same-filesystem bind mounts."""
    if not sys.platform.startswith("linux"):
        return frozenset()
    try:
        lines = Path("/proc/self/mountinfo").read_text(
            encoding="utf-8",
            errors="surrogateescape",
        ).splitlines()
    except OSError as exc:
        raise OSError("Linux mount points could not be checked safely") from exc

    mount_points: set[str] = set()
    for line in lines:
        fields = line.split()
        if len(fields) < 6 or "-" not in fields[6:]:
            raise OSError("Linux mountinfo is malformed")
        decoded = _decode_mountinfo_path(fields[4])
        mount_points.add(os.path.normcase(os.path.abspath(decoded)))
    return frozenset(mount_points)


def _is_profile_mount_point(
    path: Path,
    linux_mount_points: frozenset[str],
) -> bool:
    """Reject volume, FUSE, and same-filesystem bind mount redirects."""
    try:
        physical_path = path.resolve(strict=True)
        if os.path.ismount(physical_path):
            return True
    except (OSError, RuntimeError) as exc:
        raise OSError("profile mount state could not be checked safely") from exc
    if not linux_mount_points:
        return False
    normalized = os.path.normcase(os.path.abspath(str(physical_path)))
    return normalized in linux_mount_points


def _fd_mount_id(fd: int) -> int | None:
    """Return the Linux mount ID for an already-open directory."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        lines = Path(f"/proc/self/fdinfo/{fd}").read_text(
            encoding="utf-8",
            errors="strict",
        ).splitlines()
        values = [
            line.partition(":")[2].strip()
            for line in lines
            if line.startswith("mnt_id:")
        ]
        if len(values) != 1:
            raise ValueError("missing or repeated mount ID")
        return int(values[0])
    except (OSError, UnicodeError, ValueError) as exc:
        raise OSError(
            "Linux directory mount identity could not be checked safely"
        ) from exc


def _stat_identity(info: object) -> tuple[object, ...]:
    """Return stable directory identity fields available on every host."""
    return (
        getattr(info, "st_dev", None),
        getattr(info, "st_ino", None),
        getattr(info, "st_mode", None),
        getattr(info, "st_file_attributes", None),
        getattr(info, "st_reparse_tag", None),
    )


def _path_is_within(path: Path, root: Path) -> bool:
    """Compare physical paths with the host's case-sensitivity rules."""
    path_text = os.path.normcase(str(path))
    root_text = os.path.normcase(str(root))
    try:
        return os.path.commonpath((path_text, root_text)) == root_text
    except ValueError:
        return False


def _capture_cleanup_target(
    path: Path,
    expected_parent_identity: tuple[object, ...] | None,
) -> _CleanupTarget | None:
    """Capture one present stop under its validated physical parent."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        if not _missing_path_has_usable_ancestor(path.parent):
            raise OSError("stop path ancestry is not usable")
        try:
            os.lstat(path)
        except FileNotFoundError:
            return None

    try:
        physical_root = sentinel_path().parent.resolve(strict=True)
        physical_parent = path.parent.resolve(strict=True)
        parent_info = os.stat(physical_parent, follow_symlinks=False)
    except (OSError, RuntimeError) as exc:
        raise OSError("stop parent could not be resolved safely") from exc
    if not _path_is_within(physical_parent, physical_root):
        raise OSError("stop parent resolves outside the shared Hermes root")
    if _is_unsafe_profile_redirect(parent_info):
        raise OSError("resolved stop parent is redirected")

    parent_identity = _stat_identity(parent_info)
    if (
        expected_parent_identity is not None
        and parent_identity != expected_parent_identity
    ):
        raise OSError("profile directory changed during stop discovery")
    return _CleanupTarget(path, physical_parent, parent_identity)


def _supports_anchored_cleanup() -> bool:
    """Return whether this host can keep cleanup below open directories."""
    supports_dir_fd = getattr(os, "supports_dir_fd", set())
    supports_fd = getattr(os, "supports_fd", set())
    return (
        os.name != "nt"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in supports_dir_fd
        and os.stat in supports_dir_fd
        and os.unlink in supports_dir_fd
        and os.scandir in supports_fd
    )


def _directory_open_flags() -> int:
    """Open a real directory without following its final path component."""
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _optional_entry_info(parent_fd: int, name: str) -> object | None:
    """Read an entry below an open directory, retrying a missing leaf once."""
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None


def _reject_child_mount(
    parent_fd: int,
    child_fd: int,
    message: str,
) -> None:
    """Reject a mounted child using identities from open directories."""
    parent_info = os.fstat(parent_fd)
    child_info = os.fstat(child_fd)
    if getattr(parent_info, "st_dev", None) != getattr(
        child_info,
        "st_dev",
        None,
    ):
        raise OSError(message)
    parent_mount_id = _fd_mount_id(parent_fd)
    child_mount_id = _fd_mount_id(child_fd)
    if parent_mount_id is not None and child_mount_id != parent_mount_id:
        raise OSError(message)


def _capture_anchored_cleanup_target(
    logical_path: Path,
    physical_parent: Path,
    parent_fd: int,
) -> _CleanupTarget | None:
    """Keep an open copy of a present stop's validated parent directory."""
    if _optional_entry_info(parent_fd, logical_path.name) is None:
        return None
    parent_identity = _stat_identity(os.fstat(parent_fd))
    return _CleanupTarget(
        logical_path,
        physical_parent,
        parent_identity,
        os.dup(parent_fd),
    )


def _close_cleanup_target_fds(targets: list[_CleanupTarget]) -> None:
    """Close retained directory descriptors after failed discovery."""
    for target in targets:
        if target.parent_fd is not None:
            try:
                os.close(target.parent_fd)
            except OSError:
                pass


def _posix_sentinel_cleanup_targets() -> tuple[_CleanupTarget, ...]:
    """Discover stops through directories held open for the later unlink."""
    shared = sentinel_path()
    legacy = _active_hermes_home() / SENTINEL_NAME
    try:
        physical_root = shared.parent.resolve(strict=True)
    except FileNotFoundError:
        if not _missing_path_has_usable_ancestor(shared.parent.parent):
            raise OSError("shared Hermes root ancestry is not usable") from None
        try:
            physical_root = shared.parent.resolve(strict=True)
        except FileNotFoundError:
            if legacy != shared and _path_is_engaged(legacy):
                raise OSError("an active legacy stop is outside the shared root")
            return ()
    except (OSError, RuntimeError) as exc:
        raise OSError("shared Hermes root could not be resolved safely") from exc

    try:
        root_info = os.stat(physical_root, follow_symlinks=False)
    except OSError as exc:
        raise OSError("shared Hermes root could not be checked safely") from exc
    if (
        not stat_module.S_ISDIR(root_info.st_mode)
        or _is_unsafe_profile_redirect(root_info)
    ):
        raise OSError("shared Hermes root is not a real directory")

    root_fd: int | None = None
    profiles_fd: int | None = None
    captured: list[_CleanupTarget] = []
    covered_parent_identities: set[tuple[object, ...]] = set()
    try:
        root_fd = os.open(physical_root, _directory_open_flags())
        opened_root_info = os.fstat(root_fd)
        if _stat_identity(opened_root_info) != _stat_identity(root_info):
            raise OSError("shared Hermes root changed during stop discovery")
        root_identity = _stat_identity(opened_root_info)
        covered_parent_identities.add(root_identity)

        shared_target = _capture_anchored_cleanup_target(
            shared,
            physical_root,
            root_fd,
        )
        if shared_target is not None:
            captured.append(shared_target)

        profiles_info = _optional_entry_info(root_fd, "profiles")
        if profiles_info is not None:
            if (
                not stat_module.S_ISDIR(profiles_info.st_mode)
                or _is_unsafe_profile_redirect(profiles_info)
            ):
                raise OSError("profiles path is not a real, unredirected directory")
            profiles_fd = os.open(
                "profiles",
                _directory_open_flags(),
                dir_fd=root_fd,
            )
            opened_profiles_info = os.fstat(profiles_fd)
            if _stat_identity(opened_profiles_info) != _stat_identity(profiles_info):
                raise OSError("profiles directory changed during stop discovery")
            _reject_child_mount(
                root_fd,
                profiles_fd,
                "profiles path is a mounted directory",
            )

            with os.scandir(profiles_fd) as entries:
                profile_names = sorted(entry.name for entry in entries)
            for profile_name in profile_names:
                profile_info = _optional_entry_info(profiles_fd, profile_name)
                if profile_info is None:
                    raise OSError("profile directory changed during stop discovery")
                if _is_unsafe_profile_redirect(profile_info):
                    raise OSError("redirected profile directory is unsafe")
                if not stat_module.S_ISDIR(profile_info.st_mode):
                    continue

                profile_fd = os.open(
                    profile_name,
                    _directory_open_flags(),
                    dir_fd=profiles_fd,
                )
                try:
                    opened_profile_info = os.fstat(profile_fd)
                    if _stat_identity(opened_profile_info) != _stat_identity(
                        profile_info
                    ):
                        raise OSError(
                            "profile directory changed during stop discovery"
                        )
                    _reject_child_mount(
                        profiles_fd,
                        profile_fd,
                        "mounted profile directory is unsafe",
                    )
                    covered_parent_identities.add(
                        _stat_identity(opened_profile_info)
                    )
                    logical_parent = shared.parent / "profiles" / profile_name
                    target = _capture_anchored_cleanup_target(
                        logical_parent / SENTINEL_NAME,
                        physical_root / "profiles" / profile_name,
                        profile_fd,
                    )
                    if target is not None:
                        captured.append(target)
                finally:
                    os.close(profile_fd)

        if legacy != shared and _path_is_engaged(legacy):
            try:
                legacy_parent = legacy.parent.resolve(strict=True)
                legacy_info = os.stat(legacy_parent, follow_symlinks=False)
            except (OSError, RuntimeError) as exc:
                raise OSError("active legacy stop could not be checked safely") from exc
            if _stat_identity(legacy_info) not in covered_parent_identities:
                raise OSError(
                    "an active legacy stop is outside the anchored profile tree"
                )

        return tuple(sorted(captured, key=lambda target: str(target.logical_path)))
    except BaseException:
        _close_cleanup_target_fds(captured)
        raise
    finally:
        if profiles_fd is not None:
            os.close(profiles_fd)
        if root_fd is not None:
            os.close(root_fd)


def _sentinel_cleanup_targets() -> tuple[_CleanupTarget, ...]:
    """Capture deletions so a later path replacement cannot redirect them."""
    if _supports_anchored_cleanup():
        return _posix_sentinel_cleanup_targets()

    shared = sentinel_path()
    legacy = _active_hermes_home() / SENTINEL_NAME
    candidates: dict[Path, tuple[object, ...] | None] = {shared: None}
    if legacy != shared:
        candidates[legacy] = None
    for entry in _legacy_profile_sentinel_entries(shared.parent):
        candidates[entry.path] = entry.parent_identity

    captured: list[_CleanupTarget] = []
    for path, expected_parent_identity in candidates.items():
        target = _capture_cleanup_target(path, expected_parent_identity)
        if target is not None:
            captured.append(target)
    return tuple(captured)


def _unlink_windows_cleanup_target(target: _CleanupTarget) -> None:
    """Hold the validated Windows parent open so it cannot be replaced."""
    import ctypes
    from ctypes import wintypes

    file_read_attributes = 0x00000080
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(target.physical_parent),
        file_read_attributes,
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        current_info = os.stat(target.physical_parent, follow_symlinks=False)
        if (
            _is_unsafe_profile_redirect(current_info)
            or _stat_identity(current_info) != target.parent_identity
        ):
            raise OSError("stop parent changed before cleanup")
        (target.physical_parent / target.logical_path.name).unlink()
    finally:
        if not close_handle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def _unlink_cleanup_target(target: _CleanupTarget) -> None:
    """Delete a stop through a directory that still has captured identity."""
    if target.parent_fd is not None:
        try:
            if target.logical_path.name != SENTINEL_NAME:
                raise OSError("refusing to remove an unexpected stop filename")
            if (
                _stat_identity(os.fstat(target.parent_fd))
                != target.parent_identity
            ):
                raise OSError("stop parent changed before cleanup")
            os.unlink(target.logical_path.name, dir_fd=target.parent_fd)
        finally:
            os.close(target.parent_fd)
        return

    if target.logical_path.name != SENTINEL_NAME:
        raise OSError("refusing to remove an unexpected stop filename")
    if os.name == "nt":
        _unlink_windows_cleanup_target(target)
        return

    supports_dir_fd = os.unlink in getattr(os, "supports_dir_fd", set())
    if supports_dir_fd:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        parent_fd = os.open(target.physical_parent, flags)
        try:
            if _stat_identity(os.fstat(parent_fd)) != target.parent_identity:
                raise OSError("stop parent changed before cleanup")
            os.unlink(target.logical_path.name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        return

    current_info = os.stat(target.physical_parent, follow_symlinks=False)
    if (
        _is_unsafe_profile_redirect(current_info)
        or _stat_identity(current_info) != target.parent_identity
    ):
        raise OSError("stop parent changed before cleanup")
    (target.physical_parent / target.logical_path.name).unlink()


def is_engaged() -> bool:
    """Cheap filesystem check: is the global emergency stop engaged?

    Fail SAFE on stat errors: if we cannot determine whether the sentinel
    exists (permission error, transient I/O failure on HERMES_HOME), report
    engaged. The module contract is that the pause must hold even when the
    sentinel is unreadable — a fail-open here would silently lift an
    operator's emergency stop exactly when the filesystem is misbehaving.
    """
    try:
        return any(
            _sentinel_entry_is_engaged(entry)
            for entry in _sentinel_read_entries()
        )
    except Exception:
        return True


def _sentinel_parent_matches(entry: _SentinelReadEntry) -> bool:
    """Verify that a discovered profile path still names the same directory."""
    if entry.parent_identity is None:
        return True
    try:
        parent_info = os.stat(entry.path.parent, follow_symlinks=False)
        if (
            not stat_module.S_ISDIR(parent_info.st_mode)
            or _is_unsafe_profile_redirect(parent_info)
            or _stat_identity(parent_info) != entry.parent_identity
            or _is_profile_mount_point(
                entry.path.parent,
                _linux_mount_points(),
            )
        ):
            return False
    except (OSError, RuntimeError):
        return False
    return True


def _sentinel_entry_is_engaged(entry: _SentinelReadEntry) -> bool:
    """Check a stop without letting a replaced profile hide its old entry."""
    if not _sentinel_parent_matches(entry):
        return True
    if _path_is_engaged(entry.path):
        return True
    # An absent leaf is safe only while it remains below the exact directory
    # that the profile enumeration validated.
    return not _sentinel_parent_matches(entry)


def _path_is_engaged(path: Path) -> bool:
    """Check one sentinel path, treating every uncertain state as engaged."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        # ENOENT also means "missing" when any ancestor is a broken symlink.
        # Only accept it as an absent ESTOP below a usable directory, then
        # retry the leaf in case an operator engaged the stop during the walk.
        if not _missing_path_has_usable_ancestor(path.parent):
            return True
        try:
            os.lstat(path)
        except FileNotFoundError:
            return not _missing_path_has_usable_ancestor(path.parent)
        except OSError:
            return True
        return True
    except OSError:
        return True
    return True


def _missing_path_has_usable_ancestor(path: Path) -> bool:
    """Return True only when *path* is missing below a usable directory.

    ``lstat`` reports ``ENOENT`` both for an absent leaf and for a path below
    a broken symlink. Walk upward without following entries until an existing
    ancestor is found, then follow only that entry when it is a symlink. Any
    lookup failure keeps the emergency stop engaged.
    """
    candidate = path
    while True:
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                return False
            candidate = parent
            continue
        except OSError:
            return False
        if stat_module.S_ISLNK(info.st_mode):
            try:
                followed = os.stat(candidate)
            except OSError:
                return False
            return stat_module.S_ISDIR(followed.st_mode)
        return stat_module.S_ISDIR(info.st_mode)


def engage(reason: Optional[str] = None) -> Path:
    """Create and verify the ESTOP sentinel, or report that pause failed."""
    path = sentinel_path()
    payload = {
        "engaged_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason or None,
    }
    creation_error: OSError | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        creation_error = exc
        # Best effort: an empty/partial sentinel still pauses (fail safe).
        try:
            path.touch(exist_ok=True)
        except OSError as touch_error:
            creation_error = touch_error
    try:
        # Verify this exact shared entry. The aggregate is_engaged() check is
        # deliberately fail-closed, so an unrelated unreadable legacy path can
        # report True even when this write and fallback touch both failed.
        os.lstat(path)
    except OSError:
        raise EngageError(
            "the shared stop file could not be created or verified"
        ) from creation_error
    return path


def disengage() -> bool:
    """Remove every stop, raising when safe cleanup cannot be proved."""
    removed = False
    failed = False
    try:
        targets = _sentinel_cleanup_targets()
    except Exception as exc:
        raise DisengageError(
            "the shared or legacy profile stop paths could not be checked safely"
        ) from exc
    for target in targets:
        try:
            _unlink_cleanup_target(target)
            removed = True
        except FileNotFoundError:
            continue
        except OSError:
            failed = True
    if failed:
        raise DisengageError(
            "one or more stop files could not be removed safely"
        )
    if not removed:
        if is_engaged():
            raise DisengageError(
                "the stop state remains engaged or unreadable"
            )
        return False
    # Re-enumerate after deletion so a sibling legacy stop created during the
    # cleanup cannot be missed by the paths snapshot above.
    if is_engaged():
        raise DisengageError("a stop is still present after cleanup")
    return True


def get_state() -> Optional[dict]:
    """Return ``{"reason": ..., "engaged_at": ...}`` or None when not engaged.

    A sentinel with an unreadable/corrupt body still reports engaged, with
    both fields None — the pause is authoritative, the metadata is not.
    """
    try:
        entries = _sentinel_read_entries()
    except Exception:
        return {"reason": None, "engaged_at": None}
    entry = next(
        (item for item in entries if _sentinel_entry_is_engaged(item)),
        None,
    )
    if entry is None:
        return None
    reason = None
    engaged_at = None
    try:
        if not _sentinel_parent_matches(entry):
            raise OSError("stop parent changed before metadata read")
        raw = json.loads(entry.path.read_text(encoding="utf-8"))
        if not _sentinel_parent_matches(entry):
            raise OSError("stop parent changed during metadata read")
        if isinstance(raw, dict):
            reason = raw.get("reason") or None
            engaged_at = raw.get("engaged_at") or None
    except (OSError, ValueError):
        pass
    return {"reason": reason, "engaged_at": engaged_at}


def paused_reply() -> Optional[str]:
    """Short user-facing notice for new gateway turns, or None if not paused."""
    state = get_state()
    if state is None:
        return None
    reason = state.get("reason")
    if reason:
        return (
            f"⏸️ Hermes is paused ({reason}). New work is on hold; "
            "run `hermes resume` to pick things back up."
        )
    return (
        "⏸️ Hermes is paused. New work is on hold; "
        "run `hermes resume` to pick things back up."
    )


def check_paused(component: str, logger: logging.Logger) -> bool:
    """Return True when engaged, logging once per engagement per component.

    Dispatch loops call this every tick; the log fires on the disengaged→
    engaged transition for that component and re-arms after a resume, so a
    long pause doesn't spam one line per tick.
    """
    if not is_engaged():
        with _log_lock:
            _logged_components.discard(component)
        return False
    with _log_lock:
        first = component not in _logged_components
        if first:
            _logged_components.add(component)
    if first:
        state = get_state() or {}
        reason = state.get("reason")
        suffix = f" (reason: {reason})" if reason else ""
        logger.info(
            "%s dispatch paused by global emergency stop%s — remove with "
            "`hermes resume` (%s)",
            component,
            suffix,
            sentinel_path(),
        )
    return True


def _reset_log_state_for_tests() -> None:
    """Clear the log-once bookkeeping (test isolation helper)."""
    with _log_lock:
        _logged_components.clear()
