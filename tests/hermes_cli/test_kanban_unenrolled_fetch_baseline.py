"""Ordinary unenrolled behavior retained from the native 349818 baseline.

Every database, checkout and subprocess belongs to a temporary fixture. These
tests deliberately assert the current fail-soft behavior to locate the missing
required-policy boundary. They neither provide nor simulate policy approval.
"""

import contextlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest


@pytest.fixture
def native(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.syspath_prepend(str(Path.cwd()))
    kb = importlib.import_module("hermes_cli.kanban_db")
    profiles = importlib.import_module("hermes_cli.profiles")
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    # Observation-only notification is outside this fixture's dispatch boundary.
    monkeypatch.setattr(kb, "_fire_worker_spawned_hook", lambda *a, **kw: None)
    kb._ORIGIN_MAIN_FETCHED_AT.clear()
    return kb


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    run_git("init", "-b", "main", str(path))
    run_git("-C", str(path), "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
            "commit", "--allow-empty", "-m", "fixture")
    run_git("-C", str(path), "update-ref", "refs/remotes/origin/main", "HEAD")
    return path


def run_git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, timeout=30)


def intercept_fetch(kb, monkeypatch, *, timeout=False):
    original = subprocess.run
    attempts = []

    def run(command, **kwargs):
        if command[:2] == ["git", "-C"] and command[3:] == ["fetch", "origin", "main"]:
            assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
            attempts.append({"timeout": kwargs["timeout"], "repo": command[2]})
            if timeout:
                assert kwargs["timeout"] == 15
                # Real subprocess.run deadline, kill and reap; no detached child.
                return original([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            return subprocess.CompletedProcess(command, 42, "", "fixture fetch refused")
        return original(command, **kwargs)

    monkeypatch.setattr(kb.subprocess, "run", run)
    return attempts


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("failure", ["nonzero", "actual_timeout"])
def test_failed_fetch_still_materializes_persists_and_calls_spawn(
    native, repo, tmp_path, monkeypatch, lane, failure, record_property
):
    kb = native
    monkeypatch.setattr(kb, "read_board_metadata", lambda _board: {"default_workdir": str(repo)})
    attempts = intercept_fetch(kb, monkeypatch, timeout=failure == "actual_timeout")
    with contextlib.closing(kb.connect(db_path=tmp_path / "board.db")) as conn:
        tid = kb.create_task(conn, title="fixture only", assignee="default", workspace_kind="worktree")
        if lane == "review":
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
            conn.commit()
        spawns = []
        began = time.monotonic()
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace, **kw:
                                  spawns.append((task.id, workspace)), max_spawn=1)
        elapsed = time.monotonic() - began
        task = kb.get_task(conn, tid)
        target = repo / ".worktrees" / tid
        assert len(attempts) == 1
        assert spawns == [(tid, str(target))]
        assert result.spawned and result.spawned[0][0] == tid
        assert target.is_dir()
        assert task.workspace_path == str(target)
        assert task.branch_name == kb.default_task_branch_name(tid)
        assert str(repo) in kb._ORIGIN_MAIN_FETCHED_AT
        record_property("observation", json.dumps({"lane": lane, "failure": failure,
            "elapsed_seconds": round(elapsed, 3), "fetch_count": len(attempts),
            "materialized": True, "persisted": True, "fake_spawn_called": True,
            "failed_attempt_cached": True}))


def test_cached_failed_attempt_allows_second_native_base_selection(native, repo, monkeypatch):
    attempts = intercept_fetch(native, monkeypatch)
    assert native._worktree_base_ref(repo) == ("origin/main", False)
    assert native._worktree_base_ref(repo) == ("origin/main", False)
    assert len(attempts) == 1


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_existing_worktree_dispatch_never_reaches_fetch(native, repo, tmp_path, monkeypatch, lane):
    kb = native
    monkeypatch.setattr(kb, "read_board_metadata", lambda _board: {"default_workdir": str(repo)})
    with contextlib.closing(kb.connect(db_path=tmp_path / "board.db")) as conn:
        tid = kb.create_task(conn, title="fixture reuse", assignee="default", workspace_kind="worktree")
        target = repo / ".worktrees" / tid
        branch = kb.default_task_branch_name(tid)
        run_git("-C", str(repo), "worktree", "add", "-b", branch, str(target), "origin/main")
        kb.set_workspace_path(conn, tid, target)
        kb.set_branch_name(conn, tid, branch)
        if lane == "review":
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
            conn.commit()
        attempts = intercept_fetch(kb, monkeypatch)
        spawns = []
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace, **kw:
                                  spawns.append((task.id, workspace)), max_spawn=1)
        assert attempts == []
        assert spawns == [(tid, str(target))]
        assert result.spawned and result.spawned[0][0] == tid
