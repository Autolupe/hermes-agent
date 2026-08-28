"""Claim guard + worktree ownership: kanban workers never work on each other.

Covers the dispatcher-side pieces of the worker-isolation plan:

- a worktree locked by a LIVE hermes pid defers the card (``workspace_busy``)
  without counting a failure; a DEAD pid's lock is released and spawn proceeds
- two live cards on one branch → ``branch_conflict`` (defer)
- overlapping ``scope-paths`` against a running card → ``scope_overlap``
  (defer); disjoint scopes both spawn
- ``kanban.branch_pattern`` violations and protected paths auto-block
- ``enforce_max_runtime`` never releases a claim while the worker is alive
- new task branches are cut from ``origin/main``, not the primary's HEAD
- ``dispatch_kwargs_from_config`` is the single cap resolver
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_scope as ks


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(*args: str, cwd: Path | str | None = None, env: dict | None = None) -> str:
    result = subprocess.run(
        [
            "git",
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, env=env,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project checkout with a bare ``origin`` whose ``main`` is pushed."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-b", "main", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("checkout", "-b", "main", cwd=project)
    (project / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", "README.md", cwd=project)
    _git("commit", "-m", "init", cwd=project)
    _git("push", "-u", "origin", "main", cwd=project)
    return project


@pytest.fixture
def worktree_board(kanban_home: Path, repo: Path, all_assignees_spawnable) -> Path:
    kb.write_board_metadata(None, default_workdir=str(repo))
    return repo


def _lock_reason(repo: Path, path: Path) -> str | None:
    out = _git("worktree", "list", "--porcelain", cwd=repo)
    current = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):]).resolve()
        elif line.startswith("locked") and current == path.resolve():
            return line
    return None


def _live_child() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _stub_spawn(spawns: list):
    def _spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return 4242
    return _spawn


def _scope_body(*paths: str) -> str:
    return "Do the thing.\n\n```scope-paths\n" + "\n".join(paths) + "\n```\n"


# ---------------------------------------------------------------------------
# kanban_scope
# ---------------------------------------------------------------------------


def test_extract_scope_paths_parses_block_and_rejects_escapes():
    body = _scope_body("backend/app/", "# a comment", "tests/test_x.py  # trailing")
    assert ks.extract_scope_paths(body) == ["backend/app/", "tests/test_x.py"]
    assert ks.extract_scope_paths("no block here") is None
    assert ks.extract_scope_paths(None) is None
    with pytest.raises(ValueError):
        ks.extract_scope_paths(_scope_body("/etc/passwd"))
    with pytest.raises(ValueError):
        ks.extract_scope_paths(_scope_body("../other-repo/"))
    with pytest.raises(ValueError):
        ks.extract_scope_paths("```scope-paths\n# only comments\n```")


def test_paths_overlap_prefix_semantics():
    assert ks.paths_overlap(["backend/app/"], ["backend/app/x.py"]) == "backend/app/x.py"
    assert ks.paths_overlap(["backend/app/x.py"], ["backend/app/"]) == "backend/app/x.py"
    assert ks.paths_overlap(["backend/app/"], ["backend/apple/"]) is None
    assert ks.paths_overlap(["backend/"], None) is None


def test_load_protected_prefixes_reads_claims_file_and_falls_back(tmp_path: Path):
    assert ".github/" in ks.load_protected_prefixes(tmp_path)  # no file → fallback
    claims = tmp_path / "ops" / "autonomy"
    claims.mkdir(parents=True)
    (claims / "path-claims.json").write_text(
        '{"protected_prefixes": ["deploy/"], "lanes": {"hermes": {"exclude": ["backend/deploy/"]}}}',
        encoding="utf-8",
    )
    assert ks.load_protected_prefixes(tmp_path) == ["deploy/", "backend/deploy/"]


# ---------------------------------------------------------------------------
# Worktree lock = process liveness
# ---------------------------------------------------------------------------


def test_live_foreign_lock_defers_without_failure(worktree_board: Path):
    repo = worktree_board
    child = _live_child()
    try:
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="locked", assignee="alice", workspace_kind="worktree")
            target = repo / ".worktrees" / tid
            kb._ensure_git_worktree(repo, target, f"wt/{tid}")
            _git("worktree", "lock", "--reason", f"hermes pid={child.pid}", str(target), cwd=repo)

            spawns: list = []
            res = kb.dispatch_once(conn, spawn_fn=_stub_spawn(spawns), max_spawn=1)
            assert spawns == []
            assert res.claim_guarded == [(tid, f"workspace_busy:{child.pid}")]
            assert res.auto_blocked == []
            task = kb.get_task(conn, tid)
            assert task.status == "ready"
            assert task.consecutive_failures == 0
            assert task.worker_pid is None
            kinds = [e.kind for e in kb.list_events(conn, tid)]
            assert "workspace_busy" in kinds
            # The foreign lock is untouched.
            assert f"hermes pid={child.pid}" in (_lock_reason(repo, target) or "")
    finally:
        child.kill()
        child.wait()


def test_dead_pid_lock_is_released_and_spawn_proceeds(worktree_board: Path):
    repo = worktree_board
    dead = _dead_pid()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="stale lock", assignee="alice", workspace_kind="worktree")
        target = repo / ".worktrees" / tid
        kb._ensure_git_worktree(repo, target, f"wt/{tid}")
        _git("worktree", "lock", "--reason", f"hermes pid={dead}", str(target), cwd=repo)

        spawns: list = []
        res = kb.dispatch_once(conn, spawn_fn=_stub_spawn(spawns), max_spawn=1)
        assert [s[0] for s in spawns] == [tid]
        assert res.claim_guarded == []
        assert _lock_reason(repo, target) is None
        assert kb.get_task(conn, tid).status == "running"


def test_unlock_worktree_if_ours_refuses_live_foreign_lock(repo: Path):
    target = repo / ".worktrees" / "t_lock"
    kb._ensure_git_worktree(repo, target, "wt/t_lock")
    child = _live_child()
    try:
        _git("worktree", "lock", "--reason", f"hermes pid={child.pid}", str(target), cwd=repo)
        assert kb._worktree_lock_pid(repo, target) == child.pid
        assert kb._unlock_worktree_if_ours(repo, target, os.getpid()) is False
        assert _lock_reason(repo, target) is not None
        assert kb._unlock_worktree_if_ours(repo, target, child.pid) is True
        assert _lock_reason(repo, target) is None
    finally:
        child.kill()
        child.wait()


def test_complete_task_releases_worker_lock(worktree_board: Path):
    repo = worktree_board
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="finish", assignee="alice", workspace_kind="worktree")
        spawns: list = []
        kb.dispatch_once(conn, spawn_fn=_stub_spawn(spawns), max_spawn=1)
        target = repo / ".worktrees" / tid
        assert target.is_dir()
        # Simulate what _default_spawn does after Popen.
        assert kb._lock_worktree_for_pid(target, 4242) is True
        assert "hermes pid=4242" in (_lock_reason(repo, target) or "")
        assert kb.complete_task(conn, tid, result="done") is True
        assert _lock_reason(repo, target) is None


# ---------------------------------------------------------------------------
# Claim guard: branch + scope rules
# ---------------------------------------------------------------------------


def test_same_branch_on_two_cards_defers_the_younger(worktree_board: Path):
    with kb.connect() as conn:
        first = kb.create_task(
            conn, title="a", assignee="alice", workspace_kind="worktree",
            branch_name="hermes/x/t_00000001",
        )
        second = kb.create_task(
            conn, title="b", assignee="bob", workspace_kind="worktree",
            branch_name="hermes/x/t_00000001",
        )
        conn.execute("UPDATE tasks SET created_at = created_at + 1 WHERE id = ?", (second,))
        conn.commit()
        spawns: list = []
        res = kb.dispatch_once(
            conn, spawn_fn=_stub_spawn(spawns), max_spawn=2, max_in_progress=2,
        )
        assert [s[0] for s in spawns] == [first]
        assert res.claim_guarded == [(second, f"branch_conflict:{first}")]
        assert kb.get_task(conn, second).status == "ready"
        assert kb.get_task(conn, second).consecutive_failures == 0
        assert any(e.kind == "claim_guarded" for e in kb.list_events(conn, second))


def test_scope_overlap_with_running_card_defers(kanban_home, all_assignees_spawnable):
    with kb.connect() as conn:
        running = kb.create_task(
            conn, title="busy", assignee="alice", body=_scope_body("backend/app/"),
        )
        assert kb.claim_task(conn, running) is not None
        waiting = kb.create_task(
            conn, title="overlaps", assignee="bob",
            body=_scope_body("backend/app/services/x.py"),
        )
        spawns: list = []
        res = kb.dispatch_once(
            conn, spawn_fn=_stub_spawn(spawns), max_spawn=2, max_in_progress=2,
        )
        assert spawns == []
        assert res.claim_guarded == [
            (waiting, f"scope_overlap:{running}:backend/app/services/x.py"),
        ]
        assert kb.get_task(conn, waiting).status == "ready"


def test_disjoint_scopes_both_spawn(kanban_home, all_assignees_spawnable):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="a", assignee="alice", body=_scope_body("backend/app/"))
        b = kb.create_task(conn, title="b", assignee="bob", body=_scope_body("frontend/src/"))
        spawns: list = []
        res = kb.dispatch_once(
            conn, spawn_fn=_stub_spawn(spawns), max_spawn=2, max_in_progress=2,
        )
        assert sorted(s[0] for s in spawns) == sorted([a, b])
        assert res.claim_guarded == []


def test_overlapping_scopes_in_one_tick_spawn_only_one(kanban_home, all_assignees_spawnable):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="a", assignee="alice", body=_scope_body("backend/app/"))
        b = kb.create_task(conn, title="b", assignee="bob", body=_scope_body("backend/app/x.py"))
        spawns: list = []
        res = kb.dispatch_once(
            conn, spawn_fn=_stub_spawn(spawns), max_spawn=2, max_in_progress=2,
        )
        assert len(spawns) == 1
        assert len(res.claim_guarded) == 1
        assert {spawns[0][0], res.claim_guarded[0][0]} == {a, b}
        assert res.claim_guarded[0][1].startswith("scope_overlap:")


def test_bad_branch_with_pattern_auto_blocks(kanban_home, all_assignees_spawnable, monkeypatch):
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config_readonly",
        lambda: {"kanban": {"branch_pattern": r"^hermes/[a-z0-9-]+/t_[0-9a-f]{8}(-|$)"}},
    )
    with kb.connect() as conn:
        bad = kb.create_task(
            conn, title="bad branch", assignee="alice", workspace_kind="worktree",
            branch_name="clauseye-production-readiness/thing",
        )
        assert kb.check_claim_guard(conn, bad) == "branch_policy"
        spawns: list = []
        res = kb.dispatch_once(conn, spawn_fn=_stub_spawn(spawns), max_spawn=1)
        assert spawns == []
        assert res.auto_blocked == [bad]
        task = kb.get_task(conn, bad)
        assert task.status == "blocked"
        assert task.consecutive_failures == 0
        ev = next(e for e in kb.list_events(conn, bad) if e.kind == "blocked")
        assert ev.payload["kind"] == "capability"
        assert "branch_policy" in ev.payload["reason"]


def test_branch_pattern_unset_skips_policy(kanban_home, monkeypatch):
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(cfgmod, "load_config_readonly", lambda: {"kanban": {}})
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="legacy", assignee="alice", workspace_kind="worktree",
            branch_name="anything/goes",
        )
        assert kb.check_claim_guard(conn, tid) is None


def test_protected_path_auto_blocks(kanban_home, all_assignees_spawnable):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="touch ci", assignee="alice",
            body=_scope_body(".github/workflows/ci.yml"),
        )
        assert kb.check_claim_guard(conn, tid) == "protected_path:.github/"
        spawns: list = []
        res = kb.dispatch_once(conn, spawn_fn=_stub_spawn(spawns), max_spawn=1)
        assert spawns == []
        assert res.auto_blocked == [tid]
        assert kb.get_task(conn, tid).status == "blocked"


def test_dry_run_reports_guard_without_mutating(kanban_home, all_assignees_spawnable):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="touch ci", assignee="alice",
            body=_scope_body("AGENTS.md"),
        )
        res = kb.dispatch_once(conn, dry_run=True, max_spawn=1)
        assert res.spawned == []
        assert res.auto_blocked == []
        assert res.claim_guarded == [(tid, "protected_path:AGENTS.md")]
        assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# enforce_max_runtime: never release a claim while the worker lives
# ---------------------------------------------------------------------------


def _backdate_run(conn, tid: str, seconds: int = 30) -> None:
    old = int(time.time()) - seconds
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, tid))
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (old, tid),
        )


def test_enforce_max_runtime_keeps_row_running_while_pid_survives(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(kb.time, "sleep", lambda _s: None)
    monkeypatch.setattr(kb, "_cleanup_worker_tmux", lambda conn, tid: None)
    sent: list = []
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="immortal", assignee="alice", max_runtime_seconds=1)
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, os.getpid())
        _backdate_run(conn, tid)
        timed_out = kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: sent.append(sig))
        assert timed_out == []
        assert len(sent) == 2  # SIGTERM then SIGKILL
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.worker_pid == os.getpid()
        assert task.consecutive_failures == 0
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "kill_pending" in kinds
        assert "timed_out" not in kinds


def test_enforce_max_runtime_flips_row_once_pid_is_gone(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    cleaned: list = []
    monkeypatch.setattr(kb, "_cleanup_worker_tmux", lambda conn, tid: cleaned.append(tid))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="slow", assignee="alice", max_runtime_seconds=1)
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, os.getpid())
        _backdate_run(conn, tid)
        assert kb.enforce_max_runtime(conn, signal_fn=lambda *_: None) == [tid]
        assert cleaned == [tid]
        assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# Worktree base: origin/main, not the primary's HEAD
# ---------------------------------------------------------------------------


def test_new_worktree_is_based_on_origin_main(repo: Path):
    (repo / "second.txt").write_text("two\n", encoding="utf-8")
    _git("add", "second.txt", cwd=repo)
    _git("commit", "-m", "second", cwd=repo)
    _git("push", "origin", "main", cwd=repo)
    origin_main = _git("rev-parse", "origin/main", cwd=repo).strip()
    # Primary HEAD drifts back one commit (like a stale ``pr650`` checkout).
    _git("reset", "--hard", "HEAD~1", cwd=repo)
    assert _git("rev-parse", "HEAD", cwd=repo).strip() != origin_main

    target = repo / ".worktrees" / "t_base"
    assert kb._ensure_git_worktree(repo, target, "wt/t_base") is False
    assert _git("rev-parse", "HEAD", cwd=target).strip() == origin_main


def test_new_worktree_falls_back_to_head_without_origin_main(tmp_path: Path):
    project = tmp_path / "solo"
    _git("init", "-b", "main", str(project))
    (project / "a.txt").write_text("a\n", encoding="utf-8")
    _git("add", "a.txt", cwd=project)
    _git("commit", "-m", "init", cwd=project)
    target = project / ".worktrees" / "t_solo"
    assert kb._ensure_git_worktree(project, target, "wt/t_solo") is True
    assert _git("rev-parse", "HEAD", cwd=target).strip() == _git("rev-parse", "HEAD", cwd=project).strip()


def test_worktree_add_exports_branch_switch_override(repo: Path, monkeypatch):
    seen: list = []
    real_run = kb.subprocess.run

    def _spy(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "worktree" in cmd and "add" in cmd:
            seen.append(kwargs.get("env") or {})
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(kb.subprocess, "run", _spy)
    kb._ensure_git_worktree(repo, repo / ".worktrees" / "t_env", "wt/t_env")
    assert seen and seen[0].get("ALLOW_BRANCH_SWITCH") == "1"


def test_dispatch_records_base_fallback_event(kanban_home, all_assignees_spawnable, tmp_path):
    project = tmp_path / "solo"
    _git("init", "-b", "main", str(project))
    (project / "a.txt").write_text("a\n", encoding="utf-8")
    _git("add", "a.txt", cwd=project)
    _git("commit", "-m", "init", cwd=project)
    kb.write_board_metadata(None, default_workdir=str(project))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="solo", assignee="alice", workspace_kind="worktree")
        spawns: list = []
        kb.dispatch_once(conn, spawn_fn=_stub_spawn(spawns), max_spawn=1)
        assert [s[0] for s in spawns] == [tid]
        assert any(e.kind == "worktree_base_fallback" for e in kb.list_events(conn, tid))


# ---------------------------------------------------------------------------
# One cap resolver
# ---------------------------------------------------------------------------


def test_dispatch_kwargs_from_config(monkeypatch):
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config_readonly",
        lambda: {"kanban": {
            "max_in_progress": 2, "max_in_progress_per_profile": "1", "max_spawn": 0,
            "failure_limit": 3, "dispatch_stale_timeout_seconds": "90",
            "default_assignee": " planner ", "reconcile_orphans": False,
        }},
    )
    kwargs = kb.dispatch_kwargs_from_config()
    assert kwargs == {
        "max_spawn": None,
        "max_in_progress": 2,
        "failure_limit": 3,
        "stale_timeout_seconds": 90,
        "default_assignee": "planner",
        "max_in_progress_per_profile": 1,
        "reconcile_orphans": False,
    }
    monkeypatch.setattr(cfgmod, "load_config_readonly", lambda: {})
    defaults = kb.dispatch_kwargs_from_config(board="ops")
    assert defaults["failure_limit"] == kb.DEFAULT_FAILURE_LIMIT
    assert defaults["default_assignee"] is None
    assert defaults["reconcile_orphans"] is True
    assert defaults["max_in_progress"] == kb.resolve_max_in_progress(None)


def test_default_spawn_sets_git_identity(kanban_home, monkeypatch, tmp_path):
    captured: dict = {}

    class _Proc:
        pid = 777

    def _popen(cmd, **kwargs):
        captured.update(kwargs.get("env") or {})
        return _Proc()

    monkeypatch.setattr(kb.subprocess, "Popen", _popen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="attrib", assignee="backend")
        task = kb.get_task(conn, tid)
    assert kb._default_spawn(task, str(tmp_path)) == 777
    assert captured["GIT_AUTHOR_NAME"] == "hermes-backend"
    assert captured["GIT_COMMITTER_NAME"] == "hermes-backend"
    assert captured["GIT_AUTHOR_EMAIL"] == "hermes-backend@clauseye.local"
    assert captured["GIT_COMMITTER_EMAIL"] == "hermes-backend@clauseye.local"


# ---------------------------------------------------------------------------
# Fix round: review findings (glob scopes, empty branches, conflicts, locks)
# ---------------------------------------------------------------------------


_PATTERN = r"^hermes/[a-z0-9-]+/t_[0-9a-f]{8}(-|$)"


def _set_pattern(monkeypatch, pattern=_PATTERN):
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config_readonly", lambda: {"kanban": {"branch_pattern": pattern}},
    )


def _branch_spawn(spawns: list, pid: int = 4242):
    def _spawn(task, workspace, board=None):
        spawns.append((task.id, workspace, task.branch_name))
        return pid
    return _spawn


@pytest.mark.parametrize("bad", ["backend/**", ".github/*", "src/?.py", "a b/c", "a\\b"])
def test_glob_scope_paths_are_rejected(bad: str):
    with pytest.raises(ValueError):
        ks.normalize_scope_path(bad)
    with pytest.raises(ValueError):
        ks.extract_scope_paths(_scope_body(bad))


def test_glob_scope_blocks_instead_of_passing(kanban_home, all_assignees_spawnable):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="glob", assignee="alice", workspace_kind="worktree",
            body=_scope_body("backend/**"),
        )
        assert kb.check_claim_guard(conn, tid) == "protected_path:invalid-scope"


def test_empty_branch_is_not_a_policy_violation(worktree_board: Path, monkeypatch):
    _set_pattern(monkeypatch)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="alice", workspace_kind="worktree")
        assert kb.get_task(conn, tid).branch_name in (None, "")
        assert kb.check_claim_guard(conn, tid) is None
        spawns: list = []
        res = kb.dispatch_once(conn, spawn_fn=_branch_spawn(spawns), max_spawn=1)
        assert res.auto_blocked == []
        expected = f"hermes/{kb.get_current_board()}/{tid}"
        assert spawns == [(tid, str(worktree_board / ".worktrees" / tid), expected)]
        assert kb.get_task(conn, tid).branch_name == expected
        assert kb.get_task(conn, tid).status == "running"
        # The derived name satisfies the policy the host enforces.
        import re
        assert re.search(_PATTERN, expected)


def test_default_spawn_exports_branch_for_derived_name(kanban_home, monkeypatch, tmp_path):
    captured: dict = {}

    class _Proc:
        pid = 778

    def _popen(cmd, **kwargs):
        captured.update(kwargs.get("env") or {})
        return _Proc()

    monkeypatch.setattr(kb.subprocess, "Popen", _popen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_lock_worktree_for_pid", lambda *_a, **_k: True)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="env", assignee="backend", workspace_kind="worktree")
        task = kb.get_task(conn, tid)
        # What the dispatch lanes now do before handing the Task to spawn.
        kb._persist_resolved_branch(conn, task, None, board=None)
    assert kb._default_spawn(task, str(tmp_path)) == 778
    assert captured["HERMES_KANBAN_BRANCH"] == f"hermes/{kb.get_current_board()}/{tid}"


def test_older_parked_card_does_not_defer_ready_card(worktree_board: Path):
    with kb.connect() as conn:
        older = kb.create_task(
            conn, title="parked", assignee="alice", workspace_kind="worktree",
            branch_name="hermes/x/t_00000002", triage=True,
        )
        younger = kb.create_task(
            conn, title="go", assignee="bob", workspace_kind="worktree",
            branch_name="hermes/x/t_00000002",
        )
        conn.execute("UPDATE tasks SET created_at = created_at - 10 WHERE id = ?", (older,))
        conn.commit()
        assert kb.get_task(conn, older).status == "triage"
        assert kb.check_claim_guard(conn, younger) is None
        for parked in ("todo", "scheduled", "blocked"):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (parked, older))
            conn.commit()
            assert kb.check_claim_guard(conn, younger) is None, parked
        # A card that is actually working still wins regardless of age.
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (older,))
        conn.commit()
        assert kb.check_claim_guard(conn, younger) == f"branch_conflict:{older}"
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (older,))
        conn.commit()
        assert kb.check_claim_guard(conn, younger) == f"branch_conflict:{older}"


def test_redo_child_may_reuse_parent_branch(worktree_board: Path):
    with kb.connect() as conn:
        parent = kb.create_task(
            conn, title="parent", assignee="alice", workspace_kind="worktree",
            branch_name="hermes/x/t_00000003",
        )
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (parent,))
        conn.commit()
        child = kb.create_task(
            conn, title="REDO parent", assignee="alice", workspace_kind="worktree",
            branch_name="hermes/x/t_00000003", parents=[parent],
        )
        assert kb.check_claim_guard(conn, child) is None
        # An unrelated review card on the same branch still conflicts.
        stranger = kb.create_task(
            conn, title="stranger", assignee="bob", workspace_kind="worktree",
            branch_name="hermes/x/t_00000003",
        )
        assert kb.check_claim_guard(conn, stranger) == f"branch_conflict:{parent}"


def test_foreign_lock_reason_counts_as_busy(worktree_board: Path):
    repo = worktree_board
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="human lock", assignee="alice", workspace_kind="worktree")
        target = repo / ".worktrees" / tid
        kb._ensure_git_worktree(repo, target, f"hermes/default/{tid}")
        _git("worktree", "lock", "--reason", "claude session editing", str(target), cwd=repo)
        assert kb._worktree_lock_state(repo, target) == (None, None, "claude session editing")
        spawns: list = []
        res = kb.dispatch_once(conn, spawn_fn=_stub_spawn(spawns), max_spawn=1)
        assert spawns == []
        assert res.claim_guarded == [(tid, "workspace_busy:0")]
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        ev = next(e for e in kb.list_events(conn, tid) if e.kind == "workspace_busy")
        assert ev.payload["reason"] == "claude session editing"
        assert "claude session editing" in (_lock_reason(repo, target) or "")
        # Bare lock with no reason at all is busy too, and never auto-unlocked.
        _git("worktree", "unlock", str(target), cwd=repo)
        _git("worktree", "lock", str(target), cwd=repo)
        with pytest.raises(kb.WorkspaceBusyError):
            kb._assert_worktree_not_busy(repo, target)
        assert kb._unlock_worktree_if_ours(repo, target, os.getpid()) is False
        assert _lock_reason(repo, target) is not None


def test_recycled_pid_lock_is_treated_as_dead(worktree_board: Path):
    repo = worktree_board
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="recycled", assignee="alice", workspace_kind="worktree")
        target = repo / ".worktrees" / tid
        kb._ensure_git_worktree(repo, target, f"hermes/default/{tid}")
        # A live pid (ours) whose recorded start time belongs to an older,
        # long-gone process that happened to have the same number.
        _git(
            "worktree", "lock", "--reason", f"hermes pid={os.getpid()} start=1",
            str(target), cwd=repo,
        )
        assert kb._worktree_lock_state(repo, target)[:2] == (os.getpid(), 1)
        assert kb._lock_holder_alive(os.getpid(), 1) is False
        # Same pid with its real start time is genuinely alive.
        real_start = kb._pid_start_time(os.getpid())
        assert real_start is not None
        assert kb._lock_holder_alive(os.getpid(), real_start) is True
        child = _live_child()
        try:
            assert kb._unlock_worktree_if_ours(repo, target, child.pid) is True
        finally:
            child.kill()
            child.wait()
        assert _lock_reason(repo, target) is None


def test_lock_records_start_time_and_child_lock_is_live(repo: Path):
    target = repo / ".worktrees" / "t_start"
    kb._ensure_git_worktree(repo, target, "hermes/default/t_start")
    child = _live_child()
    try:
        assert kb._lock_worktree_for_pid(target, child.pid) is True
        reason = _lock_reason(repo, target) or ""
        assert f"hermes pid={child.pid} start={kb._pid_start_time(child.pid)}" in reason
        with pytest.raises(kb.WorkspaceBusyError):
            kb._assert_worktree_not_busy(repo, target)
    finally:
        child.kill()
        child.wait()
    kb._assert_worktree_not_busy(repo, target)
    assert _lock_reason(repo, target) is None


def test_block_task_keeps_lock_while_worker_is_alive(worktree_board: Path):
    repo = worktree_board
    child = _live_child()
    try:
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="stuck", assignee="alice", workspace_kind="worktree")
            spawns: list = []
            kb.dispatch_once(conn, spawn_fn=_branch_spawn(spawns, pid=child.pid), max_spawn=1)
            target = repo / ".worktrees" / tid
            assert kb._lock_worktree_for_pid(target, child.pid) is True
            # A human blocks the running card from another process.
            assert kb.block_task(conn, tid, kind="needs_input", reason="stuck") is True
            assert kb.get_task(conn, tid).status == "blocked"
            assert f"hermes pid={child.pid}" in (_lock_reason(repo, target) or "")
            # Redriving the card does not spawn beside the live worker.
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            conn.commit()
            spawns.clear()
            res = kb.dispatch_once(conn, spawn_fn=_branch_spawn(spawns), max_spawn=1)
            assert spawns == []
            assert res.claim_guarded == [(tid, f"workspace_busy:{child.pid}")]
    finally:
        child.kill()
        child.wait()
    # Once the worker is gone the lock is released on the next attempt.
    with kb.connect() as conn:
        spawns = []
        res = kb.dispatch_once(conn, spawn_fn=_branch_spawn(spawns), max_spawn=1)
        assert [s[0] for s in spawns] == [tid]
        assert res.claim_guarded == []


def test_block_task_releases_lock_of_dead_worker(worktree_board: Path):
    repo = worktree_board
    dead = _dead_pid()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="dead", assignee="alice", workspace_kind="worktree")
        spawns: list = []
        kb.dispatch_once(conn, spawn_fn=_branch_spawn(spawns, pid=dead), max_spawn=1)
        target = repo / ".worktrees" / tid
        _git("worktree", "lock", "--reason", f"hermes pid={dead}", str(target), cwd=repo)
        assert kb.block_task(conn, tid, kind="needs_input", reason="x") is True
        assert _lock_reason(repo, target) is None


def test_review_lane_busy_worktree_defers_without_failure(worktree_board: Path):
    repo = worktree_board
    child = _live_child()
    try:
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="review me", assignee="alice", workspace_kind="worktree")
            spawns: list = []
            kb.dispatch_once(conn, spawn_fn=_branch_spawn(spawns, pid=child.pid), max_spawn=1)
            target = repo / ".worktrees" / tid
            assert kb._lock_worktree_for_pid(target, child.pid) is True
            kb.request_review(conn, tid, summary="PR up", reviewer="reviewer", force=True)
            assert kb.get_task(conn, tid).status == "review"
            spawns.clear()
            for _ in range(3):
                res = kb.dispatch_once(conn, spawn_fn=_branch_spawn(spawns), max_spawn=1)
                assert spawns == []
                assert res.claim_guarded == [(tid, f"workspace_busy:{child.pid}")]
                assert res.auto_blocked == []
            task = kb.get_task(conn, tid)
            assert task.status == "review"
            assert task.consecutive_failures == 0
    finally:
        child.kill()
        child.wait()
    with kb.connect() as conn:
        spawns = []
        res = kb.dispatch_once(conn, spawn_fn=_branch_spawn(spawns), max_spawn=1)
        assert [s[0] for s in spawns] == [tid]
        assert spawns[0][2] == f"hermes/{kb.get_current_board()}/{tid}"


def test_d7_reboot_dead_pid_and_dead_lock_recover(worktree_board: Path):
    """D7: running row + dead pid + dead lock -> crashed, unlocked, respawned."""
    repo = worktree_board
    dead = _dead_pid()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rebooted", assignee="alice", workspace_kind="worktree")
        spawns: list = []
        kb.dispatch_once(conn, spawn_fn=_branch_spawn(spawns, pid=dead), max_spawn=1)
        target = repo / ".worktrees" / tid
        _git("worktree", "lock", "--reason", f"hermes pid={dead}", str(target), cwd=repo)
        assert kb.get_task(conn, tid).status == "running"
        _backdate_run(conn, tid, seconds=600)  # past the launch grace window
        assert kb.detect_crashed_workers(conn) == [tid]
        assert _lock_reason(repo, target) is None
        assert kb.get_task(conn, tid).status == "ready"
        spawns.clear()
        res = kb.dispatch_once(conn, spawn_fn=_branch_spawn(spawns), max_spawn=1)
        assert [s[0] for s in spawns] == [tid]
        assert res.claim_guarded == []
        assert kb.get_task(conn, tid).status == "running"


def test_d6_timeout_kills_real_child_and_unlocks(worktree_board: Path, monkeypatch):
    """D6: a sleeping child past max_runtime is gone, tree unlocked, row ready."""
    repo = worktree_board
    monkeypatch.setattr(kb, "_cleanup_worker_tmux", lambda conn, tid: None)
    child = _live_child()
    try:
        with kb.connect() as conn:
            tid = kb.create_task(
                conn, title="sleeper", assignee="alice", workspace_kind="worktree",
                max_runtime_seconds=1,
            )
            spawns: list = []
            kb.dispatch_once(conn, spawn_fn=_branch_spawn(spawns, pid=child.pid), max_spawn=1)
            target = repo / ".worktrees" / tid
            assert kb._lock_worktree_for_pid(target, child.pid) is True
            _backdate_run(conn, tid)
            assert kb.enforce_max_runtime(conn) == [tid]
            assert child.wait(timeout=5) is not None
            assert kb._pid_alive(child.pid) is False
            assert _lock_reason(repo, target) is None
            task = kb.get_task(conn, tid)
            assert task.status == "ready"
            assert task.worker_pid is None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_fetch_origin_main_is_cached_and_never_prompts(repo: Path, monkeypatch):
    calls: list = []

    def _run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(kb.subprocess, "run", _run)
    kb._ORIGIN_MAIN_FETCHED_AT.clear()
    kb._fetch_origin_main(repo)
    kb._fetch_origin_main(repo)
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd[-3:] == ["fetch", "origin", "main"]
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kwargs["timeout"] <= 15
