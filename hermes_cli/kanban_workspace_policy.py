"""Native lifetime and claim binding for required workspace preparation.

No provider, opened-database substitute, credential transport or production
worker launch is implemented here. Source-only held-child mechanics remain
separate from the unsupported launch gate. The original SQLite object is
retained for a future supported capability adapter. Optional hooks are not used.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass, field
import functools
import json
import os
import sqlite3
import threading
import time
from types import MappingProxyType
from typing import TYPE_CHECKING
import uuid

from hermes_cli import kanban_policy as policy

if TYPE_CHECKING:
    from hermes_cli.kanban_db import TaskShowSnapshot, WorkerContextInputs


_CURRENT: ContextVar[WorkspaceRequest | None] = ContextVar(
    "required_kanban_workspace_request", default=None
)
_LAUNCH_FIELDS = (
    "title", "body", "project_id", "created_by", "workflow_template_id",
    "assignee", "tenant", "workspace_kind", "model_override", "provider_override",
    "reasoning_effort", "skills", "goal_mode", "goal_max_turns",
    "max_runtime_seconds", "current_step_key",
)


def _account() -> tuple:
    return (os.getpid(), threading.get_ident(),
            *(getattr(os, name)() if hasattr(os, name) else None
              for name in ("getuid", "geteuid", "getgid", "getegid")))


@dataclass(frozen=True)
class WorkspaceClaim:
    connection: sqlite3.Connection = field(repr=False, compare=False)
    task_id: str
    run_id: int
    claim_lock: str = field(repr=False)
    board: str
    lane: str
    account: tuple
    policy_generation: str
    request_id: str
    launch_fields: tuple = field(repr=False)
    original_workspace: tuple


def effective_worker_skills(skills, lane: str) -> tuple:
    """Derive lane additions without changing the task's stored skill selection."""
    selected = tuple(skills or ())
    if lane == "review":
        return tuple(dict.fromkeys((*selected, "sdlc-review")))
    return selected


@dataclass(frozen=True)
class WorkerDatabaseContext:
    """Request-local database display only; never complete worker instructions."""

    inputs: WorkerContextInputs = field(repr=False)
    text: str = field(repr=False)
    effective_skills: tuple
    initial_show: TaskShowSnapshot = field(repr=False)


