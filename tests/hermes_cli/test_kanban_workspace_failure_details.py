"""Workspace failures retain ownership evidence without removing another task."""
from tests.hermes_cli.delivery_fixtures import ARTIFACT_CONTRACT

from pathlib import Path
import json
import subprocess

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    kb.init_db()


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c",
         "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True, timeout=20,
    ).stdout.strip()


def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "file").write_text("base")
    git(repo, "add", "file")
    git(repo, "commit", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("failure_limit", [1, 3])
def test_branch_owner_survives_short_failure_excerpt(tmp_path, failure_limit):
    repo, base = repository(tmp_path)
    owner = repo / ("long-owner-" + "x" * 160) / ("y" * 160) / ("z" * 160)
    owner.parent.mkdir(parents=True)
    branch = "hermes/owned"
    git(repo, "worktree", "add", "-b", branch, str(owner), base)
    target = repo / ".worktrees" / "different-task"
    with pytest.raises(kb.WorktreeContractError) as error:
        kb._ensure_git_worktree(repo, target, branch, base_sha=base)
    assert error.value.details["owner_worktree"] == str(owner)
    assert len(str(error.value)) <= 500
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="conflict", workspace_kind="worktree",
                                 workspace_path=str(target), branch_name=branch, body=ARTIFACT_CONTRACT)
        assert kb.claim_task(conn, task_id) is not None
        kb._record_spawn_failure(conn, task_id, str(error.value),
                                 failure_limit=failure_limit, workspace_error=error.value)
        kind = "gave_up" if failure_limit == 1 else "spawn_failed"
        event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC",
                             (task_id, kind)).fetchone()
        assert json.loads(event[0])["workspace"]["owner_worktree"] == str(owner)
        run = conn.execute("SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC",
                           (task_id,)).fetchone()
        assert json.loads(run[0])["workspace"]["requested_branch"] == branch
    assert owner.is_dir()
    assert not target.exists()


def test_resolution_failure_does_not_attempt_implicit_cleanup(tmp_path, monkeypatch):
    repo, base = repository(tmp_path)
    owner = repo / ".worktrees" / "owner"
    git(repo, "worktree", "add", "-b", "hermes/other", str(owner), base)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="stale pointer", workspace_kind="worktree",
                                 workspace_path=str(owner), branch_name="hermes/requested", body=ARTIFACT_CONTRACT)
        assert kb.claim_task(conn, task_id) is not None
        error = kb.WorktreeContractError("branch_conflict", owner, "hermes/requested", actual_branch="hermes/other")
        def forbidden(*args, **kwargs):
            pytest.fail("workspace resolution failure must not clean up an existing checkout")
        monkeypatch.setattr(kb, "_cleanup_worktree_workspace", forbidden)
        monkeypatch.setattr(kb, "_unlock_task_worktree", forbidden)
        kb._record_spawn_failure(conn, task_id, str(error), failure_limit=1, workspace_error=error)
    assert git(owner, "branch", "--show-current") == "hermes/other"


def test_cleanup_preserves_clean_foreign_branch(tmp_path):
    repo, base = repository(tmp_path)
    # The foreign branch has no unpushed work, so ownership must be the guard.
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    owner = repo / ".worktrees" / "owner"
    git(repo, "worktree", "add", "-b", "hermes/other", str(owner), base)
    kb._cleanup_worktree_workspace("t_stale", str(owner), "hermes/requested")
    assert owner.is_dir()


def test_workspace_setter_cannot_replace_recorded_base(tmp_path):
    repo, base = repository(tmp_path)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="pinned", workspace_kind="worktree", worktree_base_sha=base, body=ARTIFACT_CONTRACT)
        with pytest.raises(ValueError, match="cannot be changed"):
            kb.set_workspace_path(conn, task_id, repo, worktree_base_sha="a" * 40)
        task = kb.get_task(conn, task_id)
        assert task.worktree_base_sha == base
        assert task.workspace_path is None


def test_idempotent_create_cannot_silently_ignore_different_base(tmp_path):
    _, base = repository(tmp_path)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="pinned", workspace_kind="worktree",
                                 worktree_base_sha=base, idempotency_key="same-work", body=ARTIFACT_CONTRACT)
        assert kb.create_task(conn, title="retry", workspace_kind="worktree",
                              worktree_base_sha=base, idempotency_key="same-work", body=ARTIFACT_CONTRACT) == task_id
        with pytest.raises(ValueError, match="different worktree base"):
            kb.create_task(conn, title="wrong retry", workspace_kind="worktree",
                           worktree_base_sha="a" * 40, idempotency_key="same-work", body=ARTIFACT_CONTRACT)
        assert kb.get_task(conn, task_id).worktree_base_sha == base
