"""Root-owned, no-exec dependency snapshot provisioner for delivery workers.

The worker sandbox has no network and never receives host package caches.  A
trusted controller therefore asks this narrow root service to materialize the
exact lockfile set in a disposable ``DynamicUser=`` unit.  Package code runs
only in that unit.  The root process copies and validates files, but never
imports, executes, or shells through candidate/package content.

This module is also installed into the immutable delivery-control venv.  It
must not be run from the mutable Hermes source checkout in production.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import signal
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping
from urllib.parse import urlsplit


PROTOCOL_SCHEMA = "hermes-dependency-provision/v1"
SOCKET_PATH = Path("/run/hermes-dependency-provision/control.sock")
SNAPSHOT_ROOT = Path("/var/lib/hermes-delivery-control/dependencies")
PROJECT_ROOT = Path("/home/ab/code/clauseye-contra-rope")
WORKTREE_ROOT = PROJECT_ROOT / ".worktrees"
WORKER_UID = 1000
KANBAN_DB = Path("/home/ab/.hermes/kanban.db")
PROJECTS_DB = Path("/home/ab/.hermes/projects.db")
PROJECT_ID = "p_d85f8d02"
PROJECT_SLUG = "clauseye-production-readiness"
SYSTEMD_RUN = Path("/usr/bin/systemd-run")
SYSTEMCTL = Path("/usr/bin/systemctl")
RM_PATH = Path("/usr/bin/rm")
NODE_PATH = Path("/usr/bin/node")
NODE_SHA256 = "93956de2e59480474a7b46571da1651180b1a050cdf32641ebec4ce6e478e068"
NPM_ROOT = Path("/usr/lib/node_modules/npm")
NPM_CLI_PATH = NPM_ROOT / "bin" / "npm-cli.js"
NPM_CLI_SHA256 = "8e5f6f3429f8cdbe693cdc29904e9d5a7b127a494bd15c804bd54c7403bfcbe7"
# npm 10.9.8 as installed by the reviewed Node 22.23.1 package.  Pinning the
# complete module tree prevents a root package update from silently changing
# package-resolution code between cutover reviews.
NPM_TREE_SHA256 = "0bd14de2f30eb21f306fd8da04fff1da3b9a159059bfd23e96ec33451fd7378d"
UV_PATH = Path("/usr/libexec/hermes-delivery-control/uv")
# uv 0.11.18, copied byte-for-byte to UV_PATH and made root-owned at cutover.
# A different reviewed uv build requires an intentional source pin update.
UV_SHA256 = "8efd13c4b649d3fbd264853c2d05419f18e2dc0816f02bb408a79525e50c062d"
TRUSTED_PYTHON_VERSION = "3.13.14"
TRUSTED_SQLITE_VERSION = "3.53.1"
TRUSTED_SQLITE_SOURCE_ID = (
    "2026-05-05 10:34:17 "
    "c88b22011a54b4f6fbd149e9f8e4de77658ce58143a1af0e3785e4e6475127e9"
)
TRUSTED_PYTHON_BASE_PREFIX = Path(
    "/opt/hermes-delivery-control/python-3.13.14+20260623"
)
TRUSTED_PYTHON_BASE_EXECUTABLE = (
    TRUSTED_PYTHON_BASE_PREFIX / "bin" / "python3.13"
)
BUILDER_PYTHON = Path("/usr/lib/hermes-delivery-control/venv/bin/python")
# Snapshot identity includes the reviewed package toolchain and exact resolver
# policy.  Changing any of these values deliberately produces a new snapshot
# digest even when candidate manifests are byte-identical, so a tree built
# under an older policy can never be reused after a cutover.
DEPENDENCY_BUILD_POLICY = (
    "hermes-dependency-build/v4;"
    f"npm-tree-sha256={NPM_TREE_SHA256};"
    f"uv-sha256={UV_SHA256};"
    f"python={TRUSTED_PYTHON_VERSION};"
    f"python-base={TRUSTED_PYTHON_BASE_PREFIX};"
    f"python-base-executable={TRUSTED_PYTHON_BASE_EXECUTABLE};"
    f"builder-python={BUILDER_PYTHON};"
    f"sqlite={TRUSTED_SQLITE_VERSION};"
    f"sqlite-source-id={TRUSTED_SQLITE_SOURCE_ID};"
    "npm-ci=no-audit,no-fund;"
    "uv-sync=frozen,extra-dev,no-install-project,no-install-workspace,"
    "no-install-local,no-editable,link-mode-copy,no-managed-python,"
    "no-python-downloads;builder-entrypoint=isolated-no-bytecode;"
    "python-probe=isolated-no-bytecode;"
    "root-base-probe=isolated-no-site-no-bytecode;"
    "startup-customization=forbid"
)
SNAPSHOT_ATTESTATION_VERSION = 2
PYTHON_INSTALL_MODE = (
    "uv-sync-frozen-extra-dev-python-pinned-no-managed-python-"
    "no-python-downloads-no-install-project-no-install-workspace-"
    "no-install-local-no-editable-link-copy"
)
BUILDER_ENTRYPOINT = Path(
    "/usr/libexec/hermes-delivery-control-dependency-provisioner.py"
)
MAX_REQUEST_BYTES = 8 * 1024
MAX_RESPONSE_BYTES = 8 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_TREE_ENTRIES = 500_000
MAX_TREE_BYTES = 16 * 1024 * 1024 * 1024
# Bound one package-controlled file independently of the aggregate tree. This
# keeps both the DynamicUser build and the trusted streaming validator from
# accepting a single sparse/huge file as a memory or disk exhaustion primitive.
MAX_TREE_FILE_BYTES = 512 * 1024 * 1024
MAX_RELOCATABLE_SCRIPT_BYTES = 4 * 1024 * 1024
MAX_PYTHON_METADATA_BYTES = 1024 * 1024
PYTHON_RUNTIME_METADATA = "python-runtime.json"
BUILD_TIMEOUT_SECONDS = 30 * 60
# Root validation, streaming copy/hash, fsync, and atomic promotion happen
# after the untrusted build and can be substantial for the 7+ GiB canonical
# snapshot. Claims and the client socket cover that entire bounded operation.
PROVISION_REQUEST_TIMEOUT_SECONDS = BUILD_TIMEOUT_SECONDS + 15 * 60
STALE_BUILD_GRACE_SECONDS = 10 * 60
MAX_STALE_CLEANUPS_PER_REQUEST = 16
SNAPSHOT_RETENTION_SECONDS = 14 * 24 * 60 * 60
MAX_SNAPSHOT_GC_PER_REQUEST = 4
MAX_SNAPSHOT_STORAGE_BYTES = 128 * 1024 * 1024 * 1024
MAX_BUILDER_TMPFS_BYTES = 18 * 1024 * 1024 * 1024
TASK_ID_RE = re.compile(r"^t_[0-9a-f]{8}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UV_ARTIFACT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
NPM_INTEGRITY_RE = re.compile(r"^(sha256|sha384|sha512)-([A-Za-z0-9+/]+={0,2})$")
CLAIM_RE = re.compile(r"^[A-Za-z0-9._:@+-]{8,256}$")
BRANCH_RE = re.compile(r"^hermes/[a-z0-9][a-z0-9/_-]{1,180}$")
ALLOWED_REGISTRY_HOSTS = frozenset({
    "registry.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
})
_NODE_LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json")
_PYTHON_MANIFESTS = ("backend/pyproject.toml", "backend/uv.lock")


class DependencyProvisionError(RuntimeError):
    """A safe, non-secret provisioning failure."""


@dataclass(frozen=True)
class ManifestBundle:
    task_id: str
    workspace: Path
    files: Mapping[str, bytes]
    hashes: Mapping[str, str]
    digest: str
    has_node: bool
    has_python: bool
    project_root: Path
    worker_uid: int


@dataclass(frozen=True)
class ProvisionRequest:
    task_id: str
    workspace: Path
    run_id: int
    claim_lock: str


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")


def _manifest_digest(hashes: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    digest.update(b"dependency-build-policy\0")
    digest.update(DEPENDENCY_BUILD_POLICY.encode("ascii"))
    digest.update(b"\n")
    for relative, value in sorted(hashes.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_workspace(
    task_id: str,
    workspace: Path,
    *,
    project_root: Path = PROJECT_ROOT,
    worker_uid: int = WORKER_UID,
) -> Path:
    if not TASK_ID_RE.fullmatch(task_id):
        raise DependencyProvisionError("task id is not canonical")
    expected = project_root / ".worktrees" / task_id
    if not workspace.is_absolute() or workspace != expected:
        raise DependencyProvisionError("workspace is not the exact registered task worktree")
    current = project_root
    for path in (current, current / ".worktrees", workspace):
        try:
            info = path.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != worker_uid
                or stat.S_IMODE(info.st_mode) & 0o002
                or path.resolve(strict=True) != path
            ):
                raise DependencyProvisionError(
                    f"untrusted worktree path component: {path.name}"
                )
        except DependencyProvisionError:
            raise
        except (OSError, RuntimeError) as exc:
            raise DependencyProvisionError("registered task worktree is unavailable") from exc
    return workspace


def _validate_control_file(path: Path) -> Path:
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != WORKER_UID
            or stat.S_IMODE(info.st_mode) & 0o002
            or path.resolve(strict=True) != path
        ):
            raise DependencyProvisionError("trusted control database is unsafe")
        return path
    except DependencyProvisionError:
        raise
    except (OSError, RuntimeError) as exc:
        raise DependencyProvisionError("trusted control database is unavailable") from exc


def _open_writable_sqlite(path: Path) -> sqlite3.Connection:
    _validate_control_file(path)
    connection = sqlite3.connect(path, timeout=120, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=120000")
    return connection


_SQLITE_HEADER_BYTES = 100
_SQLITE_WAL_FORMAT = b"\x02\x02"
_SQLITE_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_CONTROL_DATABASE_BYTES = 512 * 1024 * 1024


def _sqlite_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Return the exact stable-file identity used by immutable DB reads."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _sqlite_fd_fingerprint(
    descriptor: int,
    expected: os.stat_result,
) -> bytes:
    """Hash one held DB inode while proving its metadata stays unchanged."""

    before = os.fstat(descriptor)
    if (
        _sqlite_file_identity(before) != _sqlite_file_identity(expected)
        or before.st_size < _SQLITE_HEADER_BYTES
        or before.st_size > _MAX_CONTROL_DATABASE_BYTES
    ):
        raise DependencyProvisionError(
            "trusted control database changed while fingerprinting"
        )
    digest = hashlib.sha256()
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(
            descriptor,
            min(_SQLITE_HASH_CHUNK_BYTES, before.st_size - offset),
            offset,
        )
        if not chunk:
            raise DependencyProvisionError(
                "trusted control database was truncated while fingerprinting"
            )
        digest.update(chunk)
        offset += len(chunk)
    after = os.fstat(descriptor)
    if _sqlite_file_identity(after) != _sqlite_file_identity(before):
        raise DependencyProvisionError(
            "trusted control database changed while fingerprinting"
        )
    return digest.digest()


def _sqlite_sidecar_names(path: Path) -> frozenset[str]:
    """Return every SQLite-style sidecar beside one fixed control DB."""

    prefix = f"{path.name}-"
    try:
        with os.scandir(path.parent) as entries:
            return frozenset(
                entry.name for entry in entries if entry.name.startswith(prefix)
            )
    except OSError as exc:
        raise DependencyProvisionError(
            "trusted control database sidecars are unavailable"
        ) from exc


def _require_safe_sqlite_sidecars(path: Path, names: Iterable[str]) -> None:
    """Reject redirected or cross-owner WAL files before SQLite opens them."""

    try:
        for name in names:
            metadata = (path.parent / name).lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != WORKER_UID
                or metadata.st_gid != WORKER_UID
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) & 0o002
            ):
                raise DependencyProvisionError(
                    "trusted control database sidecar is unsafe"
                )
    except DependencyProvisionError:
        raise
    except OSError as exc:
        raise DependencyProvisionError(
            "trusted control database sidecar changed before read"
        ) from exc


def _configure_read_only_connection(
    connection: sqlite3.Connection,
    *,
    require_reported_wal: bool,
) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=120000")
    connection.execute("PRAGMA query_only=ON")
    # Force SQLite to open and parse the database before authorization code is
    # allowed to run. sqlite3.connect() itself is lazy for URI connections.
    connection.execute("SELECT 1 FROM sqlite_schema LIMIT 0").fetchall()
    # ``immutable=1`` deliberately reports ``delete`` even when the on-disk
    # header is WAL format, so only the ordinary locked reader can use this
    # runtime assertion.  The immutable branch validates header bytes 18/19
    # directly before opening the held inode instead.
    if require_reported_wal:
        mode = connection.execute("PRAGMA journal_mode").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            raise DependencyProvisionError(
                "trusted control database is not using WAL journaling"
            )
    # Authorization spans several queries (task, run, event, policy).  Hold one
    # read transaction across the context so those facts cannot be assembled
    # from different committed snapshots while a writer advances the board.
    connection.execute("BEGIN")


def _close_read_only_connection(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    finally:
        connection.close()


@contextlib.contextmanager
def _read_only_sqlite(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a race-bound read-only view of one WAL control database.

    A read-only mount cannot create ``-wal``/``-shm`` when the final writer has
    checkpointed and removed them.  In that cold state SQLite's normal
    ``mode=ro`` connection fails on its first query.  ``immutable=1`` is safe
    only while the main WAL-format file is byte-stable and *no* SQLite sidecar
    appears.  Hold an ``O_NOFOLLOW`` descriptor, make SQLite reopen that exact
    inode via ``/proc/self/fd``, and revalidate both the descriptor/path identity
    and sidecar absence after the connection closes.  Any concurrent writer,
    checkpoint, rename, rollback journal, or partial WAL state fails closed.
    """

    path = _validate_control_file(path)
    wal_name = f"{path.name}-wal"
    shm_name = f"{path.name}-shm"
    sidecars = _sqlite_sidecar_names(path)
    if sidecars == frozenset({wal_name, shm_name}):
        _require_safe_sqlite_sidecars(path, sidecars)
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True, timeout=30,
        )
        try:
            _configure_read_only_connection(
                connection, require_reported_wal=True,
            )
            yield connection
        finally:
            _close_read_only_connection(connection)
        return
    if sidecars:
        raise DependencyProvisionError(
            "trusted control database sidecar state is transient"
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    before: os.stat_result | None = None
    before_fingerprint: bytes | None = None
    connection: sqlite3.Connection | None = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        rebound = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != WORKER_UID
            or before.st_gid != WORKER_UID
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o002
            or _sqlite_file_identity(before) != _sqlite_file_identity(rebound)
        ):
            raise DependencyProvisionError(
                "trusted control database changed before immutable read"
            )
        header = os.pread(descriptor, _SQLITE_HEADER_BYTES, 0)
        if (
            len(header) != _SQLITE_HEADER_BYTES
            or header[:16] != b"SQLite format 3\x00"
            or header[18:20] != _SQLITE_WAL_FORMAT
        ):
            raise DependencyProvisionError(
                "trusted control database is not a stable WAL database"
            )
        if _sqlite_sidecar_names(path):
            raise DependencyProvisionError(
                "trusted control database sidecar state changed before immutable read"
            )
        before_fingerprint = _sqlite_fd_fingerprint(descriptor, before)
        if (
            _sqlite_file_identity(os.fstat(descriptor))
            != _sqlite_file_identity(before)
            or _sqlite_file_identity(path.lstat())
            != _sqlite_file_identity(before)
            or _sqlite_sidecar_names(path)
        ):
            raise DependencyProvisionError(
                "trusted control database changed before immutable read"
            )

        connection = sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1",
            uri=True,
            timeout=30,
        )
        _configure_read_only_connection(
            connection, require_reported_wal=False,
        )
        yield connection
    except DependencyProvisionError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise DependencyProvisionError(
            "trusted control database immutable read failed"
        ) from exc
    finally:
        close_error: Exception | None = None
        if connection is not None:
            try:
                _close_read_only_connection(connection)
            except Exception as exc:  # pragma: no cover - sqlite close is defensive
                close_error = exc
        if descriptor >= 0:
            try:
                if before is not None:
                    if _sqlite_sidecar_names(path):
                        raise DependencyProvisionError(
                            "trusted control database changed during immutable read"
                        )
                    after_fingerprint = _sqlite_fd_fingerprint(descriptor, before)
                    after = os.fstat(descriptor)
                    rebound_after = path.lstat()
                    if (
                        before_fingerprint is None
                        or not hmac.compare_digest(
                            after_fingerprint, before_fingerprint,
                        )
                        or _sqlite_file_identity(after)
                        != _sqlite_file_identity(before)
                        or _sqlite_file_identity(rebound_after)
                        != _sqlite_file_identity(before)
                        or _sqlite_sidecar_names(path)
                    ):
                        close_error = DependencyProvisionError(
                            "trusted control database changed during immutable read"
                        )
            except Exception as exc:
                close_error = exc
            finally:
                os.close(descriptor)
        if close_error is not None:
            if isinstance(close_error, DependencyProvisionError):
                raise close_error
            raise DependencyProvisionError(
                "trusted control database immutable read could not be verified"
            ) from close_error


