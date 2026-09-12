"""Real-Git regressions for task checkout branch and immutable base selection.

Every repository, remote and Hermes board is temporary. No worker or provider
is started; the one subprocess wrapper moves a real Git ref at the boundary
between base resolution and worktree creation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# These fixtures audit workspace lifecycle and ownership without delivering code.
_WORKSPACE_AUDIT_CONTRACT = """```acceptance-contract
domain: ops
target: artifact-file
tier1:
  - cmd: "test -d ."
    expect_exit: 0
tier2:
  - "The workspace audit preserves its base, ownership and lifecycle evidence."
tier3: "Local filesystem audit complete; no repository change is delivered."
```"""


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    kb.init_db()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Worktree Contract Test",
            "-c", "user.email=worktree-test@example.invalid",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True, timeout=20,
    ).stdout.strip()


def commit(repo: Path, content: str) -> str:
    (repo / "README.md").write_text(content, encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", content.strip())
    return git(repo, "rev-parse", "HEAD")


def repository(path: Path) -> tuple[Path, str]:
    path.mkdir()
    git(path, "init", "-b", "main")
    return path, commit(path, "reviewed main\n")


def clone_with_origin(tmp_path: Path) -> tuple[Path, Path, str]:
    upstream, base = repository(tmp_path / "upstream")
    repo = tmp_path / "clone"
    git(tmp_path, "clone", str(upstream), str(repo))
    return repo, upstream, base


def test_existing_same_repository_worktree_must_match_requested_branch(tmp_path):
    repo, base = repository(tmp_path / "repo")
    target = repo / ".worktrees" / "occupied"
    git(repo, "worktree", "add", "-b", "hermes/other-task", str(target), base)
    marker = target / "unfinished.txt"
    marker.write_text("keep another task's edits\n", encoding="utf-8")

    with pytest.raises((ValueError, RuntimeError)):
        kb._ensure_git_worktree(repo, target, "hermes/requested-task")

    assert marker.read_text(encoding="utf-8") == "keep another task's edits\n"
    assert git(target, "branch", "--show-current") == "hermes/other-task"
    assert git(target, "rev-parse", "HEAD") == base
    assert git(repo, "branch", "--list", "hermes/requested-task") == ""


def test_occupied_canonical_task_path_cannot_silently_reuse_foreign_branch(tmp_path):
    repo, base = repository(tmp_path / "repo")
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="isolated task", workspace_kind="worktree",
            workspace_path=str(repo), branch_name="hermes/requested-task",
        )
        task = kb.get_task(conn, task_id)
    target = repo / ".worktrees" / task_id
    git(repo, "worktree", "add", "-b", "hermes/other-task", str(target), base)
    task.workspace_path = str(target)
    before = git(repo, "worktree", "list", "--porcelain")

    with pytest.raises((ValueError, RuntimeError)):
        kb._resolve_worktree_workspace(task)

    assert git(repo, "worktree", "list", "--porcelain") == before
    assert git(target, "branch", "--show-current") == "hermes/other-task"


def test_repository_without_origin_uses_local_main_not_feature_head(tmp_path):
    repo, base = repository(tmp_path / "repo")
    git(repo, "checkout", "-b", "unreviewed-feature")
    feature = commit(repo, "unreviewed feature\n")
    target = repo / ".worktrees" / "new-task"

    kb._ensure_git_worktree(repo, target, "hermes/new-task")

    assert git(target, "rev-parse", "HEAD") == base
    assert git(repo, "rev-parse", "HEAD") == feature
    assert git(repo, "branch", "--show-current") == "unreviewed-feature"


def test_failed_fetch_cannot_reuse_stale_origin_main(tmp_path):
    repo, _upstream, base = clone_with_origin(tmp_path)
    assert git(repo, "rev-parse", "refs/remotes/origin/main") == base
    git(repo, "remote", "set-url", "origin", str(tmp_path / "absent-remote"))
    target = repo / ".worktrees" / "new-task"

    with pytest.raises((ValueError, RuntimeError)):
        kb._ensure_git_worktree(repo, target, "hermes/new-task")

    assert not target.exists()
    assert git(repo, "branch", "--list", "hermes/new-task") == ""
    assert git(repo, "rev-parse", "refs/remotes/origin/main") == base


def test_origin_ref_movement_after_resolution_cannot_change_checkout(tmp_path, monkeypatch):
    repo, _upstream, base = clone_with_origin(tmp_path)
    git(repo, "checkout", "-b", "unreviewed-feature")
    later = commit(repo, "later unrelated feature\n")
    target = repo / ".worktrees" / "new-task"
    original_run = subprocess.run
    moved = []

    def move_ref_before_worktree_add(args, *positional, **kwargs):
        if "worktree" in args and args[args.index("worktree") + 1] == "add":
            original_run(
                ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", later],
                check=True, capture_output=True, text=True, timeout=20,
            )
            moved.append(True)
        return original_run(args, *positional, **kwargs)

    monkeypatch.setattr(kb.subprocess, "run", move_ref_before_worktree_add)
    kb._ensure_git_worktree(repo, target, "hermes/new-task")

    assert moved == [True]
    assert git(repo, "rev-parse", "refs/remotes/origin/main") == later
    assert git(target, "rev-parse", "HEAD") == base


def test_custom_fetch_mapping_cannot_select_stale_origin_main(tmp_path):
    repo, upstream, base = clone_with_origin(tmp_path)
    git(repo, "config", "remote.origin.fetch", "+refs/heads/feature:refs/remotes/origin/feature")
    fresh = commit(upstream, "fresh upstream main\n")
    assert git(repo, "rev-parse", "refs/remotes/origin/main") == base
    target = repo / ".worktrees" / "new-task"

    kb._ensure_git_worktree(repo, target, "hermes/new-task")

    assert git(repo, "rev-parse", "FETCH_HEAD") == fresh
    assert git(target, "rev-parse", "HEAD") == fresh


def test_cached_refresh_cannot_admit_a_changed_tracking_ref(tmp_path):
    repo, _upstream, base = clone_with_origin(tmp_path)
    git(repo, "checkout", "-b", "unreviewed-feature")
    feature = commit(repo, "feature outside the fetched main\n")
    first = repo / ".worktrees" / "first-task"
    kb._ensure_git_worktree(repo, first, "hermes/first-task")
    assert git(first, "rev-parse", "HEAD") == base
    git(repo, "update-ref", "refs/remotes/origin/main", feature)
    second = repo / ".worktrees" / "second-task"

    kb._ensure_git_worktree(repo, second, "hermes/second-task")

    assert git(second, "rev-parse", "HEAD") == base


@pytest.mark.parametrize("invalid", ["HEAD", "refs/heads/main", "0" * 40, "abc1234"])
def test_explicit_base_requires_full_nonzero_commit_identity(tmp_path, invalid):
    repo, _base = repository(tmp_path / "repo")
    target = repo / ".worktrees" / "new-task"

    with pytest.raises((ValueError, RuntimeError)):
        kb._ensure_git_worktree(repo, target, "hermes/new-task", base_sha=invalid)

    assert not target.exists()
    assert git(repo, "branch", "--list", "hermes/new-task") == ""


def test_missing_explicit_commit_is_refused_without_artifacts(tmp_path):
    repo, _base = repository(tmp_path / "repo")
    target = repo / ".worktrees" / "new-task"

    with pytest.raises((ValueError, RuntimeError)):
        kb._ensure_git_worktree(repo, target, "hermes/new-task", base_sha="f" * 40)

    assert not target.exists()
    assert git(repo, "branch", "--list", "hermes/new-task") == ""


def test_full_annotated_tag_object_is_not_a_commit_pin(tmp_path):
    repo, _base = repository(tmp_path / "repo")
    git(repo, "tag", "-a", "approved-looking-tag", "-m", "tag object")
    tag_object = git(repo, "rev-parse", "refs/tags/approved-looking-tag")
    assert git(repo, "cat-file", "-t", tag_object) == "tag"
    target = repo / ".worktrees" / "new-task"

    with pytest.raises((ValueError, RuntimeError)):
        kb._ensure_git_worktree(repo, target, "hermes/new-task", base_sha=tag_object)

    assert not target.exists()
    assert git(repo, "branch", "--list", "hermes/new-task") == ""


def test_explicit_commit_is_used_even_when_main_advances(tmp_path):
    repo, base = repository(tmp_path / "repo")
    later = commit(repo, "main has advanced\n")
    target = repo / ".worktrees" / "new-task"

    kb._ensure_git_worktree(repo, target, "hermes/new-task", base_sha=base)

    assert git(target, "rev-parse", "HEAD") == base
    assert git(repo, "rev-parse", "main") == later


def test_retry_accepts_progress_descended_from_the_same_pinned_base(tmp_path):
    repo, base = repository(tmp_path / "repo")
    target = repo / ".worktrees" / "new-task"
    kb._ensure_git_worktree(repo, target, "hermes/new-task", base_sha=base)
    progress = commit(target, "legitimate task progress\n")
    marker = target / "unfinished.txt"
    marker.write_text("uncommitted retry progress\n", encoding="utf-8")

    kb._ensure_git_worktree(repo, target, "hermes/new-task", base_sha=base)

    assert git(target, "rev-parse", "HEAD") == progress
    assert marker.read_text(encoding="utf-8") == "uncommitted retry progress\n"
    assert git(target, "branch", "--show-current") == "hermes/new-task"


def test_retry_refuses_branch_that_does_not_descend_from_pinned_base(tmp_path):
    repo, base = repository(tmp_path / "repo")
    target = repo / ".worktrees" / "new-task"
    git(repo, "worktree", "add", "-b", "hermes/new-task", str(target), base)
    different_base = commit(repo, "main advances beyond the old task branch\n")
    before = git(repo, "worktree", "list", "--porcelain")

    with pytest.raises((ValueError, RuntimeError)):
        kb._ensure_git_worktree(repo, target, "hermes/new-task", base_sha=different_base)

    assert git(repo, "worktree", "list", "--porcelain") == before
    assert git(target, "rev-parse", "HEAD") == base


def test_requested_base_survives_database_reopen(tmp_path):
    repo, base = repository(tmp_path / "repo")
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="pinned task", workspace_kind="worktree",
            workspace_path=str(repo), worktree_base_sha=base,
        )

    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).worktree_base_sha == base
        row = conn.execute("SELECT worktree_base_sha FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row["worktree_base_sha"] == base


@pytest.mark.parametrize("explicit_pin", [True, False], ids=["requested-pin", "resolved-main"])
def test_dispatcher_passes_and_persists_actual_base_before_stub_spawn(
    tmp_path, all_assignees_spawnable, explicit_pin,
):
    repo, base = repository(tmp_path / "repo")
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="dispatch pinned task", assignee="test-worker",
            body=_WORKSPACE_AUDIT_CONTRACT,
            workspace_kind="worktree", workspace_path=str(repo),
            **({"worktree_base_sha": base} if explicit_pin else {}),
        )
    later = commit(repo, "main advances before dispatch\n")
    expected = base if explicit_pin else later
    spawned = []

    def stub_spawn(task, workspace, *_args, **_kwargs):
        assert task.worktree_base_sha == expected
        assert git(Path(workspace), "rev-parse", "HEAD") == expected
        with kb.connect() as fresh:
            assert kb.get_task(fresh, task_id).worktree_base_sha == expected
        spawned.append(task.id)
        return None

    with kb.connect() as conn:
        result = kb.dispatch_once(
            conn, spawn_fn=stub_spawn, max_spawn=1, reconcile_orphans=False,
        )
        assert any(row[0] == task_id for row in result.spawned)
    assert spawned == [task_id]
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).worktree_base_sha == expected
