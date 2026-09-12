"""Ordinary worktree refresh refuses stale bases and pins successful results.

Every database, checkout and subprocess belongs to a temporary fixture. These
tests replace the historical fail-soft observations with the immutable-base
contract. They neither provide nor simulate required-policy approval.
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
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.syspath_prepend(str(Path.cwd()))
    kb = importlib.import_module("hermes_cli.kanban_db")
    profiles = importlib.import_module("hermes_cli.profiles")
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    # Observation-only notification is outside this fixture's dispatch boundary.
    monkeypatch.setattr(kb, "_fire_worker_spawned_hook", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "_ORIGIN_MAIN_FETCHED_AT", {})
    monkeypatch.setattr(kb, "_ORIGIN_MAIN_FETCHED_SHA", {})
    monkeypatch.setattr(kb, "_ORIGIN_MAIN_FETCH_FAILED_AT", {})
    return kb


@pytest.fixture
def repo(tmp_path, native):
    path = tmp_path / "repo"
    run_git("init", "-b", "main", str(path))
    run_git("-C", str(path), "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
            "commit", "--allow-empty", "-m", "fixture")
    run_git("-C", str(path), "update-ref", "refs/remotes/origin/main", "HEAD")
    # A real origin is required to exercise refresh. It points only at this
    # temporary local repository, including when an interception fails.
    run_git("-C", str(path), "remote", "add", "origin", str(path))
    return path


def run_git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, timeout=30)


def intercept_fetch(kb, monkeypatch, *, timeout=False, succeed=False):
    original = subprocess.run
    attempts = []

    def run(command, **kwargs):
        if command[:2] == ["git", "-C"] and command[3:4] == ["fetch"]:
            assert command[3:] == [
                "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main",
            ]
            assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
            attempts.append({"timeout": kwargs["timeout"], "repo": command[2]})
            if timeout:
                assert kwargs["timeout"] == 15
                # Real subprocess.run deadline, kill and reap; no detached child.
                return original([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            if not succeed:
                return subprocess.CompletedProcess(command, 42, "", "fixture credentials unavailable")
        return original(command, **kwargs)

    monkeypatch.setattr(kb.subprocess, "run", run)
    return attempts


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("failure", ["nonzero", "actual_timeout"])
def test_failed_fetch_refuses_stale_base_without_materialization_or_spawn(
    native, repo, tmp_path, monkeypatch, lane, failure, record_property
):
    kb = native
    monkeypatch.setattr(kb, "read_board_metadata", lambda _board: {"default_workdir": str(repo)})
    attempts = intercept_fetch(kb, monkeypatch, timeout=failure == "actual_timeout")
    stale_base = run_git("-C", str(repo), "rev-parse", "origin/main").stdout.strip()
    with contextlib.closing(kb.connect(db_path=tmp_path / "board.db")) as conn:
        tid = kb.create_task(conn, title="fixture only", assignee="default", workspace_kind="worktree")
        if lane == "review":
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
            conn.commit()
        original = kb.get_task(conn, tid)
        spawns = []
        began = time.monotonic()
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace, **kw:
                                  spawns.append((task.id, workspace)), max_spawn=1)
        elapsed = time.monotonic() - began
        task = kb.get_task(conn, tid)
        target = repo / ".worktrees" / tid
        assert len(attempts) == 1
        assert spawns == []
        assert result.spawned == []
        assert not target.exists()
        assert task.workspace_path == original.workspace_path
        assert task.branch_name == original.branch_name
        assert task.worktree_base_sha is None
        assert task.status != "running"
        assert run_git("-C", str(repo), "branch", "--list", kb.default_task_branch_name(tid)).stdout.strip() == ""
        assert run_git("-C", str(repo), "rev-parse", "origin/main").stdout.strip() == stale_base
        assert str(repo) not in kb._ORIGIN_MAIN_FETCHED_AT
        assert str(repo) not in kb._ORIGIN_MAIN_FETCHED_SHA
        assert str(repo) in kb._ORIGIN_MAIN_FETCH_FAILED_AT
        run = conn.execute("SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        assert run["ended_at"] is not None
        assert run["outcome"] == "spawn_failed"
        record_property("observation", json.dumps({"lane": lane, "failure": failure,
            "elapsed_seconds": round(elapsed, 3), "fetch_count": len(attempts),
            "materialized": False, "persisted": False, "fake_spawn_called": False,
            "failure_cooldown_recorded": True}))


def test_failed_refresh_cools_down_then_allows_a_fresh_retry(native, repo, monkeypatch):
    attempts = intercept_fetch(native, monkeypatch)
    with pytest.raises(RuntimeError, match="worktree_base_refresh_failed"):
        native._worktree_base_ref(repo)
    with pytest.raises(RuntimeError, match="cooling down"):
        native._worktree_base_ref(repo)
    assert len(attempts) == 1
    assert str(repo) not in native._ORIGIN_MAIN_FETCHED_SHA
    native._ORIGIN_MAIN_FETCH_FAILED_AT[str(repo)] -= native._ORIGIN_MAIN_FETCH_INTERVAL_SECONDS + 1
    with pytest.raises(RuntimeError, match="worktree_base_refresh_failed"):
        native._worktree_base_ref(repo)
    assert len(attempts) == 2


def test_cached_success_retains_exact_commit_when_tracking_ref_moves(native, repo, monkeypatch):
    attempts = intercept_fetch(native, monkeypatch, succeed=True)
    base = run_git("-C", str(repo), "rev-parse", "HEAD").stdout.strip()
    assert native._worktree_base_ref(repo) == (base, False)
    run_git("-C", str(repo), "checkout", "-b", "unreviewed-feature")
    run_git("-C", str(repo), "-c", "user.name=Fixture", "-c",
            "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
            "commit", "--allow-empty", "-m", "feature outside fetched main")
    feature = run_git("-C", str(repo), "rev-parse", "HEAD").stdout.strip()
    assert feature != base
    run_git("-C", str(repo), "update-ref", "refs/remotes/origin/main", feature)

    assert native._worktree_base_ref(repo) == (base, False)
    assert native._ORIGIN_MAIN_FETCHED_SHA[str(repo)] == base
    assert len(attempts) == 1


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_successful_refresh_persists_exact_base_before_callback(
    native, repo, tmp_path, monkeypatch, lane,
):
    kb = native
    monkeypatch.setattr(kb, "read_board_metadata", lambda _board: {"default_workdir": str(repo)})
    attempts = intercept_fetch(kb, monkeypatch, succeed=True)
    base = run_git("-C", str(repo), "rev-parse", "HEAD").stdout.strip()
    with contextlib.closing(kb.connect(db_path=tmp_path / "board.db")) as conn:
        tid = kb.create_task(conn, title="fixture pinned base", assignee="default", workspace_kind="worktree")
        if lane == "review":
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
            conn.commit()
        spawns = []

        def spawn(task, workspace):
            stored = kb.get_task(conn, task.id)
            assert task.worktree_base_sha == stored.worktree_base_sha == base
            assert run_git("-C", workspace, "rev-parse", "HEAD").stdout.strip() == base
            spawns.append(task.id)

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)
        assert spawns == [tid]
        assert result.spawned and result.spawned[0][0] == tid
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
