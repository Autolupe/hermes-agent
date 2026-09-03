"""Global emergency stop (`hermes pause` / `hermes resume`) — agent/estop.py.

The ESTOP sentinel is a resumable pause for NEW work only: cron dispatch,
kanban dispatch, and new gateway turns are halted while it is engaged; work
already in flight is never touched. Removing the sentinel (`hermes resume`)
restores normal operation with no restart.

Ported from: gastownhall/gastown estop.go (MIT); related prior art: #26778
(/panic — kill/exit semantics, deliberately different) and #44617
(interrupt in-flight cron — deliberately NOT done here).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import estop


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir and reset estop module log state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    estop._reset_log_state_for_tests()
    return tmp_path


# ── sentinel create / remove ────────────────────────────────────────────────


def test_engage_creates_sentinel_and_is_engaged(hermes_home):
    assert estop.is_engaged() is False
    estop.engage()
    assert (hermes_home / "ESTOP").exists()
    assert estop.is_engaged() is True


def test_engage_reports_when_shared_stop_cannot_be_created(
    hermes_home, monkeypatch
):
    """An unrelated fail-closed state cannot hide a failed shared write."""
    profile_home = hermes_home / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    sentinel = hermes_home / "ESTOP"
    real_write_text = estop.Path.write_text
    real_touch = estop.Path.touch

    def denied_write_text(path, *args, **kwargs):
        if path == sentinel:
            raise PermissionError("shared root is read-only")
        return real_write_text(path, *args, **kwargs)

    def denied_touch(path, *args, **kwargs):
        if path == sentinel:
            raise PermissionError("shared root is read-only")
        return real_touch(path, *args, **kwargs)

    monkeypatch.setattr(estop.Path, "write_text", denied_write_text)
    monkeypatch.setattr(estop.Path, "touch", denied_touch)
    monkeypatch.setattr(estop, "is_engaged", lambda: True)

    with pytest.raises(estop.EngageError, match="could not be created"):
        estop.engage(reason="maintenance")
    assert not os.path.lexists(sentinel)


def test_disengage_removes_sentinel(hermes_home):
    estop.engage()
    assert estop.disengage() is True
    assert not (hermes_home / "ESTOP").exists()
    assert estop.is_engaged() is False
    # Disengaging when not engaged is a no-op that reports False.
    assert estop.disengage() is False


@pytest.mark.linux_only
def test_disengage_anchors_a_configured_symlinked_home(tmp_path, monkeypatch):
    """A deliberate HERMES_HOME alias still cleans its own physical stop."""
    physical_home = tmp_path / "physical-home"
    physical_home.mkdir()
    configured_home = tmp_path / "configured-home"
    configured_home.symlink_to(physical_home, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(configured_home))
    estop._reset_log_state_for_tests()

    estop.engage(reason="maintenance")

    assert (physical_home / "ESTOP").is_file()
    assert estop.disengage() is True
    assert not (physical_home / "ESTOP").exists()
    assert estop.is_engaged() is False


def test_reason_and_timestamp_stored(hermes_home):
    estop.engage(reason="runaway cron fan-out")
    state = estop.get_state()
    assert state is not None
    assert state["reason"] == "runaway cron fan-out"
    assert state["engaged_at"]  # ISO timestamp string

    raw = json.loads((hermes_home / "ESTOP").read_text(encoding="utf-8"))
    assert raw["reason"] == "runaway cron fan-out"


def test_get_state_none_when_disengaged(hermes_home):
    assert estop.get_state() is None


def test_corrupt_sentinel_still_engages(hermes_home):
    """A hand-touched/corrupt ESTOP file must still pause (fail safe)."""
    (hermes_home / "ESTOP").write_text("not json", encoding="utf-8")
    assert estop.is_engaged() is True
    state = estop.get_state()
    assert state is not None
    assert state.get("reason") is None


@pytest.mark.linux_only
def test_broken_symlink_sentinel_still_engages(hermes_home):
    """A broken ESTOP link is still an authoritative stop entry."""
    sentinel = hermes_home / "ESTOP"
    sentinel.symlink_to(hermes_home / "missing-estop-target")

    assert estop.is_engaged() is True
    assert estop.get_state() == {"reason": None, "engaged_at": None}
    assert estop.check_paused("kanban", logging.getLogger(__name__)) is True


@pytest.mark.linux_only
@pytest.mark.parametrize("broken_at_home", [True, False])
def test_broken_hermes_home_ancestor_fails_closed(
    hermes_home, monkeypatch, broken_at_home
):
    """A broken HERMES_HOME path must never make ESTOP look absent."""
    broken = hermes_home / "broken-home"
    broken.symlink_to(hermes_home / "missing-home", target_is_directory=True)
    active_home = broken if broken_at_home else broken / "profiles" / "planner"
    monkeypatch.setenv("HERMES_HOME", str(active_home))

    assert estop.is_engaged() is True
    assert estop.get_state() == {"reason": None, "engaged_at": None}
    assert estop.check_paused("kanban", logging.getLogger(__name__)) is True


@pytest.mark.linux_only
def test_missing_stop_rechecks_ancestor_after_home_symlink_breaks(
    tmp_path, monkeypatch
):
    """A home link broken after the first absence check must stay paused."""
    physical_home = tmp_path / "physical-home"
    physical_home.mkdir()
    configured_home = tmp_path / "configured-home"
    configured_home.symlink_to(physical_home, target_is_directory=True)
    missing_target = tmp_path / "missing-home"
    sentinel = configured_home / "ESTOP"
    check_ancestor = estop._missing_path_has_usable_ancestor
    checks = 0

    def check_then_break(path):
        nonlocal checks
        result = check_ancestor(path)
        checks += 1
        if checks == 1:
            configured_home.unlink()
            configured_home.symlink_to(missing_target, target_is_directory=True)
        return result

    monkeypatch.setattr(
        estop,
        "_missing_path_has_usable_ancestor",
        check_then_break,
    )

    assert estop._path_is_engaged(sentinel) is True
    assert checks == 2


def test_genuinely_missing_hermes_home_below_real_parent_is_not_engaged(
    hermes_home, monkeypatch
):
    """An absent home under a real directory remains a normal no-ESTOP case."""
    monkeypatch.setenv("HERMES_HOME", str(hermes_home / "missing-home"))

    assert estop.is_engaged() is False
    assert estop.get_state() is None


def test_named_profile_uses_shared_root_estop(tmp_path, monkeypatch):
    """Every profile must read, write, and remove the same global stop."""
    root = tmp_path / "shared-root"
    profile_home = root / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    estop._reset_log_state_for_tests()

    assert estop.sentinel_path() == root / "ESTOP"
    assert estop.is_engaged() is False
    assert estop.engage(reason="global maintenance") == root / "ESTOP"
    assert (root / "ESTOP").is_file()
    assert not (profile_home / "ESTOP").exists()
    assert estop.get_state()["reason"] == "global maintenance"
    assert estop.disengage() is True
    assert not (root / "ESTOP").exists()


def test_named_profile_honors_and_removes_legacy_profile_estop(
    tmp_path, monkeypatch
):
    """An upgrade must not silently lift a profile-local existing stop."""
    root = tmp_path / "shared-root"
    profile_home = root / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    estop._reset_log_state_for_tests()
    (profile_home / "ESTOP").write_text(
        '{"reason": "legacy pause"}\n', encoding="utf-8"
    )

    assert estop.is_engaged() is True
    assert estop.get_state()["reason"] == "legacy pause"
    assert estop.disengage() is True
    assert estop.is_engaged() is False
    assert not (profile_home / "ESTOP").exists()


@pytest.mark.parametrize("active_kind", ["default", "sibling"])
def test_every_gateway_honors_and_removes_all_legacy_profile_estops(
    tmp_path, monkeypatch, active_kind
):
    """A stop left by any old profile is global after an upgrade."""
    root = tmp_path / "shared-root"
    coder_home = root / "profiles" / "coder"
    reviewer_home = root / "profiles" / "reviewer"
    planner_home = root / "profiles" / "planner"
    for profile_home in (coder_home, reviewer_home, planner_home):
        profile_home.mkdir(parents=True)
    monkeypatch.setenv(
        "HERMES_HOME",
        str(root if active_kind == "default" else planner_home),
    )
    estop._reset_log_state_for_tests()
    (coder_home / "ESTOP").write_text(
        '{"reason": "coder legacy pause"}\n', encoding="utf-8"
    )
    (reviewer_home / "ESTOP").write_text(
        '{"reason": "reviewer legacy pause"}\n', encoding="utf-8"
    )

    assert estop.is_engaged() is True
    assert estop.get_state()["reason"] == "coder legacy pause"
    assert estop.disengage() is True
    assert estop.is_engaged() is False
    assert not (coder_home / "ESTOP").exists()
    assert not (reviewer_home / "ESTOP").exists()


@pytest.mark.linux_only
def test_symlinked_profile_directory_fails_closed_without_external_delete(
    tmp_path, monkeypatch
):
    """Resume must not follow a profile symlink and unlink outside the root."""
    root = tmp_path / "shared-root"
    profiles_root = root / "profiles"
    profiles_root.mkdir(parents=True)
    outside = tmp_path / "outside-profile"
    outside.mkdir()
    external_stop = outside / "ESTOP"
    external_stop.write_text("{}\n", encoding="utf-8")
    (profiles_root / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    estop._reset_log_state_for_tests()

    assert estop.is_engaged() is True
    with pytest.raises(estop.DisengageError, match="could not be checked safely"):
        estop.disengage()
    assert external_stop.is_file()


@pytest.mark.linux_only
@pytest.mark.asyncio
async def test_gateway_pause_off_anchors_profile_during_symlink_swap(
    tmp_path, monkeypatch
):
    """Chat cleanup stays on the captured directory after a profile swap."""
    from gateway.run import GatewayRunner

    root = tmp_path / "shared-root"
    profiles_root = root / "profiles"
    profile = profiles_root / "linked"
    profile.mkdir(parents=True)
    original_stop = profile / "ESTOP"
    original_stop.write_text("{}\n", encoding="utf-8")
    outside = tmp_path / "outside-profile"
    outside.mkdir()
    external_stop = outside / "ESTOP"
    external_stop.write_text("{}\n", encoding="utf-8")
    moved_profile = profiles_root / "linked-original"

    monkeypatch.setenv("HERMES_HOME", str(root))
    estop._reset_log_state_for_tests()
    capture_targets = estop._sentinel_cleanup_targets

    def capture_then_replace():
        targets = capture_targets()
        profile.rename(moved_profile)
        profile.symlink_to(outside, target_is_directory=True)
        return targets

    monkeypatch.setattr(estop, "_sentinel_cleanup_targets", capture_then_replace)
    runner = object.__new__(GatewayRunner)

    reply = await runner._handle_pause_command(_FakePauseEvent("off"))

    assert "hermes is still paused" in reply.lower()
    assert external_stop.is_file()
    assert not (moved_profile / "ESTOP").exists()


def test_profile_redirect_detector_rejects_windows_reparse_attribute():
    """The junction attribute is unsafe even when its mode says directory."""
    junction_info = SimpleNamespace(
        st_mode=0o040000,
        st_file_attributes=0x400,
    )

    assert estop._is_unsafe_profile_redirect(junction_info) is True


def test_linux_mountinfo_path_decoder_preserves_literal_escape_text():
    encoded = r"/tmp/a\040b/actual\134040text"

    assert estop._decode_mountinfo_path(encoded) == "/tmp/a b/actual\\040text"


@pytest.mark.linux_only
@pytest.mark.parametrize(
    "mounted_kind",
    ["profile", "profiles_root", "post_anchor_profiles_root"],
)
def test_gateway_pause_off_rejects_real_bind_mounted_profiles(
    tmp_path, mounted_kind
):
    """Real profile mounts cannot make chat resume delete an outside stop."""
    unshare = shutil.which("unshare")
    mount = shutil.which("mount")
    if not unshare or not mount:
        pytest.skip("unshare and mount are required for the bind-mount proof")

    root = tmp_path / "shared-root"
    profiles_root = root / "profiles"
    profiles_root.mkdir(parents=True)
    outside = tmp_path / "outside-profile"
    outside.mkdir()
    original_stop = root / "unused-original-stop"
    if mounted_kind == "profile":
        mount_target = profiles_root / "mounted"
        mount_target.mkdir()
        external_stop = outside / "ESTOP"
    else:
        mount_target = profiles_root
        outside_profile = outside / "mounted"
        outside_profile.mkdir()
        external_stop = outside_profile / "ESTOP"
        if mounted_kind == "post_anchor_profiles_root":
            original_profile = profiles_root / "mounted"
            original_profile.mkdir()
            original_stop = original_profile / "ESTOP"
            original_stop.write_text("{}\n", encoding="utf-8")
    external_stop.write_text("{}\n", encoding="utf-8")
    child = r'''
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1])
outside = Path(sys.argv[2])
mount_target = Path(sys.argv[3])
external_stop = Path(sys.argv[5])
mounted_kind = sys.argv[6]
subprocess.run([sys.argv[4], "--make-rprivate", "/"], check=True)
if mounted_kind != "post_anchor_profiles_root":
    subprocess.run(
        [sys.argv[4], "--bind", str(outside), str(mount_target)],
        check=True,
    )
os.environ["HERMES_HOME"] = str(root)

from agent import estop
from gateway.run import GatewayRunner

if mounted_kind == "post_anchor_profiles_root":
    original_fd_mount_id = estop._fd_mount_id
    mount_id_calls = 0

    def mount_after_profiles_anchor(fd):
        global mount_id_calls
        result = original_fd_mount_id(fd)
        mount_id_calls += 1
        if mount_id_calls == 2:
            subprocess.run(
                [sys.argv[4], "--bind", str(outside), str(mount_target)],
                check=True,
            )
        return result

    estop._fd_mount_id = mount_after_profiles_anchor

class Event:
    def get_command_args(self):
        return "off"

reply = asyncio.run(
    object.__new__(GatewayRunner)._handle_pause_command(Event())
)
print(json.dumps({
    "reply": reply,
    "external_stop_present": external_stop.is_file(),
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
            str(outside),
            str(mount_target),
            mount,
            str(external_stop),
            mounted_kind,
        ],
        cwd=os.fspath(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "Operation not permitted" in result.stderr:
        pytest.skip("this Linux host disables unprivileged mount namespaces")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])

    assert "hermes is still paused" in payload["reply"].lower()
    assert payload["external_stop_present"] is True
    assert external_stop.is_file()
    if mounted_kind == "post_anchor_profiles_root":
        assert not original_stop.exists()


@pytest.mark.windows_only
@pytest.mark.asyncio
async def test_gateway_pause_off_rejects_windows_profile_junction(
    tmp_path, monkeypatch
):
    """A post-capture Windows junction cannot redirect the real chat cleanup."""
    from gateway.run import GatewayRunner

    root = tmp_path / "shared-root"
    profiles_root = root / "profiles"
    profile = profiles_root / "linked"
    profile.mkdir(parents=True)
    original_stop = profile / "ESTOP"
    original_stop.write_text("{}\n", encoding="utf-8")
    outside = tmp_path / "outside-profile"
    outside.mkdir()
    external_stop = outside / "ESTOP"
    external_stop.write_text("{}\n", encoding="utf-8")
    moved_profile = profiles_root / "linked-original"

    monkeypatch.setenv("HERMES_HOME", str(root))
    estop._reset_log_state_for_tests()
    capture_targets = estop._sentinel_cleanup_targets

    def capture_then_replace():
        targets = capture_targets()
        profile.rename(moved_profile)
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(profile), str(outside)],
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr.decode(
            "utf-8", errors="replace"
        )
        return targets

    monkeypatch.setattr(estop, "_sentinel_cleanup_targets", capture_then_replace)
    runner = object.__new__(GatewayRunner)

    reply = await runner._handle_pause_command(_FakePauseEvent("off"))

    assert "hermes is still paused" in reply.lower()
    assert external_stop.is_file()
    assert (moved_profile / "ESTOP").is_file()
    assert estop.is_engaged() is True


# ── paused notice for new gateway turns ─────────────────────────────────────


def test_paused_reply_none_when_disengaged(hermes_home):
    assert estop.paused_reply() is None


def test_paused_reply_surfaces_reason_and_resume_hint(hermes_home):
    estop.engage(reason="deploy window")
    notice = estop.paused_reply()
    assert notice is not None
    assert "paused" in notice.lower()
    assert "deploy window" in notice
    assert "hermes resume" in notice


def test_paused_reply_without_reason(hermes_home):
    estop.engage()
    notice = estop.paused_reply()
    assert notice is not None
    assert "paused" in notice.lower()
    assert "hermes resume" in notice


# ── check_paused: cheap gate + log-once ─────────────────────────────────────


def test_check_paused_logs_once_per_engagement(hermes_home, caplog):
    logger = logging.getLogger("test.estop.component")
    estop.engage()
    with caplog.at_level(logging.INFO, logger=logger.name):
        assert estop.check_paused("cron", logger) is True
        assert estop.check_paused("cron", logger) is True
        assert estop.check_paused("cron", logger) is True
    paused_logs = [r for r in caplog.records if "paused" in r.getMessage().lower()]
    assert len(paused_logs) == 1

    # Resume then re-engage → logs once more (transition-based, not forever).
    caplog.clear()
    estop.disengage()
    with caplog.at_level(logging.INFO, logger=logger.name):
        assert estop.check_paused("cron", logger) is False
        estop.engage()
        assert estop.check_paused("cron", logger) is True
        assert estop.check_paused("cron", logger) is True
    paused_logs = [r for r in caplog.records if "paused" in r.getMessage().lower()]
    assert len(paused_logs) == 1


# ── cron scheduler integration ──────────────────────────────────────────────


def test_cron_tick_skips_dispatch_when_engaged(hermes_home, monkeypatch):
    from cron import scheduler

    calls = []

    def _fake_get_due_jobs():
        calls.append(1)
        return []

    monkeypatch.setattr(scheduler, "get_due_jobs", _fake_get_due_jobs)

    estop.engage(reason="test")
    assert scheduler.tick(verbose=False) == 0
    assert calls == [], "engaged ESTOP must skip the due-job scan entirely"


def test_cron_tick_resumes_after_disengage(hermes_home, monkeypatch):
    from cron import scheduler

    calls = []

    def _fake_get_due_jobs():
        calls.append(1)
        return []

    monkeypatch.setattr(scheduler, "get_due_jobs", _fake_get_due_jobs)

    estop.engage()
    scheduler.tick(verbose=False)
    assert calls == []

    estop.disengage()
    scheduler.tick(verbose=False)
    assert calls == [1], "resume must restore normal cron dispatch"


# ── kanban dispatcher integration ───────────────────────────────────────────


def test_kanban_dispatch_blocked_when_engaged(hermes_home):
    from gateway.kanban_watchers import _kanban_dispatch_allowed

    assert _kanban_dispatch_allowed() is True
    estop.engage(reason="test")
    assert _kanban_dispatch_allowed() is False
    estop.disengage()
    assert _kanban_dispatch_allowed() is True


# ── gateway turn-start integration ──────────────────────────────────────────


class _FakeSource:
    platform = None
    chat_id = "c1"
    user_id = "u1"
    user_name = "user"
    chat_type = "dm"
    profile = None


class _FakeEvent:
    internal = False
    text = "hello"

    def __init__(self):
        self.source = _FakeSource()


@pytest.mark.asyncio
async def test_gateway_new_turn_gets_paused_reply(hermes_home):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = lambda source: True  # bare-instance stub
    estop.engage(reason="maintenance")
    reply = await runner._handle_message(_FakeEvent())
    assert reply is not None
    assert "paused" in reply.lower()
    assert "maintenance" in reply


@pytest.mark.asyncio
async def test_gateway_internal_events_bypass_estop(hermes_home):
    """Internal events (in-flight work completions) must NOT be paused."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    estop.engage()
    event = _FakeEvent()
    event.internal = True
    # An internal event proceeds past the estop gate; the bare runner then
    # blows up further down the pipeline on missing attributes — that error
    # (anything but a paused reply) proves the gate let it through.
    try:
        reply = await runner._handle_message(event)
    except Exception:
        return
    assert reply is None or "paused" not in (reply or "").lower()


