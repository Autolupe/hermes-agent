"""Trusted, fixed-operation delivery control for sandboxed Kanban workers.

Workers have no GitHub or cloud credentials and no network in their terminal
sandbox.  They authenticate one exact active run over a root-owned Unix socket;
this broker re-derives task, Git, GitHub, policy, review, and deployment state
before invoking the existing native Kanban transitions.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote

from hermes_cli import kanban_db as kb
from hermes_cli.trusted_delivery_runtime import (
    TRUSTED_GH_PATH,
    TRUSTED_GCLOUD_PATH,
    TRUSTED_GCLOUD_PROVENANCE_PATH,
    TRUSTED_PYTHON_PROVENANCE_PATH,
    TRUSTED_PYTHON_ROOT,
    TRUSTED_PYTHON_VERSION,
    TRUSTED_POLICY_PYTHON,
    TRUSTED_POLICY_RUNTIME_ROOT,
    TRUSTED_WORKER_PYTHON,
    TRUSTED_WORKER_RUNTIME_ROOT,
    TRUSTED_SQLITE_VERSION,
    TrustedDeliveryRuntimeError,
    require_trusted_gh,
    require_trusted_gcloud,
    require_trusted_immutable_runtime,
    require_trusted_python_base,
    require_root_owned_executable,
)


PROTOCOL_SCHEMA = "hermes-delivery-control/v1"
SOCKET_PATH = Path("/run/hermes-delivery-control/control.sock")
STATE_ROOT = Path("/var/lib/hermes-delivery-control")
ROOT_POLICY_STATE_ROOT = STATE_ROOT / "policy-root"
BASE_STATE_ROOT = STATE_ROOT / "base"
BASE_REFRESH_RECEIPT = BASE_STATE_ROOT / "clauseye-main.json"
BASE_REPOSITORY_MIRROR = BASE_STATE_ROOT / "repository.git"
BASE_REFRESH_SCHEMA = "hermes-delivery-base/v1"
BASE_REFRESH_MAX_AGE_SECONDS = 120
FIXED_KANBAN_DB = Path("/home/ab/.hermes/kanban.db")
FIXED_PROJECT_ROOT = Path("/home/ab/code/clauseye-contra-rope")
GITHUB_CREDENTIAL_NAME = "github-token"
POLICY_SIGNING_CREDENTIAL_NAME = "policy-signing-key"
_CONTROL_CREDENTIALS_DIRECTORY = Path(
    "/run/credentials/hermes-delivery-control.service"
)
_POLICY_CREDENTIALS_DIRECTORY = Path(
    "/run/credentials/hermes-delivery-policy-refresh.service"
)
_MAX_SYSTEMD_CREDENTIAL_BYTES = 64 * 1024
FIXED_REPOSITORY = "clauseye-com/clauseye-contra-rope"
FIXED_REPOSITORY_URL = f"https://github.com/{FIXED_REPOSITORY}.git"
FIXED_DEFAULT_BRANCH = "main"
FIXED_GCP_PROJECT = "clauseye-prod-491621"
FIXED_GCP_REGION = "us-central1"
FIXED_CLOUD_RUN_SERVICE = "clauseye-backend-production"
FIXED_REVIEWER_PROFILE = "reviewer"
FIXED_WORKER_UID = 1000
GIT_ASKPASS_PATH = Path(
    "/usr/libexec/hermes-delivery-control-git-askpass"
)
INSTALL_ASSET_MANIFEST = Path(
    "/usr/libexec/hermes-delivery-control/install-assets.json"
)
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 32 * 1024
MAX_SUMMARY_BYTES = 4 * 1024

_CONTROL_DB_SCHEMA = {
    "tasks": {
        "id", "title", "body", "assignee", "status", "priority",
        "created_by", "created_at", "started_at", "completed_at",
        "workspace_kind", "workspace_path", "branch_name", "worktree_base_sha",
        "project_id",
        "claim_lock", "claim_expires", "tenant", "result",
        "idempotency_key", "consecutive_failures", "worker_pid",
        "last_failure_error", "max_runtime_seconds", "last_heartbeat_at",
        "current_run_id", "workflow_template_id", "current_step_key",
        "skills", "model_override", "provider_override", "reasoning_effort",
        "max_retries", "goal_mode", "goal_max_turns", "session_id",
        "block_kind", "block_recurrences",
    },
    "task_events": {"id", "task_id", "run_id", "kind", "payload", "created_at"},
    "task_runs": {
        "id", "task_id", "profile", "step_key", "status", "claim_lock",
        "claim_expires", "worker_pid", "max_runtime_seconds",
        "last_heartbeat_at", "started_at", "ended_at", "outcome", "summary",
        "metadata", "error",
    },
    "delivery_control_operations": {
        "op_id", "task_id", "run_id", "action", "request_sha256",
        "claim_sha256", "summary", "candidate_head", "submission_event_id",
        "submission_sha256", "peer_uid", "owner_instance", "state", "stage",
        "receipt", "created_at", "updated_at",
    },
}

_TASK_ID_RE = re.compile(r"^t_[0-9a-f]{8}$")
_CLAIM_RE = re.compile(r"^[A-Za-z0-9._:@+-]{8,256}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_RE = re.compile(r"^hermes/[a-z0-9][a-z0-9/_-]{1,180}$")

_INSTALLED_SETUP_ASSET_MODES = {
    Path("/usr/local/sbin/hermes-delivery-control-install"): 0o755,
    TRUSTED_POLICY_RUNTIME_ROOT / "install-provenance.json": 0o644,
    TRUSTED_WORKER_RUNTIME_ROOT / "install-provenance.json": 0o644,
    TRUSTED_PYTHON_PROVENANCE_PATH: 0o400,
    TRUSTED_GCLOUD_PROVENANCE_PATH: 0o400,
    Path("/usr/libexec/hermes-delivery-control/uv"): 0o755,
    Path("/usr/libexec/hermes-delivery-control-dependency-provisioner.py"): 0o755,
    GIT_ASKPASS_PATH: 0o755,
    Path("/etc/systemd/system/hermes-delivery-control.socket"): 0o644,
    Path("/etc/systemd/system/hermes-delivery-control.service"): 0o644,
    Path("/etc/systemd/system/hermes-delivery-policy-refresh.service"): 0o644,
    Path("/etc/systemd/system/hermes-delivery-policy-refresh.timer"): 0o644,
    Path("/etc/systemd/system/hermes-dependency-provision.slice"): 0o644,
    Path("/etc/systemd/system/hermes-dependency-provision.socket"): 0o644,
    Path("/etc/systemd/system/hermes-dependency-provision@.service"): 0o644,
}
_REQUIRED_SYSTEMD_UNIT_STATE = {
    "hermes-delivery-control.service": ("enabled", "active"),
    "hermes-delivery-control.socket": ("enabled", "active"),
    "hermes-delivery-policy-refresh.timer": ("enabled", "active"),
    "hermes-dependency-provision.socket": ("enabled", "active"),
}

_SAFE_LOCAL_GIT_CONFIG = frozenset({
    "core.bare",
    "core.filemode",
    "core.fsmonitor",  # overridden false
    "core.hookspath",  # overridden to /dev/null on every broker command
    "core.ignorecase",
    "core.logallrefupdates",
    "core.precomposeunicode",
    "core.repositoryformatversion",
    "core.sharedrepository",  # fixed to 0600 for private imported objects
    "core.sshcommand",  # overridden to /usr/bin/false
    "core.symlinks",
    "core.worktree",
    "credential.helper",  # overridden empty
    "credential.interactive",  # overridden false
    "extensions.worktreeconfig",
    "http.followredirects",  # overridden false
    "http.proxy",  # overridden empty
    "http.sslverify",  # overridden true
    "init.defaultbranch",
    "protocol.ext.allow",  # overridden never
    "push.default",
    "remote.origin.fetch",
    "remote.origin.gh-resolved",
    "remote.origin.url",
    "user.email",
    "user.name",
})
_SAFE_BRANCH_CONFIG_SUFFIXES = frozenset({
    "github-pr-base-branch",
    "github-pr-owner-number",
    "merge",
    "remote",
    "vscode-merge-base",
})


class DeliveryControlError(RuntimeError):
    """Safe protocol error; messages never include command output or secrets."""

    def __init__(self, code: str, message: str, *, pending: bool = False):
        self.code = code
        self.pending = pending
        super().__init__(message)


@contextlib.contextmanager
def _controller_connect_closing(db_path: Path):
    """Open an existing initialized Kanban DB without importing agent state.

    The trusted controller never creates or migrates the shared database.  It
    accepts only the schema it was reviewed against, configures the same WAL
    durability pragmas as Kanban, and fails closed on a missing/partial schema.
    Keeping this connector local prevents the immutable controller runtime from
    importing ``hermes_state`` and the general agent/provider stack merely to
    execute fixed native transitions.
    """

    path = Path(db_path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DeliveryControlError(
            "database_unavailable", "trusted Kanban database is unavailable"
        ) from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise DeliveryControlError(
            "database_untrusted", "trusted Kanban database metadata is invalid"
        )
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=120.0,
    )
    try:
        connection.row_factory = sqlite3.Row
        try:
            connection.enable_load_extension(False)
        except AttributeError:
            pass
        connection.execute("PRAGMA busy_timeout=120000")
        journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal is None or str(journal[0]).casefold() != "wal":
            raise DeliveryControlError(
                "database_journal_unsafe", "trusted Kanban database is not in WAL mode"
            )
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA wal_autocheckpoint=100")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA cell_size_check=ON")
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' LIMIT 1"
        ).fetchone() is not None:
            raise DeliveryControlError(
                "database_schema_unsupported",
                "trusted Kanban database contains an unsupported trigger",
            )
        for table, required_columns in _CONTROL_DB_SCHEMA.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            columns = {str(row[1]) for row in rows}
            if not required_columns.issubset(columns):
                raise DeliveryControlError(
                    "database_schema_unsupported",
                    "trusted Kanban database schema is incomplete",
                )
        yield connection
    finally:
        connection.close()


@dataclass(frozen=True)
class ControlRequest:
    action: str
    task_id: str
    run_id: int
    claim_lock: str
    summary: str


@dataclass(frozen=True)
class PublishResult:
    pull_request: dict[str, Any]


@dataclass(frozen=True)
class ReviewResult:
    delivery: dict[str, Any]


@dataclass(frozen=True)
class OperationSnapshot:
    op_id: str
    request_sha256: str
    claim_sha256: str
    task: kb.Task
    policy: Mapping[str, Any]
    fingerprint: tuple[Any, ...]
    candidate_head: str
    submission_event_id: int | None
    submission_sha256: str | None
    submission: Mapping[str, Any] | None


class DeliveryBackend(Protocol):
    def candidate_identity(self, task: kb.Task) -> str: ...

    def publish(
        self,
        task: kb.Task,
        policy: Mapping[str, Any],
        *,
        run_id: int,
        guard: Callable[..., None],
    ) -> PublishResult: ...

    def verify_submission(
        self,
        task: kb.Task,
        policy: Mapping[str, Any],
        pull_request: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def review(
        self,
        task: kb.Task,
        policy: Mapping[str, Any],
        submission: Mapping[str, Any],
        *,
        guard: Callable[..., None],
        kanban_home: Path,
    ) -> ReviewResult: ...

    def verify_terminal(
        self,
        task: kb.Task,
        policy: Mapping[str, Any],
        submission: Mapping[str, Any],
        delivery: Mapping[str, Any],
        *,
        kanban_home: Path,
    ) -> Mapping[str, Any]: ...


def _protocol_error(code: str, message: str) -> DeliveryControlError:
    return DeliveryControlError(code, message)


def _require_base_state_root() -> Path:
    """Require the controller-owned non-worker-visible base state directory."""

    try:
        metadata = BASE_STATE_ROOT.lstat()
        if (
            BASE_STATE_ROOT.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != FIXED_WORKER_UID
            or metadata.st_gid != FIXED_WORKER_UID
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or BASE_STATE_ROOT.resolve(strict=True) != BASE_STATE_ROOT
        ):
            raise _protocol_error(
                "base_refresh_state_untrusted",
                "trusted base refresh state metadata is unsafe",
            )
    except DeliveryControlError:
        raise
    except (OSError, RuntimeError) as exc:
        raise _protocol_error(
            "base_refresh_state_unavailable",
            "trusted base refresh state is unavailable",
        ) from exc
    return BASE_STATE_ROOT


def require_base_repository_mirror() -> Path:
    """Require the exact controller-owned config-free bare repository."""

    _require_base_state_root()
    try:
        metadata = BASE_REPOSITORY_MIRROR.lstat()
        config = BASE_REPOSITORY_MIRROR / "config"
        objects = BASE_REPOSITORY_MIRROR / "objects"
        refs = BASE_REPOSITORY_MIRROR / "refs"
        config_metadata = config.lstat()
        objects_metadata = objects.lstat()
        refs_metadata = refs.lstat()
        if (
            BASE_REPOSITORY_MIRROR.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != FIXED_WORKER_UID
            or metadata.st_gid != FIXED_WORKER_UID
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or BASE_REPOSITORY_MIRROR.resolve(strict=True) != BASE_REPOSITORY_MIRROR
            or config.is_symlink()
            or not stat.S_ISREG(config_metadata.st_mode)
            or config_metadata.st_uid != FIXED_WORKER_UID
            or config_metadata.st_gid != FIXED_WORKER_UID
            or config_metadata.st_nlink != 1
            or stat.S_IMODE(config_metadata.st_mode) != 0o600
            or any(
                path.is_symlink()
                or not stat.S_ISDIR(item.st_mode)
                or item.st_uid != FIXED_WORKER_UID
                or item.st_gid != FIXED_WORKER_UID
                or stat.S_IMODE(item.st_mode) & 0o077
                for path, item in (
                    (objects, objects_metadata),
                    (refs, refs_metadata),
                )
            )
        ):
            raise _protocol_error(
                "base_repository_untrusted", "trusted base repository metadata is unsafe",
            )
    except DeliveryControlError:
        raise
    except (OSError, RuntimeError) as exc:
        raise _protocol_error(
            "base_repository_unavailable", "trusted base repository is unavailable",
        ) from exc
    return BASE_REPOSITORY_MIRROR


def _base_receipt_metadata_safe(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == FIXED_WORKER_UID
        and metadata.st_gid == FIXED_WORKER_UID
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and 0 < metadata.st_size <= 4096
    )


def _read_base_receipt_bytes() -> bytes:
    """Open the fixed receipt through its trusted directory without TOCTOU."""

    directory = _require_base_state_root()
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = -1
    receipt_fd = -1
    verify_fd = -1
    try:
        directory_fd = os.open(directory, directory_flags)
        directory_metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != FIXED_WORKER_UID
            or directory_metadata.st_gid != FIXED_WORKER_UID
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
        ):
            raise _protocol_error(
                "base_refresh_state_untrusted",
                "trusted base refresh state metadata is unsafe",
            )
        receipt_fd = os.open(
            BASE_REFRESH_RECEIPT.name,
            file_flags,
            dir_fd=directory_fd,
        )
        before = os.fstat(receipt_fd)
        if not _base_receipt_metadata_safe(before):
            raise _protocol_error(
                "base_refresh_untrusted", "authenticated base refresh receipt is unsafe",
            )
        chunks: list[bytes] = []
        remaining = 4097
        while remaining:
            chunk = os.read(receipt_fd, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(receipt_fd)
        verify_fd = os.open(
            BASE_REFRESH_RECEIPT.name,
            file_flags,
            dir_fd=directory_fd,
        )
        current = os.fstat(verify_fd)
        stable_fields = (
            "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
            "st_size", "st_mtime_ns", "st_ctime_ns",
        )
        if (
            len(raw) == 0
            or len(raw) > 4096
            or any(getattr(before, field) != getattr(after, field) for field in stable_fields)
            or before.st_dev != current.st_dev
            or before.st_ino != current.st_ino
            or not _base_receipt_metadata_safe(current)
        ):
            raise _protocol_error(
                "base_refresh_raced", "authenticated base refresh receipt changed during read",
            )
        return raw
    except DeliveryControlError:
        raise
    except OSError as exc:
        raise _protocol_error(
            "base_refresh_missing", "authenticated base refresh receipt is unavailable",
        ) from exc
    finally:
        for descriptor in (verify_fd, receipt_fd, directory_fd):
            if descriptor >= 0:
                os.close(descriptor)


def _publish_base_receipt(payload: Mapping[str, Any]) -> None:
    """Atomically publish one bounded controller-authenticated base receipt."""

    directory = _require_base_state_root()
    raw = _canonical_json(payload)
    if len(raw) > 4096:
        raise _protocol_error("base_refresh_invalid", "base refresh receipt is oversized")
    temporary_name = f".clauseye-main.{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    directory_fd = -1
    try:
        directory_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            existing = os.stat(
                BASE_REFRESH_RECEIPT.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and not _base_receipt_metadata_safe(existing):
            raise _protocol_error(
                "base_refresh_untrusted", "existing base refresh receipt is unsafe",
            )
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short base receipt write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            BASE_REFRESH_RECEIPT.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except DeliveryControlError:
        raise
    except OSError as exc:
        raise _protocol_error(
            "base_refresh_publish_failed", "authenticated base refresh could not be published",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_fd >= 0:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
            os.close(directory_fd)


def read_fresh_base_receipt(
    repo_root: Path,
    *,
    now: int | None = None,
) -> str:
    """Return the exact fresh controller-authenticated default-branch SHA."""

    try:
        mirror = require_base_repository_mirror()
        if Path(repo_root) != mirror or Path(repo_root).resolve(strict=True) != mirror:
            raise _protocol_error(
                "base_refresh_repository_mismatch",
                "authenticated base refresh is not for this repository",
            )
        payload = json.loads(_read_base_receipt_bytes().decode("ascii"))
    except DeliveryControlError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise _protocol_error(
            "base_refresh_invalid", "authenticated base refresh receipt is invalid",
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema", "repository", "ref", "sha", "fetched_at",
    }:
        raise _protocol_error(
            "base_refresh_invalid", "authenticated base refresh receipt shape is invalid",
        )
    sha = str(payload.get("sha") or "").lower()
    fetched_at = payload.get("fetched_at")
    current = int(time.time()) if now is None else int(now)
    if (
        payload.get("schema") != BASE_REFRESH_SCHEMA
        or payload.get("repository") != FIXED_REPOSITORY
        or payload.get("ref") != f"refs/heads/{FIXED_DEFAULT_BRANCH}"
        or not _SHA_RE.fullmatch(sha)
        or not isinstance(fetched_at, int)
        or isinstance(fetched_at, bool)
        or fetched_at > current + 5
        or current - fetched_at > BASE_REFRESH_MAX_AGE_SECONDS
    ):
        raise _protocol_error(
            "base_refresh_stale", "authenticated default-branch refresh is stale",
        )
    return sha


def parse_request(raw: bytes) -> ControlRequest:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise _protocol_error("request_size_invalid", "request size is invalid")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _protocol_error("request_json_invalid", "request JSON is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema", "action", "task_id", "run_id", "claim_lock", "summary",
    }:
        raise _protocol_error(
            "request_shape_invalid", "request must contain the exact protocol fields"
        )
    if value.get("schema") != PROTOCOL_SCHEMA:
        raise _protocol_error("request_schema_invalid", "request schema is unsupported")
    action = str(value.get("action") or "")
    if action not in {"builder_publish", "reviewer_complete"}:
        raise _protocol_error("request_action_invalid", "request action is unsupported")
    task_id = str(value.get("task_id") or "")
    if not _TASK_ID_RE.fullmatch(task_id):
        raise _protocol_error("task_id_invalid", "task id is not canonical")
    run_id = value.get("run_id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise _protocol_error("run_id_invalid", "run id must be a positive integer")
    claim_lock = str(value.get("claim_lock") or "")
    if not _CLAIM_RE.fullmatch(claim_lock):
        raise _protocol_error("claim_lock_invalid", "claim capability is malformed")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise _protocol_error("summary_invalid", "a non-empty summary is required")
    summary = summary.strip()
    if len(summary.encode("utf-8")) > MAX_SUMMARY_BYTES:
        raise _protocol_error("summary_invalid", "summary is too large")
    return ControlRequest(action, task_id, run_id, claim_lock, summary)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")


def request_worker_action(
    *,
    action: str,
    task_id: str,
    run_id: int,
    claim_lock: str,
    summary: str,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Send one bounded worker request to the fixed privileged socket."""

    request = ControlRequest(action, task_id, run_id, claim_lock, summary)
    raw = _canonical_json({"schema": PROTOCOL_SCHEMA, **request.__dict__})
    # Validate locally too; malformed model-controlled input never reaches the
    # privileged parser and no socket path can be selected by the caller.
    parse_request(raw)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(SOCKET_PATH))
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise _protocol_error(
                        "control_response_invalid", "delivery control response is oversized"
                    )
                chunks.append(chunk)
    except DeliveryControlError:
        raise
    except OSError as exc:
        raise DeliveryControlError(
            "control_unavailable",
            "trusted delivery control is unavailable; task remains in-flight",
            pending=True,
        ) from exc
    try:
        response = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _protocol_error(
            "control_response_invalid", "delivery control returned invalid JSON"
        ) from exc
    if (
        not isinstance(response, dict)
        or response.get("schema") != PROTOCOL_SCHEMA
        or not isinstance(response.get("ok"), bool)
    ):
        raise _protocol_error(
            "control_response_invalid", "delivery control returned an invalid envelope"
        )
    return response


