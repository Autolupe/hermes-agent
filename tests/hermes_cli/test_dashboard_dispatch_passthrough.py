"""Dashboard ``POST /dispatch`` must honour the shared cap resolver.

Modeled on ``test_kanban_cli_dispatch_passthrough.py``. The dashboard nudge
used to hand ``max_spawn=8`` straight to ``dispatch_once`` and ignore
``kanban.max_in_progress`` / the pause sentinel entirely, so one click
could fan out past every cap the CLI and gateway respected.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def kb():
    """Resolve ``hermes_cli.kanban_db`` at test time, not import time.

    Other test modules purge ``hermes_cli.*`` from ``sys.modules`` to re-read
    ``HERMES_HOME``; a module-level alias would then be a stale copy that the
    plugin never sees, so the patches below would silently miss.
    """
    return importlib.import_module("hermes_cli.kanban_db")


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_dispatch_test", plugin_file,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, kb):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    mod = _load_plugin_module()
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.fixture
def captured(monkeypatch, kb):
    """Stub ``dispatch_once`` and the cap resolver; record what the endpoint passes."""
    seen: dict = {}
    fake_caps = {
        "max_spawn": 2,
        "max_in_progress": 3,
        "max_in_progress_per_profile": 1,
        "failure_limit": 4,
        "default_assignee": "planner",
    }

    def fake_dispatch_once(conn, **kwargs):
        seen.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(
        kb, "dispatch_kwargs_from_config", lambda board=None: dict(fake_caps),
    )
    return seen


def test_dashboard_passes_every_configured_cap(client, captured):
    r = client.post("/api/plugins/kanban/dispatch?dry_run=true")
    assert r.status_code == 200, r.text
    assert captured["max_in_progress"] == 3
    assert captured["max_in_progress_per_profile"] == 1
    assert captured["failure_limit"] == 4
    assert captured["default_assignee"] == "planner"
    # No ?max → configured max_spawn is used, not the old hard-coded 8.
    assert captured["max_spawn"] == 2
    assert captured["dry_run"] is True
    assert "claim_guarded" in r.json()


def test_dashboard_max_is_clamped_to_configured_cap(client, captured):
    r = client.post("/api/plugins/kanban/dispatch?dry_run=true&max=8")
    assert r.status_code == 200, r.text
    assert captured["max_spawn"] == 2, "?max may only lower the cap, never raise it"

    r = client.post("/api/plugins/kanban/dispatch?dry_run=true&max=1")
    assert r.status_code == 200, r.text
    assert captured["max_spawn"] == 1


def test_dashboard_dispatch_409_when_paused_unless_dry_run(client, captured, monkeypatch, kb):
    monkeypatch.setattr(kb, "dispatch_is_paused", lambda: True)
    monkeypatch.setattr(kb, "dispatch_pause_path", lambda: Path("/x/dispatch_pause.json"))
    captured.clear()

    r = client.post("/api/plugins/kanban/dispatch")
    assert r.status_code == 409
    assert "dispatch paused: /x/dispatch_pause.json" in r.json()["detail"]
    assert captured == {}, "a paused board must not reach dispatch_once"

    r = client.post("/api/plugins/kanban/dispatch?dry_run=true")
    assert r.status_code == 200, r.text
    assert captured["dry_run"] is True