# ── CLI: hermes pause / hermes resume ───────────────────────────────────────


def test_cli_pause_engages_with_reason(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    rc = cmd_pause(argparse.Namespace(reason="ops incident"))
    assert rc == 0
    assert estop.is_engaged() is True
    assert estop.get_state()["reason"] == "ops incident"
    assert "paused" in capsys.readouterr().out.lower()


def test_cli_pause_idempotent(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    assert cmd_pause(argparse.Namespace(reason=None)) == 0
    assert cmd_pause(argparse.Namespace(reason=None)) == 0
    assert estop.is_engaged() is True


def test_cli_pause_reports_creation_failure(hermes_home, monkeypatch, capsys):
    from hermes_cli.subcommands.pause import cmd_pause

    def failed_engage(reason=None):
        raise estop.EngageError("the shared stop file could not be created")

    monkeypatch.setattr(estop, "engage", failed_engage)

    rc = cmd_pause(argparse.Namespace(reason="maintenance"))

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "was not paused" in captured.err.lower()


def test_cli_resume_disengages(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_pause, cmd_resume

    cmd_pause(argparse.Namespace(reason=None))
    rc = cmd_resume(argparse.Namespace())
    assert rc == 0
    assert estop.is_engaged() is False
    assert "resumed" in capsys.readouterr().out.lower()


def test_cli_resume_when_not_paused(hermes_home, capsys):
    from hermes_cli.subcommands.pause import cmd_resume

    rc = cmd_resume(argparse.Namespace())
    assert rc == 0
    assert "not paused" in capsys.readouterr().out.lower()


@pytest.mark.linux_only
def test_cli_resume_reports_an_unsafe_profile_layout_as_still_paused(
    tmp_path, monkeypatch, capsys
):
    from hermes_cli.subcommands.pause import cmd_resume

    root = tmp_path / "shared-root"
    profiles_root = root / "profiles"
    profiles_root.mkdir(parents=True)
    outside = tmp_path / "outside-profile"
    outside.mkdir()
    external_stop = outside / "ESTOP"
    external_stop.write_text("{}\n", encoding="utf-8")
    (profiles_root / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    rc = cmd_resume(argparse.Namespace())

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "still paused" in captured.err.lower()
    assert "could not be checked safely" in captured.err.lower()
    assert estop.is_engaged() is True
    assert external_stop.is_file()


def test_builtin_subcommands_include_pause_resume():
    from hermes_cli.main import _BUILTIN_SUBCOMMANDS

    assert "pause" in _BUILTIN_SUBCOMMANDS
    assert "resume" in _BUILTIN_SUBCOMMANDS


# ── hermes status surfacing ─────────────────────────────────────────────────


def test_status_line_when_paused(hermes_home):
    from hermes_cli.status import _estop_status_line

    assert _estop_status_line() is None
    estop.engage(reason="ops")
    line = _estop_status_line()
    assert line is not None
    assert "paused" in line.lower()
    assert "ops" in line
    estop.disengage()
    assert _estop_status_line() is None


# ── post-merge audit fixes (#81148 follow-up) ───────────────────────────────


def test_is_engaged_fails_safe_on_stat_error(hermes_home, monkeypatch):
    """A stat failure must report ENGAGED (fail safe) — the pause has to
    hold even when HERMES_HOME is misbehaving, matching the module's
    corrupt-sentinel doctrine."""
    sentinel = hermes_home / "ESTOP"
    real_lstat = estop.os.lstat

    def denied_lstat(path, *args, **kwargs):
        if path == sentinel:
            raise PermissionError("permission denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(estop.os, "lstat", denied_lstat)
    assert estop.is_engaged() is True
    assert estop.get_state() == {"reason": None, "engaged_at": None}


class _FakeCmdEvent(_FakeEvent):
    text = "/status"

    def get_command(self):
        return "status"

    def get_command_args(self):
        return ""


@pytest.mark.asyncio
async def test_gateway_slash_commands_bypass_estop(hermes_home):
    """Recognized slash commands must pass the estop gate — /pause off is
    the in-band resume path for messaging-only users, and /status, /help
    and friends must keep working while paused."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = lambda source: True
    estop.engage(reason="maintenance")
    # The command proceeds past the estop gate; the bare runner then blows
    # up further down on missing attributes — anything but the paused
    # notice proves the gate let it through.
    try:
        reply = await runner._handle_message(_FakeCmdEvent())
    except Exception:
        return
    assert reply is None or "hermes is paused" not in (reply or "").lower()


class _FakePauseEvent(_FakeEvent):
    def __init__(self, args=""):
        super().__init__()
        self._args = args
        self.text = f"/pause {args}".strip()

    def get_command(self):
        return "pause"

    def get_command_args(self):
        return self._args


@pytest.mark.asyncio
async def test_gateway_pause_command_engages_and_resumes(hermes_home):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)

    reply = await runner._handle_pause_command(_FakePauseEvent("deploy window"))
    assert "paused" in reply.lower()
    assert estop.is_engaged() is True
    assert estop.get_state()["reason"] == "deploy window"

    # Re-issuing without args reports already-paused instead of clobbering.
    reply = await runner._handle_pause_command(_FakePauseEvent(""))
    assert "already paused" in reply.lower()

    reply = await runner._handle_pause_command(_FakePauseEvent("off"))
    assert "resumed" in reply.lower()
    assert estop.is_engaged() is False

    reply = await runner._handle_pause_command(_FakePauseEvent("off"))
    assert "wasn't paused" in reply.lower()


@pytest.mark.asyncio
async def test_gateway_pause_command_reports_creation_failure(
    hermes_home, monkeypatch
):
    from gateway.run import GatewayRunner

    def failed_engage(reason=None):
        raise estop.EngageError("the shared stop file could not be created")

    monkeypatch.setattr(estop, "engage", failed_engage)
    runner = object.__new__(GatewayRunner)

    reply = await runner._handle_pause_command(_FakePauseEvent("maintenance"))

    assert "was not paused" in reply.lower()
    assert "new work may still be accepted" in reply.lower()


@pytest.mark.asyncio
async def test_gateway_pause_off_reports_unsafe_cleanup(hermes_home, monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    estop.engage(reason="maintenance")

    def unsafe_disengage():
        raise estop.DisengageError("the stop paths could not be checked safely")

    monkeypatch.setattr(estop, "disengage", unsafe_disengage)

    reply = await runner._handle_pause_command(_FakePauseEvent("off"))

    assert reply == (
        "⏸️ Hermes is still paused — "
        "the stop paths could not be checked safely."
    )
    assert estop.is_engaged() is True


def test_pause_command_registered_for_gateway():
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command

    cmd = resolve_command("pause")
    assert cmd is not None and cmd.name == "pause"
    assert "pause" in GATEWAY_KNOWN_COMMANDS
    # Must be dispatchable while an agent is running (in-band emergency stop).
    assert cmd.busy_policy == "dispatch"