def _valid_branch(task_id: str, branch: str) -> bool:
    if (
        not _BRANCH_RE.fullmatch(branch)
        or ".." in branch
        or "//" in branch
        or branch.endswith(("/", ".", ".lock"))
    ):
        return False
    return re.search(rf"(?:^|/){re.escape(task_id)}(?:[-/]|$)", branch) is not None


def _task_fingerprint(task: kb.Task) -> tuple[Any, ...]:
    return (
        task.id,
        task.title,
        task.body,
        task.status,
        task.assignee,
        task.workspace_kind,
        task.workspace_path,
        task.branch_name,
        task.worktree_base_sha,
        task.project_id,
        task.current_run_id,
        task.claim_lock,
        task.worker_pid,
    )


def _request_sha256(request: ControlRequest) -> str:
    # Peer PID is live socket authorization only. It must not enter the durable
    # idempotency key, otherwise a broker/worker restart cannot reconcile the
    # same exact remote operation.
    material = _canonical_json({
        "schema": PROTOCOL_SCHEMA,
        "action": request.action,
        "task_id": request.task_id,
        "run_id": request.run_id,
        "claim_sha256": hashlib.sha256(request.claim_lock.encode("utf-8")).hexdigest(),
        "summary_sha256": hashlib.sha256(request.summary.encode("utf-8")).hexdigest(),
    })
    return hashlib.sha256(material).hexdigest()


def _claim_sha256(claim_lock: str) -> str:
    return hashlib.sha256(claim_lock.encode("utf-8")).hexdigest()


def _submission_sha256(event_id: int, submission: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json({"event_id": int(event_id), "submission": dict(submission)})
    ).hexdigest()


def _request_audit_identity(
    request: ControlRequest,
    peer_pid: int,
    peer_uid: int,
) -> dict[str, Any]:
    return {
        "delivery_control": {
            "schema": PROTOCOL_SCHEMA,
            "request_sha256": _request_sha256(request),
            "claim_sha256": _claim_sha256(request.claim_lock),
            "worker_pid": int(peer_pid),
            "worker_uid": int(peer_uid),
        }
    }