def _valid_branch(task_id: str, branch: str) -> bool:
    return (
        BRANCH_RE.fullmatch(branch) is not None
        and ".." not in branch
        and "//" not in branch
        and not branch.endswith(("/", ".", ".lock"))
        and re.search(rf"(?:^|/){re.escape(task_id)}(?:[-/]|$)", branch) is not None
    )


def extend_active_claim_for_provision(
    request: ProvisionRequest,
    *,
    worker_pid: int,
    now: int | None = None,
) -> int:
    """Atomically keep the exact worker alive for the maximum build window."""
    current_time = int(time.time()) if now is None else int(now)
    expires = current_time + PROVISION_REQUEST_TIMEOUT_SECONDS + 5 * 60
    connection = _open_writable_sqlite(KANBAN_DB)
    try:
        connection.execute("BEGIN IMMEDIATE")
        task_update = connection.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ? AND status = 'running' AND current_run_id = ? "
            "AND claim_lock IS ? AND worker_pid = ? AND workspace_kind = 'worktree' "
            "AND workspace_path IS ? AND project_id IS ?",
            (
                expires, current_time, request.task_id, request.run_id,
                request.claim_lock, worker_pid, str(request.workspace), PROJECT_ID,
            ),
        )
        if task_update.rowcount != 1:
            connection.rollback()
            raise DependencyProvisionError("active dependency request claim is stale")
        run_update = connection.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ? AND task_id = ? "
            "AND ended_at IS NULL AND claim_lock IS ? AND worker_pid = ?",
            (
                expires, request.run_id, request.task_id,
                request.claim_lock, worker_pid,
            ),
        )
        if run_update.rowcount != 1:
            connection.rollback()
            raise DependencyProvisionError("active dependency request run is stale")
        connection.commit()
        return expires
    except DependencyProvisionError:
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise DependencyProvisionError("active dependency request could not be extended") from exc
    finally:
        connection.close()


def authorize_provision_request(
    request: ProvisionRequest,
    *,
    peer_pid: int,
    peer_uid: int,
) -> None:
    """Re-derive run, project, workspace, role, and delivery contract state."""
    if peer_uid != WORKER_UID:
        raise DependencyProvisionError("request peer is unauthorized")
    from hermes_cli import kanban_db as kb

    try:
        with _read_only_sqlite(KANBAN_DB) as connection:
            task = kb.get_task(connection, request.task_id)
            if (
                task is None
                or task.status != "running"
                or task.current_run_id != request.run_id
                or not hmac.compare_digest(
                    str(task.claim_lock or ""), request.claim_lock,
                )
                or task.claim_expires is None
                or int(task.claim_expires) <= int(time.time())
                or int(task.worker_pid or 0) != int(peer_pid)
            ):
                raise DependencyProvisionError("active dependency request run is stale")
            run = connection.execute(
                "SELECT * FROM task_runs WHERE id = ? AND task_id = ?",
                (request.run_id, request.task_id),
            ).fetchone()
            if (
                run is None
                or run["ended_at"] is not None
                or not hmac.compare_digest(
                    str(run["claim_lock"] or ""), request.claim_lock,
                )
                or int(run["worker_pid"] or 0) != int(peer_pid)
                or kb._run_source_status(connection, request.task_id, request.run_id)
                not in {"ready", "review"}
            ):
                raise DependencyProvisionError(
                    "active dependency request identity is inconsistent"
                )
            if (
                task.workspace_kind != "worktree"
                or task.workspace_path != str(request.workspace)
                or task.project_id != PROJECT_ID
                or request.workspace != WORKTREE_ROOT / request.task_id
                or not _valid_branch(request.task_id, str(task.branch_name or ""))
            ):
                raise DependencyProvisionError(
                    "dependency request is not an eligible project worktree"
                )
            policy = kb._completion_delivery_policy(task)
            kb._require_valid_delivery_contract(policy)
            if policy.get("pr_gate") != "merge" or not policy.get("contract_hash"):
                raise DependencyProvisionError(
                    "dependency request lacks the merge-gated contract"
                )
    except DependencyProvisionError:
        raise
    except Exception as exc:
        raise DependencyProvisionError("dependency request state is invalid") from exc

    try:
        with _read_only_sqlite(PROJECTS_DB) as projects:
            project = projects.execute(
                "SELECT id, slug, primary_path, archived FROM projects WHERE id = ?",
                (PROJECT_ID,),
            ).fetchone()
            if (
                project is None
                or project["slug"] != PROJECT_SLUG
                or int(project["archived"] or 0) != 0
                or project["primary_path"] != str(PROJECT_ROOT)
            ):
                raise DependencyProvisionError(
                    "registered dependency project identity is invalid"
                )
    except DependencyProvisionError:
        raise
    except sqlite3.Error as exc:
        raise DependencyProvisionError("registered dependency project cannot be verified") from exc


def _read_relative_file_nofollow(
    root: Path,
    relative: str,
    *,
    owner_uid: int,
) -> bytes:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise DependencyProvisionError("manifest path is unsafe")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    opened: list[int] = []
    try:
        root_fd = os.open(root, directory_flags)
        opened.append(root_fd)
        current_fd = root_fd
        for part in parts[:-1]:
            current_fd = os.open(part, directory_flags, dir_fd=current_fd)
            opened.append(current_fd)
            info = os.fstat(current_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid:
                raise DependencyProvisionError("manifest parent is untrusted")
        fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
        opened.append(fd)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner_uid
            or info.st_size > MAX_MANIFEST_BYTES
        ):
            raise DependencyProvisionError("manifest is untrusted or too large")
        chunks: list[bytes] = []
        remaining = MAX_MANIFEST_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise DependencyProvisionError("manifest exceeds the size limit")
        return payload
    except DependencyProvisionError:
        raise
    except OSError as exc:
        raise DependencyProvisionError(
            f"manifest {relative} is missing, symlinked, or unreadable"
        ) from exc
    finally:
        for fd in reversed(opened):
            try:
                os.close(fd)
            except OSError:
                pass


def _url_host_allowed(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() in ALLOWED_REGISTRY_HOSTS
        and parsed.username is None
        and parsed.password is None
        and parsed.port in (None, 443)
    )