class WorkspaceRequest:
    """One immutable claim binding and irreversible native cancellation state."""

    def __init__(self, claim: WorkspaceClaim, registration):
        self._claim = claim
        self._registration = registration
        self._cancelled = threading.Event()
        self._closed = False
        self._admission = None
        self._stored_workspace = claim.original_workspace
        self.pending_workspace = None
        self._repository = None
        self._held_worker = None
        self._workspace_was_persisted = False
        self._context_capture_started = False
        self._database_context = None

    @property
    def claim(self) -> WorkspaceClaim:
        return self._claim

    @property
    def connection(self) -> sqlite3.Connection:
        return self._claim.connection

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def stored_workspace(self) -> tuple:
        return self._stored_workspace

    @property
    def database_context(self) -> WorkerDatabaseContext | None:
        """Captured display data, including after cancellation; not admission."""
        return self._database_context

    def cancel(self, reason: str) -> None:
        # Native cancellation precedes cleanup, including cleanup which fails,
        # re-enters this request or later recovers a different request's fence.
        already_cancelled = self._cancelled.is_set()
        self._cancelled.set()
        if not already_cancelled:
            for owner in (self._held_worker, self._admission):
                if owner is not None:
                    try:
                        owner.cancel(reason)
                    except BaseException:
                        # Attempt both cleanup owners even if one fails. Native
                        # cancellation stays set; cleanup cannot replace the
                        # original refusal/interrupt with another exception.
                        pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        first_failure = None
        for name, owner in (("worker", self._held_worker), ("provider", self._admission)):
            if owner is not None:
                try:
                    owner.close()
                except BaseException as error:
                    if first_failure is None:
                        first_failure = (name, error)
        if first_failure is not None:
            name, error = first_failure
            self.cancel(f"{name}_close_failed")
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise error
            raise policy.RequiredPolicyError(f"Required workspace {name} cleanup failed.") from error

    def _check_native(self, expected_workspace=None) -> None:
        if self.cancelled or self._closed or _CURRENT.get() is not self:
            raise policy.RequiredPolicyError("Required workspace request is no longer active.")
        claim = self.claim
        if _account() != claim.account:
            raise policy.RequiredPolicyError("Required workspace request changed account or execution context.")
        selected = policy.select_required_policy()
        if selected is not self._registration or selected.generation != claim.policy_generation:
            raise policy.RequiredPolicyError("Required workspace policy changed during the request.")
        if selected.uid != claim.account[2] or selected.uid != claim.account[3]:
            raise policy.RequiredPolicyError("Required workspace enrollment belongs to another account.")
        selected.check_integrity()
        row = self.connection.execute("SELECT * FROM tasks WHERE id = ?", (claim.task_id,)).fetchone()
        run = self.connection.execute("SELECT * FROM task_runs WHERE id = ?", (claim.run_id,)).fetchone()
        now = time.time()
        if (row is None or run is None or row["status"] != "running"
                or row["current_run_id"] != claim.run_id or row["claim_lock"] != claim.claim_lock
                or run["task_id"] != claim.task_id or run["claim_lock"] != claim.claim_lock
                or run["status"] != "running" or run["ended_at"] is not None
                or run["profile"] != row["assignee"] or run["step_key"] != row["current_step_key"]
                or not isinstance(row["claim_expires"], (int, float)) or row["claim_expires"] <= now
                or not isinstance(run["claim_expires"], (int, float)) or run["claim_expires"] <= now
                or tuple(row[name] for name in _LAUNCH_FIELDS) != claim.launch_fields
                or (row["workspace_path"], row["branch_name"]) != (
                    self._stored_workspace if expected_workspace is None else expected_workspace)):
            raise policy.RequiredPolicyError("Required workspace claim or run changed.")

    def checkpoint(self, boundary: str, *, expected_workspace=None, **observation) -> None:
        try:
            self._check_native(expected_workspace)
            if self._admission is None:
                raise policy.RequiredPolicyError("Required workspace admission is unsupported.")
            result = self._admission.checkpoint(boundary, MappingProxyType(observation))
            if result is not None:
                raise policy.RequiredPolicyError("Required workspace checks cannot return approval records.")
            self._check_native(expected_workspace)
        except BaseException as exc:
            self.cancel("checkpoint_failed")
            if isinstance(exc, (policy.RequiredPolicyError, KeyboardInterrupt, SystemExit)):
                raise
            raise policy.RequiredPolicyError("Required workspace check failed.") from exc

    def bind_repository(self, repository: str) -> None:
        if self._repository is not None and self._repository != repository:
            self.cancel("repository_changed")
            raise policy.RequiredPolicyError("Required workspace repository changed.")
        self.checkpoint("repository", repository=repository)
        self._repository = repository

    def check_task(self, task) -> None:
        fields = dict(zip(_LAUNCH_FIELDS, self.claim.launch_fields))
        expected_skills = tuple(json.loads(fields["skills"] or "[]"))
        if (task.id != self.claim.task_id or task.current_run_id != self.claim.run_id
                or task.claim_lock != self.claim.claim_lock
                or any(getattr(task, name) != value for name, value in fields.items()
                       if name != "skills")
                or tuple(task.skills or ()) != expected_skills
                or (task.workspace_path, task.branch_name) != self._stored_workspace):
            self.cancel("task_object_changed")
            raise policy.RequiredPolicyError("Required workspace task object changed.")

    def workspace_persisted(self, workspace: tuple) -> None:
        self.checkpoint("after_persist", expected_workspace=workspace,
                        workspace=workspace[0], branch=workspace[1])
        self._stored_workspace = workspace
        self._workspace_was_persisted = True

    def capture_database_context(self) -> WorkerDatabaseContext:
        """Capture once on the original connection after checked persistence.

        The native transaction's default rejects a caller-owned transaction;
        this method must not commit provisional caller changes. Neither this
        data nor the surrounding checks approve launch or capture external
        skill/configuration/attachment contents.
        """
        from hermes_cli import kanban_db as kb

        try:
            if self._context_capture_started:
                raise policy.RequiredPolicyError("Required workspace database context capture already started.")
            # Set before any provider callback, including the unlocked final
            # checkpoint: re-entry must not publish then replace a capture.
            self._context_capture_started = True
            self.checkpoint("before_context_capture")
            if not self._workspace_was_persisted:
                raise policy.RequiredPolicyError("Required workspace context needs persisted workspace.")
            with kb.write_txn(self.connection):
                kb._raise_if_dispatch_paused()
                self.checkpoint("context_capture_locked")
                inputs = kb.collect_worker_context(self.connection, self.claim.task_id)
                text = kb.render_worker_context(inputs)
                initial_show = kb.collect_task_show(
                    self.connection, self.claim.task_id, worker_context=text,
                )
                if initial_show is None:
                    raise policy.RequiredPolicyError("Required worker initial task view is missing.")
                skills = json.loads(dict(zip(_LAUNCH_FIELDS, self.claim.launch_fields))["skills"] or "[]")
                if type(skills) is not list or any(type(skill) is not str for skill in skills):
                    raise policy.RequiredPolicyError("Required worker skill names are not immutable strings.")
                captured = WorkerDatabaseContext(
                    inputs, text, effective_worker_skills(skills, self.claim.lane), initial_show,
                )
                self.checkpoint("before_context_capture_commit")
                kb._raise_if_dispatch_paused()
            kb._raise_if_dispatch_paused()
            self.checkpoint("after_context_capture")
            self._database_context = captured
            return captured
        except BaseException as exc:
            self.cancel("database_context_capture_failed")
            if isinstance(exc, (policy.RequiredPolicyError, KeyboardInterrupt, SystemExit)):
                raise
            raise policy.RequiredPolicyError("Required workspace database context capture failed.") from exc


