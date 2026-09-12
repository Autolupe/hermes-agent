"""Required workspace boundaries through real native modules and temporary data."""

import contextlib
from contextvars import copy_context
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_policy as policy
from hermes_cli import kanban_workspace_policy as workspace_policy


pytestmark = pytest.mark.linux_only


@pytest.fixture
def native(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    monkeypatch.setattr(kb, "_fire_worker_spawned_hook", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", lambda *a, **kw: None)
    kb._ORIGIN_MAIN_FETCHED_AT.clear()
    with contextlib.closing(kb.connect(db_path=tmp_path / "board.db")) as conn:
        yield conn


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True,
                          capture_output=True, text=True, timeout=30)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    git("init", "-b", "main", root)
    git("-C", root, "-c", "user.name=Fixture", "-c",
        "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
        "commit", "--allow-empty", "-m", "fixture")
    git("-C", root, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setattr(kb, "read_board_metadata",
                        lambda _board: {"default_workdir": str(root)})
    return root


def enroll(monkeypatch, provider=None):
    registration = SimpleNamespace(
        uid=os.getuid() if hasattr(os, "getuid") else 0,
        generation="fixture-generation", provider=provider or policy.RequiredKanbanPolicy(),
        check_integrity=lambda: None,
    )
    monkeypatch.setattr(policy, "select_required_policy", lambda: registration)
    return registration


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_base_provider_refuses_native_dispatch_before_workspace(native, repo, monkeypatch, lane):
    task_id = kb.create_task(native, title="fixture", assignee="default",
                             workspace_kind="worktree")
    if lane == "review":
        native.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        native.commit()
    original = kb.get_task(native, task_id)
    enroll(monkeypatch)
    calls = []
    result = kb.dispatch_once(native, max_spawn=1,
                             spawn_fn=lambda *a, **kw: calls.append(True))
    assert calls == []
    assert result.spawned == []
    task = kb.get_task(native, task_id)
    assert task.workspace_path == original.workspace_path
    assert task.branch_name == original.branch_name
    assert task.worker_pid is None
    assert task.consecutive_failures == 0
    assert not (repo / ".worktrees" / task_id).exists()


def test_enrolled_public_resolver_cannot_start_implicit_request(native, tmp_path, monkeypatch):
    target = tmp_path / "must-not-exist"
    task_id = kb.create_task(native, title="fixture", assignee="default",
                             workspace_kind="dir", workspace_path=str(target))
    task = kb.claim_task(native, task_id)
    enroll(monkeypatch)
    with pytest.raises(policy.RequiredPolicyError):
        kb.resolve_workspace(task)
    assert not target.exists()


class FixtureProvider(policy.RequiredKanbanPolicy):
    """Test-only positive preparation seam; it cannot enable native launch."""

    def __init__(self, hook=lambda *args: None):
        self.hook = hook
        self.requests = []
        self.boundaries = []
        self.cancelled = []
        self.closed = []

    def open_workspace_request(self, request):
        self.requests.append(request)
        provider = self

        class Admission(policy.RequiredWorkspaceAdmission):
            def checkpoint(self, boundary, observation):
                provider.boundaries.append(boundary)
                return provider.hook(request, boundary, observation)

            def cancel(self, reason):
                assert request.cancelled
                provider.cancelled.append(request.claim.request_id)

            def close(self):
                provider.closed.append(request.claim.request_id)

        return Admission()


def claim(conn, *, lane="ready", **kwargs):
    task_id = kb.create_task(conn, title="fixture", assignee="default", **kwargs)
    if lane == "review":
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        conn.commit()
    task = (kb.claim_task if lane == "ready" else kb.claim_review_task)(conn, task_id)
    assert task is not None
    return task


def request_for(conn, task, **kwargs):
    return kb.required_workspace_request(
        conn, task_id=task.id, expected_run_id=task.current_run_id,
        expected_claim_lock=task.claim_lock, **kwargs,
    )


def test_request_retains_actual_connection_and_frozen_binding(native, monkeypatch):
    task = claim(native)
    provider = FixtureProvider()
    enroll(monkeypatch, provider)
    with request_for(native, task) as request:
        assert request.connection is native
        assert request.claim.run_id == task.current_run_id
        assert request.claim.claim_lock == task.claim_lock
        assert request.claim.lane == "manual"
        with pytest.raises(FrozenInstanceError):
            request.claim.run_id = 123
        request.checkpoint("fixture")
    assert provider.closed == [request.claim.request_id]
    with pytest.raises(policy.RequiredPolicyError):
        request.checkpoint("after_close")


@pytest.mark.parametrize("operation", ["internal", "materialize", "fetch", "base", "path", "branch", "pid", "spawn"])
def test_direct_enrolled_helpers_refuse_missing_request(native, repo, tmp_path, monkeypatch, operation):
    task = claim(native, workspace_kind="worktree")
    target = tmp_path / "never-created"
    enroll(monkeypatch, FixtureProvider())
    actions = {
        "internal": lambda: kb._resolve_worktree_workspace(task, conn=native),
        "materialize": lambda: kb._ensure_git_worktree(repo, target, "hermes/fixture"),
        "fetch": lambda: kb._fetch_origin_main(repo),
        "base": lambda: kb._worktree_base_ref(repo),
        "path": lambda: kb.set_workspace_path(native, task.id, target),
        "branch": lambda: kb.set_branch_name(native, task.id, "hermes/fixture"),
        "pid": lambda: kb._set_worker_pid(native, task.id, 4242),
        "spawn": lambda: kb._default_spawn(task, str(target)),
    }
    with pytest.raises(policy.RequiredPolicyError, match="request is missing"):
        actions[operation]()
    assert not target.exists()
    assert kb.get_task(native, task.id).worker_pid is None


@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
@pytest.mark.parametrize("lane", ["ready", "review"])
def test_required_fetch_failure_is_sticky_without_materialization_or_cache(
    native, repo, monkeypatch, lane, failure,
):
    task_id = kb.create_task(native, title="fixture", assignee="default", workspace_kind="worktree")
    if lane == "review":
        native.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        native.commit()
    original = kb.get_task(native, task_id)
    provider = FixtureProvider()
    enroll(monkeypatch, provider)
    original_run = subprocess.run
    fetches = []

    def run(command, **kwargs):
        if command[:2] == ["git", "-C"] and command[3:] == ["fetch", "origin", "main"]:
            fetches.append(command)
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return subprocess.CompletedProcess(command, 42, "", "fixture refusal")
        return original_run(command, **kwargs)

    monkeypatch.setattr(kb.subprocess, "run", run)
    spawns = []
    result = kb.dispatch_once(native, max_spawn=1, spawn_fn=lambda *a, **kw: spawns.append(True))
    assert len(fetches) == 1
    assert spawns == result.spawned == []
    assert provider.requests[0].cancelled
    assert kb._ORIGIN_MAIN_FETCHED_AT == {}
    assert not (repo / ".worktrees" / task_id).exists()
    after = kb.get_task(native, task_id)
    assert (after.workspace_path, after.branch_name) == (original.workspace_path, original.branch_name)


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_required_reuse_is_checked_and_launch_remains_unsupported(native, repo, monkeypatch, lane):
    task_id = kb.create_task(native, title="fixture", assignee="default", workspace_kind="worktree")
    target = repo / ".worktrees" / task_id
    branch = kb.default_task_branch_name(task_id)
    git("-C", repo, "worktree", "add", "-b", branch, target, "origin/main")
    kb.set_workspace_path(native, task_id, target)
    kb.set_branch_name(native, task_id, branch)
    if lane == "review":
        native.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        native.commit()
    provider = FixtureProvider()
    enroll(monkeypatch, provider)
    spawns = []
    result = kb.dispatch_once(native, max_spawn=1, spawn_fn=lambda *a, **kw: spawns.append(True))
    assert spawns == result.spawned == []
    assert "before_worktree_reuse" in provider.boundaries
    assert "workspace_ready" in provider.boundaries
    assert "before_spawn" in provider.boundaries
    assert "before_fetch" not in provider.boundaries
    assert provider.requests[0].cancelled
    assert target.is_dir()
    assert kb.get_task(native, task_id).workspace_path == str(target)


def test_later_request_cannot_revive_cancelled_earlier_context(native, monkeypatch):
    first = claim(native)
    second = claim(native)
    provider = FixtureProvider()
    enroll(monkeypatch, provider)
    with pytest.raises(policy.RequiredPolicyError):
        with request_for(native, first) as old:
            old_context = copy_context()
            old.cancel("fixture timeout")
            with pytest.raises(policy.RequiredPolicyError):
                old.checkpoint("still_cancelled")
    with request_for(native, second) as new:
        new.checkpoint("fixture recovery")
        with pytest.raises(policy.RequiredPolicyError):
            old_context.run(old.checkpoint, "late A callback")
        assert new.claim.request_id != old.claim.request_id
        assert old.cancelled
        new.checkpoint("B still active")
    assert provider.cancelled.count(old.claim.request_id) == 1


@pytest.mark.parametrize("boundary", ["before_persist", "persist_locked", "before_persist_commit", "after_persist"])
def test_denial_at_each_persistence_edge_compensates_owned_directory(native, tmp_path, monkeypatch, boundary):
    target = tmp_path / "created-by-request"
    task = claim(native, workspace_kind="dir", workspace_path=str(target))

    def deny(_request, actual, _observation):
        if actual == boundary:
            raise policy.RequiredPolicyError("fixture denial")

    provider = FixtureProvider(deny)
    enroll(monkeypatch, provider)
    with pytest.raises(policy.RequiredPolicyError):
        with request_for(native, task) as request:
            workspace = kb.resolve_workspace(task)
            kb._persist_dispatch_workspace(native, task, workspace, None,
                                           request.materialization, board=None)
    assert not target.exists()
    after = kb.get_task(native, task.id)
    assert after.workspace_path == str(target)
    assert after.current_run_id is None
    assert after.consecutive_failures == 0


def test_concurrent_reclaim_preserves_new_run_and_workspace(native, tmp_path, monkeypatch):
    target = tmp_path / "request-directory"
    task = claim(native, workspace_kind="dir", workspace_path=str(target))
    new_run = task.current_run_id + 100

    def replace_claim(request, boundary, _observation):
        if boundary == "after_persist":
            native.execute("UPDATE tasks SET current_run_id = ?, claim_lock = 'new-claim', "
                           "workspace_path = ? WHERE id = ?", (new_run, str(target), task.id))
            native.commit()

    provider = FixtureProvider(replace_claim)
    enroll(monkeypatch, provider)
    with pytest.raises(policy.RequiredPolicyError):
        with request_for(native, task) as request:
            workspace = kb.resolve_workspace(task)
            kb._persist_dispatch_workspace(native, task, workspace, None,
                                           request.materialization, board=None)
    row = kb.get_task(native, task.id)
    assert row.current_run_id == new_run
    assert row.claim_lock == "new-claim"
    assert row.workspace_path == str(target)
    assert target.is_dir()


@pytest.mark.parametrize("failure", [TypeError, ValueError])
def test_callback_exception_never_calls_spawn_twice(native, tmp_path, monkeypatch, failure):
    task = claim(native, workspace_kind="dir", workspace_path=str(tmp_path))
    monkeypatch.setattr(policy, "select_required_policy", lambda: None)
    calls = []

    def spawn(_task, _workspace, *, board=None):
        calls.append(board)
        raise failure("fixture callback failure")

    with pytest.raises(failure):
        kb._invoke_spawn_once(spawn, task, str(tmp_path), board="fixture")
    assert calls == ["fixture"]


@pytest.mark.parametrize("spawn_kind", ["default", "injected"])
def test_preparation_provider_cannot_enable_any_worker(native, tmp_path, monkeypatch, spawn_kind):
    task = claim(native, workspace_kind="dir", workspace_path=str(tmp_path))
    provider = FixtureProvider()
    enroll(monkeypatch, provider)
    calls = []
    monkeypatch.setattr(kb.subprocess, "Popen", lambda *a, **kw: calls.append("popen"))
    with pytest.raises(policy.RequiredPolicyError, match="controlled worker launch is unsupported"):
        with request_for(native, task):
            if spawn_kind == "default":
                kb._default_spawn(task, str(tmp_path))
            else:
                kb._invoke_spawn_once(lambda *a, **kw: calls.append("injected"),
                                      task, str(tmp_path), board=None)
    assert calls == []


@pytest.mark.parametrize("run_id,claim_lock", [(None, None), (None, "lock"), (1, None), (True, "lock")])
def test_invalid_request_binding_never_releases_existing_claim(native, monkeypatch, run_id, claim_lock):
    task = claim(native)
    enroll(monkeypatch)
    before = list(native.iterdump())
    with pytest.raises(policy.RequiredPolicyError):
        with kb.required_workspace_request(native, task_id=task.id,
                                           expected_run_id=run_id, expected_claim_lock=claim_lock):
            pytest.fail("invalid request entered")
    assert list(native.iterdump()) == before


@pytest.mark.parametrize("primary_failure", [False, True])
def test_provider_close_failure_compensates_and_preserves_primary_error(
    native, tmp_path, monkeypatch, primary_failure,
):
    target = tmp_path / "close-failure-directory"
    task = claim(native, workspace_kind="dir", workspace_path=str(target))

    class CloseFailure(FixtureProvider):
        def open_workspace_request(self, request):
            admission = super().open_workspace_request(request)

            def close():
                raise ValueError("fixture close failure")

            admission.close = close
            return admission

    provider = CloseFailure()
    enroll(monkeypatch, provider)
    expected = "fixture primary" if primary_failure else "provider cleanup failed"
    with pytest.raises(policy.RequiredPolicyError, match=expected):
        with request_for(native, task) as request:
            workspace = kb.resolve_workspace(task)
            kb._persist_dispatch_workspace(native, task, workspace, None,
                                           request.materialization, board=None)
            if primary_failure:
                raise policy.RequiredPolicyError("fixture primary")
    assert provider.requests[0].cancelled
    assert not target.exists()
    assert kb.get_task(native, task.id).current_run_id is None


@pytest.mark.parametrize("field", ["title", "body", "project_id", "model_override", "provider_override",
                                  "tenant", "skills", "max_runtime_seconds", "current_step_key"])
def test_instruction_or_routing_drift_cancels_request(native, monkeypatch, field):
    task = claim(native)
    enroll(monkeypatch, FixtureProvider())
    with pytest.raises(policy.RequiredPolicyError):
        with request_for(native, task) as request:
            value = 44 if field == "max_runtime_seconds" else '["changed"]' if field == "skills" else "changed"
            # Field comes only from the fixed parameter list, never from a caller.
            native.execute(f"UPDATE tasks SET {field} = ? WHERE id = ?", (value, task.id))
            native.commit()
            request.checkpoint("instruction_drift")
    assert request.cancelled


@pytest.mark.parametrize("failure", ["dict", "boolean", "raised"])
def test_provider_cannot_return_json_or_boolean_admission(native, monkeypatch, failure):
    task = claim(native)

    class InvalidProvider(policy.RequiredKanbanPolicy):
        def open_workspace_request(self, request):
            if failure == "raised":
                raise ValueError("fixture open failure")
            return {"approved": True} if failure == "dict" else True

    enroll(monkeypatch, InvalidProvider())
    with pytest.raises(policy.RequiredPolicyError):
        with request_for(native, task):
            pytest.fail("invalid provider entered")
    assert kb.get_task(native, task.id).current_run_id is None


@pytest.mark.parametrize("boundary", ["pid_write_locked", "before_pid_commit", "after_pid_commit"])
def test_pid_failure_compensates_only_introduced_records(native, monkeypatch, boundary):
    task = claim(native)

    def deny(_request, actual, _observation):
        if actual == boundary:
            raise policy.RequiredPolicyError("fixture PID denial")

    enroll(monkeypatch, FixtureProvider(deny))
    with pytest.raises(policy.RequiredPolicyError):
        with request_for(native, task):
            kb._set_worker_pid(native, task.id, 4242)
    assert kb.get_task(native, task.id).worker_pid is None
    assert native.execute("SELECT worker_pid FROM task_runs WHERE id = ?", (task.current_run_id,)).fetchone()[0] is None
    assert native.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'spawned'",
                          (task.id,)).fetchone()[0] == 0


def test_pid_write_refuses_changed_claim_without_touching_new_run(native, monkeypatch):
    task = claim(native)
    enroll(monkeypatch, FixtureProvider())
    with pytest.raises(policy.RequiredPolicyError):
        with request_for(native, task):
            native.execute("UPDATE tasks SET claim_lock = 'different' WHERE id = ?", (task.id,))
            native.commit()
            kb._set_worker_pid(native, task.id, 4242)
    row = kb.get_task(native, task.id)
    assert row.current_run_id == task.current_run_id
    assert row.claim_lock == "different"
    assert row.worker_pid is None


def test_request_rejects_other_connection_at_same_path(native, tmp_path, monkeypatch):
    task = claim(native)
    enroll(monkeypatch, FixtureProvider())
    with contextlib.closing(kb.connect(db_path=tmp_path / "board.db")) as other:
        with pytest.raises(policy.RequiredPolicyError):
            with request_for(native, task):
                kb.set_workspace_path(other, task.id, tmp_path)
    assert kb.get_task(native, task.id).workspace_path == task.workspace_path


def test_manual_claim_cannot_force_required_admission(native, tmp_path, monkeypatch, capsys):
    from hermes_cli import kanban
    task_id = kb.create_task(native, title="manual fixture", assignee="default",
                             workspace_kind="dir", workspace_path=str(tmp_path / "manual-workspace"))
    enroll(monkeypatch)
    monkeypatch.setattr(kb, "connect_closing", lambda: contextlib.nullcontext(native))
    args = SimpleNamespace(task_id=task_id, ttl=None, force=True)
    assert kanban._cmd_claim(args) == 1
    assert "required workspace admission" in capsys.readouterr().err
    assert not (tmp_path / "manual-workspace").exists()
    assert kb.get_task(native, task_id).current_run_id is None


def test_real_local_fetch_and_worktree_are_removed_after_late_denial(native, repo, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    git("init", "--bare", remote)
    git("-C", repo, "remote", "add", "origin", remote)
    git("-C", repo, "push", "origin", "main")
    task_id = kb.create_task(native, title="fixture", assignee="default", workspace_kind="worktree")
    before = kb.get_task(native, task_id)
    provider = FixtureProvider()
    enroll(monkeypatch, provider)
    result = kb.dispatch_once(native, max_spawn=1)
    assert "after_fetch" in provider.boundaries
    assert "after_materialize" in provider.boundaries
    assert "after_persist" in provider.boundaries
    assert "before_spawn" in provider.boundaries
    assert result.spawned == []
    assert not (repo / ".worktrees" / task_id).exists()
    branch = kb.default_task_branch_name(task_id)
    assert branch not in git("-C", repo, "branch", "--format=%(refname:short)").stdout.splitlines()
    after = kb.get_task(native, task_id)
    assert (after.workspace_path, after.branch_name) == (before.workspace_path, before.branch_name)
    assert kb._ORIGIN_MAIN_FETCHED_AT == {}


def test_manual_success_ends_request_without_transferable_spawn_permission(native, tmp_path, monkeypatch):
    target = tmp_path / "manual-directory"
    task = claim(native, workspace_kind="dir", workspace_path=str(target))
    provider = FixtureProvider()
    enroll(monkeypatch, provider)
    with request_for(native, task) as request:
        workspace = kb.resolve_workspace(task)
        kb._persist_dispatch_workspace(native, task, workspace, None,
                                       request.materialization, board=None)
    assert target.is_dir()
    assert kb.get_task(native, task.id).workspace_path == str(target)
    with pytest.raises(policy.RequiredPolicyError, match="request is missing"):
        kb._default_spawn(task, str(target))


def test_closed_connection_denies_without_masking_original_error(native, monkeypatch):
    task = claim(native)
    enroll(monkeypatch, FixtureProvider())
    with pytest.raises(policy.RequiredPolicyError, match="workspace check failed"):
        with request_for(native, task) as request:
            native.close()
            request.checkpoint("closed_connection")
    assert request.cancelled


@pytest.mark.parametrize("change", ["missing", "generation", "uid"])
def test_policy_change_cannot_make_active_request_ordinary(native, monkeypatch, change):
    task = claim(native)
    registration = enroll(monkeypatch, FixtureProvider())
    with pytest.raises(policy.RequiredPolicyError):
        with request_for(native, task) as request:
            if change == "missing":
                monkeypatch.setattr(policy, "select_required_policy", lambda: None)
            elif change == "generation":
                registration.generation = "replaced"
            else:
                registration.uid += 1
            request.checkpoint("changed_policy")
    assert request.cancelled


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("edge", ["resolve", "persist", "spawn"])
def test_late_enrollment_never_records_unbound_spawn_failure(native, tmp_path, monkeypatch, lane, edge):
    target = tmp_path / "ordinary-first-workspace"
    task_id = kb.create_task(native, title="fixture", assignee="default",
                             workspace_kind="dir", workspace_path=str(target))
    if lane == "review":
        native.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        native.commit()
    registration = SimpleNamespace(uid=os.getuid(), generation="new-fixture",
                                   provider=policy.RequiredKanbanPolicy(), check_integrity=lambda: None)
    selected = [None]
    monkeypatch.setattr(policy, "select_required_policy", lambda: selected[0])

    def arrive():
        native.execute("UPDATE tasks SET claim_lock = 'replacement' WHERE id = ?", (task_id,))
        native.commit()
        selected[0] = registration

    if edge == "resolve":
        original_mkdir = kb._mkdir_workspace

        def mkdir(path):
            created = original_mkdir(path)
            arrive()
            return created

        monkeypatch.setattr(kb, "_mkdir_workspace", mkdir)
    elif edge == "persist":
        original_persist = kb._persist_dispatch_workspace

        def persist(*args, **kwargs):
            original_persist(*args, **kwargs)
            arrive()

        monkeypatch.setattr(kb, "_persist_dispatch_workspace", persist)
    else:
        import inspect
        original_signature = inspect.signature

        def signature(function):
            result = original_signature(function)
            if function is spawn:
                arrive()
            return result

        monkeypatch.setattr(inspect, "signature", signature)
    calls = []

    def spawn(*args, **kwargs):
        calls.append(True)

    monkeypatch.setattr(kb, "_record_spawn_failure",
                        lambda *a, **kw: pytest.fail("unbound failure handling reached"))
    result = kb.dispatch_once(native, max_spawn=1, spawn_fn=spawn)
    assert calls == result.spawned == []
    after = kb.get_task(native, task_id)
    assert after.status == "running"
    assert after.claim_lock == "replacement"
    assert after.consecutive_failures == 0


def test_run_only_worker_evidence_preserves_workspace_and_claim(native, monkeypatch):
    task = claim(native, workspace_kind="scratch")

    def run_worker_then_deny(request, boundary, _observation):
        if boundary == "after_persist":
            native.execute("UPDATE task_runs SET worker_pid = 4242 WHERE id = ?",
                           (request.claim.run_id,))
            native.commit()
            raise policy.RequiredPolicyError("fixture run has a worker")

    enroll(monkeypatch, FixtureProvider(run_worker_then_deny))
    with pytest.raises(policy.RequiredPolicyError):
        with request_for(native, task) as request:
            workspace = kb.resolve_workspace(task)
            kb._persist_dispatch_workspace(native, task, workspace, None,
                                           request.materialization, board=None)
    after = kb.get_task(native, task.id)
    assert workspace.is_dir()
    assert after.workspace_path == str(workspace)
    assert after.current_run_id == task.current_run_id
    assert after.claim_lock == task.claim_lock


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("callback_outcome", ["pid", "policy_error", "other_error"])
@pytest.mark.parametrize("replacement", [False, True])
def test_enrollment_after_real_callback_entry_preserves_uncertain_worker_claim(
    native, tmp_path, monkeypatch, lane, callback_outcome, replacement,
):
    target = tmp_path / "ordinary-workspace"
    task_id = kb.create_task(native, title="fixture", assignee="default",
                             workspace_kind="dir", workspace_path=str(target))
    if lane == "review":
        native.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        native.commit()
    selected = [None]
    monkeypatch.setattr(policy, "select_required_policy", lambda: selected[0])
    children = []
    before = []

    def spawn(task, workspace):
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, cwd=workspace)
        children.append(child)
        if replacement:
            native.execute("UPDATE tasks SET claim_lock = 'replacement' WHERE id = ?", (task.id,))
            native.commit()
        before.append(list(native.iterdump()))
        selected[0] = SimpleNamespace(uid=os.getuid(), generation="fixture-enrollment",
                                      provider=policy.RequiredKanbanPolicy(), check_integrity=lambda: None)
        if callback_outcome == "policy_error":
            raise policy.RequiredPolicyError("fixture started child without returning PID")
        if callback_outcome == "other_error":
            raise ValueError("fixture child started before another callback error")
        return child.pid

    try:
        result = kb.dispatch_once(native, max_spawn=1, spawn_fn=spawn)
        assert len(children) == 1
        assert children[0].poll() is None
        assert result.spawned == []
        assert result.claim_guarded[-1][1] == "required_workspace_after_callback_entry"
        assert list(native.iterdump()) == before[0]
        assert target.is_dir()
        after = kb.get_task(native, task_id)
        assert after.status == "running"
        assert after.current_run_id is not None
        assert after.claim_lock is not None
        assert after.consecutive_failures == 0
    finally:
        for child in children:
            child.stdin.close()
            child.wait(timeout=5)