class DeliveryControl:
    """SQLite-bound state machine behind the privileged socket."""

    def __init__(
        self,
        db_path: Path,
        backend: DeliveryBackend,
        *,
        reviewer_profile: str = FIXED_REVIEWER_PROFILE,
        allowed_worker_uid: int = FIXED_WORKER_UID,
    ):
        raw_db_path = Path(db_path).absolute()
        try:
            if raw_db_path.is_symlink() or raw_db_path.resolve(strict=True) != raw_db_path:
                raise OSError("Kanban DB path is redirected")
        except OSError as exc:
            raise DeliveryControlError(
                "database_untrusted", "trusted Kanban database path is not exact"
            ) from exc
        self.db_path = raw_db_path
        self.backend = backend
        self.reviewer_profile = reviewer_profile
        self.allowed_worker_uid = int(allowed_worker_uid)
        self._owner_instance = secrets.token_hex(32)
        self._active_calls: set[str] = set()
        self._operation_lock = threading.RLock()
        self._recoverable_operations: set[str] = set()

    def prepare_recovery(self) -> None:
        """Authorize this freshly-started singleton broker to reconcile orphans.

        No wall-clock lease is ever stolen. systemd socket activation guarantees
        one serving process; startup explicitly snapshots operations left by the
        previous process. A retry then re-derives fixed remote facts through the
        idempotent backend before committing or quarantining native state.
        """

        with _controller_connect_closing(self.db_path) as conn:
            rows = conn.execute(
                "SELECT op_id FROM delivery_control_operations "
                "WHERE state IN ('in_progress','remote_applied')"
            ).fetchall()
        self._recoverable_operations = {str(row["op_id"]) for row in rows}

    def _quarantine_operation(self, op_id: str) -> None:
        with _controller_connect_closing(self.db_path) as conn, kb.write_txn(conn):
            conn.execute(
                "UPDATE delivery_control_operations SET state = 'quarantined', "
                "stage = 'identity_unproven', updated_at = ? WHERE op_id = ? "
                "AND state IN ('in_progress','remote_applied')",
                (int(time.time()), op_id),
            )

    def reconcile_orphans(self) -> dict[str, int]:
        """Autonomously resume durable operations without the model/worker.

        The broker rebuilds the request only from the fixed DB row plus the
        exact task/run capability already held by the trusted control plane.
        It never logs or returns that capability. Remote calls are idempotent:
        exact-SHA push uses a lease, PR creation re-queries/reuses the canonical
        PR, and reviewer recovery observes an exact merged PR before retrying
        CI/deployment/native completion.
        """

        with _controller_connect_closing(self.db_path) as conn:
            rows = conn.execute(
                "SELECT o.*, t.id AS active_task_id, "
                "t.claim_lock AS active_claim, t.worker_pid AS active_pid "
                "FROM delivery_control_operations o LEFT JOIN tasks t ON t.id = o.task_id "
                "WHERE o.state IN ('in_progress','remote_applied') "
                "ORDER BY o.created_at, o.op_id"
            ).fetchall()
        result = {"attempted": 0, "completed": 0, "pending": 0, "quarantined": 0}
        identity_failures = {
            "task_missing", "run_stale", "claim_stale", "run_source_mismatch",
            "workspace_invalid", "workspace_missing", "worktree_link_invalid",
            "repository_mismatch", "git_config_unsafe", "head_invalid",
            "candidate_drifted", "workspace_dirty", "branch_invalid",
            "branch_mismatch", "project_unbound", "contract_invalid",
            "submission_missing", "submission_drifted", "review_round_stale",
            "task_drifted", "operation_identity_mismatch", "operation_state_invalid",
            "operation_receipt_invalid", "premerge_receipt_missing",
        }
        for row in rows:
            op_id = str(row["op_id"])
            if row["active_task_id"] is None:
                # A supported lifecycle writer cannot delete a fenced task,
                # but corruption or an out-of-band SQL writer still must not
                # make the durable operation disappear from reconciliation.
                result["attempted"] += 1
                self._quarantine_operation(op_id)
                result["quarantined"] += 1
                continue
            claim_lock = str(row["active_claim"] or "")
            try:
                request = ControlRequest(
                    action=str(row["action"]),
                    task_id=str(row["task_id"]),
                    run_id=int(row["run_id"]),
                    claim_lock=claim_lock,
                    summary=str(row["summary"]),
                )
                # Apply the same exact shape/size checks without exposing the
                # DB-derived capability in argv, response, or journal.
                parse_request(_canonical_json({
                    "schema": PROTOCOL_SCHEMA,
                    **request.__dict__,
                }))
                self._recoverable_operations.add(op_id)
                result["attempted"] += 1
                self.handle(
                    request,
                    peer_pid=int(row["active_pid"] or 0),
                    peer_uid=int(row["peer_uid"]),
                    privileged=True,
                )
                result["completed"] += 1
            except DeliveryControlError as exc:
                if exc.pending:
                    result["pending"] += 1
                elif exc.code in identity_failures:
                    self._quarantine_operation(op_id)
                    result["quarantined"] += 1
                else:
                    # Command/service failures can be ambiguous after a remote
                    # write. Keep the non-expiring fence and retry; never guess
                    # that absence of a local receipt means absence remotely.
                    result["pending"] += 1
            except Exception:
                result["pending"] += 1
        return result

    def _kanban_home(self) -> Path:
        parent = self.db_path.parent
        if parent.parent.name == "boards" and parent.parent.parent.name == "kanban":
            return parent.parent.parent.parent
        return parent

    def _run_row(self, conn: Any, request: ControlRequest) -> Any:
        return conn.execute(
            "SELECT * FROM task_runs WHERE id = ? AND task_id = ?",
            (request.run_id, request.task_id),
        ).fetchone()

    def _idempotent_response(
        self,
        conn: Any,
        task: kb.Task,
        request: ControlRequest,
    ) -> dict[str, Any] | None:
        operation = conn.execute(
            "SELECT * FROM delivery_control_operations "
            "WHERE task_id = ? AND run_id = ? AND action = ?",
            (request.task_id, request.run_id, request.action),
        ).fetchone()
        if operation is not None and operation["state"] in {"committed", "rejected"}:
            if (
                not hmac.compare_digest(
                    str(operation["request_sha256"]), _request_sha256(request),
                )
                or not hmac.compare_digest(
                    str(operation["claim_sha256"]), _claim_sha256(request.claim_lock),
                )
            ):
                raise _protocol_error(
                    "operation_identity_mismatch",
                    "committed delivery operation has a different immutable identity",
                )
            try:
                receipt = json.loads(operation["receipt"] or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise _protocol_error(
                    "operation_receipt_invalid", "committed delivery receipt is invalid"
                ) from exc
            if operation["state"] == "rejected":
                stages = receipt.get("stages") if isinstance(receipt, dict) else None
                pull_stage = (
                    stages.get("pull_request")
                    if isinstance(stages, dict) else None
                )
                pull_request = (
                    pull_stage.get("pull_request")
                    if isinstance(pull_stage, dict) else None
                )
                rejection = (
                    stages.get("acceptance_rejected")
                    if isinstance(stages, dict) else None
                )
                run = self._run_row(conn, request)
                events = conn.execute(
                    "SELECT payload FROM task_events WHERE task_id = ? "
                    "AND run_id = ? AND kind = 'acceptance_rework_required'",
                    (request.task_id, request.run_id),
                ).fetchall()
                try:
                    event = (
                        json.loads(str(events[0]["payload"] or ""))
                        if len(events) == 1 else None
                    )
                except (TypeError, json.JSONDecodeError):
                    event = None
                rejection_keys = {
                    "code", "evidence_path", "evidence_sha256", "contract_hash",
                    "candidate_head", "review_run_id", "submission_event_id",
                    "failed_indexes", "observed_exits",
                }
                event_keys = rejection_keys | {
                    "operation_id", "next_status", "executor_assignee",
                }
                pr_number = (
                    pull_request.get("pr_number")
                    if isinstance(pull_request, dict)
                    and isinstance(pull_request.get("pr_number"), int)
                    and not isinstance(pull_request.get("pr_number"), bool)
                    else 0
                )
                rejection_run_id = (
                    rejection.get("review_run_id")
                    if isinstance(rejection, dict)
                    and isinstance(rejection.get("review_run_id"), int)
                    and not isinstance(rejection.get("review_run_id"), bool)
                    else 0
                )
                rejection_submission_id = (
                    rejection.get("submission_event_id")
                    if isinstance(rejection, dict)
                    and isinstance(rejection.get("submission_event_id"), int)
                    and not isinstance(rejection.get("submission_event_id"), bool)
                    else 0
                )
                operation_submission_id = (
                    operation["submission_event_id"]
                    if isinstance(operation["submission_event_id"], int)
                    and not isinstance(operation["submission_event_id"], bool)
                    else 0
                )
                submission_row = (
                    conn.execute(
                        "SELECT id, task_id, run_id, kind, payload "
                        "FROM task_events WHERE id = ? AND task_id = ? "
                        "AND kind = 'submitted_for_review'",
                        (operation_submission_id, request.task_id),
                    ).fetchone()
                    if operation_submission_id > 0 else None
                )
                try:
                    submission = (
                        json.loads(str(submission_row["payload"] or ""))
                        if submission_row is not None else None
                    )
                except (TypeError, json.JSONDecodeError):
                    submission = None
                submission_pr_number = (
                    submission.get("pr_number")
                    if isinstance(submission, dict)
                    and isinstance(submission.get("pr_number"), int)
                    and not isinstance(submission.get("pr_number"), bool)
                    else 0
                )
                submission_builder_run_id = (
                    submission.get("builder_run_id")
                    if isinstance(submission, dict)
                    and isinstance(submission.get("builder_run_id"), int)
                    and not isinstance(submission.get("builder_run_id"), bool)
                    else 0
                )
                submission_review_round = (
                    submission.get("review_round")
                    if isinstance(submission, dict)
                    and isinstance(submission.get("review_round"), int)
                    and not isinstance(submission.get("review_round"), bool)
                    else 0
                )
                submission_submitted_at = (
                    submission.get("submitted_at")
                    if isinstance(submission, dict)
                    and isinstance(submission.get("submitted_at"), int)
                    and not isinstance(submission.get("submitted_at"), bool)
                    else 0
                )
                ledger = None
                if isinstance(rejection, dict):
                    try:
                        ledger = kb._read_delivery_acceptance_evidence_ledger(
                            task_id=request.task_id,
                            path=Path(str(rejection.get("evidence_path") or "")),
                            expected_sha256=str(
                                rejection.get("evidence_sha256") or ""
                            ),
                        )
                    except kb.DeliveryOperationInProgressError:
                        ledger = None
                ledger_results = (
                    ledger.get("results") if isinstance(ledger, dict) else None
                )
                ledger_exits: list[int] = []
                ledger_failed_indexes: list[int] = []
                ledger_results_valid = isinstance(ledger_results, list)
                if ledger_results_valid:
                    for index, observed in enumerate(ledger_results):
                        if (
                            not isinstance(observed, dict)
                            or set(observed) != {"cmd", "exit", "expect_ok", "ms"}
                            or not isinstance(observed.get("exit"), int)
                            or isinstance(observed.get("exit"), bool)
                            or not isinstance(observed.get("expect_ok"), bool)
                        ):
                            ledger_results_valid = False
                            break
                        ledger_exits.append(observed["exit"])
                        if observed["expect_ok"] is False:
                            ledger_failed_indexes.append(index)
                if (
                    request.action != "reviewer_complete"
                    or operation["stage"] != "native_rejected"
                    or not isinstance(stages, dict)
                    or set(stages) != {"pull_request", "acceptance_rejected"}
                    or not isinstance(pull_stage, dict)
                    or set(pull_stage) != {"pull_request"}
                    or not isinstance(pull_request, dict)
                    or set(pull_request) != {
                        "pr_url", "pr_number", "head_sha", "candidate_ref",
                        "body_sha256",
                    }
                    or pull_request.get("head_sha") != operation["candidate_head"]
                    or not isinstance(submission, dict)
                    or submission_row["task_id"] != request.task_id
                    or submission_row["kind"] != "submitted_for_review"
                    or int(submission_row["id"] or 0) != operation_submission_id
                    or submission_builder_run_id <= 0
                    or int(submission_row["run_id"] or 0)
                    != submission_builder_run_id
                    or submission_review_round <= 0
                    or submission.get("reviewer_assignee")
                    != FIXED_REVIEWER_PROFILE
                    or not isinstance(submission.get("executor_assignee"), str)
                    or not submission["executor_assignee"].strip()
                    or submission["executor_assignee"]
                    == FIXED_REVIEWER_PROFILE
                    or submission.get("head_sha")
                    != operation["candidate_head"]
                    or submission_pr_number <= 0
                    or submission.get("pr_url")
                    != (
                        "https://github.com/clauseye-com/clauseye-contra-rope/"
                        f"pull/{submission_pr_number}"
                    )
                    or re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(submission.get("contract_hash") or ""),
                    ) is None
                    or submission_submitted_at <= 0
                    or not hmac.compare_digest(
                        str(operation["submission_sha256"] or ""),
                        _submission_sha256(operation_submission_id, submission),
                    )
                    or pr_number <= 0
                    or pr_number != submission_pr_number
                    or pull_request.get("pr_url")
                    != submission.get("pr_url")
                    or pull_request.get("head_sha")
                    != submission.get("head_sha")
                    or pull_request.get("candidate_ref")
                    != submission.get("candidate_ref")
                    or re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(pull_request.get("body_sha256") or ""),
                    ) is None
                    or run is None
                    or run["task_id"] != request.task_id
                    or run["profile"] != FIXED_REVIEWER_PROFILE
                    or run["status"] != "triage"
                    or run["outcome"] != "acceptance_rejected"
                    or run["ended_at"] is None
                    or run["error"] != "acceptance_tier1_failed"
                    or run["claim_lock"] is not None
                    or run["claim_expires"] is not None
                    or run["worker_pid"] is not None
                    or not isinstance(rejection, dict)
                    or set(rejection) != rejection_keys
                    or rejection.get("code") != "acceptance_tier1_failed"
                    or rejection.get("candidate_head") != operation["candidate_head"]
                    or rejection_run_id != request.run_id
                    or rejection_submission_id != operation_submission_id
                    or rejection.get("contract_hash")
                    != submission.get("contract_hash")
                    or re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(rejection.get("evidence_sha256") or ""),
                    ) is None
                    or re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(rejection.get("contract_hash") or ""),
                    ) is None
                    or not isinstance(rejection.get("failed_indexes"), list)
                    or not rejection["failed_indexes"]
                    or not all(
                        isinstance(index, int) and not isinstance(index, bool)
                        for index in rejection["failed_indexes"]
                    )
                    or rejection["failed_indexes"]
                    != sorted(set(rejection["failed_indexes"]))
                    or not isinstance(rejection.get("observed_exits"), list)
                    or not all(
                        isinstance(exit_code, int) and not isinstance(exit_code, bool)
                        for exit_code in rejection["observed_exits"]
                    )
                    or not isinstance(ledger, dict)
                    or set(ledger) != {
                        "schema", "task_id", "domain", "contract_hash",
                        "candidate_sha", "results", "env_fingerprint", "run_by",
                        "review_run_id", "review_profile", "submission_event_id",
                        "verdict", "created_at",
                    }
                    or ledger.get("schema") != "hermes-evidence/v1"
                    or ledger.get("task_id") != request.task_id
                    or ledger.get("contract_hash") != rejection.get("contract_hash")
                    or ledger.get("candidate_sha") != operation["candidate_head"]
                    or ledger.get("run_by") != "reviewer"
                    or ledger.get("review_run_id") != request.run_id
                    or ledger.get("review_profile") != FIXED_REVIEWER_PROFILE
                    or ledger.get("submission_event_id")
                    != operation_submission_id
                    or ledger.get("verdict") != "fail"
                    or not ledger_results_valid
                    or ledger_exits != rejection.get("observed_exits")
                    or ledger_failed_indexes != rejection.get("failed_indexes")
                    or not isinstance(event, dict)
                    or set(event) != event_keys
                    or any(event.get(key) != value for key, value in rejection.items())
                    or event.get("operation_id") != operation["op_id"]
                    or event.get("next_status") != "triage"
                    or event.get("executor_assignee")
                    != submission.get("executor_assignee")
                ):
                    raise _protocol_error(
                        "operation_receipt_invalid",
                        "rejected delivery operation is inconsistent",
                    )
                return {
                    "schema": PROTOCOL_SCHEMA,
                    "ok": False,
                    "state": "rejected",
                    "code": "acceptance_tier1_failed",
                    "message": "canonical tier1 acceptance requires task rework",
                    "task_id": request.task_id,
                    "run_id": request.run_id,
                    "idempotent": True,
                }
            response = {
                "schema": PROTOCOL_SCHEMA,
                "ok": True,
                "state": (
                    "submitted" if request.action == "builder_publish" else "completed"
                ),
                "task_id": request.task_id,
                "run_id": request.run_id,
                "idempotent": True,
            }
            if request.action == "builder_publish":
                stages = receipt.get("stages") if isinstance(receipt, dict) else None
                pr_stage = stages.get("pull_request") if isinstance(stages, dict) else None
                pull_request = (
                    pr_stage.get("pull_request") if isinstance(pr_stage, dict) else None
                )
                if not isinstance(pull_request, dict) or not pull_request.get("pr_url"):
                    raise _protocol_error(
                        "operation_receipt_invalid", "committed publish receipt is invalid"
                    )
                response["pr_url"] = pull_request["pr_url"]
            return response

        # Compatibility for operations committed before durable receipts were
        # introduced. New requests always use the table path above.
        run = self._run_row(conn, request)
        try:
            metadata = json.loads(run["metadata"]) if run and run["metadata"] else {}
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        audit = metadata.get("delivery_control") if isinstance(metadata, dict) else None
        expected_request_sha = _request_sha256(request)
        if (
            run is None
            or not isinstance(audit, dict)
            or not hmac.compare_digest(
                str(audit.get("request_sha256") or ""),
                expected_request_sha,
            )
        ):
            return None
        if request.action == "builder_publish" and run["outcome"] == "submitted_for_review":
            record = kb._latest_review_submission_record(conn, request.task_id)
            if record is None or int(record[1].get("builder_run_id") or 0) != request.run_id:
                return None
            return {
                "schema": PROTOCOL_SCHEMA,
                "ok": True,
                "state": "submitted",
                "task_id": request.task_id,
                "run_id": request.run_id,
                "pr_url": record[1]["pr_url"],
                "idempotent": True,
            }
        if request.action == "reviewer_complete" and run["outcome"] == "completed":
            if task.status != "done":
                return None
            return {
                "schema": PROTOCOL_SCHEMA,
                "ok": True,
                "state": "completed",
                "task_id": request.task_id,
                "run_id": request.run_id,
                "idempotent": True,
            }
        return None

    def _authorize(
        self,
        conn: Any,
        request: ControlRequest,
        *,
        peer_pid: int,
        peer_uid: int,
        privileged: bool,
        allow_shipping: bool = False,
    ) -> tuple[kb.Task, Mapping[str, Any], tuple[Any, ...]]:
        if not privileged and peer_uid != self.allowed_worker_uid:
            raise _protocol_error("peer_unauthorized", "socket peer is not authorized")
        task = kb.get_task(conn, request.task_id)
        if task is None:
            raise _protocol_error("task_missing", "task does not exist")
        if kb._controlled_worker_pending(conn, task.id):
            raise DeliveryControlError(
                "worker_cleanup_pending",
                "previous controlled worker cleanup is still pending",
                pending=True,
            )
        allowed_statuses = {"running", "shipping"} if allow_shipping else {"running"}
        if task.status not in allowed_statuses or task.current_run_id != request.run_id:
            raise _protocol_error("run_stale", "task no longer has this active run")
        if not hmac.compare_digest(str(task.claim_lock or ""), request.claim_lock):
            raise _protocol_error("claim_stale", "run capability is stale")
        run = self._run_row(conn, request)
        if (
            run is None
            or run["status"] != "running"
            or run["outcome"] is not None
            or run["ended_at"] is not None
            or not hmac.compare_digest(str(run["claim_lock"] or ""), request.claim_lock)
            or int(run["worker_pid"] or 0) != int(task.worker_pid or 0)
        ):
            raise _protocol_error("run_stale", "active run identity is inconsistent")
        if not privileged and int(task.worker_pid or 0) != peer_pid:
            raise _protocol_error("peer_pid_mismatch", "socket peer is not the active worker")
        if (
            task.status != "shipping"
            and (task.claim_expires is None or int(task.claim_expires) <= int(time.time()))
        ):
            raise _protocol_error("claim_expired", "active run capability has expired")
        expected_source = "ready" if request.action == "builder_publish" else "review"
        if kb._run_source_status(conn, task.id, request.run_id) != expected_source:
            raise _protocol_error(
                "run_source_mismatch", "active run does not have the required queue source"
            )
        if task.workspace_kind != "worktree" or not task.workspace_path:
            raise _protocol_error("workspace_invalid", "delivery task is not a worktree")
        workspace = Path(task.workspace_path)
        if not workspace.is_absolute() or workspace.name != task.id:
            raise _protocol_error(
                "workspace_invalid", "task workspace is not bound to its exact task id"
            )
        branch = str(task.branch_name or "")
        if not _valid_branch(task.id, branch):
            raise _protocol_error("branch_invalid", "task branch is not canonical")
        if not str(task.project_id or "").strip():
            raise _protocol_error("project_unbound", "task is not project-bound")
        policy = kb._completion_delivery_policy(task)
        kb._require_valid_delivery_contract(policy)
        if policy.get("pr_gate") != "merge" or not policy.get("contract_hash"):
            raise _protocol_error(
                "contract_invalid", "task lacks an exact merge-gated contract"
            )
        if request.action == "builder_publish":
            if task.assignee == self.reviewer_profile:
                raise _protocol_error("builder_role_invalid", "reviewer cannot publish a builder run")
        else:
            if task.assignee != self.reviewer_profile:
                raise _protocol_error("reviewer_role_invalid", "active run is not owned by reviewer")
            record = kb._latest_review_submission_record(conn, task.id)
            if record is None:
                raise _protocol_error("submission_missing", "review submission is missing")
            submission_id, submission = record
            count = int(conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
                "AND kind = 'submitted_for_review'",
                (task.id,),
            ).fetchone()[0])
            if (
                int(submission.get("review_round") or 0) != count
                or submission.get("reviewer_assignee") != self.reviewer_profile
                or submission.get("contract_hash") != policy.get("contract_hash")
                or int(submission_id) <= 0
            ):
                raise _protocol_error(
                    "review_round_stale", "latest submission/round is not authoritative"
                )
        return task, policy, _task_fingerprint(task)

    def _candidate_head(self, task: kb.Task) -> str:
        candidate_identity = getattr(self.backend, "candidate_identity", None)
        if not callable(candidate_identity):
            raise _protocol_error(
                "candidate_identity_unavailable",
                "trusted backend cannot bind an immutable candidate",
            )
        head = str(candidate_identity(task) or "").strip().lower()
        if not _SHA_RE.fullmatch(head):
            raise _protocol_error("head_invalid", "candidate HEAD is not immutable")
        return head

    def _review_submission(
        self,
        conn: Any,
        request: ControlRequest,
    ) -> tuple[int | None, Mapping[str, Any] | None, str | None]:
        if request.action != "reviewer_complete":
            return None, None, None
        record = kb._latest_review_submission_record(conn, request.task_id)
        if record is None:
            raise _protocol_error("submission_missing", "review submission is missing")
        event_id, value = record
        submission = dict(value)
        return int(event_id), submission, _submission_sha256(int(event_id), submission)

    @staticmethod
    def _receipt_stages(raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise _protocol_error(
                "operation_receipt_invalid", "delivery receipt is invalid"
            ) from exc
        stages = value.get("stages") if isinstance(value, dict) else None
        if not isinstance(stages, dict):
            raise _protocol_error(
                "operation_receipt_invalid", "delivery receipt history is invalid"
            )
        return dict(stages)

    @classmethod
    def _safe_receipt(
        cls,
        previous: str | None,
        stage: str,
        receipt: Mapping[str, Any],
    ) -> str:
        if stage not in {"push", "pull_request", "premerge", "merge", "deployment"}:
            raise _protocol_error("operation_stage_invalid", "delivery receipt stage is invalid")
        if not isinstance(receipt, Mapping):
            raise _protocol_error("operation_receipt_invalid", "delivery receipt is invalid")
        forbidden = {"token", "secret", "credential", "claim_lock", "capability"}

        def validate(value: Any) -> None:
            if isinstance(value, Mapping):
                for key, item in value.items():
                    normalized_key = str(key).casefold()
                    if (
                        normalized_key in forbidden
                        or normalized_key.endswith("_token")
                        or normalized_key.endswith("_secret")
                        or normalized_key.endswith("_credential")
                    ):
                        raise _protocol_error(
                            "operation_receipt_invalid", "delivery receipt contains a forbidden field"
                        )
                    validate(item)
            elif isinstance(value, list):
                for item in value:
                    validate(item)
            elif value is not None and not isinstance(value, (str, int, float, bool)):
                raise _protocol_error("operation_receipt_invalid", "delivery receipt is not canonical")

        stages = cls._receipt_stages(previous)
        stages[stage] = dict(receipt)
        envelope = {"stages": stages}
        validate(envelope)
        encoded = _canonical_json(envelope)
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise _protocol_error("operation_receipt_invalid", "delivery receipt is oversized")
        return encoded.decode("ascii")

    def _acquire_operation(
        self,
        request: ControlRequest,
        *,
        peer_pid: int,
        peer_uid: int,
        privileged: bool,
    ) -> OperationSnapshot | dict[str, Any]:
        # Candidate inspection intentionally happens before BEGIN IMMEDIATE.
        # The transaction re-authorizes the task, then the first pre-mutation
        # guard re-inspects HEAD against the durable binding.
        with _controller_connect_closing(self.db_path) as conn:
            task = kb.get_task(conn, request.task_id)
            if task is not None:
                previous = self._idempotent_response(conn, task, request)
                if previous is not None:
                    return previous
            task, _policy, _fingerprint = self._authorize(
                conn,
                request,
                peer_pid=peer_pid,
                peer_uid=peer_uid,
                privileged=privileged,
                allow_shipping=True,
            )
        candidate_head = self._candidate_head(task)
        now = int(time.time())
        request_sha = _request_sha256(request)
        claim_sha = _claim_sha256(request.claim_lock)

        with _controller_connect_closing(self.db_path) as conn, kb.write_txn(conn):
            task, policy, _fingerprint = self._authorize(
                conn,
                request,
                peer_pid=peer_pid,
                peer_uid=peer_uid,
                privileged=privileged,
                allow_shipping=True,
            )
            submission_id, submission, submission_sha = self._review_submission(
                conn, request,
            )
            existing = conn.execute(
                "SELECT * FROM delivery_control_operations "
                "WHERE task_id = ? AND run_id = ? AND action = ?",
                (request.task_id, request.run_id, request.action),
            ).fetchone()
            if existing is None:
                if task.status != "running":
                    raise _protocol_error(
                        "operation_state_invalid", "delivery task is fenced without an operation"
                    )
                op_id = secrets.token_hex(32)
                conn.execute(
                    "INSERT INTO delivery_control_operations ("
                    "op_id, task_id, run_id, action, request_sha256, claim_sha256, summary, "
                    "candidate_head, submission_event_id, submission_sha256, peer_uid, "
                    "owner_instance, state, stage, receipt, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', "
                    "'authorized', NULL, ?, ?)",
                    (
                        op_id, request.task_id, request.run_id, request.action,
                        request_sha, claim_sha, request.summary, candidate_head, submission_id,
                        submission_sha, int(peer_uid), self._owner_instance, now, now,
                    ),
                )
                cur = conn.execute(
                    "UPDATE tasks SET status = 'shipping' "
                    "WHERE id = ? AND status = 'running' AND current_run_id = ? "
                    "AND claim_lock IS ? AND worker_pid IS ?",
                    (
                        request.task_id, request.run_id, request.claim_lock,
                        task.worker_pid,
                    ),
                )
                if cur.rowcount != 1:
                    raise _protocol_error(
                        "task_drifted", "task changed before delivery fence acquisition"
                    )
                task = kb.get_task(conn, request.task_id)
                assert task is not None
            else:
                op_id = str(existing["op_id"])
                exact = (
                    hmac.compare_digest(str(existing["request_sha256"]), request_sha)
                    and hmac.compare_digest(str(existing["claim_sha256"]), claim_sha)
                    and str(existing["candidate_head"]) == candidate_head
                    and existing["submission_event_id"] == submission_id
                    and existing["submission_sha256"] == submission_sha
                    and int(existing["peer_uid"]) == int(peer_uid)
                )
                if not exact:
                    raise _protocol_error(
                        "operation_identity_mismatch",
                        "active delivery operation has a different immutable identity",
                    )
                if existing["state"] == "quarantined":
                    raise _protocol_error(
                        "operation_quarantined",
                        "delivery operation requires trusted operator reconciliation",
                    )
                if existing["state"] == "committed":
                    previous = self._idempotent_response(conn, task, request)
                    if previous is None:
                        raise _protocol_error(
                            "operation_receipt_invalid", "committed operation is inconsistent"
                        )
                    return previous
                owner = str(existing["owner_instance"])
                if owner != self._owner_instance:
                    if op_id not in self._recoverable_operations:
                        raise DeliveryControlError(
                            "delivery_operation_in_progress",
                            "trusted delivery operation is owned by the active broker",
                            pending=True,
                        )
                    conn.execute(
                        "UPDATE delivery_control_operations "
                        "SET owner_instance = ?, updated_at = ? WHERE op_id = ? "
                        "AND owner_instance = ? AND state IN ('in_progress','remote_applied')",
                        (self._owner_instance, now, op_id, owner),
                    )
                    self._recoverable_operations.discard(op_id)
                if task.status != "shipping":
                    raise _protocol_error(
                        "operation_state_invalid", "delivery operation lost its task fence"
                    )
            return OperationSnapshot(
                op_id=op_id,
                request_sha256=request_sha,
                claim_sha256=claim_sha,
                task=task,
                policy=policy,
                fingerprint=_task_fingerprint(task),
                candidate_head=candidate_head,
                submission_event_id=submission_id,
                submission_sha256=submission_sha,
                submission=submission,
            )

    def _guard(
        self,
        request: ControlRequest,
        snapshot: OperationSnapshot,
        *,
        peer_pid: int,
        peer_uid: int,
        privileged: bool,
        stage: str | None = None,
        receipt: Mapping[str, Any] | None = None,
    ) -> None:
        candidate_head = self._candidate_head(snapshot.task)
        with _controller_connect_closing(self.db_path) as conn, kb.write_txn(conn):
            task, _policy, current = self._authorize(
                conn,
                request,
                peer_pid=peer_pid,
                peer_uid=peer_uid,
                privileged=privileged,
                allow_shipping=True,
            )
            submission_id, _submission, submission_sha = self._review_submission(
                conn, request,
            )
            operation = conn.execute(
                "SELECT * FROM delivery_control_operations WHERE op_id = ?",
                (snapshot.op_id,),
            ).fetchone()
            if (
                current != snapshot.fingerprint
                or task.id != request.task_id
                or task.status != "shipping"
                or candidate_head != snapshot.candidate_head
                or submission_id != snapshot.submission_event_id
                or submission_sha != snapshot.submission_sha256
                or operation is None
                or operation["state"] not in {"in_progress", "remote_applied"}
                or str(operation["owner_instance"]) != self._owner_instance
                or not hmac.compare_digest(
                    str(operation["request_sha256"]), snapshot.request_sha256,
                )
                or not hmac.compare_digest(
                    str(operation["claim_sha256"]), snapshot.claim_sha256,
                )
            ):
                raise _protocol_error("task_drifted", "task changed during delivery operation")
            if stage == "require_premerge":
                premerge = self._receipt_stages(operation["receipt"]).get("premerge")
                if not isinstance(premerge, dict) or (
                    premerge.get("head_sha") != snapshot.candidate_head
                    or premerge.get("submission_event_id") != snapshot.submission_event_id
                    or premerge.get("contract_hash") != snapshot.policy.get("contract_hash")
                ):
                    raise _protocol_error(
                        "premerge_receipt_missing",
                        "already-merged delivery lacks exact durable pre-merge authorization",
                    )
            elif stage is not None:
                encoded_receipt = self._safe_receipt(
                    operation["receipt"], stage, receipt or {},
                )
                conn.execute(
                    "UPDATE delivery_control_operations SET state = 'remote_applied', "
                    "stage = ?, receipt = ?, updated_at = ? WHERE op_id = ?",
                    (stage, encoded_receipt, int(time.time()), snapshot.op_id),
                )

    def handle(
        self,
        request: ControlRequest,
        *,
        peer_pid: int,
        peer_uid: int,
        privileged: bool = False,
    ) -> dict[str, Any]:
        if not privileged and peer_uid != self.allowed_worker_uid:
            raise _protocol_error("peer_unauthorized", "socket peer is not authorized")
        with self._operation_lock:
            acquired = self._acquire_operation(
                request,
                peer_pid=peer_pid,
                peer_uid=peer_uid,
                privileged=privileged,
            )
            if isinstance(acquired, dict):
                return acquired
            snapshot = acquired
            if snapshot.op_id in self._active_calls:
                raise DeliveryControlError(
                    "delivery_operation_in_progress",
                    "trusted delivery operation is already executing",
                    pending=True,
                )
            self._active_calls.add(snapshot.op_id)

        def guard(
            stage: str | None = None,
            receipt: Mapping[str, Any] | None = None,
        ) -> None:
            self._guard(
                request,
                snapshot,
                peer_pid=peer_pid,
                peer_uid=peer_uid,
                privileged=privileged,
                stage=stage,
                receipt=receipt,
            )

        try:
            task = snapshot.task
            policy = snapshot.policy
            if request.action == "builder_publish":
                published = self.backend.publish(
                    task, policy, run_id=request.run_id, guard=guard,
                )
                guard("pull_request", {"pull_request": published.pull_request})
                with _controller_connect_closing(self.db_path) as conn:
                    ok = kb.submit_task_for_review(
                        conn,
                        task.id,
                        pull_request=published.pull_request,
                        summary=request.summary,
                        metadata=_request_audit_identity(request, peer_pid, peer_uid),
                        reviewer_assignee=self.reviewer_profile,
                        reviewer_validator=lambda profile: (
                            profile == self.reviewer_profile == FIXED_REVIEWER_PROFILE
                        ),
                        expected_run_id=request.run_id,
                        delivery_operation_id=snapshot.op_id,
                        submission_verifier=self.backend.verify_submission,
                    )
                if not ok:
                    raise _protocol_error("task_drifted", "task changed before native submit")
                return {
                    "schema": PROTOCOL_SCHEMA,
                    "ok": True,
                    "state": "submitted",
                    "task_id": task.id,
                    "run_id": request.run_id,
                    "pr_url": published.pull_request["pr_url"],
                    "idempotent": False,
                }

            if snapshot.submission is None or snapshot.submission_event_id is None:
                raise _protocol_error("submission_missing", "review submission is missing")
            submission = dict(snapshot.submission)
            submission["_event_id"] = snapshot.submission_event_id
            try:
                reviewed = self.backend.review(
                    task,
                    policy,
                    submission,
                    guard=guard,
                    kanban_home=self._kanban_home(),
                )
            except Exception as exc:
                from hermes_cli.delivery_verifier import LiveVerificationError

                if (
                    type(exc) is not LiveVerificationError
                    or exc.code != "acceptance_tier1_failed"
                    or not isinstance(exc.evidence, Mapping)
                ):
                    raise
                with _controller_connect_closing(self.db_path) as conn:
                    rejected = kb._reject_delivery_acceptance_for_rework(
                        conn,
                        task_snapshot=task,
                        operation_id=snapshot.op_id,
                        expected_submission_id=snapshot.submission_event_id,
                        expected_submission_sha256=str(snapshot.submission_sha256 or ""),
                        expected_candidate_head=snapshot.candidate_head,
                        evidence=exc.evidence,
                    )
                if not rejected:
                    raise _protocol_error(
                        "task_drifted",
                        "task changed before native acceptance rejection",
                    ) from exc
                return {
                    "schema": PROTOCOL_SCHEMA,
                    "ok": False,
                    "state": "rejected",
                    "code": "acceptance_tier1_failed",
                    "message": "canonical tier1 acceptance requires task rework",
                    "task_id": task.id,
                    "run_id": request.run_id,
                    "idempotent": False,
                }
            guard("merge", {"delivery": reviewed.delivery})
            with _controller_connect_closing(self.db_path) as conn:
                ok = kb.complete_task(
                    conn,
                    task.id,
                    summary=request.summary,
                    metadata=_request_audit_identity(request, peer_pid, peer_uid),
                    delivery=reviewed.delivery,
                    expected_run_id=request.run_id,
                    expected_submission_id=snapshot.submission_event_id,
                    delivery_operation_id=snapshot.op_id,
                    terminal_verifier=self.backend.verify_terminal,
                )
            if not ok:
                raise _protocol_error("task_drifted", "task changed before native completion")
            return {
                "schema": PROTOCOL_SCHEMA,
                "ok": True,
                "state": "completed",
                "task_id": task.id,
                "run_id": request.run_id,
                "idempotent": False,
            }
        except DeliveryControlError as exc:
            if exc.code == "premerge_receipt_missing":
                self._quarantine_operation(snapshot.op_id)
            raise
        finally:
            with self._operation_lock:
                self._active_calls.discard(snapshot.op_id)


def _safe_title(task: kb.Task) -> str:
    title = " ".join(str(task.title or "").split())
    title = "".join(ch for ch in title if ch.isprintable())[:120]
    return f"Hermes {task.id}: {title or 'autonomous delivery'}"


def canonical_pr_body(
    task: kb.Task,
    policy: Mapping[str, Any],
    *,
    run_id: int,
    head_sha: str,
) -> str:
    return (
        "## Hermes autonomous delivery\n\n"
        f"Hermes-Task-ID: {task.id}\n"
        f"Hermes-Run-ID: {run_id}\n"
        f"Hermes-Contract-SHA256: {policy['contract_hash']}\n"
        f"Hermes-Candidate-SHA: {head_sha}\n\n"
        "Deploy Plan: protected CI builds and Auto Deploy promotes the exact "
        "squash merge to production.\n\n"
        "Rollback: Revert this pull request through the protected branch workflow.\n"
    )


def require_root_policy_command_state() -> Path:
    """Require isolated root-owned CLI state before an admin-token call."""

    required = (
        ROOT_POLICY_STATE_ROOT,
        ROOT_POLICY_STATE_ROOT / "home",
        ROOT_POLICY_STATE_ROOT / "xdg",
        ROOT_POLICY_STATE_ROOT / "gh",
    )
    try:
        if ROOT_POLICY_STATE_ROOT.resolve(strict=True) != ROOT_POLICY_STATE_ROOT:
            raise _protocol_error(
                "root_policy_state_untrusted", "root policy state is symlinked",
            )
        for path in required:
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or path.resolve(strict=True) != path
            ):
                raise _protocol_error(
                    "root_policy_state_untrusted",
                    "root policy state metadata is unsafe",
                )
    except DeliveryControlError:
        raise
    except (OSError, RuntimeError) as exc:
        raise _protocol_error(
            "root_policy_state_untrusted", "root policy state is unavailable",
        ) from exc
    return ROOT_POLICY_STATE_ROOT