@contextlib.contextmanager
def workspace_request(conn, *, task_id, expected_run_id, expected_claim_lock,
                      board, lane):
    """Scope an actual native connection; never create an implicit replacement."""
    if _CURRENT.get() is not None:
        raise policy.RequiredPolicyError("Required workspace requests cannot be nested.")
    selected = policy.select_required_policy()
    if selected is None:
        yield None
        return
    if (not isinstance(conn, sqlite3.Connection) or type(expected_run_id) is not int
            or not expected_claim_lock or lane not in ("ready", "review", "manual")):
        raise policy.RequiredPolicyError("Required workspace needs its original claimed connection.")
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise policy.RequiredPolicyError("Required workspace claim is missing.")
    claim = WorkspaceClaim(conn, task_id, expected_run_id, expected_claim_lock,
                           board, lane, _account(), selected.generation, uuid.uuid4().hex,
                           tuple(row[name] for name in _LAUNCH_FIELDS),
                           (row["workspace_path"], row["branch_name"]))
    request = WorkspaceRequest(claim, selected)
    token = _CURRENT.set(request)
    primary_error = None
    try:
        request._check_native()
        admission = selected.provider.open_workspace_request(request)
        if not isinstance(admission, policy.RequiredWorkspaceAdmission):
            raise policy.RequiredPolicyError("Required workspace provider did not return a live admission.")
        request._admission = admission
        request.checkpoint("request_open")
        yield request
        request.checkpoint("request_close")
    except BaseException as exc:
        primary_error = exc
        request.cancel("request_failed")
        if isinstance(exc, (policy.RequiredPolicyError, KeyboardInterrupt, SystemExit)):
            raise
        raise policy.RequiredPolicyError("Required workspace request failed.") from exc
    finally:
        try:
            request.close()
        except BaseException:
            if primary_error is None:
                raise
        finally:
            _CURRENT.reset(token)


def current_request(*, conn=None, task_id=None, board=None, boundary="workspace"):
    """Direct enrolled helpers refuse unless inside the same live request."""
    request = _CURRENT.get()
    if request is None:
        if policy.select_required_policy() is None:
            return None
        raise policy.RequiredPolicyError("Required workspace request is missing.")
    if ((conn is not None and conn is not request.connection)
            or (task_id is not None and task_id != request.claim.task_id)
            or (board is not None and board != request.claim.board)):
        request.cancel("request_binding_mismatch")
        raise policy.RequiredPolicyError("Required workspace request binding does not match.")
    request.checkpoint(boundary)
    return request


def require_supported_worker_launch(request) -> None:
    """No production launch barrier exists; checks alone are not atomic launch."""
    request.cancel("controlled_worker_launch_unsupported")
    raise policy.RequiredPolicyError("Required controlled worker launch is unsupported.")


def guard_workspace(function):
    """Guard every public/internal resolver return, including existing reuse."""
    @functools.wraps(function)
    def guarded(task, *args, **kwargs):
        request = current_request(conn=kwargs.get("conn"), task_id=task.id,
                                  board=kwargs.get("board"), boundary="before_resolve")
        if request is not None:
            request.check_task(task)
            if kwargs.get("board") is None:
                kwargs["board"] = request.claim.board
            if kwargs.get("materialization") is None:
                kwargs["materialization"] = request.materialization
        try:
            result = function(task, *args, **kwargs)
            current_request(conn=kwargs.get("conn"), task_id=task.id,
                            board=kwargs.get("board"), boundary="after_resolve")
            if request is not None:
                workspace, branch = result if isinstance(result, tuple) else (result, None)
                request.checkpoint("workspace_ready", workspace=str(workspace), branch=branch)
            return result
        except BaseException:
            if request is not None:
                request.cancel("workspace_resolution_failed")
            raise
    return guarded


def guard_materialization(function):
    @functools.wraps(function)
    def guarded(repo_root, target, branch_name, **kwargs):
        request = current_request(boundary="before_materialize")
        if request is not None and kwargs.get("materialization") is None:
            kwargs["materialization"] = request.materialization
        try:
            result = function(repo_root, target, branch_name, **kwargs)
            current_request(boundary="after_materialize")
            if request is not None:
                request.checkpoint("after_materialize", workspace=str(target), branch=branch_name)
            return result
        except BaseException:
            if request is not None:
                request.cancel("materialization_failed")
            raise
    return guarded
