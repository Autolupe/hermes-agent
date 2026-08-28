"""Regression tests for #33488 (CLI max_in_progress / max_spawn / per-profile
config passthrough) and #29415 (kanban_swarm humanizer skill ref).

These two fixes are bundled because they're both small, both touch the
kanban dispatcher's CLI surface, and they each guard against a silent
operator footgun that only manifests in long-running setups.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Spin up a fresh HERMES_HOME with a clean kanban DB."""
    test_home = tempfile.mkdtemp(prefix="kanban_cli_passthrough_")
    os.makedirs(os.path.join(test_home, "profiles", "default"), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    yield test_home


def test_cli_dispatch_passes_max_in_progress_from_config(isolated_kanban_home, monkeypatch):
    """#33488: hermes kanban dispatch must pass kanban.max_in_progress from
    config to dispatch_once. Without this, the global concurrency cap is
    unreachable from the CLI even though it works from the gateway."""
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    # Configure max_in_progress in the loaded config.
    fake_config = {
        "kanban": {
            "max_in_progress": 3,
            "max_spawn": 5,
            "default_assignee": "default",
            "max_in_progress_per_profile": 2,
        }
    }
    # The shared resolver reads the read-only loader, so patch that one.
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: fake_config
    )

    captured = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kanban_db.DispatchResult()

    monkeypatch.setattr(kanban_db, "dispatch_once", fake_dispatch_once)

    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False)
    kb_cli._cmd_dispatch(args)

    # Every config value must have reached dispatch_once.
    assert captured.get("max_in_progress") == 3, (
        f"CLI must pass kanban.max_in_progress from config; got {captured.get('max_in_progress')!r}"
    )
    assert captured.get("max_spawn") == 5, (
        f"CLI must pass kanban.max_spawn from config when --max is not provided; got {captured.get('max_spawn')!r}"
    )
    assert captured.get("default_assignee") == "default"
    assert captured.get("max_in_progress_per_profile") == 2


def test_cli_max_flag_overrides_config_max_spawn(isolated_kanban_home, monkeypatch):
    """--max on the CLI takes precedence over kanban.max_spawn in config.
    The CLI flag is the explicit operator signal; config is the default."""
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    fake_config = {"kanban": {"max_spawn": 10}}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: fake_config)

    captured = {}
    monkeypatch.setattr(
        kanban_db, "dispatch_once",
        lambda conn, **kw: (captured.update(kw), kanban_db.DispatchResult())[1],
    )

    args = argparse.Namespace(dry_run=True, max=2, failure_limit=2, json=False)
    kb_cli._cmd_dispatch(args)

    assert captured.get("max_spawn") == 2, (
        f"CLI --max=2 must override config kanban.max_spawn=10; got {captured.get('max_spawn')!r}"
    )




def _install_cap_resolver(monkeypatch, kanban_db, caps):
    """Point the CLI at a known cap set so the merge-with-flags contract
    can be asserted independently of the on-disk config."""
    monkeypatch.setattr(
        kanban_db, "dispatch_kwargs_from_config",
        lambda board=None: dict(caps),
    )


def test_cli_dispatch_uses_shared_cap_resolver(isolated_kanban_home, monkeypatch, capsys):
    """The CLI reads caps through kanban_db.dispatch_kwargs_from_config and
    surfaces respawn/claim guard deferrals so operators can see them."""
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    _install_cap_resolver(monkeypatch, kanban_db, {
        "max_spawn": 5, "max_in_progress": 3, "failure_limit": 7,
        "default_assignee": "planner", "max_in_progress_per_profile": 2,
    })
    captured = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        res = kanban_db.DispatchResult()
        res.respawn_guarded = [("t_aaaaaaaa", "cooldown")]
        res.claim_guarded = [("t_bbbbbbbb", "branch_conflict:t_cccccccc")]
        return res

    monkeypatch.setattr(kanban_db, "dispatch_once", fake_dispatch_once)

    args = argparse.Namespace(dry_run=True, max=None, failure_limit=None, json=True)
    assert kb_cli._cmd_dispatch(args) == 0
    assert captured["max_spawn"] == 5
    assert captured["max_in_progress"] == 3
    assert captured["failure_limit"] == 7, "config failure_limit wins when the flag is absent"
    assert captured["default_assignee"] == "planner"
    assert captured["max_in_progress_per_profile"] == 2
    import json as _json
    out = _json.loads(capsys.readouterr().out)
    assert out["claim_guarded"] == [{"task_id": "t_bbbbbbbb", "reason": "branch_conflict:t_cccccccc"}]
    assert out["respawn_guarded"] == [{"task_id": "t_aaaaaaaa", "reason": "cooldown"}]

    args = argparse.Namespace(dry_run=True, max=1, failure_limit=2, json=False)
    assert kb_cli._cmd_dispatch(args) == 0
    assert captured["max_spawn"] == 1, "explicit --max wins over config"
    assert captured["failure_limit"] == 2, "explicit --failure-limit wins over config"
    text = capsys.readouterr().out
    assert "Deferred (claim guard: branch_conflict:t_cccccccc): t_bbbbbbbb" in text
    assert "Deferred (respawn guard: cooldown): t_aaaaaaaa" in text
