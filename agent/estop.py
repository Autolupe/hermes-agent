"""Global emergency stop (ESTOP) — a resumable pause for NEW work only.

``hermes pause`` writes a sentinel file at ``<shared-root>/ESTOP``;
``hermes resume`` removes it. Named profiles use the same shared sentinel.
Profile-local sentinels created by older versions remain authoritative and are
removed by ``hermes resume`` so an upgrade can never lift an existing stop.
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
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SENTINEL_NAME = "ESTOP"

# Per-component "logged already for this engagement" flags so a paused
# dispatch loop logs once per engagement instead of once per tick.
_log_lock = threading.Lock()
_logged_components: set[str] = set()


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


def _sentinel_paths() -> tuple[Path, ...]:
    """Return the canonical global path plus any distinct legacy profile path."""
    shared = sentinel_path()
    legacy = _active_hermes_home() / SENTINEL_NAME
    return (shared,) if legacy == shared else (shared, legacy)


def is_engaged() -> bool:
    """Cheap filesystem check: is the global emergency stop engaged?

    Fail SAFE on stat errors: if we cannot determine whether the sentinel
    exists (permission error, transient I/O failure on HERMES_HOME), report
    engaged. The module contract is that the pause must hold even when the
    sentinel is unreadable — a fail-open here would silently lift an
    operator's emergency stop exactly when the filesystem is misbehaving.
    """
    try:
        return any(_path_is_engaged(path) for path in _sentinel_paths())
    except Exception:
        return True


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
            return False
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
    """Create the ESTOP sentinel. Idempotent; re-engaging updates the file."""
    path = sentinel_path()
    payload = {
        "engaged_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason or None,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        # Best effort: an empty/partial sentinel still pauses (fail safe).
        try:
            path.touch(exist_ok=True)
        except OSError:
            pass
    return path


def disengage() -> bool:
    """Remove global and legacy profile sentinels if the pause was lifted."""
    removed = False
    failed = False
    try:
        paths = _sentinel_paths()
    except Exception:
        return False
    for path in paths:
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
        except OSError:
            failed = True
    if failed or not removed:
        return False
    return not any(_path_is_engaged(path) for path in paths)


def get_state() -> Optional[dict]:
    """Return ``{"reason": ..., "engaged_at": ...}`` or None when not engaged.

    A sentinel with an unreadable/corrupt body still reports engaged, with
    both fields None — the pause is authoritative, the metadata is not.
    """
    try:
        paths = _sentinel_paths()
    except Exception:
        return {"reason": None, "engaged_at": None}
    path = next((item for item in paths if _path_is_engaged(item)), None)
    if path is None:
        return None
    reason = None
    engaged_at = None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
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