def _walk_values(value: Any) -> Iterable[tuple[str | None, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield None, child
            yield from _walk_values(child)


def _valid_npm_integrity(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    for token in value.split():
        match = NPM_INTEGRITY_RE.fullmatch(token)
        if match is None:
            continue
        try:
            decoded = base64.b64decode(match.group(2), validate=True)
        except (ValueError, binascii.Error):
            continue
        if len(decoded) == {
            "sha256": 32,
            "sha384": 48,
            "sha512": 64,
        }[match.group(1)]:
            return True
    return False


def _bundled_npm_parent(
    package_path: str,
    packages: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    candidates = [
        parent
        for parent in packages
        if parent
        and package_path.startswith(f"{parent}/node_modules/")
    ]
    if not candidates:
        return None
    parent_path = max(candidates, key=len)
    parent = packages.get(parent_path)
    if not isinstance(parent, dict):
        return None
    resolved = parent.get("resolved")
    if (
        not isinstance(resolved, str)
        or not _url_host_allowed(resolved)
        or not _valid_npm_integrity(parent.get("integrity"))
    ):
        return None
    tail = package_path[len(parent_path) + len("/node_modules/") :]
    parts = tail.split("/")
    package_name = "/".join(parts[:2]) if tail.startswith("@") else parts[0]
    bundled = parent.get("bundleDependencies", parent.get("bundledDependencies"))
    if not isinstance(bundled, list) or package_name not in bundled:
        return None
    return parent


def _validate_node_manifests(package: bytes, lock: bytes) -> None:
    try:
        package_data = json.loads(package)
        lock_data = json.loads(lock)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DependencyProvisionError("Node manifests are not valid JSON") from exc
    if not isinstance(package_data, dict) or not isinstance(lock_data, dict):
        raise DependencyProvisionError("Node manifests have an invalid shape")
    if lock_data.get("lockfileVersion") not in (2, 3):
        raise DependencyProvisionError("automatic provisioning requires npm lockfile v2 or v3")
    for section in (
        "dependencies", "devDependencies", "optionalDependencies", "peerDependencies",
    ):
        values = package_data.get(section, {})
        if values is None:
            continue
        if not isinstance(values, dict):
            raise DependencyProvisionError(f"package.json {section} is invalid")
        for spec in values.values():
            if not isinstance(spec, str):
                raise DependencyProvisionError("package.json dependency spec is invalid")
            lowered = spec.strip().lower()
            if lowered.startswith(("file:", "link:", "workspace:", "git", "ssh:")):
                raise DependencyProvisionError("local, workspace, and Git Node dependencies are forbidden")
            if "://" in lowered and not _url_host_allowed(spec):
                raise DependencyProvisionError("Node dependency URL is outside the registry allowlist")
    packages = lock_data.get("packages", {})
    if not isinstance(packages, dict):
        raise DependencyProvisionError("npm lockfile packages map is invalid")
    for package_path, entry in packages.items():
        if not isinstance(package_path, str):
            raise DependencyProvisionError("npm lockfile package path is invalid")
        if not isinstance(entry, dict):
            raise DependencyProvisionError("npm lockfile package entry is invalid")
        resolved = entry.get("resolved")
        if entry.get("inBundle") is True:
            if resolved is not None or entry.get("integrity") is not None:
                raise DependencyProvisionError("bundled npm package has an ambiguous artifact")
            if _bundled_npm_parent(package_path, packages) is None:
                raise DependencyProvisionError("bundled npm package lacks a pinned parent artifact")
            continue
        if resolved is not None:
            if not isinstance(resolved, str) or not _url_host_allowed(resolved):
                raise DependencyProvisionError("npm lockfile URL is outside the registry allowlist")
            integrity = entry.get("integrity")
            if integrity is None:
                raise DependencyProvisionError("npm lockfile artifact integrity is missing")
            if not _valid_npm_integrity(integrity):
                raise DependencyProvisionError("npm lockfile artifact integrity is invalid")
    for key, value in _walk_values(lock_data):
        if key == "resolved" and isinstance(value, str) and not _url_host_allowed(value):
            raise DependencyProvisionError("npm lockfile URL is outside the registry allowlist")
        if key == "link" and value is True:
            raise DependencyProvisionError("npm lockfile contains a local or bundled link")


def _validate_python_manifests(pyproject: bytes, lock: bytes) -> None:
    try:
        project_data = tomllib.loads(pyproject.decode("utf-8"))
        lock_data = tomllib.loads(lock.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DependencyProvisionError("Python manifests are not valid TOML") from exc
    if not isinstance(project_data, dict) or not isinstance(lock_data, dict):
        raise DependencyProvisionError("Python manifests have an invalid shape")
    project = project_data.get("project")
    project_name = project.get("name") if isinstance(project, dict) else None
    if not isinstance(project_name, str) or not project_name.strip():
        raise DependencyProvisionError("pyproject project name is missing")
    optional_dependencies = (
        project.get("optional-dependencies") if isinstance(project, dict) else None
    )
    if (
        not isinstance(optional_dependencies, dict)
        or not isinstance(optional_dependencies.get("dev"), list)
    ):
        raise DependencyProvisionError(
            "pyproject must define the locked dev extra required by acceptance"
        )
    root_editable_count = 0
    packages = lock_data.get("package", [])
    if not isinstance(packages, list):
        raise DependencyProvisionError("uv.lock package list is invalid")
    for package in packages:
        if not isinstance(package, dict):
            raise DependencyProvisionError("uv.lock package entry is invalid")
        source = package.get("source")
        if isinstance(source, dict) and "editable" in source:
            if source.get("editable") != "." or package.get("name") != project_name:
                raise DependencyProvisionError("uv.lock contains a foreign editable source")
            root_editable_count += 1
        elif not isinstance(source, dict) or set(source) != {"registry"}:
            raise DependencyProvisionError(
                "uv.lock contains a local, Git, URL, or unknown package source"
            )
        else:
            registry = source.get("registry")
            if not isinstance(registry, str) or not _url_host_allowed(registry):
                raise DependencyProvisionError("uv.lock registry is outside the allowlist")
            artifacts: list[dict[str, Any]] = []
            sdist = package.get("sdist")
            if sdist is not None:
                if not isinstance(sdist, dict):
                    raise DependencyProvisionError("uv.lock sdist artifact is invalid")
                artifacts.append(sdist)
            wheels = package.get("wheels", [])
            if not isinstance(wheels, list) or any(not isinstance(item, dict) for item in wheels):
                raise DependencyProvisionError("uv.lock wheel artifacts are invalid")
            artifacts.extend(wheels)
            if not artifacts:
                raise DependencyProvisionError("uv.lock registry package has no pinned artifact")
            for artifact in artifacts:
                url = artifact.get("url")
                digest = artifact.get("hash")
                size = artifact.get("size")
                if not isinstance(url, str) or not _url_host_allowed(url):
                    raise DependencyProvisionError("uv.lock artifact URL is outside the allowlist")
                if not isinstance(digest, str) or UV_ARTIFACT_HASH_RE.fullmatch(digest) is None:
                    raise DependencyProvisionError("uv.lock artifact hash is missing or invalid")
                if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                    raise DependencyProvisionError("uv.lock artifact size is missing or invalid")
    editable_occurrences = 0
    for key, value in _walk_values(lock_data):
        if key == "editable":
            editable_occurrences += 1
            if value != ".":
                raise DependencyProvisionError("uv.lock contains a foreign editable source")
        if key in {"git", "path", "directory"}:
            raise DependencyProvisionError("uv.lock contains a local, Git, or editable source")
        if key in {"url", "registry"} and isinstance(value, str):
            if not _url_host_allowed(value):
                raise DependencyProvisionError("uv.lock URL is outside the registry allowlist")
    if editable_occurrences != root_editable_count or root_editable_count > 1:
        raise DependencyProvisionError("uv.lock editable source is not the exact root project")
    for _key, value in _walk_values(project_data):
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        if any(token in lowered for token in ("file://", "git+", "../", " @ ./")):
            raise DependencyProvisionError("pyproject contains a local or Git dependency")
        if " @ https://" in lowered:
            url = value[value.lower().index("https://") :].split()[0]
            if not _url_host_allowed(url):
                raise DependencyProvisionError("pyproject dependency URL is outside the registry allowlist")


def collect_manifest_bundle(
    task_id: str,
    workspace: str | Path,
    *,
    project_root: Path = PROJECT_ROOT,
    worker_uid: int = WORKER_UID,
) -> ManifestBundle:
    """Copy and validate the only candidate bytes the builder may receive."""
    workspace = _validate_workspace(
        task_id, Path(workspace), project_root=project_root, worker_uid=worker_uid,
    )
    files: dict[str, bytes] = {}

    node_paths = [
        name for name in ("package.json", *_NODE_LOCKFILES)
        if os.path.lexists(workspace / name)
    ]
    has_node = bool(node_paths)
    if has_node:
        present_locks = [name for name in _NODE_LOCKFILES if name in node_paths]
        if "package.json" not in node_paths or len(present_locks) != 1:
            raise DependencyProvisionError(
                "Node provisioning requires package.json and exactly one npm lockfile"
            )
        for relative in ("package.json", present_locks[0]):
            files[relative] = _read_relative_file_nofollow(
                workspace, relative, owner_uid=worker_uid,
            )
        _validate_node_manifests(files["package.json"], files[present_locks[0]])

    python_paths = [name for name in _PYTHON_MANIFESTS if os.path.lexists(workspace / name)]
    has_python = bool(python_paths)
    if has_python:
        if set(python_paths) != set(_PYTHON_MANIFESTS):
            raise DependencyProvisionError(
                "Python provisioning requires backend/pyproject.toml and backend/uv.lock"
            )
        for relative in _PYTHON_MANIFESTS:
            files[relative] = _read_relative_file_nofollow(
                workspace, relative, owner_uid=worker_uid,
            )
        _validate_python_manifests(
            files["backend/pyproject.toml"], files["backend/uv.lock"],
        )

    if not files:
        raise DependencyProvisionError("candidate declares no supported dependency manifests")
    hashes = {name: hashlib.sha256(payload).hexdigest() for name, payload in files.items()}
    return ManifestBundle(
        task_id=task_id,
        workspace=workspace,
        files=files,
        hashes=hashes,
        digest=_manifest_digest(hashes),
        has_node=has_node,
        has_python=has_python,
        project_root=project_root,
        worker_uid=worker_uid,
    )


def _write_file_exclusive(path: Path, payload: bytes, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        # The root request unit deliberately runs with UMask=0077.  Apply the
        # caller's reviewed mode to the already-open inode so the registry
        # hosts/NSS projections requested as 0644 remain readable by the
        # transient DynamicUser package builder.  Relying on the mode passed
        # to open(2) silently produced 0600 files in production and made every
        # registry lookup fail with EAI_AGAIN.
        os.fchmod(fd, mode)
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_bundle_input(bundle: ManifestBundle, input_root: Path) -> None:
    input_root.mkdir(mode=0o700, parents=False)
    for relative, payload in sorted(bundle.files.items()):
        destination = input_root / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_file_exclusive(destination, payload)
        os.chmod(destination, 0o444)
    for current, directories, _files in os.walk(input_root, topdown=False):
        for name in directories:
            os.chmod(Path(current) / name, 0o755)
        fd = os.open(current, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    os.chmod(input_root, 0o755)


def _trusted_executable(path: Path, *, expected_sha256: str | None = None) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
            or not os.access(resolved, os.X_OK)
        ):
            raise DependencyProvisionError(f"trusted executable is unsafe: {path}")
        current = resolved.parent
        while current != current.parent:
            parent_info = current.lstat()
            if (
                stat.S_ISLNK(parent_info.st_mode)
                or parent_info.st_uid != 0
                or stat.S_IMODE(parent_info.st_mode) & 0o022
            ):
                raise DependencyProvisionError(f"trusted executable parent is unsafe: {current}")
            current = current.parent
        if expected_sha256:
            if not SHA256_RE.fullmatch(expected_sha256):
                raise DependencyProvisionError("trusted executable digest pin is missing")
            digest = hashlib.sha256()
            with resolved.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                raise DependencyProvisionError(f"trusted executable digest differs: {path}")
        return resolved
    except DependencyProvisionError:
        raise
    except (OSError, RuntimeError) as exc:
        raise DependencyProvisionError(f"trusted executable is unavailable: {path}") from exc


def _trusted_npm_runtime() -> tuple[Path, Path]:
    node = _trusted_executable(NODE_PATH, expected_sha256=NODE_SHA256)
    cli = _trusted_executable(NPM_CLI_PATH, expected_sha256=NPM_CLI_SHA256)
    try:
        root = NPM_ROOT.resolve(strict=True)
        info = root.lstat()
        if (
            root != NPM_ROOT
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
            or not cli.is_relative_to(root)
        ):
            raise DependencyProvisionError("trusted npm module root is unsafe")
        for path in root.rglob("*"):
            child = path.lstat()
            if (
                stat.S_ISLNK(child.st_mode)
                or child.st_uid != 0
                or stat.S_IMODE(child.st_mode) & 0o022
                or (
                    stat.S_ISREG(child.st_mode)
                    and child.st_nlink != 1
                )
                or not (stat.S_ISREG(child.st_mode) or stat.S_ISDIR(child.st_mode))
            ):
                raise DependencyProvisionError("trusted npm module tree is unsafe")
        digest, _entries, _bytes = _tree_digest(root)
        if digest != NPM_TREE_SHA256:
            raise DependencyProvisionError("trusted npm module tree differs from its reviewed pin")
        return node, cli
    except DependencyProvisionError:
        raise
    except (OSError, RuntimeError) as exc:
        raise DependencyProvisionError("trusted npm runtime is unavailable") from exc


def _resolve_registry_addresses() -> dict[str, tuple[str, ...]]:
    resolved: dict[str, tuple[str, ...]] = {}
    for hostname in sorted(ALLOWED_REGISTRY_HOSTS):
        addresses: set[str] = set()
        try:
            records = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise DependencyProvisionError("registry DNS resolution failed") from exc
        for _family, _type, _protocol, _canonname, sockaddr in records:
            address = ipaddress.ip_address(sockaddr[0])
            if not address.is_global:
                raise DependencyProvisionError("registry DNS returned a non-public address")
            addresses.add(str(address))
        if not addresses:
            raise DependencyProvisionError("registry allowlist resolved to no public addresses")
        resolved[hostname] = tuple(sorted(addresses))
    return resolved


def _write_registry_hosts(
    input_root: Path,
    resolution: Mapping[str, tuple[str, ...]],
) -> tuple[Path, Path]:
    """Create root-owned hosts/NSS files so the builder needs no DNS egress."""
    if set(resolution) != set(ALLOWED_REGISTRY_HOSTS):
        raise DependencyProvisionError("registry host resolution is incomplete")
    network = input_root / ".network"
    network.mkdir(mode=0o700)
    lines = ["127.0.0.1 localhost", "::1 localhost"]
    for hostname in sorted(resolution):
        if not resolution[hostname]:
            raise DependencyProvisionError("registry host has no pinned address")
        for raw_address in resolution[hostname]:
            address = ipaddress.ip_address(raw_address)
            if not address.is_global:
                raise DependencyProvisionError("registry hosts map contains a non-public address")
            lines.append(f"{address} {hostname}")
    hosts = network / "hosts"
    nsswitch = network / "nsswitch.conf"
    _write_file_exclusive(hosts, ("\n".join(lines) + "\n").encode("ascii"), 0o644)
    _write_file_exclusive(nsswitch, b"hosts: files\n", 0o644)
    os.chmod(network, 0o755)
    return hosts, nsswitch


def build_dynamic_worker_argv(
    *,
    bundle: ManifestBundle,
    input_root: Path,
    state_name: str,
    addresses: tuple[str, ...],
) -> list[str]:
    """Return the exact root transient-unit argv for untrusted package code."""
    if not re.fullmatch(r"hermes-dependency-build-[0-9a-f]{16}", state_name):
        raise DependencyProvisionError("builder state name is invalid")
    systemd_run = _trusted_executable(SYSTEMD_RUN)
    node, npm_cli = _trusted_npm_runtime()
    uv = _trusted_executable(UV_PATH, expected_sha256=UV_SHA256)
    python = _trusted_executable(BUILDER_PYTHON)
    entrypoint = _trusted_executable(BUILDER_ENTRYPOINT)
    output = "/output/snapshot"
    command = [
        str(python), "-I", "-B", str(entrypoint),
        "build-stage", "--input", "/input", "--output", output,
        "--node", str(node), "--npm-cli", str(npm_cli), "--uv", str(uv),
        "--python", str(python),
        "--notify-hold",
    ]
    properties = [
        "Type=notify",
        "NotifyAccess=main",
        "Slice=hermes-dependency-provision.slice",
        "DynamicUser=yes",
        "UMask=0022",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "PrivateDevices=yes",
        "PrivateTmp=yes",
        "PrivateUsers=yes",
        # PrivatePIDs=yes is unavailable on the production container host
        # (the kernel rejects systemd's user-namespace setup).  A unique
        # DynamicUser plus hidepid=2 semantics still prevents process reads,
        # ptrace, and signalling across UIDs; the real canary asserts this.
        "ProtectProc=invisible",
        "ProcSubset=pid",
        "ProtectSystem=strict",
        "ProtectHome=tmpfs",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "RestrictSUIDSGID=yes",
        "RestrictNamespaces=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "IPAddressDeny=any",
        "SystemCallArchitectures=native",
        "TasksMax=512",
        "MemoryMax=20G",
        f"LimitFSIZE={MAX_TREE_FILE_BYTES}",
        f"RuntimeMaxSec={BUILD_TIMEOUT_SECONDS}",
        f"TimeoutStartSec={BUILD_TIMEOUT_SECONDS}",
        # Making /run wholly inaccessible prevents systemd from preparing a
        # DynamicUser StateDirectory on this host. An empty private tmpfs
        # gives package code the same no-host-socket view without that setup
        # conflict.
        "TemporaryFileSystem=/run:ro,nodev,nosuid,noexec /etc:ro,nodev,nosuid,noexec",
        f"TemporaryFileSystem=/output:rw,nodev,nosuid,size={MAX_BUILDER_TMPFS_BYTES},nr_inodes={MAX_TREE_ENTRIES},mode=1777",
        "InaccessiblePaths=-/home -/root -/sys/fs/cgroup "
        "-/var/cache -/var/spool -/var/backups -/var/mail "
        "-/var/lib/hermes-delivery-control -/var/lib/ucf -/var/lib/postgresql "
        "-/var/lib/docker -/var/lib/containers -/var/lib/tailscale "
        "-/var/lib/cloud -/var/lib/snapd",
        f"BindReadOnlyPaths={input_root}:/input",
        # /run is otherwise an empty tmpfs. This one systemd datagram socket
        # is required for Type=notify; NotifyAccess=main authenticates the PID,
        # so dependency subprocesses cannot signal readiness or other units.
        "BindReadOnlyPaths=/run/systemd/notify:/run/systemd/notify",
        "BindReadOnlyPaths=-/etc/ssl/certs",
        "BindReadOnlyPaths=-/etc/ssl/openssl.cnf",
        f"BindReadOnlyPaths={input_root / '.network' / 'hosts'}:/etc/hosts",
        f"BindReadOnlyPaths={input_root / '.network' / 'nsswitch.conf'}:/etc/nsswitch.conf",
    ]
    properties.extend(f"IPAddressAllow={address}" for address in addresses)
    argv = [
        str(systemd_run), "--system", "--collect", "--quiet",
        f"--unit={state_name}",
    ]
    for value in properties:
        argv.extend(["-p", value])
    argv.extend(command)
    return argv


def _clean_builder_env(output: Path) -> dict[str, str]:
    home = output / ".home"
    cache = output / ".cache"
    home.mkdir(parents=True, mode=0o700)
    cache.mkdir(parents=True, mode=0o700)
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "NPM_CONFIG_CACHE": str(cache / "npm"),
        "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/",
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "UV_CACHE_DIR": str(cache / "uv"),
        "UV_INDEX_URL": "https://pypi.org/simple",
        "UV_NO_CONFIG": "1",
        "UV_NO_ENV_FILE": "1",
        "UV_PROJECT_ENVIRONMENT": str(output / "backend" / ".venv"),
        "PYTHONNOUSERSITE": "1",
        "PIP_CONFIG_FILE": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
    }


_PYTHON_RUNTIME_PROBE = r"""
import importlib.util
import json
import sqlite3
import sys

connection = sqlite3.connect(":memory:")
connection.execute("CREATE VIRTUAL TABLE hermes_fts5_probe USING fts5(value)")
connection.execute("INSERT INTO hermes_fts5_probe(value) VALUES ('hermes')")
fts5 = connection.execute(
    "SELECT count(*) FROM hermes_fts5_probe WHERE hermes_fts5_probe MATCH 'hermes'"
).fetchone()[0] == 1
source_id = connection.execute("SELECT sqlite_source_id()").fetchone()[0]
print(json.dumps({
    "base_prefix": sys.base_prefix,
    "dont_write_bytecode": sys.dont_write_bytecode,
    "executable": sys.executable,
    "fts5": fts5,
    "prefix": sys.prefix,
    "python_version": ".".join(str(value) for value in sys.version_info[:3]),
    "sqlite_origin": importlib.util.find_spec("_sqlite3").origin,
    "sqlite_source_id": source_id,
    "sqlite_version": sqlite3.sqlite_version,
}, sort_keys=True, separators=(",", ":")))
"""


def _expected_python_runtime_binding() -> dict[str, Any]:
    """Return the relocation-safe runtime facts bound into every Python snapshot."""
    return {
        "base_executable": str(TRUSTED_PYTHON_BASE_EXECUTABLE),
        "base_prefix": str(TRUSTED_PYTHON_BASE_PREFIX),
        "dont_write_bytecode": True,
        "fts5": True,
        "prefix": "backend/.venv",
        "python_version": TRUSTED_PYTHON_VERSION,
        "sqlite_origin": "built-in",
        "sqlite_source_id": TRUSTED_SQLITE_SOURCE_ID,
        "sqlite_version": TRUSTED_SQLITE_VERSION,
    }


def _validate_python_runtime_payload(
    payload: Any,
    *,
    expected_executable: Path,
    expected_prefix: Path,
) -> dict[str, Any]:
    """Validate one real interpreter probe and normalize its movable prefix."""
    expected = _expected_python_runtime_binding()
    if not isinstance(payload, dict):
        raise DependencyProvisionError("Python runtime probe returned invalid metadata")
    try:
        actual_base = Path(str(payload.get("base_prefix") or ""))
        actual_executable = Path(str(payload.get("executable") or ""))
        actual_prefix = Path(str(payload.get("prefix") or ""))
        base_matches = (
            actual_base == TRUSTED_PYTHON_BASE_PREFIX
            and actual_base.resolve(strict=True)
            == TRUSTED_PYTHON_BASE_PREFIX.resolve(strict=True)
        )
        executable_matches = (
            actual_executable == expected_executable
            and actual_executable.resolve(strict=True)
            == expected_executable.resolve(strict=True)
        )
        prefix_matches = (
            actual_prefix == expected_prefix
            and actual_prefix.resolve(strict=True)
            == expected_prefix.resolve(strict=True)
        )
    except (OSError, RuntimeError) as exc:
        raise DependencyProvisionError(
            "Python runtime probe prefix is unavailable"
        ) from exc
    if (
        payload.get("python_version") != expected["python_version"]
        or payload.get("sqlite_version") != expected["sqlite_version"]
        or payload.get("sqlite_source_id") != expected["sqlite_source_id"]
        or payload.get("sqlite_origin") != expected["sqlite_origin"]
        or payload.get("dont_write_bytecode") is not True
        or payload.get("fts5") is not True
        or not base_matches
        or not executable_matches
        or not prefix_matches
    ):
        raise DependencyProvisionError(
            "Python runtime does not match the reviewed interpreter, "
            "no-bytecode, and SQLite policy"
        )
    return expected


def _probe_python_runtime(
    python: Path,
    *,
    expected_prefix: Path,
    no_site: bool = False,
) -> dict[str, Any]:
    """Probe the selected interpreter without writing into its immutable base."""
    clean_env = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/nonexistent-hermes-dependency-builder",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    try:
        command = [str(python), "-I"]
        if no_site:
            command.append("-S")
        command.append("-B")
        command.extend(["-c", _PYTHON_RUNTIME_PROBE])
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            env=clean_env,
            cwd="/",
        )
        if (
            result.returncode != 0
            or not result.stdout
            or len(result.stdout) > MAX_REQUEST_BYTES
        ):
            raise DependencyProvisionError("Python runtime probe failed")
        payload = json.loads(result.stdout.decode("ascii", errors="strict"))
    except DependencyProvisionError:
        raise
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError) as exc:
        raise DependencyProvisionError("Python runtime probe failed") from exc
    return _validate_python_runtime_payload(
        payload,
        expected_executable=python,
        expected_prefix=expected_prefix,
    )


def _run_builder_boundary_canary(host_pid: int, output: Path) -> None:
    """Real-only probe that proves the DynamicUser cannot inspect/signal host PID."""
    if host_pid <= 1 or host_pid == os.getpid() or os.geteuid() == 0:
        raise DependencyProvisionError("builder boundary canary identity is invalid")
    for relative in ("environ", "cmdline", "status"):
        try:
            (Path("/proc") / str(host_pid) / relative).read_bytes()
        except (FileNotFoundError, PermissionError):
            continue
        raise DependencyProvisionError("builder can inspect a host process")
    try:
        os.kill(host_pid, 0)
    except (PermissionError, ProcessLookupError):
        pass
    else:
        raise DependencyProvisionError("builder can signal a host process")
    for forbidden_socket in (
        "/run/tailscale/tailscaled.sock",
        "/run/postgresql/.s.PGSQL.5432",
    ):
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError:
            # AF_UNIX is not in RestrictAddressFamilies; even constructing a
            # socket can fail, which is the strongest possible denial.
            continue
        try:
            try:
                probe.connect(forbidden_socket)
            except OSError:
                continue
            raise DependencyProvisionError(
                f"builder can connect to a forbidden host socket: {forbidden_socket}"
            )
        finally:
            probe.close()
    for forbidden_file in (
        Path("/var/lib/ucf/cache/:etc:ssh:sshd_config"),
        Path("/etc/ssh/sshd_config"),
    ):
        try:
            forbidden_file.read_bytes()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        raise DependencyProvisionError(
            f"builder can read a forbidden host file: {forbidden_file}"
        )
    try:
        next(Path("/var/lib/hermes-delivery-control").iterdir())
    except (FileNotFoundError, PermissionError, StopIteration, OSError):
        pass
    else:
        raise DependencyProvisionError("builder can enumerate delivery control state")
    (output / "boundary-canary.json").write_text(
        json.dumps({"host_pid_hidden": True, "uid": os.geteuid()}, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_stage(
    input_root: Path,
    output: Path,
    *,
    node: Path,
    npm_cli: Path,
    uv: Path,
    python: Path,
    probe_host_pid: int | None = None,
    probe_background_child: bool = False,
) -> None:
    """Run inside DynamicUser=; this is the only package-code execution stage."""
    input_root = input_root.resolve(strict=True)
    python = Path(python)
    try:
        if (
            not python.is_absolute()
            or Path(sys.executable) != python
            or Path(sys.executable).resolve(strict=True) != python.resolve(strict=True)
        ):
            raise DependencyProvisionError(
                "builder Python is not the exact executing interpreter"
            )
    except (OSError, RuntimeError) as exc:
        raise DependencyProvisionError("builder Python is unavailable") from exc
    if output.exists():
        info = output.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or any(output.iterdir())
        ):
            raise DependencyProvisionError("builder output mount is unsafe")
    else:
        output.mkdir(parents=True, mode=0o700)
    env = _clean_builder_env(output)
    _probe_python_runtime(python, expected_prefix=python.parent.parent)
    if probe_host_pid is not None:
        _run_builder_boundary_canary(probe_host_pid, output)
    if (input_root / "package.json").is_file():
        node_root = output / "node"
        node_root.mkdir(mode=0o700)
        package = json.loads((input_root / "package.json").read_text(encoding="utf-8"))
        # Candidate lifecycle scripts are unnecessary for resolving a locked
        # dependency graph. Dependency package install hooks may still run,
        # but only inside this credential-free DynamicUser unit.
        package.pop("scripts", None)
        (node_root / "package.json").write_text(
            json.dumps(package, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        lock_name = next(name for name in _NODE_LOCKFILES if (input_root / name).is_file())
        shutil.copyfile(input_root / lock_name, node_root / lock_name)
        proc = subprocess.run(
            [str(node), str(npm_cli), "ci", "--no-audit", "--no-fund"],
            cwd=node_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=BUILD_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            diagnostic = proc.stderr.decode("utf-8", errors="replace")[-2000:]
            compact = " ".join(diagnostic.split())[-1000:]
            raise DependencyProvisionError(
                f"npm dependency build failed ({compact or 'no diagnostic'})"
            )
        if not (node_root / "node_modules").is_dir():
            (node_root / "node_modules").mkdir(mode=0o700)
        os.replace(node_root / "node_modules", output / "node_modules")
    if (input_root / "backend" / "pyproject.toml").is_file():
        backend = output / "backend"
        backend.mkdir(mode=0o700)
        proc = subprocess.run(
            [
                str(uv), "sync", "--project", str(input_root / "backend"),
                "--frozen", "--extra", "dev", "--python", str(python),
                "--no-managed-python", "--no-python-downloads",
                "--no-install-project",
                "--no-install-workspace",
                "--no-install-local", "--no-editable", "--link-mode", "copy",
            ],
            cwd=backend,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=BUILD_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0 or not (backend / ".venv" / "bin" / "python").exists():
            diagnostic = proc.stderr.decode("utf-8", errors="replace")[-2000:]
            compact = " ".join(diagnostic.split())[-1000:]
            raise DependencyProvisionError(
                f"uv dependency build failed ({compact or 'uv returned no environment'})"
            )
        runtime = _probe_python_runtime(
            backend / ".venv" / "bin" / "python",
            expected_prefix=backend / ".venv",
        )
        _write_file_exclusive(
            output / PYTHON_RUNTIME_METADATA,
            _canonical_json(runtime) + b"\n",
            0o400,
        )
    if probe_background_child:
        # Real canary only: model a hostile install hook that continuously
        # hands output mutation to a freshly forked descendant.  PID polling
        # cannot make this stable; the trusted broker must freeze the entire
        # transient-unit cgroup before it observes or promotes these bytes.
        churn_marker = output / "fork-churn-mutation"
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,pathlib,time;"
                f"p=pathlib.Path({str(churn_marker)!r});"
                "i=0;"
                "\nwhile True:"
                "\n p.write_text(str(i));i+=1"
                "\n child=os.fork()"
                "\n if child: os._exit(0)"
                "\n time.sleep(.005)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        for _attempt in range(200):
            if churn_marker.is_file():
                break
            time.sleep(0.01)
        else:
            raise DependencyProvisionError(
                "background package-process canary did not start"
            )


def _notify_ready_and_hold() -> None:
    """Tell systemd the trusted main stage finished, then preserve its tmpfs."""
    address = os.environ.pop("NOTIFY_SOCKET", "")
    if not address:
        raise DependencyProvisionError("builder readiness socket is unavailable")
    if address.startswith("@"):  # systemd's abstract AF_UNIX notation
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.connect(address)
            notifier.sendall(b"READY=1\nSTATUS=dependency snapshot ready for validation")
    except OSError as exc:
        raise DependencyProvisionError("builder readiness notification failed") from exc
    # NotifyAccess=main makes child/package processes unable to forge READY.
    # The trusted root broker freezes this entire unit cgroup before reading
    # /output, so even fork/exit handoff cannot race validation or promotion.
    # Stopping the unit afterwards automatically thaws it and atomically
    # destroys the size-bounded private /output tmpfs.
    while True:
        signal.pause()


def _read_bounded_regular_nofollow(path: Path, *, maximum_bytes: int) -> bytes:
    """Read one stable builder file without following its final component."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise DependencyProvisionError("builder metadata is unsafe")
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size != before.st_size
            ):
                raise DependencyProvisionError("builder metadata changed during open")
            payload = os.read(descriptor, before.st_size + 1)
            after = os.fstat(descriptor)
            if len(payload) != before.st_size or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ):
                raise DependencyProvisionError("builder metadata changed during read")
            return payload
        finally:
            os.close(descriptor)
    except DependencyProvisionError:
        raise
    except OSError as exc:
        raise DependencyProvisionError("builder metadata is unavailable") from exc


def _read_python_runtime_metadata(build_output: Path) -> tuple[dict[str, Any], str]:
    raw = _read_bounded_regular_nofollow(
        build_output / PYTHON_RUNTIME_METADATA,
        maximum_bytes=MAX_REQUEST_BYTES,
    )
    try:
        payload = json.loads(raw.decode("ascii", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DependencyProvisionError("Python runtime metadata is invalid") from exc
    expected = _expected_python_runtime_binding()
    canonical = _canonical_json(expected) + b"\n"
    if payload != expected or raw != canonical:
        raise DependencyProvisionError(
            "Python runtime metadata does not match the reviewed policy"
        )
    return expected, hashlib.sha256(canonical).hexdigest()


def _validate_python_venv_layout(root: Path, *, owner_uid: int) -> None:
    """Statically bind the movable venv to the exact immutable base runtime."""
    try:
        root_info = root.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise DependencyProvisionError("Python environment is not a real directory")
        raw = _read_bounded_regular_nofollow(
            root / "pyvenv.cfg",
            maximum_bytes=MAX_PYTHON_METADATA_BYTES,
        )
        text = raw.decode("utf-8", errors="strict")
        fields: dict[str, str] = {}
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            key = key.strip().casefold()
            if not separator or not key or key in fields:
                raise DependencyProvisionError("Python environment metadata is invalid")
            fields[key] = value.strip()
        home = Path(fields.get("home", ""))
        version = fields.get("version_info", fields.get("version", ""))
        major_minor = ".".join(TRUSTED_PYTHON_VERSION.split(".")[:2])
        if (
            fields.get("include-system-site-packages", "").casefold() != "false"
            or not home.is_absolute()
            or home.resolve(strict=True)
            != (TRUSTED_PYTHON_BASE_PREFIX / "bin").resolve(strict=True)
            or not (version == TRUSTED_PYTHON_VERSION or version == major_minor)
        ):
            raise DependencyProvisionError(
                "Python environment does not use the reviewed immutable base"
            )
        base_info = TRUSTED_PYTHON_BASE_EXECUTABLE.lstat()
        if (
            stat.S_ISLNK(base_info.st_mode)
            or not stat.S_ISREG(base_info.st_mode)
            or base_info.st_uid != owner_uid
            or stat.S_IMODE(base_info.st_mode) & 0o022
            or TRUSTED_PYTHON_BASE_EXECUTABLE.resolve(strict=True)
            != TRUSTED_PYTHON_BASE_EXECUTABLE
        ):
            raise DependencyProvisionError("reviewed Python base executable is unsafe")
        expected_executable = TRUSTED_PYTHON_BASE_EXECUTABLE.resolve(strict=True)
        for name in ("python", "python3", f"python{major_minor}"):
            executable = root / "bin" / name
            executable_info = executable.lstat()
            if (
                not stat.S_ISLNK(executable_info.st_mode)
                or executable.resolve(strict=True) != expected_executable
            ):
                raise DependencyProvisionError(
                    "Python environment interpreter does not use the reviewed base"
                )
    except DependencyProvisionError:
        raise
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise DependencyProvisionError("Python environment metadata is invalid") from exc


def _allowed_symlink(
    path: Path,
    root: Path,
    *,
    python_tree: bool,
    owner_uid: int,
) -> bool:
    target_text = os.readlink(path)
    target = Path(target_text)
    try:
        resolved = (path.parent / target).resolve(strict=True)
        resolved.relative_to(root)
        if target.is_absolute():
            return False
        relative_parent = path.parent.relative_to(root)
        lexical_target = Path(os.path.normpath(str(relative_parent / target)))
        return not (
            lexical_target.is_absolute()
            or lexical_target == Path("..")
            or lexical_target.parts[:1] == ("..",)
        )
    except ValueError:
        if not python_tree:
            return False
        try:
            relative = path.relative_to(root)
            info = resolved.stat()
        except (OSError, RuntimeError, ValueError):
            return False
        return (
            relative.parent == Path("bin")
            and relative.name.startswith("python")
            and resolved == TRUSTED_PYTHON_BASE_EXECUTABLE.resolve(strict=True)
            and stat.S_ISREG(info.st_mode)
            and info.st_uid == owner_uid
            and not stat.S_IMODE(info.st_mode) & 0o022
        )
    except (OSError, RuntimeError):
        return False


def _python_import_root_layout_is_safe(root: Path) -> bool:
    """Require real Python import-root directories and one exact lib64 alias."""
    major_minor = ".".join(TRUSTED_PYTHON_VERSION.split(".")[:2])
    try:
        library = root / "lib"
        if os.path.lexists(library):
            if not stat.S_ISDIR(library.lstat().st_mode):
                return False
            version = library / f"python{major_minor}"
            if os.path.lexists(version):
                if not stat.S_ISDIR(version.lstat().st_mode):
                    return False
                site_packages = version / "site-packages"
                if os.path.lexists(site_packages) and not stat.S_ISDIR(
                    site_packages.lstat().st_mode
                ):
                    return False
        lib64 = root / "lib64"
        if os.path.lexists(lib64):
            return stat.S_ISLNK(lib64.lstat().st_mode) and os.readlink(lib64) == "lib"
        return True
    except OSError:
        return False


def _is_python_startup_customization(path: Path, root: Path) -> bool:
    """Return whether an entry is a top-level site startup module."""
    major_minor = ".".join(TRUSTED_PYTHON_VERSION.split(".")[:2])
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if len(relative.parts) != 4:
        return False
    library, version, packages, name = relative.parts
    module = name.casefold().partition(".")[0]
    return (
        library in {"lib", "lib64"}
        and version == f"python{major_minor}"
        and packages == "site-packages"
        and module in {"sitecustomize", "usercustomize"}
    )


def _validate_python_metadata(path: Path, root: Path) -> None:
    if _is_python_startup_customization(path, root):
        raise DependencyProvisionError(
            "Python snapshot contains startup customization code"
        )
    if path.suffix == ".egg-link" or path.name.startswith("__editable__"):
        raise DependencyProvisionError("Python snapshot contains editable metadata")
    if path.suffix == ".pth":
        try:
            if path.stat().st_size > MAX_PYTHON_METADATA_BYTES:
                raise DependencyProvisionError(
                    "Python .pth metadata exceeds the byte limit"
                )
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise DependencyProvisionError("Python .pth metadata is invalid") from exc
        for raw_line in text.splitlines():
            # Match CPython site.py: leading whitespace is significant and
            # makes this a path line, while trailing whitespace is discarded.
            line = raw_line.rstrip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(("import ", "import\t")):
                # Executable statements are content-attested dependency code.
                # Path additions are forbidden because they can turn a nested
                # sitecustomize module into a top-level startup hook.
                continue
            raise DependencyProvisionError("Python .pth metadata contains a path addition")
    if path.name == "direct_url.json":
        try:
            if path.stat().st_size > MAX_PYTHON_METADATA_BYTES:
                raise DependencyProvisionError(
                    "Python direct_url metadata exceeds the byte limit"
                )
            data = json.loads(path.read_text(encoding="utf-8"))
        except DependencyProvisionError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DependencyProvisionError("Python direct_url metadata is invalid") from exc
        if not isinstance(data, dict):
            raise DependencyProvisionError("Python direct_url metadata has an invalid shape")
        url = str(data.get("url") or "")
        directory_info = data.get("dir_info")
        if url.startswith("file:") or (
            isinstance(directory_info, dict) and directory_info.get("editable") is True
        ):
            raise DependencyProvisionError("Python snapshot contains a local/editable install")


def _relocatable_script(payload: bytes, *, source_root: Path) -> bytes:
    first, separator, rest = payload.partition(b"\n")
    if not separator or not first.startswith(b"#!"):
        return payload
    root_bytes = str(source_root).encode("utf-8")
    if root_bytes not in first:
        if first.startswith((b"#!/usr/bin/", b"#!/bin/", b"#!/usr/bin/env ")):
            return payload
        raise DependencyProvisionError("Python console script has an untrusted interpreter")
    return (
        b"#!/bin/sh\n"
        b"'''exec' \"$(dirname -- \"$0\")/python\" \"$0\" \"$@\"\n"
        b"' '''\n" + rest
    )


def _source_file_identity(info: os.stat_result) -> tuple[int, ...]:
    """Return stable inode metadata used across frozen-tree scan and copy."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _confined_source_hardlinks(
    source: Path,
    *,
    allow_confined: bool,
) -> dict[tuple[int, int], tuple[int, ...]]:
    """Prove every admitted source hardlink name is inside one frozen tree."""
    identities: dict[tuple[int, int], tuple[int, ...]] = {}
    observations: dict[tuple[int, int], int] = {}
    entries = 0
    stack = [source]
    while stack:
        current = stack.pop()
        with os.scandir(current) as children:
            for entry in children:
                info = entry.stat(follow_symlinks=False)
                entries += 1
                if entries > MAX_TREE_ENTRIES:
                    raise DependencyProvisionError("dependency tree exceeds the entry limit")
                if stat.S_ISDIR(info.st_mode):
                    stack.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink == 1:
                    continue
                if not allow_confined:
                    raise DependencyProvisionError(
                        "dependency tree contains a hard-linked file"
                    )
                key = (info.st_dev, info.st_ino)
                identity = _source_file_identity(info)
                previous = identities.setdefault(key, identity)
                if previous != identity:
                    raise DependencyProvisionError(
                        "dependency hardlink metadata changed during scan"
                    )
                observations[key] = observations.get(key, 0) + 1
    for key, identity in identities.items():
        if observations.get(key, 0) != identity[5]:
            raise DependencyProvisionError(
                "dependency hardlink escapes the frozen builder tree"
            )
    return identities


def _copy_tree_no_follow(
    source: Path,
    destination: Path,
    *,
    python_tree: bool,
    owner_uid: int = 0,
) -> None:
    source_info = source.lstat()
    if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISDIR(source_info.st_mode):
        raise DependencyProvisionError("builder output tree is not a real directory")
    source_parts = source.parts
    proc_identity = source_parts[2] if len(source_parts) >= 3 else ""
    proc_suffix = source_parts[4:] if len(source_parts) >= 5 else ()
    frozen_proc_source = (
        len(source_parts) >= 5
        and source_parts[:2] == ("/", "proc")
        and re.fullmatch(r"(?:[1-9][0-9]*|self)", proc_identity) is not None
        and source_parts[3] == "root"
        and ".." not in proc_suffix
        and (
            proc_identity == "self"
            or proc_suffix
            in {
                ("output", "snapshot", "node_modules"),
                ("output", "snapshot", "backend", ".venv"),
            }
        )
    )
    if frozen_proc_source:
        namespace_source = Path("/").joinpath(*proc_suffix)
    elif Path("/proc") in source.parents or source == Path("/proc"):
        raise DependencyProvisionError("builder output proc-root path is invalid")
    else:
        namespace_source = source
    confined_hardlinks = _confined_source_hardlinks(
        source,
        allow_confined=frozen_proc_source,
    )
    destination.mkdir(mode=0o755)
    entries = 0
    byte_count = 0
    copied_symlinks: list[Path] = []
    copied_python_metadata: list[Path] = []
    copied_python_entries: list[Path] = []
    stack: list[tuple[Path, Path]] = [(source, destination)]
    while stack:
        src_dir, dst_dir = stack.pop()
        with os.scandir(src_dir) as children:
            for entry in sorted(children, key=lambda item: item.name, reverse=True):
                src = Path(entry.path)
                dst = dst_dir / entry.name
                info = entry.stat(follow_symlinks=False)
                entries += 1
                if entries > MAX_TREE_ENTRIES:
                    raise DependencyProvisionError("dependency tree exceeds the entry limit")
                if python_tree:
                    copied_python_entries.append(dst)
                if stat.S_ISLNK(info.st_mode):
                    os.symlink(os.readlink(src), dst)
                    os.lchown(dst, owner_uid, 0 if owner_uid == 0 else os.getgid())
                    copied_symlinks.append(dst)
                    if python_tree:
                        copied_python_metadata.append(dst)
                elif stat.S_ISDIR(info.st_mode):
                    dst.mkdir(mode=0o755)
                    stack.append((src, dst))
                elif stat.S_ISREG(info.st_mode):
                    current_identity = _source_file_identity(info)
                    expected_hardlink = confined_hardlinks.get(
                        (info.st_dev, info.st_ino)
                    )
                    if (
                        (info.st_nlink != 1 or expected_hardlink is not None)
                        and expected_hardlink != current_identity
                    ):
                        raise DependencyProvisionError(
                            "dependency hardlink metadata changed before copy"
                        )
                    if info.st_size < 0 or info.st_size > MAX_TREE_FILE_BYTES:
                        raise DependencyProvisionError(
                            "dependency file exceeds the per-file byte limit"
                        )
                    byte_count += info.st_size
                    if byte_count > MAX_TREE_BYTES:
                        raise DependencyProvisionError("dependency tree exceeds the byte limit")
                    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    src_fd = os.open(src, flags)
                    try:
                        opened = os.fstat(src_fd)
                        if _source_file_identity(opened) != current_identity:
                            raise DependencyProvisionError("dependency file changed during copy")
                        python_bin_executable = bool(
                            python_tree
                            and src.parent == source / "bin"
                            and info.st_mode & 0o111
                        )
                        executable_script = False
                        if python_bin_executable:
                            magic = os.read(src_fd, 4)
                            os.lseek(src_fd, 0, os.SEEK_SET)
                            if magic.startswith(b"#!"):
                                executable_script = True
                            elif magic != b"\x7fELF":
                                raise DependencyProvisionError(
                                    "Python bin executable has an untrusted format"
                                )
                        mode = 0o755 if info.st_mode & 0o111 else 0o644
                        if executable_script:
                            if info.st_size > MAX_RELOCATABLE_SCRIPT_BYTES:
                                raise DependencyProvisionError(
                                    "Python console script exceeds the byte limit"
                                )
                            payload = bytearray()
                            while len(payload) <= MAX_RELOCATABLE_SCRIPT_BYTES:
                                chunk = os.read(src_fd, 1024 * 1024)
                                if not chunk:
                                    break
                                payload.extend(chunk)
                            if len(payload) != info.st_size:
                                raise DependencyProvisionError(
                                    "Python console script changed during copy"
                                )
                            rewritten = _relocatable_script(
                                bytes(payload), source_root=namespace_source,
                            )
                            _write_file_exclusive(dst, rewritten, mode)
                        else:
                            dst_flags = (
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                | getattr(os, "O_CLOEXEC", 0)
                            )
                            if hasattr(os, "O_NOFOLLOW"):
                                dst_flags |= os.O_NOFOLLOW
                            dst_fd = os.open(dst, dst_flags, mode)
                            copied = 0
                            try:
                                while True:
                                    chunk = os.read(src_fd, 1024 * 1024)
                                    if not chunk:
                                        break
                                    view = memoryview(chunk)
                                    while view:
                                        written = os.write(dst_fd, view)
                                        if written <= 0:
                                            raise OSError("short dependency file write")
                                        view = view[written:]
                                    copied += len(chunk)
                                if copied != info.st_size:
                                    raise DependencyProvisionError(
                                        "dependency file changed during copy"
                                    )
                            except Exception:
                                try:
                                    dst.unlink()
                                except OSError:
                                    pass
                                raise
                            finally:
                                os.close(dst_fd)
                        after = os.fstat(src_fd)
                        if _source_file_identity(after) != current_identity:
                            raise DependencyProvisionError(
                                "dependency file changed during copy"
                            )
                    finally:
                        os.close(src_fd)
                    os.chown(dst, owner_uid, 0 if owner_uid == 0 else os.getgid())
                    os.chmod(dst, mode)
                    if python_tree:
                        copied_python_metadata.append(dst)
                else:
                    raise DependencyProvisionError("dependency tree contains a special file")
        os.chown(dst_dir, owner_uid, 0 if owner_uid == 0 else os.getgid())
        os.chmod(dst_dir, 0o755)
    # The source is visible only through /proc/<pid>/root. Resolving one of
    # its links canonicalizes away that magic prefix and then fails against
    # the host mount namespace. Copying link text follows nothing. Validate
    # the complete root-owned staging tree instead, after every possible
    # in-tree target exists and before any attestation or atomic promotion.
    for copied in copied_symlinks:
        if not _allowed_symlink(
            copied,
            destination,
            python_tree=python_tree,
            owner_uid=owner_uid,
        ):
            raise DependencyProvisionError("dependency tree contains an escaping symlink")
    if python_tree and not _python_import_root_layout_is_safe(destination):
        raise DependencyProvisionError("Python snapshot import-root layout is unsafe")
    for copied in copied_python_entries:
        if _is_python_startup_customization(copied, destination):
            raise DependencyProvisionError(
                "Python snapshot contains startup customization code"
            )
    for copied in copied_python_metadata:
        _validate_python_metadata(copied, destination)


def _tree_digest(root: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    entries = 0
    byte_count = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        info = path.lstat()
        entries += 1
        digest.update(relative + b"\0")
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"link\0" + os.readlink(path).encode("utf-8") + b"\n")
        elif stat.S_ISDIR(info.st_mode):
            digest.update(b"dir\n")
        elif stat.S_ISREG(info.st_mode):
            if info.st_size < 0 or info.st_size > MAX_TREE_FILE_BYTES:
                raise DependencyProvisionError(
                    "promoted file exceeds the per-file byte limit"
                )
            byte_count += info.st_size
            if byte_count > MAX_TREE_BYTES:
                raise DependencyProvisionError(
                    "promoted tree exceeds the byte limit"
                )
            digest.update(b"file\0")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if (
                    opened.st_dev != info.st_dev
                    or opened.st_ino != info.st_ino
                    or opened.st_size != info.st_size
                ):
                    raise DependencyProvisionError(
                        "promoted file changed during hashing"
                    )
                observed = 0
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    observed += len(chunk)
                    digest.update(chunk)
                after = os.fstat(descriptor)
                if observed != info.st_size or (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ) != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                ):
                    raise DependencyProvisionError(
                        "promoted file changed during hashing"
                    )
            finally:
                os.close(descriptor)
            digest.update(b"\n")
        else:
            raise DependencyProvisionError("promoted tree contains a special file")
    return digest.hexdigest(), entries, byte_count


def _fsync_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            if path.is_symlink():
                continue
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(current_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _existing_snapshot_matches(
    snapshot: Path,
    bundle: ManifestBundle,
    *,
    owner_uid: int = 0,
) -> bool:
    try:
        info = snapshot.lstat()
        attestation = snapshot / "attestation.json"
        att_info = attestation.lstat()
        projection_roots = []
        if bundle.has_node:
            projection_roots.append(snapshot / "node_modules")
        if bundle.has_python:
            projection_roots.extend((
                snapshot / "backend",
                snapshot / "backend" / ".venv",
            ))
        projection_info = [path.lstat() for path in projection_roots]
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o755
            or stat.S_ISLNK(att_info.st_mode)
            or not stat.S_ISREG(att_info.st_mode)
            or att_info.st_uid != owner_uid
            or stat.S_IMODE(att_info.st_mode) != 0o644
            or any(
                stat.S_ISLNK(entry.st_mode)
                or not stat.S_ISDIR(entry.st_mode)
                or entry.st_uid != owner_uid
                or stat.S_IMODE(entry.st_mode) != 0o755
                for entry in projection_info
            )
        ):
            return False
        data = json.loads(attestation.read_text(encoding="utf-8"))
        trees = data.get("trees")
        expected_trees = {
            label
            for label, present in (
                ("node_modules", bundle.has_node),
                ("backend/.venv", bundle.has_python),
            )
            if present
        }
        python_tree = (
            trees.get("backend/.venv") if isinstance(trees, dict) else None
        )
        expected_runtime = _expected_python_runtime_binding()
        expected_runtime_sha256 = hashlib.sha256(
            _canonical_json(expected_runtime) + b"\n"
        ).hexdigest()
        return (
            data.get("version") == SNAPSHOT_ATTESTATION_VERSION
            and data.get("build_policy") == DEPENDENCY_BUILD_POLICY
            and data.get("manifest_sha256") == bundle.digest
            and data.get("manifests") == dict(bundle.hashes)
            and isinstance(trees, dict)
            and set(trees) == expected_trees
            and (
                (
                    not bundle.has_python
                    and "python_runtime" not in data
                    and "python_runtime_sha256" not in data
                )
                or (
                    bundle.has_python
                    and isinstance(python_tree, dict)
                    and python_tree.get("install_mode") == PYTHON_INSTALL_MODE
                    and data.get("python_runtime") == expected_runtime
                    and data.get("python_runtime_sha256")
                    == expected_runtime_sha256
                )
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return False


def promote_snapshot(
    bundle: ManifestBundle,
    build_output: Path,
    *,
    snapshot_root: Path = SNAPSHOT_ROOT,
    owner_uid: int = 0,
    reauthorize: Callable[[], None] | None = None,
) -> Path:
    """Validate/copy package bytes and atomically publish; executes no code."""
    if os.geteuid() != owner_uid:
        raise DependencyProvisionError("snapshot promotion requires root")
    snapshot_root.mkdir(parents=True, mode=0o755, exist_ok=True)
    root_info = snapshot_root.lstat()
    if (
        stat.S_ISLNK(root_info.st_mode)
        or not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != owner_uid
        or stat.S_IMODE(root_info.st_mode) & 0o700 != 0o700
        or stat.S_IMODE(root_info.st_mode) & 0o022
    ):
        raise DependencyProvisionError("snapshot root is not root-owned and immutable")
    # systemd's UMask=0077 also masks a newly created snapshot root. After
    # binding a real, owner-controlled directory, publish the fixed traversal
    # mode required by the unprivileged worker and rebind the inode.
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    root_flags |= getattr(os, "O_CLOEXEC", 0)
    root_flags |= getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(snapshot_root, root_flags)
    try:
        opened_root = os.fstat(root_fd)
        if (
            opened_root.st_dev != root_info.st_dev
            or opened_root.st_ino != root_info.st_ino
            or not stat.S_ISDIR(opened_root.st_mode)
            or opened_root.st_uid != owner_uid
        ):
            raise DependencyProvisionError("snapshot root changed during validation")
        os.fchmod(root_fd, 0o755)
        os.fsync(root_fd)
        normalized_root = os.fstat(root_fd)
        rebound_root = snapshot_root.lstat()
        if (
            normalized_root.st_dev != root_info.st_dev
            or normalized_root.st_ino != root_info.st_ino
            or normalized_root.st_uid != owner_uid
            or stat.S_IMODE(normalized_root.st_mode) != 0o755
            or rebound_root.st_dev != root_info.st_dev
            or rebound_root.st_ino != root_info.st_ino
            or stat.S_IMODE(rebound_root.st_mode) != 0o755
        ):
            raise DependencyProvisionError("snapshot root mode could not be normalized")
    finally:
        os.close(root_fd)
    final = snapshot_root / bundle.digest
    if os.path.lexists(final):
        if _existing_snapshot_matches(final, bundle, owner_uid=owner_uid):
            return final
        raise DependencyProvisionError("an invalid snapshot already occupies the manifest digest")
    staging = snapshot_root / f".staging-{bundle.digest}-{uuid.uuid4().hex[:12]}"
    staging.mkdir(mode=0o700)
    try:
        trees: dict[str, dict[str, Any]] = {}
        python_runtime: dict[str, Any] | None = None
        python_runtime_sha256: str | None = None
        total_entries = 0
        total_bytes = 0
        if bundle.has_node:
            _copy_tree_no_follow(
                build_output / "node_modules", staging / "node_modules",
                python_tree=False, owner_uid=owner_uid,
            )
            tree_hash, entries, byte_count = _tree_digest(staging / "node_modules")
            trees["node_modules"] = {
                "sha256": tree_hash, "entries": entries, "bytes": byte_count,
            }
            total_entries += entries
            total_bytes += byte_count
        if bundle.has_python:
            # The post-build venv probe is a required diagnostic handshake, but
            # package-installed .pth/sitecustomize code can influence that
            # subprocess. Never use its claims as the attestation authority.
            _builder_runtime, _builder_runtime_sha256 = _read_python_runtime_metadata(
                build_output
            )
            if owner_uid == 0:
                trusted_base = _trusted_executable(
                    TRUSTED_PYTHON_BASE_EXECUTABLE
                )
                if trusted_base != TRUSTED_PYTHON_BASE_EXECUTABLE:
                    raise DependencyProvisionError(
                        "reviewed Python base executable is indirect"
                    )
            # Probe only the immutable central base with site initialization
            # disabled. Combined with the static pyvenv/symlink validation
            # above, these are root-observed facts about the resulting venv.
            python_runtime = _probe_python_runtime(
                TRUSTED_PYTHON_BASE_EXECUTABLE,
                expected_prefix=TRUSTED_PYTHON_BASE_PREFIX,
                no_site=True,
            )
            python_runtime_sha256 = hashlib.sha256(
                _canonical_json(python_runtime) + b"\n"
            ).hexdigest()
            backend_wrapper = staging / "backend"
            backend_wrapper.mkdir(mode=0o755)
            _copy_tree_no_follow(
                build_output / "backend" / ".venv",
                backend_wrapper / ".venv",
                python_tree=True,
                owner_uid=owner_uid,
            )
            _validate_python_venv_layout(
                backend_wrapper / ".venv",
                owner_uid=owner_uid,
            )
            # The root provisioner runs with UMask=0077, so mkdir(0755) alone
            # produces a 0700 wrapper that the uid-1000 worker cannot traverse.
            # Publish the reviewed cross-UID projection mode on a no-follow,
            # inode-bound descriptor before the staging tree can be renamed.
            wrapper_info = backend_wrapper.lstat()
            wrapper_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            wrapper_flags |= getattr(os, "O_CLOEXEC", 0)
            wrapper_flags |= getattr(os, "O_NOFOLLOW", 0)
            wrapper_fd = os.open(backend_wrapper, wrapper_flags)
            try:
                opened_wrapper = os.fstat(wrapper_fd)
                if (
                    stat.S_ISLNK(wrapper_info.st_mode)
                    or not stat.S_ISDIR(opened_wrapper.st_mode)
                    or opened_wrapper.st_dev != wrapper_info.st_dev
                    or opened_wrapper.st_ino != wrapper_info.st_ino
                ):
                    raise DependencyProvisionError(
                        "backend dependency wrapper changed during validation"
                    )
                os.fchown(
                    wrapper_fd,
                    owner_uid,
                    0 if owner_uid == 0 else os.getgid(),
                )
                os.fchmod(wrapper_fd, 0o755)
                os.fsync(wrapper_fd)
                normalized_wrapper = os.fstat(wrapper_fd)
                rebound_wrapper = backend_wrapper.lstat()
                if (
                    normalized_wrapper.st_uid != owner_uid
                    or stat.S_IMODE(normalized_wrapper.st_mode) != 0o755
                    or rebound_wrapper.st_dev != normalized_wrapper.st_dev
                    or rebound_wrapper.st_ino != normalized_wrapper.st_ino
                    or stat.S_IMODE(rebound_wrapper.st_mode) != 0o755
                ):
                    raise DependencyProvisionError(
                        "backend dependency wrapper mode is unsafe"
                    )
            finally:
                os.close(wrapper_fd)
            tree_hash, entries, byte_count = _tree_digest(backend_wrapper / ".venv")
            trees["backend/.venv"] = {
                "sha256": tree_hash,
                "entries": entries,
                "bytes": byte_count,
                "install_mode": PYTHON_INSTALL_MODE,
            }
            total_entries += entries
            total_bytes += byte_count
        if total_entries > MAX_TREE_ENTRIES or total_bytes > MAX_TREE_BYTES:
            raise DependencyProvisionError(
                "combined dependency snapshot exceeds the storage limit"
            )
        attestation = {
            "version": SNAPSHOT_ATTESTATION_VERSION,
            "build_policy": DEPENDENCY_BUILD_POLICY,
            "manifest_sha256": bundle.digest,
            "manifests": dict(bundle.hashes),
            "trees": trees,
        }
        if python_runtime is not None and python_runtime_sha256 is not None:
            attestation["python_runtime"] = python_runtime
            attestation["python_runtime_sha256"] = python_runtime_sha256
        _write_file_exclusive(
            staging / "attestation.json", _canonical_json(attestation) + b"\n", 0o644,
        )
        os.chown(
            staging / "attestation.json", owner_uid,
            0 if owner_uid == 0 else os.getgid(),
        )
        os.chmod(staging / "attestation.json", 0o644)
        os.chown(staging, owner_uid, 0 if owner_uid == 0 else os.getgid())
        os.chmod(staging, 0o755)
        _fsync_tree(staging)
        # Re-read the candidate after the potentially long build. Promotion
        # is valid only if the task still has the exact bytes we copied.
        current = collect_manifest_bundle(
            bundle.task_id,
            bundle.workspace,
            project_root=bundle.project_root,
            worker_uid=bundle.worker_uid,
        )
        if current.digest != bundle.digest or current.hashes != bundle.hashes:
            raise DependencyProvisionError("candidate manifests changed during provisioning")
        if reauthorize is not None:
            # This is deliberately the last operation before the atomic
            # rename.  A worker whose run/claim/PID/source/contract changed
            # during a long registry build can never publish a snapshot.
            reauthorize()
        os.rename(staging, final)
        root_fd = os.open(snapshot_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        return final
    finally:
        if os.path.lexists(staging):
            shutil.rmtree(staging)


def _builder_unit_status(state_name: str) -> tuple[int, str, str]:
    """Return the exact active builder PID, freezer state, and cgroup."""
    if not re.fullmatch(r"hermes-dependency-build-[0-9a-f]{16}", state_name):
        raise DependencyProvisionError("builder state name is invalid")
    unit_name = f"{state_name}.service"
    systemctl = _trusted_executable(SYSTEMCTL)
    proc = subprocess.run(
        [
            str(systemctl), "show", unit_name, "--no-pager",
            "--property=Id",
            "--property=ActiveState", "--property=SubState",
            "--property=MainPID", "--property=FreezerState",
            "--property=ControlGroup",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
    )
    values = {}
    for line in proc.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    try:
        pid = int(values.get("MainPID", "0"))
    except ValueError as exc:
        raise DependencyProvisionError("isolated builder PID is invalid") from exc
    if (
        proc.returncode != 0
        or values.get("Id") != unit_name
        or values.get("ActiveState") != "active"
        or values.get("SubState") != "running"
        or pid <= 1
    ):
        raise DependencyProvisionError("isolated builder did not remain ready")
    freezer_state = values.get("FreezerState", "")
    control_group = values.get("ControlGroup", "")
    if freezer_state not in {"running", "freezing", "frozen"}:
        raise DependencyProvisionError("isolated builder freezer state is invalid")
    if (
        not control_group.startswith("/")
        or Path(control_group).name != unit_name
        or ".." in Path(control_group).parts
    ):
        raise DependencyProvisionError("isolated builder cgroup is invalid")
    return pid, freezer_state, control_group


def _validate_builder_pid_identity(
    pid: int,
    control_group: str,
    *,
    proc_root: Path = Path("/proc"),
) -> None:
    """Require the MainPID to be the unique non-worker DynamicUser in its cgroup."""
    try:
        status = (proc_root / str(pid) / "status").read_text(
            encoding="utf-8", errors="strict",
        )
        cgroups = (proc_root / str(pid) / "cgroup").read_text(
            encoding="utf-8", errors="strict",
        )
    except (OSError, UnicodeError) as exc:
        raise DependencyProvisionError("isolated builder identity is unavailable") from exc
    uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
    uid_fields = uid_line.split()
    if len(uid_fields) < 5 or not all(field.isdigit() for field in uid_fields[1:5]):
        raise DependencyProvisionError("isolated builder identity is invalid")
    real_uid = int(uid_fields[1])
    if real_uid in {0, WORKER_UID} or any(int(field) != real_uid for field in uid_fields[1:5]):
        raise DependencyProvisionError("isolated builder lacks a unique DynamicUser identity")
    memberships = {
        line.partition("::")[2]
        for line in cgroups.splitlines()
        if line.startswith("0::")
    }
    if memberships != {control_group}:
        raise DependencyProvisionError("isolated builder PID left its exact cgroup")


def _freeze_builder_output_path(
    state_name: str,
    *,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> Path:
    """Atomically freeze the exact builder cgroup, then expose stable output."""
    pid, freezer_state, control_group = _builder_unit_status(state_name)
    if freezer_state != "running":
        raise DependencyProvisionError("isolated builder was not running before freeze")
    _validate_builder_pid_identity(pid, control_group, proc_root=proc_root)
    systemctl = _trusted_executable(SYSTEMCTL)
    unit_name = f"{state_name}.service"
    freeze = subprocess.run(
        [str(systemctl), "freeze", unit_name],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
    )
    if freeze.returncode != 0:
        raise DependencyProvisionError("isolated builder cgroup could not be frozen")
    for _attempt in range(100):
        current_pid, current_state, current_group = _builder_unit_status(state_name)
        if current_pid != pid or current_group != control_group:
            raise DependencyProvisionError("isolated builder identity changed during freeze")
        if current_state == "frozen":
            break
        if current_state != "freezing":
            raise DependencyProvisionError("isolated builder did not enter frozen state")
        time.sleep(0.05)
    else:
        raise DependencyProvisionError("isolated builder cgroup freeze timed out")
    _validate_builder_pid_identity(pid, control_group, proc_root=proc_root)
    events_path = cgroup_root / control_group.lstrip("/") / "cgroup.events"
    try:
        events = dict(
            line.split(maxsplit=1)
            for line in events_path.read_text(encoding="ascii", errors="strict").splitlines()
            if len(line.split(maxsplit=1)) == 2
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise DependencyProvisionError("isolated builder cgroup state is unavailable") from exc
    if events.get("populated") != "1" or events.get("frozen") != "1":
        raise DependencyProvisionError("isolated builder cgroup freeze was not durable")
    output = Path("/proc") / str(pid) / "root" / "output" / "snapshot"
    if proc_root != Path("/proc"):
        output = proc_root / str(pid) / "root" / "output" / "snapshot"
    try:
        info = output.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise DependencyProvisionError("isolated builder output is unsafe")
    except DependencyProvisionError:
        raise
    except OSError as exc:
        raise DependencyProvisionError("isolated builder output is unavailable") from exc
    return output


def stop_dynamic_builder(state_name: str) -> None:
    """Stop one exact held builder, destroying its private bounded tmpfs."""
    if not re.fullmatch(r"hermes-dependency-build-[0-9a-f]{16}", state_name):
        raise DependencyProvisionError("builder state name is invalid")
    systemctl = _trusted_executable(SYSTEMCTL)
    unit_name = f"{state_name}.service"
    def cleanup_state() -> tuple[int, dict[str, str]]:
        show = subprocess.run(
            [
                str(systemctl), "show", unit_name, "--no-pager",
                "--property=ActiveState", "--property=FreezerState",
                "--property=MainPID",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
        properties: dict[str, str] = {}
        for line in show.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key] = value
        return show.returncode, properties

    show_returncode, properties = cleanup_state()
    if (
        show_returncode == 0
        and properties.get("ActiveState") == "active"
        and properties.get("FreezerState") in {"freezing", "frozen"}
    ):
        # Contrary to older systemctl documentation, the production systemd
        # manager rejects StopUnit while this transient is frozen.  Thawing
        # after the root-owned copy/rename is complete is safe: /output is
        # private, immutable promotion never hardlinks it, and stop follows
        # immediately to destroy the tmpfs.
        thaw = subprocess.run(
            [str(systemctl), "thaw", unit_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
        if thaw.returncode != 0:
            raise DependencyProvisionError("isolated builder could not be thawed")
        for _attempt in range(100):
            show_returncode, properties = cleanup_state()
            if show_returncode != 0 or properties.get("ActiveState") != "active":
                break
            if properties.get("FreezerState") == "running":
                break
            time.sleep(0.05)
        else:
            raise DependencyProvisionError("isolated builder thaw timed out")
        if (
            show_returncode == 0
            and properties.get("ActiveState") == "active"
            and properties.get("FreezerState") != "running"
        ):
            raise DependencyProvisionError("isolated builder did not thaw safely")
    if show_returncode != 0 or properties.get("ActiveState") != "active":
        return
    proc = subprocess.run(
        [str(systemctl), "stop", unit_name],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
    )
    if proc.returncode not in {0, 5}:
        raise DependencyProvisionError("isolated builder could not be stopped")
    for _attempt in range(100):
        show_returncode, properties = cleanup_state()
        if show_returncode != 0:
            return
        if (
            properties.get("ActiveState") in {"inactive", "failed"}
            and properties.get("MainPID", "0") == "0"
            and properties.get("FreezerState") not in {"freezing", "frozen"}
        ):
            return
        time.sleep(0.05)
    raise DependencyProvisionError("isolated builder remained after stop")


def build_builder_cleanup_argv(
    state_name: str,
    *,
    var_lib: Path = Path("/var/lib"),
) -> list[str]:
    """Build one root transient cleanup with only the exact state paths RW."""
    if not re.fullmatch(r"hermes-dependency-build-[0-9a-f]{16}", state_name):
        raise DependencyProvisionError("refusing unsafe builder state cleanup target")
    if Path(var_lib) != Path("/var/lib"):
        raise DependencyProvisionError("transient cleanup requires the fixed var-lib root")
    systemd_run = _trusted_executable(SYSTEMD_RUN)
    rm = _trusted_executable(RM_PATH)
    private = Path("/var/lib/private") / state_name
    public = Path("/var/lib") / state_name
    unit = state_name.replace("-build-", "-clean-")
    properties = [
        "Type=oneshot",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_FOWNER",
        "PrivateDevices=yes",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=tmpfs",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "RestrictSUIDSGID=yes",
        "RestrictNamespaces=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "TasksMax=16",
        "MemoryMax=256M",
        "RuntimeMaxSec=120",
        # Removing a directory entry requires its parent mount to be writable.
        # This transient executes only fixed root-owned /usr/bin/rm with the
        # two validated literal targets below; no package or shell code enters
        # this namespace. The controller subtree remains explicitly masked.
        "ReadWritePaths=/var/lib /var/lib/private",
        "InaccessiblePaths=-/home -/root -/etc/hermes-delivery-control "
        "-/var/lib/hermes-delivery-control",
    ]
    argv = [
        str(systemd_run), "--system", "--wait", "--collect", "--quiet",
        f"--unit={unit}",
    ]
    for value in properties:
        argv.extend(["-p", value])
    argv.extend([
        str(rm), "--recursive", "--force", "--one-file-system", "--",
        str(public), str(private),
    ])
    return argv


def remove_builder_state(
    state_name: str,
    *,
    var_lib: Path = Path("/var/lib"),
) -> None:
    """Remove only one exact generated StateDirectory after the unit stopped."""
    if not re.fullmatch(r"hermes-dependency-build-[0-9a-f]{16}", state_name):
        raise DependencyProvisionError("refusing unsafe builder state cleanup target")
    private = var_lib / "private" / state_name
    public = var_lib / state_name
    # The socket broker itself has ProtectSystem=strict. Delegate deletion to
    # a second tiny root transient whose only writable mounts are these exact
    # generated paths; never make all of /var/lib writable in the broker.
    use_transient = Path(var_lib) == Path("/var/lib") and os.geteuid() == 0
    if os.path.lexists(public):
        info = public.lstat()
        if stat.S_ISLNK(info.st_mode):
            try:
                if public.resolve(strict=False) != private:
                    raise DependencyProvisionError("builder state link has an unsafe target")
            except RuntimeError as exc:
                raise DependencyProvisionError("builder state link is invalid") from exc
            if not use_transient:
                public.unlink()
        elif stat.S_ISDIR(info.st_mode) and not os.path.lexists(private):
            if not use_transient:
                shutil.rmtree(public)
        else:
            raise DependencyProvisionError("builder state public path has an unsafe type")
    if os.path.lexists(private):
        info = private.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise DependencyProvisionError("builder private state path has an unsafe type")
        if not use_transient:
            shutil.rmtree(private)
    if use_transient and (os.path.lexists(public) or os.path.lexists(private)):
        proc = subprocess.run(
            build_builder_cleanup_argv(state_name, var_lib=var_lib),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=150,
            check=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
        if proc.returncode != 0:
            raise DependencyProvisionError("isolated builder state cleanup failed")
        if os.path.lexists(public) or os.path.lexists(private):
            raise DependencyProvisionError("isolated builder state cleanup was incomplete")


def _builder_unit_is_inactive(state_name: str) -> bool:
    """Return true only when systemd proves the exact generated unit stopped."""
    if not re.fullmatch(r"hermes-dependency-build-[0-9a-f]{16}", state_name):
        raise DependencyProvisionError("builder state name is invalid")
    try:
        proc = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", f"{state_name}.service"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # systemctl documents 3 for an inactive/failed unit and 4 for unknown.
    # Every other outcome is treated as active/indeterminate and preserved.
    return proc.returncode in {3, 4}


def scavenge_stale_builder_artifacts(
    *,
    snapshot_root: Path = SNAPSHOT_ROOT,
    var_lib: Path = Path("/var/lib"),
    now: float | None = None,
    owner_uid: int = 0,
    max_cleanups: int = MAX_STALE_CLEANUPS_PER_REQUEST,
    unit_inactive: Callable[[str], bool] = _builder_unit_is_inactive,
) -> tuple[str, ...]:
    """Boundedly remove abandoned exact generated request/state directories.

    This is recovery for SIGKILL/reboot, not a general temporary-directory
    cleaner.  Young or active units, malformed names, symlinks, unexpected
    types, and indeterminate systemd state are always retained.
    """
    if max_cleanups < 0 or max_cleanups > MAX_STALE_CLEANUPS_PER_REQUEST:
        raise DependencyProvisionError("stale cleanup bound is invalid")
    cutoff = (
        time.time() if now is None else float(now)
    ) - BUILD_TIMEOUT_SECONDS - STALE_BUILD_GRACE_SECONDS
    removed: list[str] = []
    request_root = Path(snapshot_root) / ".requests"
    private_root = Path(var_lib) / "private"

    def safe_directory(path: Path, *, require_owner: bool) -> os.stat_result | None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or (require_owner and metadata.st_uid != owner_uid)
        ):
            return None
        return metadata

    request_metadata = safe_directory(request_root, require_owner=True)
    if request_metadata is not None:
        for entry in sorted(request_root.iterdir(), key=lambda item: item.name):
            if len(removed) >= max_cleanups:
                break
            if re.fullmatch(r"[0-9a-f]{32}", entry.name) is None:
                continue
            metadata = safe_directory(entry, require_owner=True)
            if metadata is None or metadata.st_mtime > cutoff:
                continue
            state_name = f"hermes-dependency-build-{entry.name[:16]}"
            if not unit_inactive(state_name):
                continue
            # Re-stat immediately before deletion and retain on replacement.
            current = safe_directory(entry, require_owner=True)
            if (
                current is None
                or current.st_dev != metadata.st_dev
                or current.st_ino != metadata.st_ino
                or current.st_mtime > cutoff
            ):
                continue
            shutil.rmtree(entry)
            remove_builder_state(state_name, var_lib=var_lib)
            removed.append(entry.name)

    # Root promotion can be interrupted after it has copied a large bounded
    # tree but before its Python finally block runs. Socket concurrency is one;
    # a staging tree older than the entire request deadline cannot belong to
    # the current request and is safe to remove by its exact generated name.
    staging_cutoff = (
        time.time() if now is None else float(now)
    ) - PROVISION_REQUEST_TIMEOUT_SECONDS - STALE_BUILD_GRACE_SECONDS
    if len(removed) < max_cleanups:
        for entry in sorted(snapshot_root.iterdir(), key=lambda item: item.name):
            if len(removed) >= max_cleanups:
                break
            if re.fullmatch(
                r"\.staging-[0-9a-f]{64}-[0-9a-f]{12}", entry.name,
            ) is None:
                continue
            metadata = safe_directory(entry, require_owner=True)
            if metadata is None or metadata.st_mtime > staging_cutoff:
                continue
            current = safe_directory(entry, require_owner=True)
            if (
                current is None
                or current.st_dev != metadata.st_dev
                or current.st_ino != metadata.st_ino
                or current.st_mtime > staging_cutoff
            ):
                continue
            shutil.rmtree(entry)
            removed.append(entry.name)

    # A crash can occur after the request manifest was removed but before the
    # DynamicUser StateDirectory cleanup. Sweep those exact orphan names too.
    if len(removed) < max_cleanups and safe_directory(
        private_root, require_owner=False,
    ) is not None:
        for entry in sorted(private_root.iterdir(), key=lambda item: item.name):
            if len(removed) >= max_cleanups:
                break
            if re.fullmatch(
                r"hermes-dependency-build-[0-9a-f]{16}", entry.name,
            ) is None:
                continue
            metadata = safe_directory(entry, require_owner=False)
            if metadata is None or metadata.st_mtime > cutoff:
                continue
            request_id_prefix = entry.name.removeprefix(
                "hermes-dependency-build-"
            )
            if request_metadata is not None and any(
                candidate.name.startswith(request_id_prefix)
                for candidate in request_root.iterdir()
                if re.fullmatch(r"[0-9a-f]{32}", candidate.name)
            ):
                continue
            if not unit_inactive(entry.name):
                continue
            current = safe_directory(entry, require_owner=False)
            if (
                current is None
                or current.st_dev != metadata.st_dev
                or current.st_ino != metadata.st_ino
                or current.st_mtime > cutoff
            ):
                continue
            remove_builder_state(entry.name, var_lib=var_lib)
            removed.append(entry.name)
    return tuple(removed)


def active_dependency_manifest_digests() -> frozenset[str]:
    """Derive exact snapshot digests referenced by live delivery worktrees."""
    try:
        with _read_only_sqlite(KANBAN_DB) as connection:
            rows = connection.execute(
                "SELECT id, workspace_path FROM tasks "
                "WHERE status IN ('ready','running','review','shipping') "
                "AND workspace_kind = 'worktree' AND project_id = ?",
                (PROJECT_ID,),
            ).fetchall()
    except (DependencyProvisionError, sqlite3.Error) as exc:
        raise DependencyProvisionError(
            "active dependency snapshot reachability cannot be derived"
        ) from exc
    digests: set[str] = set()
    for row in rows:
        task_id = str(row["id"] or "")
        workspace = Path(str(row["workspace_path"] or ""))
        if (
            TASK_ID_RE.fullmatch(task_id) is None
            or workspace != WORKTREE_ROOT / task_id
        ):
            raise DependencyProvisionError(
                "active dependency snapshot has an invalid worktree binding"
            )
        if not workspace.is_dir():
            # A ready task can be claimed before its worktree is materialized;
            # it cannot reference candidate dependency bytes yet.
            continue
        bundle = collect_manifest_bundle(task_id, workspace)
        digests.add(bundle.digest)
    return frozenset(digests)


def _dependency_snapshot_record(
    snapshot: Path,
    *,
    owner_uid: int,
) -> tuple[int, float]:
    """Return attested byte count/creation time for one immutable snapshot."""
    try:
        if SHA256_RE.fullmatch(snapshot.name) is None:
            raise DependencyProvisionError("dependency snapshot name is invalid")
        info = snapshot.lstat()
        attestation = snapshot / "attestation.json"
        att_info = attestation.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) & 0o022
            or stat.S_ISLNK(att_info.st_mode)
            or not stat.S_ISREG(att_info.st_mode)
            or att_info.st_uid != owner_uid
            or att_info.st_nlink != 1
            or stat.S_IMODE(att_info.st_mode) & 0o022
            or att_info.st_size <= 0
            or att_info.st_size > MAX_REQUEST_BYTES
        ):
            raise DependencyProvisionError(
                "dependency snapshot metadata is unsafe"
            )
        data = json.loads(attestation.read_text(encoding="utf-8"))
        trees = data.get("trees")
        version = data.get("version")
        current_policy = (
            version == SNAPSHOT_ATTESTATION_VERSION
            and data.get("build_policy") == DEPENDENCY_BUILD_POLICY
        )
        # Legacy v1 snapshots are never consumable because the current build
        # policy is part of the digest.  They remain parseable here only so
        # bounded retention GC can account for and eventually remove them.
        legacy_policy = version == 1 and "build_policy" not in data
        if (
            not (current_policy or legacy_policy)
            or data.get("manifest_sha256") != snapshot.name
            or not isinstance(trees, dict)
        ):
            raise DependencyProvisionError(
                "dependency snapshot attestation is invalid"
            )
        total = 0
        has_python_tree = False
        for label, record in trees.items():
            if label not in {"node_modules", "backend/.venv"} or not isinstance(record, dict):
                raise DependencyProvisionError(
                    "dependency snapshot tree attestation is invalid"
                )
            byte_count = record.get("bytes")
            if (
                not isinstance(byte_count, int)
                or isinstance(byte_count, bool)
                or byte_count < 0
                or byte_count > MAX_TREE_BYTES
                or SHA256_RE.fullmatch(str(record.get("sha256") or "")) is None
            ):
                raise DependencyProvisionError(
                    "dependency snapshot tree size is invalid"
                )
            total += byte_count
            if label == "backend/.venv":
                has_python_tree = True
                expected_mode = (
                    PYTHON_INSTALL_MODE
                    if current_policy
                    else "uv-sync-no-install-project"
                )
                if record.get("install_mode") != expected_mode:
                    raise DependencyProvisionError(
                        "dependency snapshot Python install policy is invalid"
                    )
        if current_policy:
            expected_runtime = _expected_python_runtime_binding()
            expected_runtime_sha256 = hashlib.sha256(
                _canonical_json(expected_runtime) + b"\n"
            ).hexdigest()
            if has_python_tree:
                if (
                    data.get("python_runtime") != expected_runtime
                    or data.get("python_runtime_sha256")
                    != expected_runtime_sha256
                ):
                    raise DependencyProvisionError(
                        "dependency snapshot Python runtime policy is invalid"
                    )
            elif (
                "python_runtime" in data
                or "python_runtime_sha256" in data
            ):
                raise DependencyProvisionError(
                    "dependency snapshot has unexpected Python runtime metadata"
                )
        if total > MAX_TREE_BYTES:
            raise DependencyProvisionError(
                "dependency snapshot exceeds the storage limit"
            )
        return total, info.st_mtime
    except DependencyProvisionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError) as exc:
        raise DependencyProvisionError(
            "dependency snapshot record cannot be validated"
        ) from exc


def scavenge_unused_dependency_snapshots(
    *,
    live_digests: frozenset[str],
    snapshot_root: Path = SNAPSHOT_ROOT,
    now: float | None = None,
    owner_uid: int = 0,
    retention_seconds: int = SNAPSHOT_RETENTION_SECONDS,
    max_cleanups: int = MAX_SNAPSHOT_GC_PER_REQUEST,
) -> tuple[tuple[str, ...], int]:
    """Boundedly GC expired snapshots not reachable from live task manifests."""
    if (
        max_cleanups < 0
        or max_cleanups > MAX_SNAPSHOT_GC_PER_REQUEST
        or retention_seconds < 0
        or any(SHA256_RE.fullmatch(digest) is None for digest in live_digests)
    ):
        raise DependencyProvisionError("dependency snapshot GC inputs are invalid")
    try:
        root_info = snapshot_root.lstat()
        if (
            stat.S_ISLNK(root_info.st_mode)
            or not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != owner_uid
            or stat.S_IMODE(root_info.st_mode) & 0o022
        ):
            raise DependencyProvisionError("dependency snapshot root is unsafe")
    except DependencyProvisionError:
        raise
    except OSError as exc:
        raise DependencyProvisionError("dependency snapshot root is unavailable") from exc
    records: list[tuple[float, str, Path, int, tuple[int, int]]] = []
    total = 0
    for snapshot in snapshot_root.iterdir():
        if SHA256_RE.fullmatch(snapshot.name) is None:
            continue
        size, created = _dependency_snapshot_record(
            snapshot, owner_uid=owner_uid,
        )
        info = snapshot.lstat()
        total += size
        records.append((created, snapshot.name, snapshot, size, (info.st_dev, info.st_ino)))
    cutoff = (time.time() if now is None else float(now)) - retention_seconds
    removed: list[str] = []
    for created, digest, snapshot, size, identity in sorted(records):
        if len(removed) >= max_cleanups or created > cutoff or digest in live_digests:
            continue
        current = snapshot.lstat()
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != identity
        ):
            raise DependencyProvisionError(
                "dependency snapshot changed during GC"
            )
        shutil.rmtree(snapshot)
        total -= size
        removed.append(digest)
    return tuple(removed), total


@contextlib.contextmanager
def _snapshot_digest_lock(
    digest: str,
    *,
    snapshot_root: Path = SNAPSHOT_ROOT,
    owner_uid: int = 0,
) -> Iterator[None]:
    """Serialize one manifest digest using a root-owned, no-follow lease."""
    if SHA256_RE.fullmatch(digest) is None:
        raise DependencyProvisionError("snapshot lease digest is invalid")
    snapshot_root.mkdir(parents=True, mode=0o755, exist_ok=True)
    locks = snapshot_root / ".locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    for path in (snapshot_root, locks):
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise DependencyProvisionError("snapshot lease directory is unsafe")
    lock_path = locks / f"{digest}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise DependencyProvisionError("snapshot lease cannot be opened") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner_uid
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise DependencyProvisionError("snapshot lease file is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def provision_bundle(
    bundle: ManifestBundle,
    *,
    reauthorize: Callable[[], None],
    live_digests: frozenset[str] | None = None,
) -> Path:
    if os.geteuid() != 0:
        raise DependencyProvisionError("dependency provisioning service must run as root")
    if not callable(reauthorize):
        raise DependencyProvisionError("dependency promotion authorization is missing")
    with _snapshot_digest_lock(bundle.digest):
        final = SNAPSHOT_ROOT / bundle.digest
        if os.path.lexists(final):
            if _existing_snapshot_matches(final, bundle):
                reauthorize()
                return final
            raise DependencyProvisionError("invalid existing dependency snapshot")
        protected = frozenset({bundle.digest}) if live_digests is None else (
            frozenset(live_digests) | {bundle.digest}
        )
        _removed, retained_bytes = scavenge_unused_dependency_snapshots(
            live_digests=protected,
            snapshot_root=SNAPSHOT_ROOT,
        )
        # Reserve for both the bounded DynamicUser output and the root-owned
        # staging copy that coexist during validation/promotion. This makes
        # durable snapshot growth fail closed before another registry build.
        if retained_bytes + (2 * MAX_TREE_BYTES) > MAX_SNAPSHOT_STORAGE_BYTES:
            raise DependencyProvisionError(
                "dependency snapshot storage quota requires trusted cleanup"
            )
        request_root = SNAPSHOT_ROOT / ".requests"
        request_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        request_id = uuid.uuid4().hex
        input_root = request_root / request_id
        state_name = f"hermes-dependency-build-{request_id[:16]}"
        _write_bundle_input(bundle, input_root)
        try:
            resolution = _resolve_registry_addresses()
            _write_registry_hosts(input_root, resolution)
            argv = build_dynamic_worker_argv(
                bundle=bundle,
                input_root=input_root,
                state_name=state_name,
                addresses=tuple(sorted({
                    address
                    for addresses in resolution.values()
                    for address in addresses
                })),
            )
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=BUILD_TIMEOUT_SECONDS + 60,
                check=False,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
            )
            if proc.returncode != 0:
                raise DependencyProvisionError("isolated dependency builder failed")
            return promote_snapshot(
                bundle,
                _freeze_builder_output_path(state_name),
                reauthorize=reauthorize,
            )
        finally:
            if input_root.parent == request_root and input_root.name == request_id:
                shutil.rmtree(input_root, ignore_errors=True)
            try:
                stop_dynamic_builder(state_name)
            finally:
                # Remove only legacy pre-cutover StateDirectory remnants, if any.
                remove_builder_state(state_name)


def _parse_request(payload: bytes) -> ProvisionRequest:
    if not payload or len(payload) > MAX_REQUEST_BYTES:
        raise DependencyProvisionError("request size is invalid")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DependencyProvisionError("request JSON is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema", "task_id", "workspace", "run_id", "claim_lock",
    }:
        raise DependencyProvisionError("request shape is invalid")
    if value.get("schema") != PROTOCOL_SCHEMA:
        raise DependencyProvisionError("request schema is unsupported")
    task_id = str(value.get("task_id") or "")
    workspace = Path(str(value.get("workspace") or ""))
    run_id = value.get("run_id")
    claim_lock = str(value.get("claim_lock") or "")
    if not TASK_ID_RE.fullmatch(task_id):
        raise DependencyProvisionError("task id is not canonical")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise DependencyProvisionError("run id is invalid")
    if not CLAIM_RE.fullmatch(claim_lock):
        raise DependencyProvisionError("claim capability is invalid")
    return ProvisionRequest(task_id, workspace, run_id, claim_lock)


def _peer_credentials(connection: socket.socket) -> tuple[int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        raise DependencyProvisionError("peer credentials are unavailable")
    credentials = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"),
    )
    peer_pid, peer_uid, _peer_gid = struct.unpack("3i", credentials)
    return int(peer_pid), int(peer_uid)


def serve_one(connection: socket.socket) -> None:
    """Serve one socket-activated request as root."""
    response: dict[str, Any]
    try:
        if os.geteuid() != 0:
            raise DependencyProvisionError("request peer is unauthorized")
        # Bounded crash/reboot recovery runs before accepting more 8G-class
        # package work. It only removes exact old generated paths whose unit
        # is proven inactive.
        scavenge_stale_builder_artifacts()
        peer_pid, peer_uid = _peer_credentials(connection)
        payload = b""
        while b"\n" not in payload and len(payload) <= MAX_REQUEST_BYTES:
            chunk = connection.recv(4096)
            if not chunk:
                break
            payload += chunk
        request = _parse_request(payload.split(b"\n", 1)[0])
        authorize_provision_request(
            request, peer_pid=peer_pid, peer_uid=peer_uid,
        )
        bundle = collect_manifest_bundle(request.task_id, request.workspace)
        live_digests = active_dependency_manifest_digests()
        snapshot = provision_bundle(
            bundle,
            live_digests=live_digests,
            reauthorize=lambda: authorize_provision_request(
                request, peer_pid=peer_pid, peer_uid=peer_uid,
            ),
        )
        response = {
            "ok": True,
            "manifest_sha256": bundle.digest,
            "snapshot": str(snapshot),
        }
    except Exception as exc:
        message = str(exc) if isinstance(exc, DependencyProvisionError) else "internal provisioning failure"
        response = {"ok": False, "error": message[:512]}
    encoded = _canonical_json(response) + b"\n"
    connection.sendall(encoded[:MAX_RESPONSE_BYTES])


def request_dependency_snapshot(
    workspace: str | Path,
    *,
    socket_path: Path = SOCKET_PATH,
    timeout: float = PROVISION_REQUEST_TIMEOUT_SECONDS,
) -> str:
    """Request provisioning from the trusted host controller and wait."""
    workspace = Path(workspace)
    task_id = workspace.name
    if not TASK_ID_RE.fullmatch(task_id) or workspace != WORKTREE_ROOT / task_id:
        raise DependencyProvisionError("dependency request is not an exact registered worktree")
    raw_task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    raw_run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
    claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK", "").strip()
    try:
        run_id = int(raw_run_id)
    except (TypeError, ValueError) as exc:
        raise DependencyProvisionError("dependency request has no active run identity") from exc
    if raw_task_id != task_id or run_id <= 0 or not CLAIM_RE.fullmatch(claim_lock):
        raise DependencyProvisionError("dependency request has no active run capability")
    active_request = ProvisionRequest(task_id, workspace, run_id, claim_lock)
    # A registry build may legitimately outlive the normal 15-minute claim.
    # Extend both task and run with an exact CAS before blocking on the socket.
    extend_active_claim_for_provision(active_request, worker_pid=os.getpid())
    request = _canonical_json({
        "schema": PROTOCOL_SCHEMA,
        "task_id": task_id,
        "workspace": str(workspace),
        "run_id": run_id,
        "claim_lock": claim_lock,
    }) + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(request)
            payload = b""
            while b"\n" not in payload and len(payload) <= MAX_RESPONSE_BYTES:
                chunk = client.recv(4096)
                if not chunk:
                    break
                payload += chunk
    except OSError as exc:
        raise DependencyProvisionError(
            "trusted dependency provisioner is unavailable; activate the root socket/service"
        ) from exc
    try:
        response = json.loads(payload.split(b"\n", 1)[0])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DependencyProvisionError("trusted provisioner returned an invalid response") from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        detail = response.get("error") if isinstance(response, dict) else None
        raise DependencyProvisionError(str(detail or "dependency provisioning failed"))
    digest = response.get("manifest_sha256")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise DependencyProvisionError("trusted provisioner returned an invalid digest")
    return digest


def _dry_run(bundle: ManifestBundle) -> dict[str, Any]:
    blockers: list[str] = []
    for path, expected in (
        (SYSTEMD_RUN, None),
        (UV_PATH, UV_SHA256),
        (BUILDER_PYTHON, None),
        (BUILDER_ENTRYPOINT, None),
    ):
        try:
            _trusted_executable(path, expected_sha256=expected)
        except DependencyProvisionError as exc:
            blockers.append(str(exc))
    try:
        _trusted_npm_runtime()
    except DependencyProvisionError as exc:
        blockers.append(str(exc))
    return {
        "ok": not blockers,
        "task_id": bundle.task_id,
        "manifest_sha256": bundle.digest,
        "manifests": dict(bundle.hashes),
        "blockers": blockers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build-stage")
    build.add_argument("--input", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--node", type=Path, required=True)
    build.add_argument("--npm-cli", type=Path, required=True)
    build.add_argument("--uv", type=Path, required=True)
    build.add_argument("--python", type=Path, required=True)
    build.add_argument("--probe-host-pid", type=int)
    build.add_argument("--probe-background-child", action="store_true")
    build.add_argument("--notify-hold", action="store_true")
    direct = subparsers.add_parser("provision")
    direct.add_argument("--task-id", required=True)
    direct.add_argument("--workspace", type=Path, required=True)
    direct.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("serve-one")
    args = parser.parse_args(argv)
    try:
        if args.command == "build-stage":
            build_stage(
                args.input,
                args.output,
                node=args.node,
                npm_cli=args.npm_cli,
                uv=args.uv,
                python=args.python,
                probe_host_pid=args.probe_host_pid,
                probe_background_child=args.probe_background_child,
            )
            if args.notify_hold:
                _notify_ready_and_hold()
            return 0
        if args.command == "serve-one":
            connection = socket.socket(fileno=0)
            serve_one(connection)
            return 0
        bundle = collect_manifest_bundle(args.task_id, args.workspace)
        if args.dry_run:
            print(json.dumps(_dry_run(bundle), sort_keys=True))
            return 0
        raise DependencyProvisionError(
            "direct promotion is disabled; use the authenticated socket service"
        )
    except DependencyProvisionError as exc:
        print(f"dependency provisioning failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