class SubprocessDeliveryBackend:
    """Production backend with fixed argv, repo, branch, and cloud targets."""

    def __init__(self, github_token: str, *, root_policy: bool = False):
        if not github_token or any(ch.isspace() for ch in github_token):
            raise _protocol_error("github_credential_invalid", "GitHub credential is invalid")
        self._token = github_token
        self._env = self._command_environment(
            github_token, root_policy=root_policy,
        )

    @staticmethod
    def _command_environment(
        token: str,
        *,
        root_policy: bool = False,
    ) -> dict[str, str]:
        state_root = (
            require_root_policy_command_state()
            if root_policy else STATE_ROOT
        )
        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": str(state_root / "home"),
            "XDG_CONFIG_HOME": str(state_root / "xdg"),
            "GH_CONFIG_DIR": str(state_root / "gh"),
            "GH_HOST": "github.com",
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_TOKEN": token,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSL_NO_VERIFY": "false",
            "GIT_ASKPASS": str(GIT_ASKPASS_PATH),
            "CLOUDSDK_CONFIG": str(
                STATE_ROOT / "gcloud"
                if not root_policy else ROOT_POLICY_STATE_ROOT / "gcloud-disabled"
            ),
        }
        if not root_policy:
            env.update({
                "XDG_RUNTIME_DIR": f"/run/user/{FIXED_WORKER_UID}",
                "DBUS_SESSION_BUS_ADDRESS": (
                    f"unix:path=/run/user/{FIXED_WORKER_UID}/bus"
                ),
            })
        return env

    def _run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        input_bytes: bytes | None = None,
        timeout: int = 120,
        github_auth: bool = False,
    ) -> bytes:
        if Path(argv[0]).name == "gh":
            if argv[0] != str(TRUSTED_GH_PATH):
                raise _protocol_error(
                    "trusted_runtime_invalid", "trusted gh path is not fixed"
                )
            try:
                require_trusted_gh()
            except TrustedDeliveryRuntimeError as exc:
                raise _protocol_error(
                    "trusted_runtime_unavailable", "trusted gh runtime is unavailable"
                ) from exc
        if Path(argv[0]).name == "gcloud":
            if argv[0] != str(TRUSTED_GCLOUD_PATH):
                raise _protocol_error(
                    "trusted_runtime_invalid", "trusted gcloud path is not fixed"
                )
            try:
                require_trusted_gcloud()
            except TrustedDeliveryRuntimeError as exc:
                raise _protocol_error(
                    "trusted_runtime_unavailable", "trusted gcloud runtime is unavailable"
                ) from exc
        env = dict(self._env)
        if not github_auth:
            # Local candidate inspection and canonical acceptance must never
            # inherit the broker's GitHub capability. Only fixed gh requests
            # and network Git from the trusted throwaway publisher opt in.
            env.pop("GH_TOKEN", None)
            env.pop("GIT_ASKPASS", None)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                env=env,
                input=input_bytes,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _protocol_error(
                "trusted_command_unavailable", f"trusted {argv[0]} command is unavailable"
            ) from exc
        if proc.returncode != 0:
            # Never reflect stderr/stdout: a compromised helper must not turn
            # the worker response or service journal into a token oracle.
            raise _protocol_error(
                "trusted_command_failed", f"trusted {argv[0]} command failed"
            )
        if len(proc.stdout) > 2 * 1024 * 1024:
            raise _protocol_error("trusted_response_oversized", "trusted response is oversized")
        return proc.stdout

    def _gh_json(
        self,
        endpoint: str,
        *,
        cwd: Path,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        argv = [
            str(TRUSTED_GH_PATH), "api", "--hostname", "github.com",
            "-H", "Accept: application/vnd.github+json",
            "-H", "X-GitHub-Api-Version: 2022-11-28",
        ]
        if method != "GET":
            argv.extend(["--method", method])
        if body is not None:
            argv.extend(["--input", "-"])
        argv.append(endpoint)
        raw = self._run(
            argv,
            cwd=cwd,
            input_bytes=_canonical_json(body) if body is not None else None,
            github_auth=True,
        )
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _protocol_error(
                "trusted_response_invalid", "GitHub returned invalid JSON"
            ) from exc

    @staticmethod
    def _workspace(task: kb.Task) -> Path:
        raw_workspace = Path(str(task.workspace_path))
        workspace = raw_workspace.resolve()
        worktree_root = (FIXED_PROJECT_ROOT / ".worktrees").resolve()
        try:
            metadata = raw_workspace.lstat()
        except OSError as exc:
            raise _protocol_error(
                "workspace_missing", "task workspace is unavailable"
            ) from exc
        if (
            not workspace.is_dir()
            or raw_workspace.is_symlink()
            or metadata.st_uid != FIXED_WORKER_UID
            or stat.S_IMODE(metadata.st_mode) & 0o002
            or workspace.parent != worktree_root
            or workspace.name != task.id
            or raw_workspace != FIXED_PROJECT_ROOT / ".worktrees" / task.id
        ):
            raise _protocol_error(
                "workspace_missing", "task workspace is not the fixed task worktree"
            )
        return workspace

    @staticmethod
    def _config_key_is_data_only(key: str) -> bool:
        normalized = key.strip().casefold()
        if normalized in _SAFE_LOCAL_GIT_CONFIG:
            return True
        if normalized.startswith("branch."):
            return normalized.rsplit(".", 1)[-1] in _SAFE_BRANCH_CONFIG_SUFFIXES
        return False

    @staticmethod
    def _read_worktree_link(path: Path, label: str) -> str:
        try:
            metadata = path.lstat()
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise _protocol_error(
                "worktree_link_invalid", f"linked-worktree {label} is unavailable"
            ) from exc
        lines = raw.splitlines()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != FIXED_WORKER_UID
            or metadata.st_nlink != 1
            or metadata.st_size > 4096
            or stat.S_IMODE(metadata.st_mode) & 0o002
            or len(lines) != 1
            or not lines[0].strip()
        ):
            raise _protocol_error(
                "worktree_link_invalid", f"linked-worktree {label} metadata is unsafe"
            )
        return lines[0].strip()

    def _validate_linked_worktree(self, task: kb.Task, workspace: Path) -> Path:
        common = require_base_repository_mirror()
        expected_admin = (common / "worktrees" / task.id).resolve()
        pointer = self._read_worktree_link(workspace / ".git", "pointer")
        prefix = "gitdir: "
        if not pointer.casefold().startswith(prefix):
            raise _protocol_error(
                "worktree_link_invalid", "task .git pointer is not canonical"
            )
        raw_admin = Path(pointer[len(prefix):].strip())
        if not raw_admin.is_absolute() or raw_admin.resolve() != expected_admin:
            raise _protocol_error(
                "worktree_link_invalid", "task .git pointer is not exact"
            )
        try:
            admin_metadata = expected_admin.lstat()
        except OSError as exc:
            raise _protocol_error(
                "worktree_link_invalid", "linked-worktree admin directory is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(admin_metadata.st_mode)
            or expected_admin.is_symlink()
            or admin_metadata.st_uid != FIXED_WORKER_UID
            or stat.S_IMODE(admin_metadata.st_mode) & 0o002
            or expected_admin.parent != common / "worktrees"
            or expected_admin.name != task.id
        ):
            raise _protocol_error(
                "worktree_link_invalid", "linked-worktree admin directory is unsafe"
            )
        reciprocal = Path(
            self._read_worktree_link(expected_admin / "gitdir", "reciprocal pointer")
        )
        if not reciprocal.is_absolute() or reciprocal.resolve() != workspace / ".git":
            raise _protocol_error(
                "worktree_link_invalid", "linked-worktree reciprocal pointer mismatches"
            )
        commondir_raw = Path(
            self._read_worktree_link(expected_admin / "commondir", "common pointer")
        )
        commondir = (
            commondir_raw
            if commondir_raw.is_absolute()
            else expected_admin / commondir_raw
        ).resolve()
        if commondir != common:
            raise _protocol_error(
                "worktree_link_invalid", "linked-worktree common pointer mismatches"
            )
        return expected_admin

    @staticmethod
    def _require_fixed_authenticated_git(args: list[str]) -> None:
        """Allow credentials only on the three closed-form GitHub operations."""

        verb = args[0] if args else ""
        valid = False
        if verb == "fetch":
            valid = args == [
                "fetch", "--no-tags", FIXED_REPOSITORY_URL,
                f"+refs/heads/{FIXED_DEFAULT_BRANCH}:refs/heads/"
                f"{FIXED_DEFAULT_BRANCH}",
            ]
        elif verb == "ls-remote" and len(args) == 3:
            branch_ref = args[2]
            branch = branch_ref.removeprefix("refs/heads/")
            valid = (
                args[1] == FIXED_REPOSITORY_URL
                and branch_ref == f"refs/heads/{branch}"
                and _BRANCH_RE.fullmatch(branch) is not None
            )
        elif verb == "push" and len(args) == 5:
            lease_prefix = "--force-with-lease=refs/heads/"
            lease = args[2].removeprefix(lease_prefix)
            branch, separator, remote_head = lease.partition(":")
            candidate_head, ref_separator, destination = args[4].partition(":")
            valid = (
                args[1] == "--porcelain"
                and args[2].startswith(lease_prefix)
                and separator == ":"
                and (not remote_head or _SHA_RE.fullmatch(remote_head) is not None)
                and args[3] == FIXED_REPOSITORY_URL
                and _SHA_RE.fullmatch(candidate_head) is not None
                and ref_separator == ":"
                and destination == f"refs/heads/{branch}"
                and _BRANCH_RE.fullmatch(branch) is not None
            )
        if not valid:
            raise _protocol_error(
                "authenticated_git_invalid",
                "credentialed Git operation is not the fixed GitHub contract",
            )

    def _git(
        self,
        workspace: Path,
        args: list[str],
        *,
        timeout: int = 120,
        github_auth: bool = False,
    ) -> str:
        if github_auth:
            self._require_fixed_authenticated_git(args)
        argv = [
            "/usr/bin/git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
            "-c", "credential.helper=",
            # Authenticated network Git may prompt only through the fixed
            # root-owned GIT_ASKPASS adapter.  ``credential.interactive=false``
            # disables that adapter as well as terminal prompting, so keep it
            # true only when _run also retains the fixed askpass + token.
            # GIT_TERMINAL_PROMPT=0 still forbids an actual terminal prompt.
            "-c", (
                "credential.interactive=true"
                if github_auth
                else "credential.interactive=false"
            ),
            *(
                ["-c", "credential.username=x-access-token"]
                if github_auth
                else []
            ),
            "-c", "core.sshCommand=/usr/bin/false",
            "-c", "protocol.ext.allow=never",
            "-c", "http.proxy=",
            "-c", "http.sslVerify=true",
            "-c", "http.followRedirects=false",
            "-C", str(workspace),
            *args,
        ]
        return self._run(
            argv,
            cwd=workspace,
            timeout=timeout,
            github_auth=github_auth,
        ).decode(
            "utf-8", errors="strict"
        )

    def _validate_base_repository_for_refresh(self) -> Path:
        """Validate the config-free controller mirror before a remote fetch."""

        workspace = require_base_repository_mirror()
        common_dir = Path(
            self._git(workspace, ["rev-parse", "--git-common-dir"]).strip()
        )
        if not common_dir.is_absolute():
            common_dir = workspace / common_dir
        if common_dir.resolve() != workspace:
            raise _protocol_error(
                "base_repository_untrusted", "trusted base repository identity is invalid",
            )
        config_names = self._git(
            workspace,
            ["config", "--no-includes", "--name-only", "--null", "--list"],
        )
        if any(
            name and not self._config_key_is_data_only(name)
            for name in config_names.split("\0")
        ):
            raise _protocol_error(
                "git_config_unsafe",
                "fixed repository contains executable or network-altering config",
            )
        shared_repository = self._git(
            workspace,
            ["config", "--no-includes", "--get", "core.sharedRepository"],
        ).strip()
        if shared_repository != "0600":
            raise _protocol_error(
                "base_repository_untrusted",
                "trusted base repository does not enforce private object modes",
            )
        origin = self._git(workspace, ["remote", "get-url", "origin"]).strip()
        from hermes_cli.delivery_verifier import _repo_from_remote

        if (_repo_from_remote(origin) or "").casefold() != FIXED_REPOSITORY.casefold():
            raise _protocol_error(
                "repository_mismatch", "trusted base repository remote is not fixed",
            )
        return workspace

    def _ensure_base_repository_for_refresh(self) -> Path:
        _require_base_state_root()
        if not os.path.lexists(BASE_REPOSITORY_MIRROR):
            transactions = STATE_ROOT / "transactions"
            try:
                transactions_metadata = transactions.lstat()
            except OSError as exc:
                raise _protocol_error(
                    "publisher_state_unavailable", "trusted transaction state is unavailable",
                ) from exc
            if (
                transactions.is_symlink()
                or not stat.S_ISDIR(transactions_metadata.st_mode)
                or transactions_metadata.st_uid != FIXED_WORKER_UID
                or transactions_metadata.st_gid != FIXED_WORKER_UID
                or stat.S_IMODE(transactions_metadata.st_mode) != 0o700
            ):
                raise _protocol_error(
                    "publisher_state_untrusted", "trusted transaction state is unsafe",
                )
            with tempfile.TemporaryDirectory(
                prefix="mirror-init-", dir=transactions,
            ) as temporary:
                empty_template = Path(temporary) / "empty-template"
                empty_template.mkdir(mode=0o700)
                self._run(
                    [
                        "/usr/bin/git", "-c", "core.hooksPath=/dev/null", "init",
                        "--bare", f"--template={empty_template}", "--initial-branch=main",
                        str(BASE_REPOSITORY_MIRROR),
                    ],
                    cwd=BASE_STATE_ROOT,
                )
            for current_root, directories, files in os.walk(
                BASE_REPOSITORY_MIRROR, topdown=True, followlinks=False,
            ):
                os.chmod(current_root, 0o700)
                for name in directories:
                    path = Path(current_root) / name
                    if path.is_symlink():
                        raise _protocol_error(
                            "base_repository_untrusted",
                            "trusted base repository initialization created a symlink",
                        )
                    os.chmod(path, 0o700)
                for name in files:
                    path = Path(current_root) / name
                    if path.is_symlink():
                        raise _protocol_error(
                            "base_repository_untrusted",
                            "trusted base repository initialization created a symlink",
                        )
                    os.chmod(path, 0o600)
            self._git(
                BASE_REPOSITORY_MIRROR,
                ["remote", "add", "origin", FIXED_REPOSITORY_URL],
            )
            self._git(
                BASE_REPOSITORY_MIRROR,
                ["config", "core.sharedRepository", "0600"],
            )
        return self._validate_base_repository_for_refresh()

    def refresh_default_branch(self) -> str:
        """Authenticate and refresh the fixed private repository's main SHA.

        Network Git runs only in a config-free temporary bare repository. The
        resulting object is copied into a fixed controller-owned bare mirror,
        never the mutable project checkout. The dispatcher consumes the exact
        short-lived receipt and therefore never needs host GitHub state.
        """

        workspace = self._ensure_base_repository_for_refresh()
        transactions = STATE_ROOT / "transactions"
        try:
            metadata = transactions.lstat()
        except OSError as exc:
            raise _protocol_error(
                "publisher_state_unavailable", "trusted transaction state is unavailable",
            ) from exc
        if (
            transactions.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != FIXED_WORKER_UID
            or metadata.st_gid != FIXED_WORKER_UID
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise _protocol_error(
                "publisher_state_untrusted", "trusted transaction state metadata is unsafe",
            )
        with tempfile.TemporaryDirectory(prefix="base-", dir=transactions) as temporary:
            root = Path(temporary)
            publisher = root / "publisher.git"
            empty_template = root / "empty-template"
            empty_template.mkdir(mode=0o700)
            self._run(
                [
                    "/usr/bin/git", "-c", "core.hooksPath=/dev/null", "init",
                    "--bare", f"--template={empty_template}", "--initial-branch=main",
                    str(publisher),
                ],
                cwd=root,
            )
            self._git(
                publisher,
                [
                    "fetch", "--no-tags", FIXED_REPOSITORY_URL,
                    f"+refs/heads/{FIXED_DEFAULT_BRANCH}:refs/heads/{FIXED_DEFAULT_BRANCH}",
                ],
                timeout=300,
                github_auth=True,
            )
            sha = self._git(
                publisher,
                ["rev-parse", f"refs/heads/{FIXED_DEFAULT_BRANCH}^{{commit}}"],
            ).strip().lower()
            if not _SHA_RE.fullmatch(sha):
                raise _protocol_error("base_refresh_invalid", "GitHub base SHA is invalid")
            bundle = root / "main.bundle"
            self._git(
                publisher,
                ["bundle", "create", str(bundle), f"refs/heads/{FIXED_DEFAULT_BRANCH}"],
            )
            self._git(workspace, [
                "-c", "core.fsync=committed",
                "fetch", "--no-tags", str(bundle), f"refs/heads/{FIXED_DEFAULT_BRANCH}",
            ])
            copied = self._git(workspace, ["rev-parse", "FETCH_HEAD^{commit}"]).strip().lower()
            if copied != sha:
                raise _protocol_error(
                    "base_refresh_invalid", "authenticated base copy changed unexpectedly",
                )
            self._git(
                workspace,
                ["update-ref", f"refs/remotes/origin/{FIXED_DEFAULT_BRANCH}", sha],
            )
        if self._validate_base_repository_for_refresh() != workspace:
            raise _protocol_error("repository_mismatch", "fixed repository changed during refresh")
        observed = self._git(
            workspace,
            ["rev-parse", f"refs/remotes/origin/{FIXED_DEFAULT_BRANCH}^{{commit}}"],
        ).strip().lower()
        if observed != sha:
            raise _protocol_error(
                "base_refresh_invalid", "authenticated base ref changed during refresh",
            )
        ref_path = workspace / "refs" / "remotes" / "origin" / FIXED_DEFAULT_BRANCH
        try:
            ref_fd = os.open(
                ref_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(ref_fd)
            finally:
                os.close(ref_fd)
            for directory in (ref_path.parent, workspace):
                directory_fd = os.open(
                    directory,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise _protocol_error(
                "base_refresh_publish_failed", "authenticated base ref was not durable",
            ) from exc
        _publish_base_receipt({
            "schema": BASE_REFRESH_SCHEMA,
            "repository": FIXED_REPOSITORY,
            "ref": f"refs/heads/{FIXED_DEFAULT_BRANCH}",
            "sha": sha,
            "fetched_at": int(time.time()),
        })
        return sha

    @contextlib.contextmanager
    def _trusted_publish_repository(self, workspace: Path, head: str):
        """Copy one exact commit into a config-free publisher repository.

        The network-facing Git process never opens the candidate repository's
        config, hooks, helpers, or scripts. The candidate-side bundle command
        runs without credentials and with hooks/fsmonitor/helpers disabled.
        """

        transactions = STATE_ROOT / "transactions"
        try:
            directory = transactions.lstat()
        except OSError as exc:
            raise _protocol_error(
                "publisher_state_unavailable",
                "trusted publisher state directory is unavailable",
            ) from exc
        if (
            not stat.S_ISDIR(directory.st_mode)
            or transactions.is_symlink()
            or directory.st_uid != FIXED_WORKER_UID
            or stat.S_IMODE(directory.st_mode) & 0o077
        ):
            raise _protocol_error(
                "publisher_state_untrusted",
                "trusted publisher state directory metadata is unsafe",
            )
        with tempfile.TemporaryDirectory(
            prefix="publish-", dir=transactions,
        ) as temporary:
            root = Path(temporary)
            bundle = root / "candidate.bundle"
            publisher = root / "publisher.git"
            empty_template = root / "empty-template"
            empty_template.mkdir(mode=0o700)
            self._git(workspace, ["bundle", "create", str(bundle), "HEAD"])
            self._run(
                [
                    "/usr/bin/git", "-c", "core.hooksPath=/dev/null", "init", "--bare",
                    f"--template={empty_template}", "--initial-branch=main",
                    str(publisher),
                ],
                cwd=root,
            )
            self._git(
                publisher,
                ["fetch", "--no-tags", str(bundle), "HEAD"],
            )
            copied = self._git(publisher, ["rev-parse", "FETCH_HEAD"]).strip().lower()
            if copied != head:
                raise _protocol_error(
                    "candidate_drifted", "candidate HEAD changed during publication"
                )
            yield publisher

    def _exact_candidate(self, task: kb.Task) -> tuple[Path, str, str]:
        workspace = self._workspace(task)
        expected_admin = self._validate_linked_worktree(task, workspace)
        top_level = Path(
            self._git(workspace, ["rev-parse", "--show-toplevel"]).strip()
        )
        if not top_level.is_absolute() or top_level.resolve() != workspace:
            raise _protocol_error(
                "worktree_link_invalid", "Git top-level is not the exact task workspace"
            )
        git_dir = Path(self._git(workspace, ["rev-parse", "--git-dir"]).strip())
        if not git_dir.is_absolute():
            git_dir = workspace / git_dir
        if git_dir.resolve() != expected_admin:
            raise _protocol_error(
                "worktree_link_invalid", "Git admin directory is not exact"
            )
        common_dir = Path(
            self._git(workspace, ["rev-parse", "--git-common-dir"]).strip()
        )
        if not common_dir.is_absolute():
            common_dir = workspace / common_dir
        if common_dir.resolve() != require_base_repository_mirror():
            raise _protocol_error(
                "repository_mismatch", "worktree is not attached to the fixed repository"
            )
        configured_worktree = self._git(
            workspace,
            ["config", "--default", "", "--get", "core.worktree"],
        ).strip()
        if configured_worktree and (
            not Path(configured_worktree).is_absolute()
            or Path(configured_worktree).resolve() != workspace
        ):
            raise _protocol_error(
                "git_config_unsafe", "core.worktree is not the exact task workspace"
            )
        config_names = self._git(
            workspace,
            ["config", "--no-includes", "--name-only", "--null", "--list"],
        )
        if any(
            name and not self._config_key_is_data_only(name)
            for name in config_names.split("\0")
        ):
            raise _protocol_error(
                "git_config_unsafe",
                "candidate repository contains executable or network-altering config",
            )
        origin = self._git(workspace, ["remote", "get-url", "origin"]).strip()
        from hermes_cli.delivery_verifier import _repo_from_remote

        if (_repo_from_remote(origin) or "").casefold() != FIXED_REPOSITORY.casefold():
            raise _protocol_error("repository_mismatch", "worktree repository is not fixed target")
        head = self._git(workspace, ["rev-parse", "HEAD"]).strip().lower()
        if not _SHA_RE.fullmatch(head):
            raise _protocol_error("head_invalid", "candidate HEAD is not immutable")
        if self._git(workspace, ["status", "--porcelain"]).strip():
            raise _protocol_error("workspace_dirty", "candidate worktree is dirty")
        branch = str(task.branch_name or "")
        if not _valid_branch(task.id, branch):
            raise _protocol_error("branch_invalid", "task branch is not canonical")
        actual_branch = self._git(
            workspace, ["symbolic-ref", "--quiet", "--short", "HEAD"],
        ).strip()
        if actual_branch != branch:
            raise _protocol_error(
                "branch_mismatch", "candidate is not checked out on the task branch"
            )
        if self._validate_linked_worktree(task, workspace) != expected_admin:
            raise _protocol_error(
                "worktree_link_invalid", "linked-worktree binding changed during inspection"
            )
        return workspace, head, branch

    def candidate_identity(self, task: kb.Task) -> str:
        """Return the exact clean linked-worktree HEAD for operation fencing."""

        _workspace, head, _branch = self._exact_candidate(task)
        return head

    def _remote_head(self, workspace: Path, branch: str) -> str:
        raw = self._git(
            workspace,
            ["ls-remote", FIXED_REPOSITORY_URL, f"refs/heads/{branch}"],
            github_auth=True,
        ).strip()
        if not raw:
            return ""
        fields = raw.split()
        if len(fields) != 2 or fields[1] != f"refs/heads/{branch}":
            raise _protocol_error("remote_ref_invalid", "remote branch response is invalid")
        sha = fields[0].lower()
        if not _SHA_RE.fullmatch(sha):
            raise _protocol_error("remote_ref_invalid", "remote branch head is invalid")
        return sha

    def publish(
        self,
        task: kb.Task,
        policy: Mapping[str, Any],
        *,
        run_id: int,
        guard: Callable[[], None],
    ) -> PublishResult:
        workspace, head, branch = self._exact_candidate(task)
        from hermes_cli import delivery_verifier

        with delivery_verifier.trusted_command_environment(self._env):
            delivery_verifier.verify_registered_project_binding(
                task, FIXED_REPOSITORY,
            )
        with self._trusted_publish_repository(workspace, head) as publisher:
            remote_head = self._remote_head(publisher, branch)
            guard()
            self._git(
                publisher,
                [
                    "push", "--porcelain",
                    f"--force-with-lease=refs/heads/{branch}:{remote_head}",
                    FIXED_REPOSITORY_URL,
                    f"{head}:refs/heads/{branch}",
                ],
                timeout=300,
                github_auth=True,
            )
            guard("push", {
                "candidate_head": head,
                "candidate_ref": branch,
                "remote_head_before": remote_head,
            })
        guard()
        owner = FIXED_REPOSITORY.split("/", 1)[0]
        endpoint = (
            f"repos/{FIXED_REPOSITORY}/pulls?state=open&base={FIXED_DEFAULT_BRANCH}"
            f"&head={owner}%3A{quote(branch, safe='')}&per_page=100"
        )
        rows = self._gh_json(endpoint, cwd=workspace)
        exact = [
            row for row in rows or []
            if isinstance(row, Mapping)
            and str((row.get("head") or {}).get("sha") or "").lower() == head
            and str((row.get("head") or {}).get("ref") or "") == branch
            and str((row.get("base") or {}).get("ref") or "") == FIXED_DEFAULT_BRANCH
        ] if isinstance(rows, list) else []
        title = _safe_title(task)
        body = canonical_pr_body(task, policy, run_id=run_id, head_sha=head)
        if len(exact) > 1:
            raise _protocol_error("pr_ambiguous", "multiple exact open pull requests exist")
        if exact:
            pr = dict(exact[0])
            pr_number = pr.get("number")
            pr_head = pr.get("head") if isinstance(pr.get("head"), Mapping) else {}
            pr_base = pr.get("base") if isinstance(pr.get("base"), Mapping) else {}
            if (
                type(pr_number) is not int
                or pr_number <= 0
                or str(pr.get("html_url") or "").rstrip("/")
                != f"https://github.com/{FIXED_REPOSITORY}/pull/{pr_number}"
                or str(pr.get("state") or "") != "open"
                or pr.get("merged_at") is not None
                or pr.get("draft") is not False
                or str(pr_head.get("sha") or "").lower() != head
                or str(pr_head.get("ref") or "") != branch
                or str(pr_base.get("ref") or "") != FIXED_DEFAULT_BRANCH
            ):
                raise _protocol_error(
                    "pr_invalid", "canonical pull request identity is invalid"
                )
            if pr.get("title") != title or pr.get("body") != body:
                guard()
                updated = self._gh_json(
                    f"repos/{FIXED_REPOSITORY}/pulls/{pr_number}",
                    cwd=workspace,
                    method="PATCH",
                    body={"title": title, "body": body},
                )
                if not isinstance(updated, Mapping):
                    raise _protocol_error(
                        "pr_invalid", "GitHub pull request response is invalid"
                    )
                updated_head = (
                    updated.get("head")
                    if isinstance(updated.get("head"), Mapping)
                    else {}
                )
                updated_base = (
                    updated.get("base")
                    if isinstance(updated.get("base"), Mapping)
                    else {}
                )
                updated_number = updated.get("number")
                if (
                    type(updated_number) is not int
                    or updated_number != pr_number
                    or str(updated.get("state") or "") != "open"
                    or updated.get("merged_at") is not None
                    or updated.get("draft") is not False
                    or updated.get("title") != title
                    or updated.get("body") != body
                    or str(updated_head.get("sha") or "").lower() != head
                    or str(updated_head.get("ref") or "") != branch
                    or str(updated_base.get("ref") or "") != FIXED_DEFAULT_BRANCH
                ):
                    raise _protocol_error(
                        "pr_invalid", "canonical pull request metadata was not applied"
                    )
                pr = dict(updated)
        else:
            guard()
            pr = self._gh_json(
                f"repos/{FIXED_REPOSITORY}/pulls",
                cwd=workspace,
                method="POST",
                body={
                    "title": title,
                    "body": body,
                    "head": branch,
                    "base": FIXED_DEFAULT_BRANCH,
                    "draft": False,
                    "maintainer_can_modify": False,
                },
            )
        if not isinstance(pr, Mapping):
            raise _protocol_error("pr_invalid", "GitHub pull request response is invalid")
        number = int(pr.get("number") or 0)
        url = str(pr.get("html_url") or "").rstrip("/")
        expected_url = f"https://github.com/{FIXED_REPOSITORY}/pull/{number}"
        if number <= 0 or url != expected_url:
            raise _protocol_error("pr_invalid", "GitHub pull request identity is invalid")
        pull_request = {
            "pr_url": url,
            "pr_number": number,
            "head_sha": head,
            "candidate_ref": branch,
        }
        guard("pull_request", {"pull_request": pull_request})
        return PublishResult(pull_request)

    def verify_submission(
        self,
        task: kb.Task,
        policy: Mapping[str, Any],
        pull_request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        from hermes_cli import delivery_verifier

        with delivery_verifier.trusted_command_environment(self._env):
            return delivery_verifier.verify_submission(task, policy, pull_request)

    def _pull(self, workspace: Path, number: int) -> dict[str, Any]:
        value = self._gh_json(
            f"repos/{FIXED_REPOSITORY}/pulls/{number}", cwd=workspace,
        )
        if not isinstance(value, dict):
            raise _protocol_error("pr_invalid", "GitHub pull request response is invalid")
        return value

    def _reconcile_open_pull_body(
        self,
        workspace: Path,
        task: kb.Task,
        policy: Mapping[str, Any],
        submission: Mapping[str, Any],
        pr: Mapping[str, Any],
        *,
        guard: Callable[..., None],
    ) -> dict[str, Any]:
        number = int(submission.get("pr_number") or 0)
        expected_head = str(submission.get("head_sha") or "").lower()
        expected_branch = str(submission.get("candidate_ref") or "")
        expected_url = f"https://github.com/{FIXED_REPOSITORY}/pull/{number}"
        expected_title = _safe_title(task)
        builder_run_id = int(submission.get("builder_run_id") or 0)

        def require_identity(value: Mapping[str, Any]) -> None:
            head = value.get("head") if isinstance(value.get("head"), Mapping) else {}
            base = value.get("base") if isinstance(value.get("base"), Mapping) else {}
            if (
                number <= 0
                or builder_run_id <= 0
                or int(value.get("number") or 0) != number
                or str(value.get("html_url") or "").rstrip("/") != expected_url
                or str(value.get("state") or "") != "open"
                or value.get("merged_at") is not None
                or value.get("draft") is not False
                or str(head.get("sha") or "").lower() != expected_head
                or str(head.get("ref") or "") != expected_branch
                or str(base.get("ref") or "") != FIXED_DEFAULT_BRANCH
            ):
                raise _protocol_error(
                    "submission_drifted", "open pull request identity no longer matches"
                )

        require_identity(pr)
        expected_body = canonical_pr_body(
            task,
            policy,
            run_id=builder_run_id,
            head_sha=expected_head,
        )
        receipt = {
            "pr_url": expected_url,
            "pr_number": number,
            "head_sha": expected_head,
            "candidate_ref": expected_branch,
            "body_sha256": hashlib.sha256(expected_body.encode("utf-8")).hexdigest(),
        }
        if (
            str(pr.get("title") or "") == expected_title
            and str(pr.get("body") or "") == expected_body
        ):
            guard("pull_request", {"pull_request": receipt})
            return dict(pr)

        guard()
        updated = self._gh_json(
            f"repos/{FIXED_REPOSITORY}/pulls/{number}",
            cwd=workspace,
            method="PATCH",
            body={"title": expected_title, "body": expected_body},
        )
        if not isinstance(updated, Mapping):
            raise _protocol_error("pr_invalid", "GitHub pull request response is invalid")
        require_identity(updated)
        if (
            str(updated.get("title") or "") != expected_title
            or str(updated.get("body") or "") != expected_body
        ):
            raise _protocol_error(
                "submission_drifted", "canonical pull request metadata was not applied"
            )
        guard("pull_request", {"pull_request": receipt})
        return dict(updated)

    def _derive_deployment(
        self,
        workspace: Path,
        *,
        candidate_sha: str,
        merge_sha: str,
    ) -> dict[str, Any]:
        service = json.loads(self._run(
            [
                str(TRUSTED_GCLOUD_PATH), "run", "services", "describe", FIXED_CLOUD_RUN_SERVICE,
                "--project", FIXED_GCP_PROJECT,
                "--region", FIXED_GCP_REGION,
                "--format=json",
            ],
            cwd=workspace,
        ))
        status = service.get("status") if isinstance(service, Mapping) else {}
        revision = str((status or {}).get("latestReadyRevisionName") or "")
        if not revision:
            raise DeliveryControlError(
                "deployment_pending", "production revision is not ready", pending=True,
            )
        revision_value = json.loads(self._run(
            [
                str(TRUSTED_GCLOUD_PATH), "run", "revisions", "describe", revision,
                "--project", FIXED_GCP_PROJECT,
                "--region", FIXED_GCP_REGION,
                "--format=json",
            ],
            cwd=workspace,
        ))
        metadata = revision_value.get("metadata") if isinstance(revision_value, Mapping) else {}
        labels = metadata.get("labels") if isinstance(metadata, Mapping) else {}
        if (
            not isinstance(labels, Mapping)
            or str(labels.get("clauseye-candidate-sha") or "").lower() != candidate_sha
            or str(labels.get("clauseye-merge-sha") or "").lower() != merge_sha
        ):
            raise DeliveryControlError(
                "deployment_pending",
                "latest production revision is not this exact delivery",
                pending=True,
            )
        run_id = int(labels.get("clauseye-github-run-id") or 0)
        if run_id <= 0:
            raise DeliveryControlError(
                "deployment_pending", "production workflow lineage is pending", pending=True,
            )
        run = self._gh_json(
            f"repos/{FIXED_REPOSITORY}/actions/runs/{run_id}", cwd=workspace,
        )
        jobs = self._gh_json(
            f"repos/{FIXED_REPOSITORY}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
            cwd=workspace,
        )
        job_rows = jobs.get("jobs") if isinstance(jobs, Mapping) else []
        smoke = [
            job for job in job_rows or []
            if isinstance(job, Mapping)
            and "Cloud Run Smoke Test" in str(job.get("name") or "")
            and job.get("conclusion") == "success"
        ]
        if not isinstance(run, Mapping) or not smoke:
            raise DeliveryControlError(
                "deployment_pending", "production verification is still pending", pending=True,
            )
        best = max(smoke, key=lambda item: int(item.get("id") or 0))
        reference = str(best.get("html_url") or run.get("html_url") or "")
        checked = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
        return {
            "environment": "production",
            "revision": revision,
            "source_sha": merge_sha,
            "workflow_run_url": str(run.get("html_url") or ""),
            "workflow_run_id": run_id,
            "health_status": "healthy",
            "health_reference": reference,
            "health_checked_at": checked,
            "verification": {
                "verifier": "clauseye-production-probe",
                "status": "passed",
                "reference": reference,
            },
        }

    def review(
        self,
        task: kb.Task,
        policy: Mapping[str, Any],
        submission: Mapping[str, Any],
        *,
        guard: Callable[[], None],
        kanban_home: Path,
    ) -> ReviewResult:
        from hermes_cli import delivery_verifier

        workspace, local_head, branch = self._exact_candidate(task)
        expected_head = str(submission.get("head_sha") or "").lower()
        number = int(submission.get("pr_number") or 0)
        if (
            local_head != expected_head
            or branch != str(submission.get("candidate_ref") or "")
            or str(submission.get("pr_url") or "")
            != f"https://github.com/{FIXED_REPOSITORY}/pull/{number}"
        ):
            raise _protocol_error("submission_drifted", "review candidate no longer matches")
        pr = self._pull(workspace, number)
        if not pr.get("merged_at"):
            pr = self._reconcile_open_pull_body(
                workspace,
                task,
                policy,
                submission,
                pr,
                guard=guard,
            )
            with delivery_verifier.trusted_command_environment(self._env):
                premerge = delivery_verifier.verify_reviewer_merge_ready(
                    task, policy, submission, kanban_home=kanban_home,
                )
            ruleset = premerge.get("ruleset") if isinstance(premerge, Mapping) else {}
            acceptance = (
                premerge.get("acceptance") if isinstance(premerge, Mapping) else {}
            )
            guard("premerge", {
                "verification_sha256": hashlib.sha256(
                    _canonical_json(dict(premerge))
                ).hexdigest(),
                "policy_claim_sha256": str(
                    (ruleset or {}).get("policy_claim_sha256") or ""
                ),
                "acceptance_verdict": str(
                    (acceptance or {}).get("verdict") or ""
                ),
                "contract_hash": str(policy.get("contract_hash") or ""),
                "head_sha": expected_head,
                "submission_event_id": int(submission.get("_event_id") or 0),
            })
            guard()
            merge = self._gh_json(
                f"repos/{FIXED_REPOSITORY}/pulls/{number}/merge",
                cwd=workspace,
                method="PUT",
                body={
                    "sha": expected_head,
                    "merge_method": "squash",
                    "commit_title": f"Hermes {task.id} (#{number})",
                },
            )
            if not isinstance(merge, Mapping) or merge.get("merged") is not True:
                raise DeliveryControlError(
                    "merge_pending", "protected-branch merge is not ready", pending=True,
                )
            pr = self._pull(workspace, number)
        else:
            # A merged PR is recoverable only when this exact operation durably
            # recorded successful policy/check/thread/acceptance verification
            # before the irreversible PUT. Never authorize an externally
            # pre-merged PR retroactively.
            guard("require_premerge")
        merge_sha = str(pr.get("merge_commit_sha") or "").lower()
        head = pr.get("head") if isinstance(pr.get("head"), Mapping) else {}
        if (
            not pr.get("merged_at")
            or not _SHA_RE.fullmatch(merge_sha)
            or str(head.get("sha") or "").lower() != expected_head
            or str(head.get("ref") or "") != branch
        ):
            raise DeliveryControlError(
                "merge_pending", "exact-head merge is not yet observable", pending=True,
            )
        delivery: dict[str, Any] = {
            "classification": "merged_pr",
            "pr_url": submission["pr_url"],
            "pr_number": number,
            "head_sha": expected_head,
            "merge_sha": merge_sha,
        }
        guard("merge", {"delivery": dict(delivery)})
        if policy.get("deployment_required"):
            delivery["deployment"] = self._derive_deployment(
                workspace,
                candidate_sha=expected_head,
                merge_sha=merge_sha,
            )
            guard("deployment", {"delivery": dict(delivery)})
        return ReviewResult(delivery)

    def verify_terminal(
        self,
        task: kb.Task,
        policy: Mapping[str, Any],
        submission: Mapping[str, Any],
        delivery: Mapping[str, Any],
        *,
        kanban_home: Path,
    ) -> Mapping[str, Any]:
        from hermes_cli import delivery_verifier

        with delivery_verifier.trusted_command_environment(self._env):
            return delivery_verifier.verify_terminal(
                task, policy, submission, delivery, kanban_home=kanban_home,
            )


def _peer_credentials(connection: socket.socket) -> tuple[int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        raise _protocol_error("peer_credentials_unavailable", "peer credentials unavailable")
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    pid, uid, _gid = struct.unpack("3i", raw)
    return int(pid), int(uid)


def _response_for_error(error: BaseException) -> dict[str, Any]:
    if isinstance(error, DeliveryControlError):
        code = error.code
        message = str(error)
        pending = error.pending
    else:
        code = str(getattr(error, "code", "delivery_control_failed"))
        message = "trusted delivery control rejected the transition"
        pending = code in {
            "required_check_missing",
            "autonomous_required_workflow_missing",
            "autonomous_gate_attestation_missing",
            "deployment_run_unverifiable",
            "deployment_revision_mismatch",
        }
    return {
        "schema": PROTOCOL_SCHEMA,
        "ok": False,
        "state": "pending" if pending else "rejected",
        "code": code,
        "message": message,
    }


def handle_connection(connection: socket.socket, control: DeliveryControl) -> None:
    """Handle one socket request without logging its capability-bearing body."""

    response: dict[str, Any]
    try:
        connection.settimeout(5.0)
        peer_pid, peer_uid = _peer_credentials(connection)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_REQUEST_BYTES:
                raise _protocol_error("request_size_invalid", "request is oversized")
            chunks.append(chunk)
        request = parse_request(b"".join(chunks))
        response = control.handle(
            request, peer_pid=peer_pid, peer_uid=peer_uid,
        )
    except BaseException as exc:  # response is deliberately redacted/safe
        response = _response_for_error(exc)
    raw = _canonical_json(response)
    if len(raw) > MAX_RESPONSE_BYTES:
        raw = _canonical_json(_response_for_error(
            _protocol_error("control_response_invalid", "response is oversized")
        ))
    try:
        connection.sendall(raw)
    except OSError:
        # A long acceptance check may outlive its caller's socket timeout. The
        # native transition remains durable/idempotent; never crash the broker
        # or log the capability-bearing request because the peer disconnected.
        pass


def _systemd_listener() -> socket.socket:
    if (
        os.environ.get("LISTEN_PID") != str(os.getpid())
        or os.environ.get("LISTEN_FDS") != "1"
    ):
        raise _protocol_error(
            "socket_activation_required", "delivery control requires systemd socket activation"
        )
    listener = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    if Path(str(listener.getsockname())).resolve() != SOCKET_PATH:
        listener.close()
        raise _protocol_error("socket_identity_invalid", "activated socket path is not fixed")
    return listener


def _refresh_default_branch_forever(backend: DeliveryBackend) -> None:
    """Keep the fixed private-repository base fresh without worker credentials."""

    refresh = getattr(backend, "refresh_default_branch", None)
    if not callable(refresh):
        return
    while True:
        try:
            refresh()
            delay = 60
        except BaseException:
            # Never journal command output or credential-adjacent failures.
            # A missing/stale receipt makes dispatcher materialization fail
            # closed while this trusted loop retries independently.
            delay = 15
        time.sleep(delay)


def serve_forever(control: DeliveryControl) -> None:
    if os.geteuid() != FIXED_WORKER_UID:
        raise _protocol_error(
            "service_identity_invalid", "delivery control service identity is not fixed"
        )
    listener = _systemd_listener()
    listener.settimeout(30.0)
    control.prepare_recovery()
    refresh_thread = threading.Thread(
        target=_refresh_default_branch_forever,
        args=(control.backend,),
        name="hermes-delivery-base-refresh",
        daemon=True,
    )
    refresh_thread.start()
    try:
        while True:
            # No model call is required to finish an operation whose worker
            # died after push/PR/merge. Poll the durable fence between socket
            # requests and on every idle interval until fixed remote evidence
            # can be committed natively.
            control.reconcile_orphans()
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                handle_connection(connection, control)
    finally:
        listener.close()


def _systemd_credential_projection(
    name: str,
    effective_uid: int,
) -> tuple[Path, int, int]:
    """Return the one systemd unit projection allowed for this service UID."""

    if effective_uid == FIXED_WORKER_UID:
        if name != GITHUB_CREDENTIAL_NAME:
            raise _protocol_error(
                "credential_untrusted",
                "systemd credential is unavailable to this service",
            )
        return _CONTROL_CREDENTIALS_DIRECTORY, 0o550, 0o440
    if effective_uid == 0:
        return _POLICY_CREDENTIALS_DIRECTORY, 0o500, 0o400
    raise _protocol_error(
        "credential_untrusted", "systemd credential service identity is invalid"
    )


def _systemd_credential_metadata_safe(
    metadata: os.stat_result,
    *,
    mode: int,
    directory: bool,
) -> bool:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    return (
        expected_type(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_gid == 0
        and stat.S_IMODE(metadata.st_mode) == mode
        and (directory or metadata.st_nlink == 1)
        and (directory or 0 < metadata.st_size <= _MAX_SYSTEMD_CREDENTIAL_BYTES)
    )


def read_systemd_credential(name: str) -> str:
    """Read one fixed systemd credential through its manager-owned projection."""

    if name not in {GITHUB_CREDENTIAL_NAME, POLICY_SIGNING_CREDENTIAL_NAME}:
        raise _protocol_error("credential_name_invalid", "credential name is unsupported")
    expected_directory, directory_mode, file_mode = _systemd_credential_projection(
        name, os.geteuid()
    )
    configured_directory = os.environ.get("CREDENTIALS_DIRECTORY") or ""
    if not Path(configured_directory).is_absolute():
        raise _protocol_error("credential_unavailable", "systemd credentials unavailable")
    if configured_directory != str(expected_directory):
        raise _protocol_error(
            "credential_untrusted", "systemd credential directory is unexpected"
        )

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_fd = -1
    credential_fd = -1
    verify_fd = -1
    try:
        directory_fd = os.open(expected_directory, directory_flags)
        directory_metadata = os.fstat(directory_fd)
        if not _systemd_credential_metadata_safe(
            directory_metadata,
            mode=directory_mode,
            directory=True,
        ):
            raise _protocol_error(
                "credential_untrusted", "systemd credential metadata is unsafe"
            )

        entry_metadata = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if not _systemd_credential_metadata_safe(
            entry_metadata,
            mode=file_mode,
            directory=False,
        ):
            raise _protocol_error(
                "credential_untrusted", "systemd credential metadata is unsafe"
            )
        credential_fd = os.open(name, file_flags, dir_fd=directory_fd)
        before = os.fstat(credential_fd)
        if (
            not _systemd_credential_metadata_safe(
                before,
                mode=file_mode,
                directory=False,
            )
            or (entry_metadata.st_dev, entry_metadata.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise _protocol_error(
                "credential_untrusted", "systemd credential metadata is unsafe"
            )

        chunks: list[bytes] = []
        remaining = _MAX_SYSTEMD_CREDENTIAL_BYTES + 1
        while remaining:
            chunk = os.read(credential_fd, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(credential_fd)
        verify_fd = os.open(name, file_flags, dir_fd=directory_fd)
        current = os.fstat(verify_fd)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(raw) != before.st_size
            or any(
                getattr(before, field) != getattr(after, field)
                for field in stable_fields
            )
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
            or not _systemd_credential_metadata_safe(
                current,
                mode=file_mode,
                directory=False,
            )
        ):
            raise _protocol_error(
                "credential_untrusted", "systemd credential changed during read"
            )
        try:
            value = raw.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise _protocol_error(
                "credential_untrusted", "systemd credential is invalid"
            ) from exc
        if not value:
            raise _protocol_error(
                "credential_untrusted", "systemd credential is invalid"
            )
        return value
    except DeliveryControlError:
        raise
    except OSError as exc:
        raise _protocol_error(
            "credential_unavailable", "systemd credential unavailable"
        ) from exc
    finally:
        for descriptor in (verify_fd, credential_fd, directory_fd):
            if descriptor >= 0:
                os.close(descriptor)


def _require_root() -> None:
    if os.geteuid() != 0:
        raise _protocol_error("root_required", "delivery-control command requires root")


def _read_installed_setup_file(
    path: Path,
    *,
    mode: int,
    maximum_bytes: int,
) -> bytes:
    """Read one fixed root-owned setup asset without pathname races."""

    path = Path(path)
    if not path.is_absolute():
        raise OSError("setup asset path is not absolute")
    current = Path("/")
    for part in path.parts[1:-1]:
        current /= part
        metadata = current.lstat()
        if (
            current.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise OSError("setup asset parent metadata is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_gid != 0
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise OSError("setup asset metadata is unsafe")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                raise OSError("setup asset was truncated")
            chunks.append(block)
            remaining -= len(block)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OSError("setup asset changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _validate_installed_setup_assets() -> tuple[list[str], list[str]]:
    """Validate installed copies against the root-published reviewed manifest."""

    failures: list[str] = []
    targets = [str(path) for path in sorted(_INSTALLED_SETUP_ASSET_MODES, key=str)]
    try:
        raw = _read_installed_setup_file(
            INSTALL_ASSET_MANIFEST, mode=0o400, maximum_bytes=64 * 1024,
        )
        payload = json.loads(raw)
        files = payload.get("files") if isinstance(payload, Mapping) else None
        if (
            payload.get("schema") != "hermes-delivery-installed-assets/v1"
            or not isinstance(files, Mapping)
            or set(files) != set(targets)
        ):
            raise OSError("installed asset manifest is incomplete")
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return targets, ["installed_asset_manifest_missing_or_untrusted"]
    for target, expected_mode in _INSTALLED_SETUP_ASSET_MODES.items():
        claim = files.get(str(target))
        try:
            if not isinstance(claim, Mapping):
                raise OSError("asset claim missing")
            digest = str(claim.get("sha256") or "")
            claimed_mode = int(claim.get("mode"))
            if (
                claimed_mode != expected_mode
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise OSError("asset claim invalid")
            content = _read_installed_setup_file(
                target, mode=expected_mode, maximum_bytes=128 * 1024 * 1024,
            )
            if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), digest):
                raise OSError("asset digest mismatch")
        except (OSError, TypeError, ValueError):
            failures.append(f"installed_asset_missing_or_untrusted:{target}")
    return targets, failures


def _systemd_unit_matches(unit: str, verb: str, expected: str) -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", verb, unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            cwd="/",
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == expected


def _trusted_runtime_versions_match(python: Path) -> bool:
    """Probe the exact child interpreter, including its hermetic base binding."""

    from hermes_cli.sqlite_runtime import probe_sqlite_runtime

    try:
        _base, binding = require_trusted_python_base()
        info = probe_sqlite_runtime(python)
        expected_python = tuple(int(part) for part in TRUSTED_PYTHON_VERSION.split("."))
        expected_sqlite = tuple(int(part) for part in TRUSTED_SQLITE_VERSION.split("."))
        return bool(
            info is not None
            and info.python_version == expected_python
            and info.sqlite_version == expected_sqlite
            and info.base_prefix == TRUSTED_PYTHON_ROOT
            and info.sqlite_source_id == binding["sqlite_source_id"]
        )
    except (OSError, TrustedDeliveryRuntimeError, ValueError):
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-delivery-control")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("setup-dry-run")
    sub.add_parser("serve")
    sub.add_parser("policy-refresh")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "setup-dry-run":
        assets, missing = _validate_installed_setup_assets()
        activation_blockers: list[str] = []
        try:
            require_trusted_gcloud()
        except TrustedDeliveryRuntimeError:
            activation_blockers.append("root_owned_gcloud_runtime_missing")
        policy_runtime_ready = False
        try:
            require_trusted_immutable_runtime(
                TRUSTED_POLICY_RUNTIME_ROOT,
                executable=TRUSTED_POLICY_PYTHON,
                schema="hermes-delivery-controller-runtime/v1",
            )
            policy_runtime_ready = True
        except TrustedDeliveryRuntimeError:
            activation_blockers.append("root_owned_policy_runtime_missing")
        if policy_runtime_ready and not _trusted_runtime_versions_match(
            TRUSTED_POLICY_PYTHON
        ):
            activation_blockers.append("policy_runtime_python_sqlite_unreviewed")
        worker_runtime_ready = False
        try:
            require_trusted_immutable_runtime(
                TRUSTED_WORKER_RUNTIME_ROOT,
                executable=TRUSTED_WORKER_PYTHON,
                schema="hermes-worker-runtime/v1",
            )
            worker_runtime_ready = True
        except TrustedDeliveryRuntimeError:
            activation_blockers.append("root_owned_worker_runtime_missing")
        if worker_runtime_ready and not _trusted_runtime_versions_match(
            TRUSTED_WORKER_PYTHON
        ):
            activation_blockers.append("worker_runtime_python_sqlite_unreviewed")
        try:
            require_trusted_gh()
        except TrustedDeliveryRuntimeError:
            activation_blockers.append("root_owned_gh_runtime_missing")
        from hermes_cli.dependency_provisioner import (
            BUILDER_ENTRYPOINT,
            UV_PATH,
            UV_SHA256,
            _trusted_executable,
        )
        try:
            _trusted_executable(UV_PATH, expected_sha256=UV_SHA256)
        except Exception:
            activation_blockers.append("reviewed_uv_runtime_missing")
        try:
            _trusted_executable(BUILDER_ENTRYPOINT)
        except Exception:
            activation_blockers.append("immutable_dependency_entrypoint_missing")
        try:
            require_root_owned_executable(
                GIT_ASKPASS_PATH,
                root=Path("/usr"),
            )
        except TrustedDeliveryRuntimeError:
            activation_blockers.append("immutable_git_askpass_missing")
        try:
            require_root_owned_executable(
                Path("/usr/local/sbin/hermes-delivery-control-install"),
                root=Path("/usr/local"),
            )
        except TrustedDeliveryRuntimeError:
            activation_blockers.append("immutable_setup_installer_missing")

        required_state = {
            STATE_ROOT: (0, 0, 0o755),
            STATE_ROOT / "base": (FIXED_WORKER_UID, FIXED_WORKER_UID, 0o700),
            STATE_ROOT / "acceptance": (FIXED_WORKER_UID, FIXED_WORKER_UID, 0o700),
            STATE_ROOT / "evidence": (FIXED_WORKER_UID, FIXED_WORKER_UID, 0o700),
            STATE_ROOT / "transactions": (FIXED_WORKER_UID, FIXED_WORKER_UID, 0o700),
            STATE_ROOT / "home": (FIXED_WORKER_UID, FIXED_WORKER_UID, 0o700),
            STATE_ROOT / "xdg": (FIXED_WORKER_UID, FIXED_WORKER_UID, 0o700),
            STATE_ROOT / "gh": (FIXED_WORKER_UID, FIXED_WORKER_UID, 0o700),
            STATE_ROOT / "gcloud": (FIXED_WORKER_UID, FIXED_WORKER_UID, 0o700),
            STATE_ROOT / "dependencies": (0, 0, 0o755),
            STATE_ROOT / "attestations": (0, 0, 0o755),
            ROOT_POLICY_STATE_ROOT: (0, 0, 0o700),
            ROOT_POLICY_STATE_ROOT / "home": (0, 0, 0o700),
            ROOT_POLICY_STATE_ROOT / "xdg": (0, 0, 0o700),
            ROOT_POLICY_STATE_ROOT / "gh": (0, 0, 0o700),
            ROOT_POLICY_STATE_ROOT / "gcloud-disabled": (0, 0, 0o700),
        }
        for state_path, expected in required_state.items():
            try:
                metadata = state_path.lstat()
                actual = (
                    metadata.st_uid,
                    metadata.st_gid,
                    stat.S_IMODE(metadata.st_mode),
                )
                if (
                    state_path.is_symlink()
                    or not stat.S_ISDIR(metadata.st_mode)
                    or actual != expected
                ):
                    raise OSError("unsafe metadata")
            except OSError:
                activation_blockers.append(
                    f"required_state_missing:{state_path}"
                )
        for credential in (
            Path("/etc/hermes-delivery-control/github-token"),
            Path("/etc/hermes-delivery-control/policy-signing-key.pem"),
        ):
            try:
                metadata = credential.lstat()
                if (
                    credential.is_symlink()
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != 0
                    or metadata.st_gid != 0
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise OSError("unsafe metadata")
            except OSError:
                activation_blockers.append(
                    f"root_credential_missing:{credential.name}"
                )
        try:
            mirror = require_base_repository_mirror()
            read_fresh_base_receipt(mirror)
        except DeliveryControlError:
            activation_blockers.append("fresh_authenticated_base_mirror_missing")
        for unit, (enabled, active) in _REQUIRED_SYSTEMD_UNIT_STATE.items():
            if not _systemd_unit_matches(unit, "is-enabled", enabled):
                activation_blockers.append(f"systemd_unit_not_enabled:{unit}")
            if not _systemd_unit_matches(unit, "is-active", active):
                activation_blockers.append(f"systemd_unit_not_active:{unit}")
        from hermes_cli.delivery_verifier import (
            _CLAUSEYE_POLICY_ATTESTATION_PUBLIC_KEY_B64,
        )
        if not _CLAUSEYE_POLICY_ATTESTATION_PUBLIC_KEY_B64:
            activation_blockers.append("policy_public_key_pin_missing")
        print(json.dumps({
            "schema": PROTOCOL_SCHEMA,
            "mode": "dry-run",
            "installed_assets": assets,
            "fixed_socket": str(SOCKET_PATH),
            "fixed_db": str(FIXED_KANBAN_DB),
            "missing_assets": missing,
            "ready": not missing,
            "activation_ready": not missing and not activation_blockers,
            "activation_blockers": activation_blockers,
            "required_state": {
                str(path): f"{uid}:{gid} {mode:04o}"
                for path, (uid, gid, mode) in required_state.items()
            },
            "applied": False,
        }, sort_keys=True))
        return 0 if not missing else 1
    if args.action == "serve":
        backend = SubprocessDeliveryBackend(
            read_systemd_credential(GITHUB_CREDENTIAL_NAME)
        )
        control = DeliveryControl(FIXED_KANBAN_DB, backend)
        control.prepare_recovery()
        serve_forever(control)
        return 0
    if args.action == "policy-refresh":
        _require_root()
        backend = SubprocessDeliveryBackend(
            read_systemd_credential(GITHUB_CREDENTIAL_NAME),
            root_policy=True,
        )
        from hermes_cli.delivery_verifier import (
            produce_clauseye_policy_attestation,
            publish_clauseye_policy_attestation,
            trusted_command_environment,
        )

        with trusted_command_environment(backend._env):
            envelope = produce_clauseye_policy_attestation(
                FIXED_REPOSITORY,
                FIXED_DEFAULT_BRANCH,
                cwd=ROOT_POLICY_STATE_ROOT,
                private_key_pem=read_systemd_credential(
                    POLICY_SIGNING_CREDENTIAL_NAME
                ).encode("ascii"),
            )
            result = publish_clauseye_policy_attestation(envelope)
        print(json.dumps(result, sort_keys=True))
        return 0
    raise _protocol_error("command_invalid", "delivery-control command is unsupported")


if __name__ == "__main__":  # pragma: no cover - exercised via CLI behavior tests
    try:
        exit_code = main()
    except Exception as exc:
        print(json.dumps(_response_for_error(exc), sort_keys=True), file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(exit_code)
