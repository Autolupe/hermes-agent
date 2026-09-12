"""Required workspace requests retain their exact Git creation base."""

import subprocess
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_policy as policy
from tests.hermes_cli import test_kanban_required_workspace as required_fixture
from tests.hermes_cli.test_kanban_required_workspace import native as native
from tests.hermes_cli.test_kanban_required_workspace import repo as repo


pytestmark = pytest.mark.linux_only


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setattr(kb, "_ORIGIN_MAIN_FETCHED_AT", {})
    monkeypatch.setattr(kb, "_ORIGIN_MAIN_FETCHED_SHA", {})
    monkeypatch.setattr(kb, "_ORIGIN_MAIN_FETCH_FAILED_AT", {})


@pytest.fixture
def local_remote(repo, tmp_path):
    remote = tmp_path / "remote.git"
    required_fixture.git("init", "--bare", remote)
    required_fixture.git("-C", repo, "remote", "add", "origin", remote)
    required_fixture.git("-C", repo, "push", "origin", "main")
    return required_fixture.git("-C", repo, "rev-parse", "HEAD").stdout.strip()


def pointers(conn, task_id):
    return tuple(conn.execute(
        "SELECT workspace_path, branch_name, worktree_base_sha FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone())


def advance(repo):
    required_fixture.git("-C", repo, "-c", "user.name=Fixture", "-c",
                         "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                         "commit", "--allow-empty", "-m", "later fixture commit")
    return required_fixture.git("-C", repo, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_required_request_persists_selected_base_with_workspace(native, repo, local_remote, monkeypatch, lane):
    task = required_fixture.claim(native, lane=lane, workspace_kind="worktree", workspace_path=str(repo))
    provider = required_fixture.FixtureProvider()
    required_fixture.enroll(monkeypatch, provider)
    with required_fixture.request_for(native, task, lane=lane) as request:
        assert request.claim.original_base_sha is None
        assert request.stored_base_sha is None
        workspace, branch = kb._resolve_worktree_workspace(
            task, conn=native, materialization=request.materialization,
        )
        assert request.materialization.selected_base_sha == local_remote
        assert required_fixture.git("-C", workspace, "rev-parse", "HEAD").stdout.strip() == local_remote
        kb._persist_dispatch_workspace(native, task, workspace, branch,
                                       request.materialization, board=None)
        assert pointers(native, task.id) == (str(workspace), branch, local_remote)
        assert request.stored_workspace == (str(workspace), branch)
        assert request.stored_base_sha == task.worktree_base_sha == local_remote
        assert request.claim.original_base_sha is None
        request.checkpoint("fixture persisted base remains bound")
    assert pointers(native, task.id) == (str(workspace), branch, local_remote)
    assert not request.cancelled


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("boundary", ["before_persist_commit", "after_persist"])
def test_required_denial_restores_selected_base_and_workspace_together(
    native, repo, local_remote, monkeypatch, lane, boundary,
):
    task = required_fixture.claim(native, lane=lane, workspace_kind="worktree", workspace_path=str(repo))
    original = pointers(native, task.id)
    selected = []

    def deny(request, actual, _observation):
        if actual == boundary:
            selected.append(pointers(native, request.claim.task_id))
            raise policy.RequiredPolicyError("fixture denies selected-base persistence")

    required_fixture.enroll(monkeypatch, required_fixture.FixtureProvider(deny))
    with pytest.raises(policy.RequiredPolicyError, match="fixture denies"):
        with required_fixture.request_for(native, task, lane=lane) as request:
            workspace, branch = kb._resolve_worktree_workspace(
                task, conn=native, materialization=request.materialization,
            )
            kb._persist_dispatch_workspace(native, task, workspace, branch,
                                           request.materialization, board=None)
    assert selected == [(str(workspace), branch, local_remote)]
    assert pointers(native, task.id) == original
    assert not workspace.exists()
    assert branch not in required_fixture.git("-C", repo, "branch", "--format=%(refname:short)").stdout.splitlines()
    assert kb.get_task(native, task.id).current_run_id is None
    assert not native.in_transaction


def test_changed_task_base_cancels_request_without_overwriting_new_value(native, repo, monkeypatch):
    original = required_fixture.git("-C", repo, "rev-parse", "HEAD").stdout.strip()
    later = advance(repo)
    task = required_fixture.claim(native, workspace_kind="worktree", workspace_path=str(repo),
                                  worktree_base_sha=original)
    required_fixture.enroll(monkeypatch, required_fixture.FixtureProvider())
    with pytest.raises(policy.RequiredPolicyError):
        with required_fixture.request_for(native, task) as request:
            assert request.claim.original_base_sha == request.stored_base_sha == original
            native.execute("UPDATE tasks SET worktree_base_sha = ? WHERE id = ?", (later, task.id))
            native.commit()
            request.checkpoint("fixture base changed")
    assert request.cancelled
    assert kb.get_task(native, task.id).worktree_base_sha == later
    assert not (repo / ".worktrees" / task.id).exists()


def test_explicit_base_skips_fetch_but_rechecks_policy_before_creation(native, repo, monkeypatch):
    original = required_fixture.git("-C", repo, "rev-parse", "HEAD").stdout.strip()
    later = advance(repo)
    task = required_fixture.claim(native, workspace_kind="worktree", workspace_path=str(repo),
                                  worktree_base_sha=original)
    target = repo / ".worktrees" / task.id
    branch = kb.default_task_branch_name(task.id)
    observed = []

    def change_base(request, boundary, observation):
        if boundary == "base_ready":
            observed.append(observation["base_oid"])
            assert not target.exists()
            native.execute("UPDATE tasks SET worktree_base_sha = ? WHERE id = ?", (later, task.id))
            native.commit()

    def must_not_fetch(*_args, **_kwargs):
        raise AssertionError("An explicit available commit must not fetch")

    monkeypatch.setattr(kb, "_fetch_origin_main", must_not_fetch)
    required_fixture.enroll(monkeypatch, required_fixture.FixtureProvider(change_base))
    with pytest.raises(policy.RequiredPolicyError):
        with required_fixture.request_for(native, task) as request:
            kb._ensure_git_worktree(repo, target, branch, base_sha=original,
                                    materialization=request.materialization)
    assert observed == [original]
    assert request.cancelled
    assert kb.get_task(native, task.id).worktree_base_sha == later
    assert not target.exists()
    assert branch not in required_fixture.git("-C", repo, "branch", "--format=%(refname:short)").stdout.splitlines()


def test_required_direct_setter_cannot_replace_pinned_base(native, repo, monkeypatch):
    original = required_fixture.git("-C", repo, "rev-parse", "HEAD").stdout.strip()
    later = advance(repo)
    task = required_fixture.claim(native, workspace_kind="worktree", workspace_path=str(repo),
                                  worktree_base_sha=original)
    before = pointers(native, task.id)
    required_fixture.enroll(monkeypatch, required_fixture.FixtureProvider())
    with pytest.raises(policy.RequiredPolicyError):
        with required_fixture.request_for(native, task):
            kb.set_workspace_path(native, task.id, repo, worktree_base_sha=later)
    assert pointers(native, task.id) == before


def test_required_fetch_ignores_ordinary_cache_and_custom_mapping(native, repo, local_remote, monkeypatch):
    later = advance(repo)
    required_fixture.git("-C", repo, "push", "origin", "main")
    required_fixture.git("-C", repo, "update-ref", "refs/remotes/origin/main", local_remote)
    required_fixture.git("-C", repo, "config", "remote.origin.fetch",
                         "+refs/heads/other:refs/remotes/origin/other")
    key = str(repo)
    cached_at = time.monotonic()
    kb._ORIGIN_MAIN_FETCHED_AT[key] = cached_at
    kb._ORIGIN_MAIN_FETCHED_SHA[key] = local_remote
    task = required_fixture.claim(native, workspace_kind="worktree", workspace_path=str(repo))
    provider = required_fixture.FixtureProvider()
    required_fixture.enroll(monkeypatch, provider)
    original_run = subprocess.run
    fetches = []

    def observe_fetch(command, *args, **kwargs):
        if command[:2] == ["git", "-C"] and command[3:5] == ["fetch", "origin"]:
            fetches.append(command)
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(kb.subprocess, "run", observe_fetch)
    with required_fixture.request_for(native, task):
        assert kb._fetch_origin_main(repo) == later
    assert fetches == [["git", "-C", str(repo), "fetch", "origin",
                        "+refs/heads/main:refs/remotes/origin/main"]]
    assert required_fixture.git("-C", repo, "rev-parse", "refs/remotes/origin/main").stdout.strip() == later
    assert "before_fetch" in provider.boundaries and "after_fetch" in provider.boundaries
    assert kb._ORIGIN_MAIN_FETCHED_AT == {key: cached_at}
    assert kb._ORIGIN_MAIN_FETCHED_SHA == {key: local_remote}
    assert kb._ORIGIN_MAIN_FETCH_FAILED_AT == {}
