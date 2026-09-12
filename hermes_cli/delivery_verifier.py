"""Trusted live verification for Kanban code-delivery transitions.

Workers provide claims; this module re-derives the important facts from Git,
GitHub Actions, reviewer acceptance evidence, and (for ClausEye production)
Cloud Run.  It deliberately uses argv-only subprocess calls and never shells
out through task-controlled strings.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from hermes_cli.trusted_delivery_runtime import (
    TRUSTED_GH_PATH,
    TRUSTED_GCLOUD_PATH,
    TrustedDeliveryRuntimeError,
    require_trusted_gh,
    require_trusted_gcloud,
)


class LiveVerificationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        evidence: Mapping[str, Any] | None = None,
    ):
        self.code = code
        self.evidence = dict(evidence) if evidence is not None else None
        super().__init__(message)


_GITHUB_REMOTE_PATTERNS = (
    re.compile(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", re.I),
    re.compile(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?$", re.I),
    re.compile(r"ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", re.I),
)

_CLAUSEYE_REPOSITORY = "clauseye-com/clauseye-contra-rope"
_CLAUSEYE_ORGANIZATION = "clauseye-com"
_CLAUSEYE_REPOSITORY_ID = 1182097307
_CLAUSEYE_AUTONOMOUS_BRANCH_PREFIX = "hermes/"
_CLAUSEYE_DEPLOY_WORKFLOW_PATH = ".github/workflows/auto-deploy-production.yml"
_CLAUSEYE_DEPLOY_WORKFLOW_ID = 261021157
_CLAUSEYE_GATE_WORKFLOW_PATH = ".github/workflows/autonomous-merge-gate.yml"
_CLAUSEYE_GATE_WORKFLOW_ID = 278086691
_CLAUSEYE_REQUIRED_WORKFLOW_RULESET = "clauseye-autonomous-merge-workflow"
_CLAUSEYE_MAIN_RULESET_TEMPLATE = ".github/rulesets/main-protection.json"
_CLAUSEYE_GATE_ATTESTATION_NAME = "hermes-autonomy-attestation-v1"
_CLAUSEYE_GATE_ATTESTATION_SCHEMA = "hermes-autonomy-attestation/v1"
_GITHUB_ACTIONS_APP_ID = 15368
_CLAUSEYE_MAIN_RULESET_ID = 15579823
_REVISION_CANDIDATE_LABEL = "clauseye-candidate-sha"
_REVISION_MERGE_LABEL = "clauseye-merge-sha"
_REVISION_RUN_LABEL = "clauseye-github-run-id"
_REVISION_CANDIDATE_ANNOTATION = "clauseye.dev/candidate-sha"
_REVISION_MERGE_ANNOTATION = "clauseye.dev/merge-sha"
_REVISION_RUN_ANNOTATION = "clauseye.dev/github-run-id"
_REVISION_RUN_ATTEMPT_ANNOTATION = "clauseye.dev/github-run-attempt"
_REVISION_WORKFLOW_ANNOTATION = "clauseye.dev/github-workflow-ref"
_REVISION_WORKFLOW_SHA_ANNOTATION = "clauseye.dev/github-workflow-sha"
_REVISION_IMAGE_ANNOTATION = "clauseye.dev/image-digest"
_REVISION_MODE_ANNOTATION = "clauseye.dev/deployment-mode"
_MAX_ATTESTATION_ARCHIVE_BYTES = 128 * 1024
_MAX_ATTESTATION_JSON_BYTES = 16 * 1024
_POLICY_ATTESTATION_SCHEMA = "hermes-delivery-policy-attestation/v1"
_POLICY_ATTESTATION_ISSUER = "hermes-trusted-delivery-control-plane"
_POLICY_ATTESTATION_MAX_BYTES = 64 * 1024
_POLICY_ATTESTATION_MAX_TTL_SECONDS = 15 * 60
_POLICY_ATTESTATION_CLOCK_SKEW_SECONDS = 30
_POLICY_ATTESTATION_SYSTEM_ROOT = Path("/var/lib")
_POLICY_ATTESTATION_DIRECTORY = (
    _POLICY_ATTESTATION_SYSTEM_ROOT
    / "hermes-delivery-control"
    / "attestations"
)
_POLICY_ATTESTATION_PATH = (
    _POLICY_ATTESTATION_DIRECTORY / "clauseye-production.json"
)
_POLICY_ATTESTATION_OWNER_UID = 0
_POLICY_ATTESTATION_OWNER_GID = 0
_POLICY_ATTESTATION_DIRECTORY_MODE = 0o755
_POLICY_ATTESTATION_FILE_MODE = 0o644
_ACCEPTANCE_EVIDENCE_ROOT = Path(
    "/var/lib/hermes-delivery-control/evidence"
)
_DELIVERY_CONTROL_UID = 1000
_ACCEPTANCE_SANDBOX_FINGERPRINT = (
    "systemd sandbox/v3: private network,pids,home,tmp,devices,ipc,keyring; "
    "host run,var,etc hidden; exact root-owned dependency projections; "
    "bounded cgroup"
)
# Provisioning boundary: pin the trusted control-plane Ed25519 public key as
# raw 32-byte base64 before activating autonomous production delivery.  The
# private key must never be present in a Kanban worker or worker-readable
# filesystem.  Keeping the trust anchor in code prevents a same-UID worker
# from replacing a mutable public-key file alongside the signed attestation.
_CLAUSEYE_POLICY_ATTESTATION_PUBLIC_KEY_B64 = (
    "RVCW7dxi10l9ub7DaHvrwMjz802NhpPTtcCSpalzpW8="
)
_CLAUSEYE_PROTECTED_CONTROL_PATHS = frozenset({
    ".github/rulesets/autonomous-required-workflow.json",
    ".github/rulesets/main-protection.json",
    ".github/workflows/auto-deploy-production.yml",
    ".github/workflows/autonomous-merge-gate.yml",
    ".github/workflows/build-backend-image.yml",
    ".github/workflows/build-frontend-artifact.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/deploy-backend.yml",
    ".github/workflows/deploy-frontend.yml",
    ".github/workflows/gitleaks.yml",
    ".github/workflows/rollback-backend.yml",
    ".github/workflows/rollback-frontend.yml",
    ".github/workflows/semgrep.yml",
    "backend/deploy/smoke-test-cloud-run.sh",
    "scripts/autonomous-deploy-gate.mjs",
    "scripts/autonomous-merge-gate.mjs",
    "scripts/autonomous-policy-lib.mjs",
    "scripts/render-backend-terraform-tfvars.py",
    "scripts/resolve-production-deploy-lineage.mjs",
    "scripts/verify-backend-promotion-evidence.mjs",
    "scripts/write-autonomous-gate-attestation.mjs",
    "scripts/bootstrap-autonomous-required-workflow.sh",
    "scripts/bootstrap-branch-protection.sh",
    "scripts/bootstrap-repo-settings.sh",
    "scripts/setup-github-environments.sh",
})
_CLAUSEYE_PROTECTED_CONTROL_PREFIXES = ("backend/deploy/terraform/",)

# Trusted delivery-control supplies a process-local, request-scoped subprocess
# environment.  A ContextVar keeps concurrent broker requests isolated and
# avoids ever copying a GitHub token into argv, logs, or global os.environ.
_TRUSTED_COMMAND_ENV: contextvars.ContextVar[Mapping[str, str] | None] = (
    contextvars.ContextVar("hermes_delivery_verifier_command_env", default=None)
)


@contextlib.contextmanager
def trusted_command_environment(env: Mapping[str, str]):
    """Use ``env`` only for verifier child commands in this context."""

    token = _TRUSTED_COMMAND_ENV.set(dict(env))
    try:
        yield
    finally:
        _TRUSTED_COMMAND_ENV.reset(token)


def _trusted_subprocess_boundary(
    argv: list[str],
) -> tuple[list[str], Mapping[str, str] | None]:
    """Keep broker credentials out of local Git and acceptance commands."""

    executable = Path(argv[0]).name
    if executable == "gh" and len(argv) > 1 and argv[1] == "api":
        if "--hostname" not in argv:
            argv = [*argv[:2], "--hostname", "github.com", *argv[2:]]
    configured = _TRUSTED_COMMAND_ENV.get()
    if configured is None:
        return argv, None
    env = dict(configured)
    if executable != "gh":
        for key in (
            "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
            "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_PRIVATE_KEY_PATH",
        ):
            env.pop(key, None)
    if executable != "gcloud":
        env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        env["CLOUDSDK_CONFIG"] = "/var/empty/hermes-delivery-verifier/gcloud"
    if executable == "git":
        argv = [
            argv[0],
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
            "-c", "credential.helper=",
            "-c", "credential.interactive=false",
            *argv[1:],
        ]
    return argv, env


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 45,
) -> str:
    command, command_env = _trusted_subprocess_boundary(argv)
    if Path(command[0]).name == "gh":
        if command[0] != str(TRUSTED_GH_PATH):
            raise LiveVerificationError(
                "trusted gh path is not fixed",
                code="trusted_runtime_invalid",
            )
        try:
            require_trusted_gh()
        except TrustedDeliveryRuntimeError as exc:
            raise LiveVerificationError(
                "trusted gh runtime is unavailable",
                code="trusted_runtime_unavailable",
            ) from exc
    if Path(command[0]).name == "gcloud":
        if command[0] != str(TRUSTED_GCLOUD_PATH):
            raise LiveVerificationError(
                "trusted gcloud path is not fixed",
                code="trusted_runtime_unavailable",
            )
        try:
            require_trusted_gcloud()
        except TrustedDeliveryRuntimeError as exc:
            raise LiveVerificationError(
                "trusted gcloud runtime is unavailable",
                code="trusted_runtime_unavailable",
            ) from exc
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=command_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LiveVerificationError(
            f"verification command failed: {argv[0]}: {exc}",
            code="live_verifier_unavailable",
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-600:]
        raise LiveVerificationError(
            f"verification command failed ({argv[0]} rc={proc.returncode}): {detail}",
            code="live_verifier_command_failed",
        )
    return proc.stdout


def _json(argv: list[str], *, cwd: Path | None = None, timeout: int = 45) -> Any:
    raw = _run(argv, cwd=cwd, timeout=timeout)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LiveVerificationError(
            f"verification command returned invalid JSON: {argv[0]}",
            code="live_verifier_invalid_json",
        ) from exc


def _bytes(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 45,
    max_bytes: int = _MAX_ATTESTATION_ARCHIVE_BYTES,
) -> bytes:
    """Run a trusted argv command and return a bounded binary response."""
    command, command_env = _trusted_subprocess_boundary(argv)
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=command_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LiveVerificationError(
            f"verification command failed: {argv[0]}: {exc}",
            code="live_verifier_unavailable",
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"")[-600:].decode(
            "utf-8", errors="replace",
        ).strip()
        raise LiveVerificationError(
            f"verification command failed ({argv[0]} rc={proc.returncode}): "
            f"{detail}",
            code="live_verifier_command_failed",
        )
    if len(proc.stdout) > max_bytes:
        raise LiveVerificationError(
            f"verification response exceeds {max_bytes} bytes",
            code="live_verifier_response_too_large",
        )
    return proc.stdout


def _repo_from_pr_url(pr_url: str) -> str:
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/[1-9][0-9]*",
        pr_url.rstrip("/"),
    )
    if match is None:
        raise LiveVerificationError("PR URL is not canonical", code="pr_url_invalid")
    return match.group(1)


def _repo_from_remote(remote: str) -> str | None:
    for pattern in _GITHUB_REMOTE_PATTERNS:
        match = pattern.fullmatch(remote.strip())
        if match is not None:
            return match.group(1).removesuffix(".git")
    return None


def _workspace(task: Any) -> Path:
    raw = str(getattr(task, "workspace_path", None) or "").strip()
    if not raw:
        raise LiveVerificationError(
            "task has no resolved workspace_path", code="workspace_unbound"
        )
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise LiveVerificationError(
            f"task workspace does not exist: {path}", code="workspace_missing"
        )
    return path


def _require_registered_clauseye_project(
    task: Any,
    workspace: Path,
    repo: str,
) -> Path:
    """Bind a worker-mutable worktree remote to the trusted project registry."""
    project_id = str(getattr(task, "project_id", None) or "").strip()
    if not project_id:
        raise LiveVerificationError(
            "ClausEye autonomous delivery requires a project-linked task",
            code="project_identity_unbound",
        )
    try:
        from hermes_constants import get_default_hermes_root

        root_projects_db = get_default_hermes_root() / "projects.db"
        metadata = root_projects_db.lstat()
        if (
            root_projects_db.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise OSError("projects registry metadata is unsafe")
        project_conn = sqlite3.connect(
            f"file:{root_projects_db}?mode=ro", uri=True, timeout=30.0,
        )
        try:
            project_conn.row_factory = sqlite3.Row
            columns = {
                str(row[1])
                for row in project_conn.execute("PRAGMA table_info(projects)")
            }
            required = {"id", "slug", "primary_path", "archived"}
            if not required.issubset(columns):
                raise sqlite3.DatabaseError("projects registry schema is incomplete")
            project = project_conn.execute(
                "SELECT id, slug, primary_path, archived FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        finally:
            project_conn.close()
    except Exception as exc:
        raise LiveVerificationError(
            "trusted task project identity is not available",
            code="project_identity_unverifiable",
        ) from exc
    if (
        project is None
        or bool(project["archived"])
        or project["slug"] != "clauseye-production-readiness"
        or not project["primary_path"]
    ):
        raise LiveVerificationError(
            "task is not linked to the registered ClausEye delivery project",
            code="project_identity_mismatch",
        )
    project_root = Path(str(project["primary_path"])).expanduser().resolve()
    try:
        workspace.relative_to(project_root / ".worktrees")
    except ValueError as exc:
        raise LiveVerificationError(
            "task workspace is outside the registered project worktree root",
            code="project_workspace_mismatch",
        ) from exc
    project_origin = _run(
        ["git", "-C", str(project_root), "remote", "get-url", "origin"],
        cwd=project_root,
    ).strip()
    registered_repo = _repo_from_remote(project_origin)
    if registered_repo is None or registered_repo.casefold() != repo.casefold():
        raise LiveVerificationError(
            "registered project repository does not match the submitted PR",
            code="project_repository_mismatch",
        )
    return project_root


def _pull_request(repo: str, number: int, *, cwd: Path) -> dict[str, Any]:
    value = _json(
        [str(TRUSTED_GH_PATH), "api", f"repos/{repo}/pulls/{number}"], cwd=cwd,
    )
    if not isinstance(value, dict):
        raise LiveVerificationError(
            "GitHub PR response is not an object", code="pr_state_unverifiable"
        )
    return value


def _default_branch_for_pr(
    repo: str,
    pr: Mapping[str, Any],
    *,
    cwd: Path,
) -> str:
    """Require the PR to target this repository's live default branch."""
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    if str(base_repo.get("full_name") or "").casefold() != repo.casefold():
        raise LiveVerificationError(
            "PR base repository does not match the task repository",
            code="pr_base_repository_mismatch",
        )
    default_branch = str(base_repo.get("default_branch") or "").strip()
    if not default_branch:
        repo_info = _json([str(TRUSTED_GH_PATH), "api", f"repos/{repo}"], cwd=cwd)
        if isinstance(repo_info, dict):
            default_branch = str(repo_info.get("default_branch") or "").strip()
    if not default_branch:
        raise LiveVerificationError(
            "repository default branch is not verifiable",
            code="default_branch_unverifiable",
        )
    if str(base.get("ref") or "") != default_branch:
        raise LiveVerificationError(
            "autonomous delivery PR must target the repository default branch",
            code="pr_base_branch_mismatch",
        )
    return default_branch


def verify_submission(
    task: Any,
    policy: Mapping[str, Any],
    pull_request: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify a clean local candidate and the exact open GitHub PR head."""
    workspace = _workspace(task)
    repo = _repo_from_pr_url(str(pull_request["pr_url"]))
    if repo.casefold() != _CLAUSEYE_REPOSITORY:
        raise LiveVerificationError(
            f"no trusted review/deployment verifier is registered for {repo!r}",
            code="repository_verifier_unregistered",
        )
    _require_registered_clauseye_project(task, workspace, repo)
    origin = _run(
        ["git", "-C", str(workspace), "remote", "get-url", "origin"],
        cwd=workspace,
    ).strip()
    origin_repo = _repo_from_remote(origin)
    if origin_repo is None or origin_repo.casefold() != repo.casefold():
        raise LiveVerificationError(
            "task worktree origin does not match the submitted PR repository",
            code="pr_repository_mismatch",
        )
    local_head = _run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], cwd=workspace,
    ).strip().lower()
    if local_head != str(pull_request["head_sha"]).lower():
        raise LiveVerificationError(
            "task worktree HEAD does not match submitted head_sha",
            code="candidate_head_mismatch",
        )
    dirty = _run(
        ["git", "-C", str(workspace), "status", "--porcelain"], cwd=workspace,
    ).strip()
    if dirty:
        raise LiveVerificationError(
            "task worktree has uncommitted changes at review submission",
            code="candidate_worktree_dirty",
        )

    pr = _pull_request(repo, int(pull_request["pr_number"]), cwd=workspace)
    if str(pr.get("html_url") or "").rstrip("/") != str(
        pull_request["pr_url"]
    ).rstrip("/"):
        raise LiveVerificationError("GitHub PR URL mismatch", code="pr_url_mismatch")
    if str(pr.get("state") or "").casefold() != "open" or pr.get("merged_at"):
        raise LiveVerificationError(
            "review submission requires an open, unmerged PR",
            code="pr_not_open",
        )
    if bool(pr.get("draft")):
        raise LiveVerificationError(
            "review submission requires a non-draft PR",
            code="pr_is_draft",
        )
    default_branch = _default_branch_for_pr(repo, pr, cwd=workspace)
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    if str(head.get("sha") or "").lower() != local_head:
        raise LiveVerificationError(
            "GitHub PR head does not match submitted head_sha",
            code="pr_head_mismatch",
        )
    expected_ref = str(pull_request["candidate_ref"]).removeprefix("refs/heads/")
    if str(head.get("ref") or "") != expected_ref:
        raise LiveVerificationError(
            "GitHub PR head branch does not match candidate_ref",
            code="pr_branch_mismatch",
        )
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    if (
        str(head_repo.get("full_name") or "").casefold() != repo.casefold()
        or int(head_repo.get("id") or 0) != _CLAUSEYE_REPOSITORY_ID
        or int(base_repo.get("id") or 0) != _CLAUSEYE_REPOSITORY_ID
    ):
        raise LiveVerificationError(
            "cross-repository PR heads are not accepted for autonomous delivery",
            code="pr_head_repository_mismatch",
        )
    if (
        repo.casefold() == _CLAUSEYE_REPOSITORY
        and not expected_ref.startswith(_CLAUSEYE_AUTONOMOUS_BRANCH_PREFIX)
    ):
        raise LiveVerificationError(
            "ClausEye autonomous delivery requires a canonical hermes/* branch",
            code="autonomous_branch_required",
        )
    base_sha = str(base.get("sha") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise LiveVerificationError(
            "GitHub PR base SHA is not immutable",
            code="pr_base_sha_unverifiable",
        )
    return {
        "verified_at": _utc_now(),
        "repository": repo,
        "local_head_sha": local_head,
        "pr_state": "open",
        "pr_draft": False,
        "base_ref": default_branch,
        "base_sha": base_sha,
        "head_repository_id": int(head_repo.get("id") or 0),
        "base_repository_id": int(base_repo.get("id") or 0),
        "contract_hash": policy.get("contract_hash"),
    }


def verify_registered_project_binding(task: Any, repo: str) -> Path:
    """Fail closed unless ``task`` is the registered fixed-repository worktree."""

    workspace = _workspace(task)
    return _require_registered_clauseye_project(task, workspace, repo)


def _parse_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _safe_acceptance_evidence_directory(task_id: str) -> Path:
    """Return one controller-owned task ledger directory, creating it safely."""

    if not re.fullmatch(r"t_[0-9a-f]{8}", task_id):
        raise LiveVerificationError(
            "acceptance task identity is invalid",
            code="acceptance_evidence_store_unavailable",
        )
    root = _ACCEPTANCE_EVIDENCE_ROOT
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise LiveVerificationError(
            "trusted acceptance evidence root is unavailable",
            code="acceptance_evidence_store_unavailable",
        ) from exc
    if (
        root.is_symlink()
        or not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != _DELIVERY_CONTROL_UID
        or stat.S_IMODE(root_stat.st_mode) & 0o077
    ):
        raise LiveVerificationError(
            "trusted acceptance evidence root metadata is unsafe",
            code="acceptance_evidence_store_unavailable",
        )
    task_dir = root / task_id
    try:
        task_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise LiveVerificationError(
            "trusted task evidence directory cannot be created",
            code="acceptance_evidence_store_unavailable",
        ) from exc
    try:
        task_stat = task_dir.lstat()
    except OSError as exc:
        raise LiveVerificationError(
            "trusted task evidence directory is unavailable",
            code="acceptance_evidence_store_unavailable",
        ) from exc
    if (
        task_dir.is_symlink()
        or not stat.S_ISDIR(task_stat.st_mode)
        or task_stat.st_uid != _DELIVERY_CONTROL_UID
        or stat.S_IMODE(task_stat.st_mode) & 0o077
    ):
        raise LiveVerificationError(
            "trusted task evidence directory metadata is unsafe",
            code="acceptance_evidence_store_unavailable",
        )
    return task_dir


def _publish_acceptance_evidence(
    task_id: str,
    evidence: Mapping[str, Any],
) -> tuple[Path, bytes, str]:
    """Atomically publish verifier-derived evidence without trusting a worker."""

    directory = _safe_acceptance_evidence_directory(task_id)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"acceptance-{timestamp}-{secrets.token_hex(4)}.json"
    path = directory / filename
    raw = (json.dumps(dict(evidence), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    digest = hashlib.sha256(raw).hexdigest()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fchmod(fd, 0o400)
        finally:
            os.close(fd)

        manifest_tmp = directory / f".SHA256SUMS-{secrets.token_hex(8)}"
        manifest_flags = flags
        manifest_fd = os.open(manifest_tmp, manifest_flags, 0o600)
        try:
            manifest_raw = f"{digest}  {filename}\n".encode("ascii")
            os.write(manifest_fd, manifest_raw)
            os.fsync(manifest_fd)
            os.fchmod(manifest_fd, 0o400)
        finally:
            os.close(manifest_fd)
        os.replace(manifest_tmp, directory / "SHA256SUMS")
        directory_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise LiveVerificationError(
            "trusted acceptance evidence could not be published",
            code="acceptance_evidence_store_unavailable",
        ) from exc
    return path, raw, digest


def _acceptance_projection_identity(projections: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(
        (
            item.label,
            str(item.source),
            str(item.destination),
            item.source_device,
            item.source_inode,
            item.tree_sha256,
            item.attestation_sha256,
            tuple((str(path), digest) for path, digest in item.manifest_hashes),
        )
        for item in projections
    )


def _run_acceptance_tier1_in_sandbox(
    detached_workspace: Path,
    sandbox_home: Path,
    tier1: list[Mapping[str, Any]],
    *,
    task_id: str,
    candidate_sha: str,
    base_sha: str,
    base_ref: str,
    git_overlay: Any,
) -> list[dict[str, Any]]:
    """Execute exact contract commands inside the shared no-credential unit."""

    from tools.environments.local import (
        WorkerTerminalSandboxError,
        build_delivery_sandbox_argv,
        build_delivery_sandbox_environment,
        delivery_systemd_client_environment,
        resolve_trusted_dependency_projections,
    )

    try:
        projections = resolve_trusted_dependency_projections(detached_workspace)
        projection_identity = _acceptance_projection_identity(projections)
        run_env = build_delivery_sandbox_environment({}, sandbox_home, projections)
        client_env = delivery_systemd_client_environment()
    except WorkerTerminalSandboxError as exc:
        raise LiveVerificationError(
            "trusted dependency snapshot is unavailable for this exact candidate",
            code="acceptance_dependency_snapshot_unavailable",
        ) from exc

    results: list[dict[str, Any]] = []
    for index, item in enumerate(tier1):
        command = str(item["cmd"])
        expected_exit = int(item["expect_exit"])
        try:
            current = resolve_trusted_dependency_projections(detached_workspace)
            if _acceptance_projection_identity(current) != projection_identity:
                raise WorkerTerminalSandboxError(
                    "candidate dependency projection changed during acceptance"
                )
            unit_name = (
                f"hermes-kanban-acceptance-{task_id[2:]}-{os.getpid()}-"
                f"{index}-{secrets.token_hex(4)}"
            )
            argv = build_delivery_sandbox_argv(
                command=["/bin/bash", "-c", command],
                workspace=detached_workspace,
                sandbox_home=sandbox_home,
                run_env=run_env,
                timeout=300,
                unit_name=unit_name,
                dependency_projections=current,
                workspace_writable=False,
                git_overlay=git_overlay,
            )
        except WorkerTerminalSandboxError as exc:
            raise LiveVerificationError(
                "acceptance sandbox boundary could not be established",
                code="acceptance_sandbox_unavailable",
            ) from exc
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(detached_workspace),
                env=client_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=330,
                check=False,
            )
            exit_code = int(proc.returncode)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LiveVerificationError(
                "authoritative acceptance command did not complete",
                code="acceptance_sandbox_unavailable",
            ) from exc
        finally:
            try:
                subprocess.run(
                    ["/usr/bin/systemctl", "--user", "stop", unit_name],
                    cwd=str(detached_workspace),
                    env=client_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                # RuntimeMaxSec/KillMode are authoritative; a cleanup client
                # failure must not replace the acceptance result or expose
                # command output. A later retry receives a fresh unit name.
                pass
        results.append({
            "cmd": command,
            "exit": exit_code,
            "expect_ok": exit_code == expected_exit,
            "ms": int((time.monotonic() - started) * 1000),
        })

    try:
        final = resolve_trusted_dependency_projections(detached_workspace)
        if _acceptance_projection_identity(final) != projection_identity:
            raise WorkerTerminalSandboxError(
                "candidate dependency projection changed during acceptance"
            )
    except WorkerTerminalSandboxError as exc:
        raise LiveVerificationError(
            "candidate dependency manifests changed during acceptance",
            code="acceptance_dependency_snapshot_drifted",
        ) from exc
    return results


def _link_acceptance_dependencies(
    project_root: Path,
    detached_workspace: Path,
) -> tuple[Any, ...]:
    """Compatibility facade: resolve immutable snapshots; never make symlinks."""

    del project_root
    from tools.environments.local import resolve_trusted_dependency_projections

    return resolve_trusted_dependency_projections(detached_workspace)


def _authoritative_acceptance(
    task: Any,
    submission: Mapping[str, Any],
    *,
    contract_hash: str,
    candidate_sha: str,
    base_sha: str,
    base_ref: str,
    kanban_home: Path,
    workspace: Path,
    project_root: Path,
) -> dict[str, Any]:
    """Run canonical tier1 now and bind evidence to this exact review attempt.

    Existing files in the worker-writable evidence directory are never enough
    to authorize completion. The trusted terminal verifier invokes the runner
    itself, captures the exact emitted file, then validates its content and
    digest before returning an immutable record to the DB transaction.
    """
    from hermes_cli.acceptance_contract import lint_body

    lint = lint_body(getattr(task, "body", None))
    contract = lint.get("contract") if lint.get("valid") else None
    if (
        not isinstance(contract, dict)
        or str(lint.get("contract_hash") or "") != contract_hash
    ):
        raise LiveVerificationError(
            "canonical acceptance contract changed before terminal verification",
            code="acceptance_contract_mismatch",
        )
    if (
        not re.fullmatch(r"[0-9a-f]{40}", base_sha)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", base_ref)
        or ".." in base_ref
        or "//" in base_ref
    ):
        raise LiveVerificationError(
            "acceptance base revision is not an immutable canonical ref",
            code="acceptance_candidate_mismatch",
        )
    tier1 = contract.get("tier1")
    if not isinstance(tier1, list) or not tier1:
        raise LiveVerificationError(
            "canonical tier1 acceptance is missing",
            code="acceptance_contract_mismatch",
        )

    review_run_id = int(getattr(task, "current_run_id", 0) or 0)
    review_profile = str(getattr(task, "assignee", None) or "").strip()
    submission_event_id = int(submission.get("_event_id") or 0)
    submitted_at = int(submission.get("submitted_at") or 0)
    if not all((review_run_id, review_profile, submission_event_id, submitted_at)):
        raise LiveVerificationError(
            "review acceptance lacks run/submission provenance",
            code="acceptance_provenance_missing",
        )

    # These caller paths are identity inputs to earlier validation only.  The
    # authoritative runner must not read or mutate either a worker worktree or
    # the canonical checkout.  It projects exact objects from the fixed
    # controller mirror into the controller-owned acceptance root instead.
    del kanban_home, workspace, project_root
    from tools.environments.local import (
        WorkerTerminalSandboxError,
        delivery_acceptance_candidate,
    )

    try:
        candidate_context = delivery_acceptance_candidate(
            task_id=str(task.id),
            candidate_sha=candidate_sha,
            base_sha=base_sha,
            base_ref=base_ref,
        )
        with candidate_context as (
            detached_workspace,
            sandbox_home,
            git_overlay,
        ):
            results = _run_acceptance_tier1_in_sandbox(
                detached_workspace,
                sandbox_home,
                tier1,
                task_id=str(task.id),
                candidate_sha=candidate_sha,
                base_sha=base_sha,
                base_ref=base_ref,
                git_overlay=git_overlay,
            )
    except WorkerTerminalSandboxError as exc:
        raise LiveVerificationError(
            "exact acceptance candidate could not be projected safely",
            code="acceptance_sandbox_unavailable",
        ) from exc

    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    evidence = {
        "schema": "hermes-evidence/v1",
        "task_id": str(task.id),
        "domain": contract.get("domain"),
        "contract_hash": contract_hash,
        "candidate_sha": candidate_sha,
        "results": results,
        "env_fingerprint": _ACCEPTANCE_SANDBOX_FINGERPRINT,
        "run_by": "reviewer",
        "review_run_id": review_run_id,
        "review_profile": review_profile,
        "submission_event_id": submission_event_id,
        "verdict": "pass" if all(item["expect_ok"] for item in results) else "fail",
        "created_at": created_at,
    }
    path, raw, digest = _publish_acceptance_evidence(str(task.id), evidence)

    evidence_dir = _ACCEPTANCE_EVIDENCE_ROOT / str(task.id)
    if path.parent != evidence_dir or not path.name.startswith("acceptance-"):
        raise LiveVerificationError(
            "acceptance publisher returned an invalid ledger path",
            code="acceptance_evidence_path_invalid",
        )

    if (
        not isinstance(raw, bytes)
        or re.fullmatch(r"[0-9a-f]{64}", str(digest or "")) is None
        or hashlib.sha256(raw).hexdigest() != digest
    ):
        raise LiveVerificationError(
            "acceptance publisher returned an inconsistent digest",
            code="acceptance_evidence_checksum_mismatch",
        )
    try:
        ledger = json.loads(raw)
        canonical_raw = (
            json.dumps(ledger, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise LiveVerificationError(
            "serialized acceptance evidence is not canonical JSON",
            code="acceptance_evidence_mismatch",
        ) from exc
    expected_ledger_keys = {
        "schema", "task_id", "domain", "contract_hash", "candidate_sha",
        "results", "env_fingerprint", "run_by", "review_run_id",
        "review_profile", "submission_event_id", "verdict", "created_at",
    }
    if (
        not isinstance(ledger, dict)
        or set(ledger) != expected_ledger_keys
        or raw != canonical_raw
    ):
        raise LiveVerificationError(
            "serialized acceptance evidence is not canonical",
            code="acceptance_evidence_mismatch",
        )

    created_at = _parse_utc(ledger.get("created_at"))
    now = datetime.now(timezone.utc)
    if (
        created_at is None
        or created_at.timestamp() < submitted_at
        or created_at > now.replace(microsecond=now.microsecond)
    ):
        raise LiveVerificationError(
            "acceptance evidence timestamp is outside this review round",
            code="acceptance_evidence_stale",
        )
    expected_identity = (
        ledger.get("schema") == "hermes-evidence/v1"
        and ledger.get("task_id") == str(task.id)
        and ledger.get("domain") == contract.get("domain")
        and ledger.get("contract_hash") == contract_hash
        and ledger.get("candidate_sha") == candidate_sha
        and ledger.get("run_by") == "reviewer"
        and ledger.get("review_run_id") == review_run_id
        and ledger.get("review_profile") == review_profile
        and ledger.get("submission_event_id") == submission_event_id
        and ledger.get("env_fingerprint") == _ACCEPTANCE_SANDBOX_FINGERPRINT
        and ledger.get("verdict") in {"pass", "fail"}
    )
    if not expected_identity:
        raise LiveVerificationError(
            "acceptance evidence is not bound to this exact review attempt",
            code="acceptance_evidence_mismatch",
        )
    results = ledger.get("results")
    if not isinstance(results, list) or len(results) != len(tier1):
        raise LiveVerificationError(
            "acceptance result count does not match canonical tier1",
            code="acceptance_results_mismatch",
        )
    failed_indexes: list[int] = []
    observed_exits: list[int] = []
    for index, (expected, observed) in enumerate(zip(tier1, results)):
        observed_exit = observed.get("exit") if isinstance(observed, dict) else None
        expected_exit = expected.get("expect_exit")
        if (
            not isinstance(observed, dict)
            or set(observed) != {"cmd", "exit", "expect_ok", "ms"}
            or observed.get("cmd") != expected.get("cmd")
            or not isinstance(observed_exit, int)
            or isinstance(observed_exit, bool)
            or observed.get("expect_ok") is not (observed_exit == expected_exit)
            or not isinstance(observed.get("ms"), int)
            or isinstance(observed.get("ms"), bool)
            or observed["ms"] < 0
        ):
            raise LiveVerificationError(
                "acceptance results do not exactly match canonical tier1",
                code="acceptance_results_mismatch",
            )
        observed_exits.append(observed_exit)
        if observed_exit != expected_exit:
            failed_indexes.append(index)

    if ledger.get("verdict") != ("fail" if failed_indexes else "pass"):
        raise LiveVerificationError(
            "acceptance verdict does not match canonical tier1 results",
            code="acceptance_evidence_mismatch",
        )
    if failed_indexes:
        raise LiveVerificationError(
            "one or more canonical tier1 acceptance commands failed",
            code="acceptance_tier1_failed",
            evidence={
                "schema": "hermes-acceptance-failure/v1",
                "path": str(path),
                "sha256": digest,
                "contract_hash": contract_hash,
                "candidate_sha": candidate_sha,
                "review_run_id": review_run_id,
                "review_profile": review_profile,
                "submission_event_id": submission_event_id,
                "created_at": ledger["created_at"],
                "failed_indexes": failed_indexes,
                "observed_exits": observed_exits,
            },
        )
    return {
        "path": str(path),
        "sha256": digest,
        "contract_hash": contract_hash,
        "candidate_sha": candidate_sha,
        "run_by": "reviewer",
        "review_run_id": review_run_id,
        "review_profile": review_profile,
        "submission_event_id": submission_event_id,
        "created_at": ledger["created_at"],
        "verdict": "pass",
        "commands_verified": len(tier1),
    }


def _association_matches(
    value: Any,
    *,
    pr_number: int,
    head_sha: str,
    head_ref: str,
    base_sha: str,
    base_ref: str,
) -> bool:
    """Bind an Actions run to the immutable PR event payload."""
    if not isinstance(value, Mapping):
        return False
    head = value.get("head") if isinstance(value.get("head"), Mapping) else {}
    base = value.get("base") if isinstance(value.get("base"), Mapping) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), Mapping) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), Mapping) else {}
    return (
        int(value.get("number") or 0) == pr_number
        and str(head.get("sha") or "").lower() == head_sha
        and str(head.get("ref") or "") == head_ref
        and int(head_repo.get("id") or 0) == _CLAUSEYE_REPOSITORY_ID
        and str(base.get("sha") or "").lower() == base_sha
        and str(base.get("ref") or "") == base_ref
        and int(base_repo.get("id") or 0) == _CLAUSEYE_REPOSITORY_ID
    )


def _trusted_gate_attestation(
    repo: str,
    *,
    run_id: int,
    run_attempt: int,
    pr_number: int,
    candidate_sha: str,
    base_sha: str,
    base_ref: str,
    workflow_sha: str,
    cwd: Path,
) -> dict[str, Any]:
    """Download and validate the exact run-owned gate attestation artifact."""
    payload = _json(
        [
            str(TRUSTED_GH_PATH), "api", "-H", "Accept: application/vnd.github+json",
            f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100",
        ],
        cwd=cwd,
    )
    artifacts = payload.get("artifacts") if isinstance(payload, Mapping) else None
    matches = [
        item for item in artifacts or []
        if isinstance(item, Mapping)
        and item.get("name") == _CLAUSEYE_GATE_ATTESTATION_NAME
        and item.get("expired") is False
    ]
    if len(matches) != 1:
        raise LiveVerificationError(
            "trusted workflow must expose exactly one live gate attestation",
            code="autonomous_gate_attestation_missing",
        )
    artifact = matches[0]
    artifact_id = int(artifact.get("id") or 0)
    advertised_size = int(artifact.get("size_in_bytes") or 0)
    if (
        artifact_id <= 0
        or advertised_size <= 0
        or advertised_size > _MAX_ATTESTATION_ARCHIVE_BYTES
    ):
        raise LiveVerificationError(
            "trusted gate attestation metadata is invalid or oversized",
            code="autonomous_gate_attestation_invalid",
        )
    archive = _bytes(
        [
            str(TRUSTED_GH_PATH), "api", "-H", "Accept: application/vnd.github+json",
            f"repos/{repo}/actions/artifacts/{artifact_id}/zip",
        ],
        cwd=cwd,
    )
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = bundle.infolist()
            if len(entries) != 1:
                raise LiveVerificationError(
                    "trusted gate attestation archive must contain exactly one file",
                    code="autonomous_gate_attestation_invalid",
                )
            entry = entries[0]
            unix_mode = entry.external_attr >> 16
            if (
                entry.filename != "attestation.json"
                or entry.is_dir()
                or entry.flag_bits & 0x1
                or entry.file_size <= 0
                or entry.file_size > _MAX_ATTESTATION_JSON_BYTES
                or entry.compress_size > _MAX_ATTESTATION_ARCHIVE_BYTES
                or stat.S_ISLNK(unix_mode)
                or (stat.S_IFMT(unix_mode) not in (0, stat.S_IFREG))
            ):
                raise LiveVerificationError(
                    "trusted gate attestation archive entry is unsafe",
                    code="autonomous_gate_attestation_invalid",
                )
            raw = bundle.read(entry)
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise LiveVerificationError(
            "trusted gate attestation archive is invalid",
            code="autonomous_gate_attestation_invalid",
        ) from exc
    if len(raw) != entry.file_size or len(raw) > _MAX_ATTESTATION_JSON_BYTES:
        raise LiveVerificationError(
            "trusted gate attestation payload is oversized",
            code="autonomous_gate_attestation_invalid",
        )
    try:
        attestation = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveVerificationError(
            "trusted gate attestation JSON is invalid",
            code="autonomous_gate_attestation_invalid",
        ) from exc
    expected = {
        "schema": _CLAUSEYE_GATE_ATTESTATION_SCHEMA,
        "repository_id": _CLAUSEYE_REPOSITORY_ID,
        "repository": repo,
        "pull_request_number": pr_number,
        "candidate_sha": candidate_sha,
        "candidate_repository_id": _CLAUSEYE_REPOSITORY_ID,
        "base_sha": base_sha,
        "base_ref": base_ref,
        "workflow_path": _CLAUSEYE_GATE_WORKFLOW_PATH,
        "workflow_sha": workflow_sha,
        "workflow_run_id": run_id,
        "workflow_run_attempt": run_attempt,
        "event": "pull_request_target",
    }
    if not isinstance(attestation, dict) or attestation != expected:
        raise LiveVerificationError(
            "trusted gate attestation does not match the exact PR workflow run",
            code="autonomous_gate_attestation_mismatch",
        )
    return {
        "artifact_id": artifact_id,
        "name": _CLAUSEYE_GATE_ATTESTATION_NAME,
        "sha256": hashlib.sha256(raw).hexdigest(),
        **attestation,
    }


def _protected_control_plane_unchanged(
    repo: str,
    *,
    baseline_sha: str,
    merge_sha: str,
    cwd: Path,
) -> dict[str, Any]:
    """Prove deploy/merge trust code matches the immutable gate baseline."""

    def blobs_at(revision: str) -> dict[str, str]:
        payload = _json(
            [
                str(TRUSTED_GH_PATH), "api", "-H", "Accept: application/vnd.github+json",
                f"repos/{repo}/git/trees/{revision}?recursive=1",
            ],
            cwd=cwd,
        )
        if (
            not isinstance(payload, Mapping)
            or payload.get("truncated") is not False
            or not isinstance(payload.get("tree"), list)
        ):
            raise LiveVerificationError(
                "protected control-plane tree is incomplete",
                code="control_plane_tree_unverifiable",
            )
        result: dict[str, str] = {}
        for item in payload["tree"]:
            if not isinstance(item, Mapping) or item.get("type") != "blob":
                continue
            path = str(item.get("path") or "")
            protected = (
                path in _CLAUSEYE_PROTECTED_CONTROL_PATHS
                or any(
                    path.startswith(prefix)
                    for prefix in _CLAUSEYE_PROTECTED_CONTROL_PREFIXES
                )
            )
            if not protected:
                continue
            blob_sha = str(item.get("sha") or "").lower()
            if (
                path in result
                or not re.fullmatch(r"[0-9a-f]{40}", blob_sha)
            ):
                raise LiveVerificationError(
                    "protected control-plane tree contains invalid entries",
                    code="control_plane_tree_unverifiable",
                )
            result[path] = blob_sha
        missing = sorted(_CLAUSEYE_PROTECTED_CONTROL_PATHS - set(result))
        if missing:
            raise LiveVerificationError(
                "protected control-plane tree is missing required paths: "
                + ", ".join(missing[:5]),
                code="control_plane_tree_unverifiable",
            )
        return result

    baseline = blobs_at(baseline_sha)
    merged = blobs_at(merge_sha)
    if baseline != merged:
        changed = sorted(
            path for path in set(baseline) | set(merged)
            if baseline.get(path) != merged.get(path)
        )
        raise LiveVerificationError(
            "merged delivery changed protected merge/deploy control paths: "
            + ", ".join(changed[:8]),
            code="protected_control_plane_changed",
        )
    manifest = json.dumps(
        baseline, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return {
        "baseline_sha": baseline_sha,
        "merge_sha": merge_sha,
        "path_count": len(baseline),
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }


def _successful_autonomous_gate(
    repo: str,
    head_sha: str,
    pr_number: int,
    *,
    head_ref: str,
    base_ref: str,
    merge_sha: str,
    trusted_workflow_sha: str,
    cwd: Path,
) -> dict[str, Any]:
    """Verify the native, base-controlled required-workflow run.

    The exact-head custom check is useful correlation evidence but is not the
    trust boundary: same-repository candidate workflows share the GitHub
    Actions App identity. Authorization comes from the no-bypass organization
    ``workflows`` rule plus this successful ``pull_request_target`` run loaded
    from an immutable trusted workflow source.
    """
    merge_commit = _json(
        [str(TRUSTED_GH_PATH), "api", f"repos/{repo}/commits/{merge_sha}"], cwd=cwd,
    )
    parents = (
        merge_commit.get("parents") if isinstance(merge_commit, Mapping) else None
    )
    if (
        not isinstance(parents, list)
        or len(parents) != 1
        or not isinstance(parents[0], Mapping)
        or not re.fullmatch(r"[0-9a-f]{40}", str(parents[0].get("sha") or ""))
    ):
        raise LiveVerificationError(
            "autonomous delivery requires an exact squash merge parent",
            code="merge_parent_unverifiable",
        )
    base_sha = str(parents[0]["sha"]).lower()

    expected_external_prefix = (
        f"hermes-autonomy/v1:repo:{_CLAUSEYE_REPOSITORY_ID}:pr:{pr_number}:"
        f"candidate:{head_sha}:base:{base_sha}:workflow:{trusted_workflow_sha}:"
        "run:"
    )
    checks_payload = _json(
        [
            str(TRUSTED_GH_PATH), "api", "-H", "Accept: application/vnd.github+json",
            f"repos/{repo}/commits/{head_sha}/check-runs?filter=all&per_page=100",
        ],
        cwd=cwd,
    )
    checks = (
        checks_payload.get("check_runs")
        if isinstance(checks_payload, Mapping)
        else None
    )
    gate_checks = [
        check for check in checks or []
        if isinstance(check, Mapping)
        and check.get("name") == "Autonomous Merge Gate"
        and str(check.get("head_sha") or "").lower() == head_sha
        and isinstance(check.get("app"), Mapping)
        and check["app"].get("slug") == "github-actions"
        and int(check["app"].get("id") or 0) == _GITHUB_ACTIONS_APP_ID
    ]
    correlated: list[tuple[Mapping[str, Any], int, int]] = []
    external_pattern = re.compile(
        re.escape(expected_external_prefix) + r"([1-9]\d*):attempt:([1-9]\d*)"
    )
    for check in gate_checks:
        match = external_pattern.fullmatch(str(check.get("external_id") or ""))
        if (
            match is not None
            and int(check.get("id") or 0) > 0
            and check.get("status") == "completed"
            and check.get("conclusion") == "success"
        ):
            correlated.append((check, int(match.group(1)), int(match.group(2))))
    if len(correlated) != 1:
        if not correlated and any(
            check.get("status") == "completed"
            and check.get("conclusion") not in (None, "success")
            and external_pattern.fullmatch(str(check.get("external_id") or ""))
            for check in gate_checks
        ):
            raise LiveVerificationError(
                "trusted required-workflow run did not complete successfully",
                code="autonomous_required_workflow_failed",
            )
        raise LiveVerificationError(
            "trusted required-workflow run lacks one exact-head correlation check",
            code="autonomous_merge_gate_correlation_missing",
        )
    check, run_id, run_attempt = correlated[0]
    run = _json(
        [str(TRUSTED_GH_PATH), "api", f"repos/{repo}/actions/runs/{run_id}"],
        cwd=cwd,
    )
    repository = run.get("repository") if isinstance(run, Mapping) else None
    associations = run.get("pull_requests") if isinstance(run, Mapping) else None
    workflow_id = int(run.get("workflow_id") or 0) if isinstance(run, Mapping) else 0
    associations_match = (
        isinstance(associations, list)
        and (
            not associations
            or (
                len(associations) == 1
                and _association_matches(
                    associations[0],
                    pr_number=pr_number,
                    head_sha=head_sha,
                    head_ref=head_ref,
                    base_sha=base_sha,
                    base_ref=base_ref,
                )
            )
        )
    )
    if (
        isinstance(run, Mapping)
        and int(run.get("id") or 0) == run_id
        and int(run.get("run_attempt") or 0) == run_attempt
        and run.get("status") == "completed"
        and run.get("conclusion") not in (None, "success")
    ):
        raise LiveVerificationError(
            "trusted required-workflow run did not complete successfully",
            code="autonomous_required_workflow_failed",
        )
    if (
        not isinstance(run, Mapping)
        or int(run.get("id") or 0) != run_id
        or int(run.get("run_attempt") or 0) != run_attempt
        or workflow_id <= 0
        or run.get("workflow_url")
        != f"https://api.github.com/repos/{repo}/actions/required_workflows/{workflow_id}"
        or run.get("html_url") != f"https://github.com/{repo}/actions/runs/{run_id}"
        or run.get("name") != "Autonomous Merge Gate"
        or run.get("path") != _CLAUSEYE_GATE_WORKFLOW_PATH
        or run.get("event") != "pull_request_target"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or str(run.get("head_sha") or "").lower() != head_sha
        or str(run.get("head_branch") or "") != head_ref
        or not isinstance(repository, Mapping)
        or int(repository.get("id") or 0) != _CLAUSEYE_REPOSITORY_ID
        or str(repository.get("full_name") or "").casefold() != repo.casefold()
        # GitHub can elide run associations after the exact PR is merged; the
        # terminal caller has already bound that merge through the PR API.
        or not associations_match
    ):
        raise LiveVerificationError(
            "trusted required-workflow run does not match the exact PR/base/head",
            code="autonomous_required_workflow_missing",
        )
    attestation = _trusted_gate_attestation(
        repo,
        run_id=run_id,
        run_attempt=run_attempt,
        pr_number=pr_number,
        candidate_sha=head_sha,
        base_sha=base_sha,
        base_ref=base_ref,
        workflow_sha=trusted_workflow_sha,
        cwd=cwd,
    )
    expected_external_id = (
        f"{expected_external_prefix}{run_id}:attempt:{run_attempt}"
    )
    control_plane = _protected_control_plane_unchanged(
        repo,
        baseline_sha=trusted_workflow_sha,
        merge_sha=merge_sha,
        cwd=cwd,
    )
    return {
        "authoritative_source": "native_required_workflow",
        "workflow_run_id": run_id,
        "workflow_run_attempt": run_attempt,
        "workflow_id": workflow_id,
        "workflow_path": _CLAUSEYE_GATE_WORKFLOW_PATH,
        "workflow_source_sha": trusted_workflow_sha,
        "event": "pull_request_target",
        "candidate_sha": head_sha,
        "base_sha": base_sha,
        "check_run_id": int(check.get("id") or 0),
        "external_id": expected_external_id,
        "attestation": attestation,
        "protected_control_plane": control_plane,
    }


def _unresolved_review_threads(
    repo: str,
    pr_number: int,
    *,
    cwd: Path,
) -> int:
    owner, name = repo.split("/", 1)
    query = (
        "query($owner:String!,$name:String!,$number:Int!,$cursor:String){"
        "repository(owner:$owner,name:$name){pullRequest(number:$number){"
        "reviewThreads(first:100,after:$cursor){nodes{isResolved}"
        "pageInfo{hasNextPage endCursor}}}}}"
    )
    cursor: str | None = None
    unresolved = 0
    while True:
        argv = [
            str(TRUSTED_GH_PATH), "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}",
            "-F", f"name={name}",
            "-F", f"number={pr_number}",
        ]
        if cursor:
            argv.extend(["-F", f"cursor={cursor}"])
        data = _json(argv, cwd=cwd)
        try:
            threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = threads["nodes"]
            page = threads["pageInfo"]
        except (KeyError, TypeError) as exc:
            raise LiveVerificationError(
                "GitHub review thread state is not verifiable",
                code="review_threads_unverifiable",
            ) from exc
        unresolved += sum(
            1 for node in nodes
            if isinstance(node, dict) and node.get("isResolved") is not True
        )
        if not page.get("hasNextPage"):
            return unresolved
        cursor = str(page.get("endCursor") or "")
        if not cursor:
            raise LiveVerificationError(
                "GitHub review thread pagination is incomplete",
                code="review_threads_unverifiable",
            )


def _privileged_required_workflow_policy(
    repo: str,
    default_branch: str,
    *,
    cwd: Path,
) -> dict[str, Any]:
    """Verify the admin-only org workflow rule and its immutable source.

    This function belongs to the trusted attestation producer.  Worker-side
    delivery verification must never call it: both the source-access endpoint
    and organization ruleset endpoints require Administration authority.
    """
    source_access = _json(
        [str(TRUSTED_GH_PATH), "api", f"repos/{repo}/actions/permissions/access"],
        cwd=cwd,
    )
    if (
        not isinstance(source_access, Mapping)
        or source_access.get("access_level") != "organization"
    ):
        raise LiveVerificationError(
            "required-workflow source is not accessible to the organization",
            code="required_workflow_source_access_mismatch",
        )
    listing = _json(
        [str(TRUSTED_GH_PATH), "api", f"orgs/{_CLAUSEYE_ORGANIZATION}/rulesets?per_page=100"],
        cwd=cwd,
    )
    matches = [
        item for item in listing or []
        if isinstance(item, Mapping)
        and item.get("name") == _CLAUSEYE_REQUIRED_WORKFLOW_RULESET
    ] if isinstance(listing, list) else []
    if len(matches) != 1 or int(matches[0].get("id") or 0) <= 0:
        raise LiveVerificationError(
            "canonical organization required-workflow ruleset is ambiguous or missing",
            code="required_workflow_ruleset_missing",
        )
    ruleset_id = int(matches[0]["id"])
    ruleset = _json(
        [str(TRUSTED_GH_PATH), "api", f"orgs/{_CLAUSEYE_ORGANIZATION}/rulesets/{ruleset_id}"],
        cwd=cwd,
    )
    if not isinstance(ruleset, Mapping):
        raise LiveVerificationError(
            "organization required-workflow ruleset is not verifiable",
            code="required_workflow_ruleset_unverifiable",
        )
    conditions = (
        ruleset.get("conditions")
        if isinstance(ruleset.get("conditions"), Mapping)
        else {}
    )
    repository_ids = (
        (conditions.get("repository_id") or {}).get("repository_ids")
        if isinstance(conditions.get("repository_id"), Mapping)
        else None
    )
    included_refs = (
        (conditions.get("ref_name") or {}).get("include")
        if isinstance(conditions.get("ref_name"), Mapping)
        else None
    )
    rules = ruleset.get("rules") if isinstance(ruleset.get("rules"), list) else []
    pinned = [
        workflow
        for rule in rules
        if (
            isinstance(rule, Mapping)
            and rule.get("type") == "workflows"
            and isinstance(rule.get("parameters"), Mapping)
            and rule["parameters"].get("do_not_enforce_on_create") is False
        )
        for workflow in ((
            rule["parameters"].get("workflows")
        ) or [])
        if isinstance(workflow, Mapping)
        and int(workflow.get("repository_id") or 0) == _CLAUSEYE_REPOSITORY_ID
        and workflow.get("path") == _CLAUSEYE_GATE_WORKFLOW_PATH
        and re.fullmatch(r"[0-9a-f]{40}", str(workflow.get("sha") or "").lower())
    ]
    workflow_shas = {
        str(workflow.get("sha") or "").lower() for workflow in pinned
    }
    if (
        ruleset.get("name") != _CLAUSEYE_REQUIRED_WORKFLOW_RULESET
        or ruleset.get("target") != "branch"
        or ruleset.get("enforcement") != "active"
        or ruleset.get("bypass_actors") != []
        or _CLAUSEYE_REPOSITORY_ID not in (repository_ids or [])
        or f"refs/heads/{default_branch}" not in (included_refs or [])
        or len(pinned) != 1
        or len(workflow_shas) != 1
    ):
        raise LiveVerificationError(
            "organization required-workflow policy is not active, pinned, and no-bypass",
            code="required_workflow_policy_mismatch",
        )
    workflow_sha = next(iter(workflow_shas))
    return {
        "ruleset_id": ruleset_id,
        "ruleset_name": _CLAUSEYE_REQUIRED_WORKFLOW_RULESET,
        "workflow_id": _CLAUSEYE_GATE_WORKFLOW_ID,
        "workflow_path": _CLAUSEYE_GATE_WORKFLOW_PATH,
        "workflow_source_sha": workflow_sha,
        "source_access_level": "organization",
        "bypass_actors": 0,
    }


def _trusted_main_ruleset_template(
    repo: str,
    workflow_source_sha: str,
    *,
    cwd: Path,
) -> Mapping[str, Any]:
    """Load the protected canonical main ruleset at the pinned gate SHA."""
    payload = _json(
        [
            str(TRUSTED_GH_PATH), "api", "-H", "Accept: application/vnd.github+json",
            f"repos/{repo}/contents/{_CLAUSEYE_MAIN_RULESET_TEMPLATE}"
            f"?ref={workflow_source_sha}",
        ],
        cwd=cwd,
    )
    if (
        not isinstance(payload, Mapping)
        or payload.get("type") != "file"
        or payload.get("encoding") != "base64"
        or int(payload.get("size") or 0) <= 0
        or int(payload.get("size") or 0) > 128 * 1024
        or not isinstance(payload.get("content"), str)
    ):
        raise LiveVerificationError(
            "trusted main ruleset template is unavailable or oversized",
            code="ruleset_template_unverifiable",
        )
    try:
        raw = base64.b64decode(
            payload["content"].replace("\n", ""), validate=True,
        )
        template = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveVerificationError(
            "trusted main ruleset template is invalid",
            code="ruleset_template_unverifiable",
        ) from exc
    if len(raw) != int(payload["size"]) or not isinstance(template, Mapping):
        raise LiveVerificationError(
            "trusted main ruleset template does not match its GitHub blob",
            code="ruleset_template_unverifiable",
        )
    return template


def _ruleset_policy_shape(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalize only the security-relevant ruleset policy contract."""
    try:
        conditions = value["conditions"]["ref_name"]
        rules = value["rules"]
        if not isinstance(conditions, Mapping) or not isinstance(rules, list):
            return None
        by_type: dict[str, list[Mapping[str, Any]]] = {}
        for rule in rules:
            if not isinstance(rule, Mapping) or not isinstance(rule.get("type"), str):
                return None
            by_type.setdefault(str(rule["type"]), []).append(rule)
        expected_types = {
            "deletion",
            "non_fast_forward",
            "required_linear_history",
            "pull_request",
            "required_status_checks",
        }
        if set(by_type) != expected_types or any(
            len(items) != 1 for items in by_type.values()
        ):
            return None
        pull = by_type["pull_request"][0].get("parameters")
        status = by_type["required_status_checks"][0].get("parameters")
        if not isinstance(pull, Mapping) or not isinstance(status, Mapping):
            return None
        checks = status.get("required_status_checks")
        if not isinstance(checks, list) or not checks:
            return None
        normalized_checks: list[tuple[str, int]] = []
        for check in checks:
            if not isinstance(check, Mapping):
                return None
            context = str(check.get("context") or "")
            integration_id = int(check.get("integration_id") or 0)
            if not context or integration_id <= 0:
                return None
            normalized_checks.append((context, integration_id))
        if len(set(normalized_checks)) != len(normalized_checks):
            return None
        return {
            "name": value.get("name"),
            "target": value.get("target"),
            "enforcement": value.get("enforcement"),
            "bypass_actors": value.get("bypass_actors"),
            "include": conditions.get("include"),
            "exclude": conditions.get("exclude"),
            "rule_types": sorted(by_type),
            "pull_request": {
                key: pull.get(key)
                for key in (
                    "dismiss_stale_reviews_on_push",
                    "require_code_owner_review",
                    "require_last_push_approval",
                    "required_approving_review_count",
                    "required_review_thread_resolution",
                    "allowed_merge_methods",
                )
            },
            "strict": status.get("strict_required_status_checks_policy"),
            "required_status_checks": sorted(normalized_checks),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _canonical_policy_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the one canonical byte representation used for signatures."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _policy_shape_sha256(shape: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_policy_bytes(shape)).hexdigest()


def _policy_attestation_path() -> Path:
    """Return the fixed producer-owned cache path."""
    return _POLICY_ATTESTATION_PATH


def _open_policy_attestation_directory() -> int:
    """Open the fixed cache directory without following any path symlink."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("safe delivery-policy cache access is unsupported")
    system_root = _POLICY_ATTESTATION_SYSTEM_ROOT
    directory = _POLICY_ATTESTATION_DIRECTORY
    path = _policy_attestation_path()
    if (
        not system_root.is_absolute()
        or not directory.is_absolute()
        or not path.is_absolute()
        or path.parent != directory
        or path.name != "clauseye-production.json"
    ):
        raise OSError("delivery-policy cache path is not fixed")
    if (
        Path(os.path.realpath(system_root)) != system_root
        or Path(os.path.realpath(directory)) != directory
    ):
        raise OSError("delivery-policy cache path contains a symlink")
    try:
        relative_parts = directory.relative_to(system_root).parts
    except ValueError as exc:
        raise OSError("delivery-policy cache escapes its system root") from exc
    if not relative_parts:
        raise OSError("delivery-policy cache directory is ambiguous")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(system_root, flags)
    try:
        root_stat = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != _POLICY_ATTESTATION_OWNER_UID
            or root_stat.st_gid != _POLICY_ATTESTATION_OWNER_GID
            or stat.S_IMODE(root_stat.st_mode) & 0o022
        ):
            raise OSError("delivery-policy system root is not trusted")
        for part in relative_parts:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            try:
                directory_stat = os.fstat(next_descriptor)
                if (
                    not stat.S_ISDIR(directory_stat.st_mode)
                    or directory_stat.st_uid != _POLICY_ATTESTATION_OWNER_UID
                    or directory_stat.st_gid != _POLICY_ATTESTATION_OWNER_GID
                    or stat.S_IMODE(directory_stat.st_mode)
                    != _POLICY_ATTESTATION_DIRECTORY_MODE
                ):
                    raise OSError(
                        "delivery-policy cache directory is not producer-owned"
                    )
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validate_policy_attestation_file(file_stat: os.stat_result) -> None:
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid != _POLICY_ATTESTATION_OWNER_UID
        or file_stat.st_gid != _POLICY_ATTESTATION_OWNER_GID
        or stat.S_IMODE(file_stat.st_mode) != _POLICY_ATTESTATION_FILE_MODE
        or file_stat.st_nlink != 1
        or file_stat.st_size <= 0
        or file_stat.st_size > _POLICY_ATTESTATION_MAX_BYTES
    ):
        raise OSError("delivery-policy cache file is not producer-owned")


def _policy_public_key_bytes() -> bytes:
    encoded = _CLAUSEYE_POLICY_ATTESTATION_PUBLIC_KEY_B64.strip()
    if not encoded:
        raise LiveVerificationError(
            "trusted delivery-policy producer key is not provisioned",
            code="delivery_policy_trust_unconfigured",
        )
    try:
        value = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise LiveVerificationError(
            "trusted delivery-policy producer key is invalid",
            code="delivery_policy_trust_invalid",
        ) from exc
    if len(value) != 32:
        raise LiveVerificationError(
            "trusted delivery-policy producer key is invalid",
            code="delivery_policy_trust_invalid",
        )
    return value


def _parse_policy_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise LiveVerificationError(
            "delivery-policy attestation timestamp is invalid",
            code="delivery_policy_attestation_invalid",
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise LiveVerificationError(
            "delivery-policy attestation timestamp is invalid",
            code="delivery_policy_attestation_invalid",
        ) from exc
    if parsed.tzinfo is None:
        raise LiveVerificationError(
            "delivery-policy attestation timestamp is invalid",
            code="delivery_policy_attestation_invalid",
        )
    return parsed.astimezone(timezone.utc)


def _policy_integer(value: Any) -> int | None:
    """Accept JSON integers without treating booleans as numeric claims."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _validate_policy_payload(
    payload: Mapping[str, Any],
    *,
    repo: str,
    default_branch: str,
    now: datetime,
) -> None:
    expected_keys = {
        "schema",
        "issuer",
        "issued_at",
        "expires_at",
        "repository",
        "repository_id",
        "organization",
        "default_branch",
        "main_ruleset",
        "required_workflow",
    }
    if set(payload) != expected_keys or (
        payload.get("schema") != _POLICY_ATTESTATION_SCHEMA
        or payload.get("issuer") != _POLICY_ATTESTATION_ISSUER
        or str(payload.get("repository") or "").casefold() != repo.casefold()
        or _policy_integer(payload.get("repository_id"))
        != _CLAUSEYE_REPOSITORY_ID
        or payload.get("organization") != _CLAUSEYE_ORGANIZATION
        or payload.get("default_branch") != default_branch
    ):
        raise LiveVerificationError(
            "delivery-policy attestation is not bound to this repository and branch",
            code="delivery_policy_attestation_mismatch",
        )

    issued_at = _parse_policy_timestamp(payload.get("issued_at"))
    expires_at = _parse_policy_timestamp(payload.get("expires_at"))
    if (
        expires_at <= issued_at
        or (expires_at - issued_at).total_seconds()
        > _POLICY_ATTESTATION_MAX_TTL_SECONDS
        or issued_at
        > now + timedelta(seconds=_POLICY_ATTESTATION_CLOCK_SKEW_SECONDS)
        or expires_at <= now
    ):
        raise LiveVerificationError(
            "delivery-policy attestation is expired or outside its freshness window",
            code="delivery_policy_attestation_stale",
        )

    main = payload.get("main_ruleset")
    workflow = payload.get("required_workflow")
    if not isinstance(main, Mapping) or set(main) != {
        "ruleset_id",
        "ruleset_name",
        "policy_sha256",
        "required_checks",
        "bypass_actors",
    }:
        raise LiveVerificationError(
            "delivery-policy main-ruleset claim is invalid",
            code="delivery_policy_attestation_invalid",
        )
    checks = main.get("required_checks")
    normalized_checks: list[tuple[str, int]] = []
    if isinstance(checks, list):
        for item in checks:
            if not isinstance(item, Mapping) or set(item) != {
                "context", "integration_id",
            }:
                normalized_checks = []
                break
            context = item.get("context")
            integration_id = _policy_integer(item.get("integration_id"))
            if (
                not isinstance(context, str)
                or not context
                or integration_id is None
                or integration_id <= 0
            ):
                normalized_checks = []
                break
            normalized_checks.append((context, integration_id))
    if (
        _policy_integer(main.get("ruleset_id")) != _CLAUSEYE_MAIN_RULESET_ID
        or main.get("ruleset_name") != "main-protection"
        or main.get("bypass_actors") != 0
        or not re.fullmatch(r"[0-9a-f]{64}", str(main.get("policy_sha256") or ""))
        or not normalized_checks
        or normalized_checks != sorted(set(normalized_checks))
        or any(
            integration_id != _GITHUB_ACTIONS_APP_ID
            for _, integration_id in normalized_checks
        )
        or "Autonomous Merge Gate"
        not in {context for context, _ in normalized_checks}
    ):
        raise LiveVerificationError(
            "delivery-policy main-ruleset claim is invalid",
            code="delivery_policy_attestation_invalid",
        )

    if not isinstance(workflow, Mapping) or set(workflow) != {
        "ruleset_id",
        "ruleset_name",
        "workflow_id",
        "workflow_path",
        "workflow_source_sha",
        "source_access_level",
        "bypass_actors",
    } or (
        (_policy_integer(workflow.get("ruleset_id")) or 0) <= 0
        or workflow.get("ruleset_name") != _CLAUSEYE_REQUIRED_WORKFLOW_RULESET
        or _policy_integer(workflow.get("workflow_id"))
        != _CLAUSEYE_GATE_WORKFLOW_ID
        or workflow.get("workflow_path") != _CLAUSEYE_GATE_WORKFLOW_PATH
        or not re.fullmatch(
            r"[0-9a-f]{40}", str(workflow.get("workflow_source_sha") or "")
        )
        or workflow.get("source_access_level") != "organization"
        or workflow.get("bypass_actors") != 0
    ):
        raise LiveVerificationError(
            "delivery-policy required-workflow claim is invalid",
            code="delivery_policy_attestation_invalid",
        )


def _policy_claim_sha256(payload: Mapping[str, Any]) -> str:
    """Digest every signed policy claim except rotation timestamps."""
    immutable = {
        key: value
        for key, value in payload.items()
        if key not in {"issued_at", "expires_at"}
    }
    return hashlib.sha256(_canonical_policy_bytes(immutable)).hexdigest()


def _verify_policy_attestation_envelope(
    envelope: Any,
    repo: str,
    default_branch: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(envelope, Mapping) or set(envelope) != {
        "key_id", "payload", "signature",
    } or not isinstance(envelope.get("payload"), Mapping):
        raise LiveVerificationError(
            "delivery-policy attestation envelope is invalid",
            code="delivery_policy_attestation_invalid",
        )

    public_bytes = _policy_public_key_bytes()
    key_id = hashlib.sha256(public_bytes).hexdigest()[:24]
    if envelope.get("key_id") != key_id:
        raise LiveVerificationError(
            "delivery-policy attestation key does not match the pinned producer",
            code="delivery_policy_attestation_signature_invalid",
        )
    try:
        signature = base64.b64decode(str(envelope.get("signature") or ""), validate=True)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature,
            _canonical_policy_bytes(envelope["payload"]),
        )
    except Exception as exc:
        raise LiveVerificationError(
            "delivery-policy attestation signature is invalid",
            code="delivery_policy_attestation_signature_invalid",
        ) from exc

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    payload = dict(envelope["payload"])
    _validate_policy_payload(
        payload,
        repo=repo,
        default_branch=default_branch,
        now=current.astimezone(timezone.utc),
    )
    return {
        "key_id": key_id,
        "payload": payload,
        "policy_claim_sha256": _policy_claim_sha256(payload),
    }


def _load_policy_attestation(
    repo: str,
    default_branch: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify and return one fresh control-plane policy attestation."""
    path = _policy_attestation_path()
    directory_descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        directory_descriptor = _open_policy_attestation_directory()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(
            path.name,
            flags,
            dir_fd=directory_descriptor,
        )
        file_stat = os.fstat(file_descriptor)
        _validate_policy_attestation_file(file_stat)
        with os.fdopen(file_descriptor, "rb") as handle:
            file_descriptor = None
            raw = handle.read(_POLICY_ATTESTATION_MAX_BYTES + 1)
        if len(raw) != file_stat.st_size:
            raise OSError("attestation changed while it was read")
    except FileNotFoundError as exc:
        raise LiveVerificationError(
            "fresh trusted delivery-policy attestation is unavailable",
            code="delivery_policy_attestation_missing",
        ) from exc
    except OSError as exc:
        raise LiveVerificationError(
            "trusted delivery-policy cache ownership or mode is unsafe",
            code="delivery_policy_cache_untrusted",
        ) from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
    try:
        envelope = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveVerificationError(
            "delivery-policy attestation JSON is invalid",
            code="delivery_policy_attestation_invalid",
        ) from exc
    return _verify_policy_attestation_envelope(
        envelope,
        repo,
        default_branch,
        now=now,
    )


def _privileged_main_ruleset_policy(
    repo: str,
    default_branch: str,
    workflow_source_sha: str,
    *,
    cwd: Path,
) -> dict[str, Any]:
    """Collect and validate admin-only live main-ruleset policy."""
    ruleset = _json(
        [str(TRUSTED_GH_PATH), "api", f"repos/{repo}/rulesets/{_CLAUSEYE_MAIN_RULESET_ID}"],
        cwd=cwd,
    )
    if not isinstance(ruleset, Mapping):
        raise LiveVerificationError(
            "main branch ruleset is not verifiable", code="ruleset_unverifiable"
        )
    if ruleset.get("bypass_actors"):
        raise LiveVerificationError(
            "main branch ruleset still permits bypass actors",
            code="ruleset_bypass_enabled",
        )
    raw_rules = ruleset.get("rules") if isinstance(ruleset.get("rules"), list) else []
    raw_status = next(
        (
            item for item in raw_rules
            if isinstance(item, Mapping)
            and item.get("type") == "required_status_checks"
        ),
        None,
    )
    raw_parameters = (
        raw_status.get("parameters") if isinstance(raw_status, Mapping) else {}
    )
    raw_checks = (
        raw_parameters.get("required_status_checks")
        if isinstance(raw_parameters, Mapping)
        else None
    )
    if isinstance(raw_checks, list) and any(
        not isinstance(item, Mapping)
        or int(item.get("integration_id") or 0) != _GITHUB_ACTIONS_APP_ID
        for item in raw_checks
    ):
        raise LiveVerificationError(
            "required checks are not pinned to the GitHub Actions integration",
            code="ruleset_check_integration_unbound",
        )
    shape = _ruleset_policy_shape(ruleset)
    if (
        int(ruleset.get("id") or 0) != _CLAUSEYE_MAIN_RULESET_ID
        or ruleset.get("name") != "main-protection"
        or ruleset.get("target") != "branch"
        or ruleset.get("enforcement") != "active"
        or ruleset.get("bypass_actors") != []
        or shape is None
        or f"refs/heads/{default_branch}" not in (shape.get("include") or [])
        or shape.get("strict") is not True
        or shape.get("pull_request", {}).get(
            "required_review_thread_resolution"
        ) is not True
        or shape.get("pull_request", {}).get("allowed_merge_methods") != ["squash"]
    ):
        raise LiveVerificationError(
            "main branch ruleset is not strict, active, and no-bypass",
            code="ruleset_policy_mismatch",
        )
    required_checks = [
        {"context": str(context), "integration_id": int(integration_id)}
        for context, integration_id in shape["required_status_checks"]
    ]
    if any(
        item["integration_id"] != _GITHUB_ACTIONS_APP_ID
        for item in required_checks
    ):
        raise LiveVerificationError(
            "required checks are not pinned to the GitHub Actions integration",
            code="ruleset_check_integration_unbound",
        )
    if "Autonomous Merge Gate" not in {
        item["context"] for item in required_checks
    }:
        raise LiveVerificationError(
            "Autonomous Merge Gate is not required by the live main ruleset",
            code="autonomous_merge_gate_not_enforced",
        )
    template = _trusted_main_ruleset_template(
        repo, workflow_source_sha, cwd=cwd,
    )
    template_shape = _ruleset_policy_shape(template)
    if template_shape is None or shape != template_shape:
        raise LiveVerificationError(
            "live main ruleset differs from the protected canonical template",
            code="ruleset_template_mismatch",
        )
    return {
        "ruleset_id": _CLAUSEYE_MAIN_RULESET_ID,
        "ruleset_name": "main-protection",
        "policy_sha256": _policy_shape_sha256(shape),
        "required_checks": required_checks,
        "bypass_actors": 0,
    }


def produce_clauseye_policy_attestation(
    repo: str,
    default_branch: str,
    *,
    cwd: Path,
    private_key_pem: bytes,
    now: datetime | None = None,
    ttl_seconds: int = 5 * 60,
) -> dict[str, Any]:
    """Admin-side producer interface for one signed policy snapshot.

    The caller is the trusted control plane.  It supplies the private key
    explicitly; the key is never read from worker environment or Kanban state.
    """
    if repo.casefold() != _CLAUSEYE_REPOSITORY or default_branch != "main":
        raise LiveVerificationError(
            "no trusted policy producer is registered for this target",
            code="repository_verifier_unregistered",
        )
    if ttl_seconds <= 0 or ttl_seconds > _POLICY_ATTESTATION_MAX_TTL_SECONDS:
        raise LiveVerificationError(
            "delivery-policy attestation TTL is invalid",
            code="delivery_policy_attestation_invalid",
        )
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        private_key = serialization.load_pem_private_key(
            private_key_pem, password=None,
        )
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError("not Ed25519")
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    except Exception as exc:
        raise LiveVerificationError(
            "trusted delivery-policy producer key is invalid",
            code="delivery_policy_signing_key_invalid",
        ) from exc
    if public_bytes != _policy_public_key_bytes():
        raise LiveVerificationError(
            "delivery-policy signing key does not match the pinned producer",
            code="delivery_policy_signing_key_mismatch",
        )

    issued = now or datetime.now(timezone.utc)
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    issued = issued.astimezone(timezone.utc).replace(microsecond=0)
    expires = issued + timedelta(seconds=ttl_seconds)
    required_workflow = _privileged_required_workflow_policy(
        repo, default_branch, cwd=cwd,
    )
    main_ruleset = _privileged_main_ruleset_policy(
        repo,
        default_branch,
        str(required_workflow["workflow_source_sha"]),
        cwd=cwd,
    )
    payload = {
        "schema": _POLICY_ATTESTATION_SCHEMA,
        "issuer": _POLICY_ATTESTATION_ISSUER,
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "repository": repo.lower(),
        "repository_id": _CLAUSEYE_REPOSITORY_ID,
        "organization": _CLAUSEYE_ORGANIZATION,
        "default_branch": default_branch,
        "main_ruleset": main_ruleset,
        "required_workflow": required_workflow,
    }
    signature = private_key.sign(_canonical_policy_bytes(payload))
    return {
        "key_id": hashlib.sha256(public_bytes).hexdigest()[:24],
        "payload": payload,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def publish_clauseye_policy_attestation(
    envelope: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Durably publish one verified envelope from the trusted root helper.

    The signing service supplies a public, already-signed envelope.  This
    helper holds no GitHub or signing credential; it only owns the fixed cache
    directory.  Provisioning must create that directory with the ownership and
    mode pinned above before this interface is activated.
    """
    verified = _verify_policy_attestation_envelope(
        envelope,
        _CLAUSEYE_REPOSITORY,
        "main",
        now=now,
    )
    try:
        raw = _canonical_policy_bytes(dict(envelope))
    except (TypeError, ValueError) as exc:
        raise LiveVerificationError(
            "delivery-policy attestation cannot be serialized",
            code="delivery_policy_attestation_invalid",
        ) from exc
    if not raw or len(raw) > _POLICY_ATTESTATION_MAX_BYTES:
        raise LiveVerificationError(
            "delivery-policy attestation is oversized",
            code="delivery_policy_attestation_invalid",
        )

    path = _policy_attestation_path()
    directory_descriptor: int | None = None
    file_descriptor: int | None = None
    temporary_name: str | None = None
    temporary_created = False
    try:
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise OSError("safe delivery-policy publication is unsupported")
        if (
            os.geteuid() != _POLICY_ATTESTATION_OWNER_UID
            or os.getegid() != _POLICY_ATTESTATION_OWNER_GID
        ):
            raise OSError("publisher is not the pinned cache owner")
        directory_descriptor = _open_policy_attestation_directory()
        try:
            existing = os.stat(
                path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            _validate_policy_attestation_file(existing)

        temporary_name = (
            f".{path.name}.{os.getpid()}.{secrets.token_hex(16)}.tmp"
        )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        file_descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        temporary_created = True
        view = memoryview(raw)
        while view:
            written = os.write(file_descriptor, view)
            if written <= 0:
                raise OSError("short delivery-policy cache write")
            view = view[written:]
        os.fchmod(file_descriptor, _POLICY_ATTESTATION_FILE_MODE)
        os.fsync(file_descriptor)
        _validate_policy_attestation_file(os.fstat(file_descriptor))
        os.close(file_descriptor)
        file_descriptor = None

        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        temporary_name = None
        temporary_created = False
        os.fsync(directory_descriptor)
        published = os.stat(
            path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        _validate_policy_attestation_file(published)
    except OSError as exc:
        raise LiveVerificationError(
            "trusted delivery-policy attestation could not be published",
            code="delivery_policy_attestation_publish_failed",
        ) from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if (
            temporary_created
            and temporary_name is not None
            and directory_descriptor is not None
        ):
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except OSError:
                pass
        if directory_descriptor is not None:
            os.close(directory_descriptor)

    payload = verified["payload"]
    return {
        "path": str(path),
        "key_id": verified["key_id"],
        "policy_claim_sha256": verified["policy_claim_sha256"],
        "issued_at": payload["issued_at"],
        "expires_at": payload["expires_at"],
    }


def _required_workflow_policy(
    repo: str,
    default_branch: str,
    *,
    required_contexts: list[str],
    attested: Mapping[str, Any],
    cwd: Path,
) -> dict[str, Any]:
    """Verify narrow-token applied rules against the signed admin snapshot."""
    payload = attested.get("payload")
    workflow = (
        payload.get("required_workflow")
        if isinstance(payload, Mapping)
        else None
    )
    if not isinstance(workflow, Mapping):
        raise LiveVerificationError(
            "trusted required-workflow policy claim is unavailable",
            code="delivery_policy_attestation_invalid",
        )
    applied = _json(
        [
            str(TRUSTED_GH_PATH), "api", "-H", "Accept: application/vnd.github+json",
            "-H", "X-GitHub-Api-Version: 2026-03-10",
            f"repos/{repo}/rules/branches/{default_branch}",
        ],
        cwd=cwd,
    )
    if not isinstance(applied, list):
        raise LiveVerificationError(
            "applied branch rules are not verifiable",
            code="applied_rules_unverifiable",
        )
    applied_pull = any(
        isinstance(rule, Mapping)
        and rule.get("type") == "pull_request"
        and isinstance(rule.get("parameters"), Mapping)
        and rule["parameters"].get("required_review_thread_resolution") is True
        for rule in applied
    )
    applied_status_contexts: set[str] = set()
    applied_strict = False
    workflow_matches: list[Mapping[str, Any]] = []
    for rule in applied:
        if not isinstance(rule, Mapping):
            continue
        parameters = (
            rule.get("parameters")
            if isinstance(rule.get("parameters"), Mapping)
            else {}
        )
        if rule.get("type") == "required_status_checks":
            applied_strict = (
                applied_strict
                or parameters.get("strict_required_status_checks_policy") is True
            )
            for check in parameters.get("required_status_checks") or []:
                if isinstance(check, Mapping) and str(check.get("context") or ""):
                    applied_status_contexts.add(str(check["context"]))
        elif (
            rule.get("type") == "workflows"
            and parameters.get("do_not_enforce_on_create") is False
        ):
            for candidate in parameters.get("workflows") or []:
                if (
                    isinstance(candidate, Mapping)
                    and int(candidate.get("repository_id") or 0)
                    == _CLAUSEYE_REPOSITORY_ID
                    and candidate.get("path") == _CLAUSEYE_GATE_WORKFLOW_PATH
                    and str(candidate.get("sha") or "").lower()
                    == str(workflow.get("workflow_source_sha") or "").lower()
                ):
                    workflow_matches.append(candidate)
    if (
        not applied_pull
        or not applied_strict
        or not set(required_contexts).issubset(applied_status_contexts)
    ):
        raise LiveVerificationError(
            "strict checks and review-thread resolution are not applied to main",
            code="applied_rules_policy_mismatch",
        )
    if len(workflow_matches) != 1:
        raise LiveVerificationError(
            "attested required workflow is not actively applied to main",
            code="required_workflow_rule_missing",
        )
    return {
        **dict(workflow),
        "policy_attestation": {
            "key_id": attested.get("key_id"),
            "issued_at": payload.get("issued_at"),
            "expires_at": payload.get("expires_at"),
        },
    }


def _required_ruleset_evidence(
    repo: str,
    default_branch: str,
    head_sha: str,
    pr_number: int,
    *,
    cwd: Path,
) -> dict[str, Any]:
    """Verify worker-visible policy against a fresh signed admin snapshot."""
    attested = _load_policy_attestation(
        repo,
        default_branch,
    )
    payload = attested["payload"]
    main = payload["main_ruleset"]
    contexts = [
        str(item["context"]) for item in main["required_checks"]
    ]
    required_workflow = _required_workflow_policy(
        repo,
        default_branch,
        required_contexts=contexts,
        attested=attested,
        cwd=cwd,
    )
    template = _trusted_main_ruleset_template(
        repo,
        str(required_workflow.get("workflow_source_sha") or ""),
        cwd=cwd,
    )
    template_shape = _ruleset_policy_shape(template)
    if (
        template_shape is None
        or _policy_shape_sha256(template_shape) != main["policy_sha256"]
    ):
        raise LiveVerificationError(
            "protected canonical template differs from the attested live policy",
            code="ruleset_template_mismatch",
        )

    check_data = _json(
        [
            str(TRUSTED_GH_PATH), "api", "-H", "Accept: application/vnd.github+json",
            f"repos/{repo}/commits/{head_sha}/check-runs?filter=all&per_page=100",
        ],
        cwd=cwd,
    )
    checks = check_data.get("check_runs") if isinstance(check_data, dict) else []
    verified_checks: dict[str, int] = {}
    for context in contexts:
        candidates = [
            check for check in checks
            if isinstance(check, dict)
            and check.get("name") == context
            and str(check.get("head_sha") or "").lower() == head_sha
            and isinstance(check.get("app"), dict)
            and check["app"].get("slug") == "github-actions"
            and int(check["app"].get("id") or 0) == _GITHUB_ACTIONS_APP_ID
        ]
        if not candidates:
            raise LiveVerificationError(
                f"required exact-head check is missing: {context}",
                code="required_check_missing",
            )
        latest = max(candidates, key=lambda check: int(check.get("id") or 0))
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            raise LiveVerificationError(
                f"required exact-head check did not succeed: {context}",
                code="required_check_failed",
            )
        verified_checks[context] = int(latest.get("id") or 0)

    unresolved = _unresolved_review_threads(repo, pr_number, cwd=cwd)
    if unresolved:
        raise LiveVerificationError(
            f"PR still has {unresolved} unresolved review thread(s)",
            code="review_threads_unresolved",
        )
    return {
        "ruleset_id": _CLAUSEYE_MAIN_RULESET_ID,
        "ruleset_name": "main-protection",
        "policy_sha256": main["policy_sha256"],
        "policy_key_id": attested["key_id"],
        "policy_claim_sha256": attested["policy_claim_sha256"],
        "policy_attested_at": payload["issued_at"],
        "policy_expires_at": payload["expires_at"],
        "strict": True,
        "bypass_actors": 0,
        "review_threads_unresolved": 0,
        "required_checks": verified_checks,
        "required_workflow": required_workflow,
    }


def _revalidate_terminal_policy(
    repo: str,
    default_branch: str,
    expected_claim_sha256: str,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Require a fresh final envelope with the same immutable policy claims."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_claim_sha256):
        raise LiveVerificationError(
            "initial delivery-policy claim digest is unavailable",
            code="delivery_policy_attestation_invalid",
        )
    current = _load_policy_attestation(
        repo,
        default_branch,
        now=now,
    )
    observed = str(current["policy_claim_sha256"])
    if not secrets.compare_digest(observed, expected_claim_sha256):
        raise LiveVerificationError(
            "delivery policy changed during terminal verification",
            code="delivery_policy_attestation_changed",
        )
    payload = current["payload"]
    return {
        "key_id": str(current["key_id"]),
        "policy_claim_sha256": observed,
        "issued_at": str(payload["issued_at"]),
        "expires_at": str(payload["expires_at"]),
    }


def _variables(repo: str, environment: str, *, cwd: Path) -> dict[str, str]:
    args = [str(TRUSTED_GH_PATH), "variable", "list", "--repo", repo]
    if environment:
        args.extend(["--env", environment])
    args.extend(["--json", "name,value"])
    values = _json(args, cwd=cwd)
    if not isinstance(values, list):
        return {}
    return {
        str(item.get("name")): str(item.get("value"))
        for item in values
        if isinstance(item, dict) and item.get("name") and item.get("value")
    }


def _verify_clauseye_production(
    repo: str,
    candidate_sha: str,
    merge_sha: str,
    deployment: Mapping[str, Any],
    *,
    pr_number: int,
    merged_at: Any,
    trusted_control_sha: str,
    cwd: Path,
) -> dict[str, Any]:
    run_id = int(deployment["workflow_run_id"])
    run = _json([str(TRUSTED_GH_PATH), "api", f"repos/{repo}/actions/runs/{run_id}"], cwd=cwd)
    if not isinstance(run, dict):
        raise LiveVerificationError(
            "deployment workflow run is not verifiable", code="deployment_run_unverifiable"
        )
    repository = run.get("repository") if isinstance(run.get("repository"), dict) else {}
    head_repository = (
        run.get("head_repository")
        if isinstance(run.get("head_repository"), dict)
        else {}
    )
    workflow_source_sha = str(run.get("head_sha") or "").lower()
    if (
        int(run.get("id") or 0) != run_id
        or run.get("name") != "Auto Deploy Production"
        or run.get("path") != _CLAUSEYE_DEPLOY_WORKFLOW_PATH
        or int(run.get("workflow_id") or 0) != _CLAUSEYE_DEPLOY_WORKFLOW_ID
        or run.get("event") != "workflow_run"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("head_branch") != "main"
        or not re.fullmatch(r"[0-9a-f]{40}", workflow_source_sha)
        or int(repository.get("id") or 0) != _CLAUSEYE_REPOSITORY_ID
        or str(repository.get("full_name") or "").casefold() != repo.casefold()
        or int(head_repository.get("id") or 0) != _CLAUSEYE_REPOSITORY_ID
        or str(head_repository.get("full_name") or "").casefold()
        != repo.casefold()
        or str(run.get("html_url") or "").rstrip("/")
        != str(deployment["workflow_run_url"]).rstrip("/")
    ):
        raise LiveVerificationError(
            "deployment workflow is not a successful exact-merge production run",
            code="deployment_run_mismatch",
        )
    run_attempt = int(run.get("run_attempt") or 0)
    run_created_at = _parse_utc(run.get("created_at"))
    pr_merged_at = _parse_utc(merged_at)
    if (
        run_attempt <= 0
        or run_created_at is None
        or pr_merged_at is None
        or run_created_at < pr_merged_at
    ):
        raise LiveVerificationError(
            "deployment run does not post-date the exact PR merge",
            code="deployment_run_lineage_mismatch",
        )
    control_plane = _protected_control_plane_unchanged(
        repo,
        baseline_sha=trusted_control_sha,
        merge_sha=workflow_source_sha,
        cwd=cwd,
    )
    associated = _json(
        [
            str(TRUSTED_GH_PATH), "api", "-H", "Accept: application/vnd.github+json",
            f"repos/{repo}/commits/{merge_sha}/pulls?per_page=100",
        ],
        cwd=cwd,
    )
    association = (
        associated[0]
        if isinstance(associated, list) and len(associated) == 1
        else None
    )
    association_merged = (
        association.get("merged") if isinstance(association, Mapping) else None
    )
    if not (
        isinstance(association, Mapping)
        and association.get("number") == pr_number
        and association.get("state") == "closed"
        and isinstance(merged_at, str)
        and association.get("merged_at") == merged_at
        # The commit-association endpoint returns null here for merged PRs.
        # Reject an explicit contradiction, but bind merge state through its
        # canonical state/time/SHA fields and the terminal caller's full PR.
        and (association_merged is None or association_merged is True)
        and str(association.get("merge_commit_sha") or "").lower() == merge_sha
        and isinstance(association.get("head"), Mapping)
        and str(association["head"].get("sha") or "").lower() == candidate_sha
        and isinstance(association.get("base"), Mapping)
        and str(association["base"].get("ref") or "") == "main"
    ):
        raise LiveVerificationError(
            "deployment merge is not uniquely associated with the submitted PR head",
            code="deployment_pr_lineage_mismatch",
        )
    jobs = _json(
        [str(TRUSTED_GH_PATH), "api", f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"],
        cwd=cwd,
    )
    job_rows = jobs.get("jobs") if isinstance(jobs, dict) else []
    smoke_jobs = [
        job for job in job_rows
        if isinstance(job, dict) and "Cloud Run Smoke Test" in str(job.get("name") or "")
    ]
    if not smoke_jobs or not all(job.get("conclusion") == "success" for job in smoke_jobs):
        raise LiveVerificationError(
            "production workflow lacks a successful Cloud Run smoke job",
            code="deployment_smoke_failed",
        )

    repo_vars = _variables(repo, "", cwd=cwd)
    env_vars = _variables(repo, "production", cwd=cwd)
    project = repo_vars.get("GCP_PROJECT_ID")
    region = repo_vars.get("GCP_REGION")
    service = env_vars.get("CLOUD_RUN_SERVICE_NAME")
    if not all((project, region, service)):
        raise LiveVerificationError(
            "production Cloud Run coordinates are not configured",
            code="deployment_coordinates_missing",
        )
    service_json = _json(
        [
            str(TRUSTED_GCLOUD_PATH), "run", "services", "describe", service,
            "--project", project, "--region", region, "--format=json",
        ],
        cwd=cwd,
    )
    status = service_json.get("status") if isinstance(service_json, dict) else {}
    latest_ready = str((status or {}).get("latestReadyRevisionName") or "")
    claimed_revision = str(deployment.get("revision") or "")
    if not claimed_revision or claimed_revision != latest_ready:
        raise LiveVerificationError(
            "claimed production revision is not the latest ready Cloud Run revision",
            code="deployment_revision_mismatch",
        )
    traffic = (status or {}).get("traffic") or []
    live = any(
        isinstance(item, dict)
        and item.get("revisionName") == claimed_revision
        and int(item.get("percent") or 0) == 100
        for item in traffic
    )
    ready = any(
        isinstance(item, dict)
        and item.get("type") == "Ready"
        and str(item.get("status")) == "True"
        for item in ((status or {}).get("conditions") or [])
    )
    if not live or not ready:
        raise LiveVerificationError(
            "claimed Cloud Run revision is not ready at 100% traffic",
            code="deployment_revision_unhealthy",
        )
    revision_json = _json(
        [
            str(TRUSTED_GCLOUD_PATH), "run", "revisions", "describe", claimed_revision,
            "--project", project, "--region", region, "--format=json",
        ],
        cwd=cwd,
    )
    metadata = (
        revision_json.get("metadata") if isinstance(revision_json, dict) else {}
    )
    labels = metadata.get("labels") if isinstance(metadata, dict) else {}
    annotations = (
        metadata.get("annotations") if isinstance(metadata, dict) else {}
    )
    if not isinstance(labels, dict) or (
        str(labels.get(_REVISION_CANDIDATE_LABEL) or "").lower()
        != candidate_sha
        or str(labels.get(_REVISION_MERGE_LABEL) or "").lower() != merge_sha
        or str(labels.get(_REVISION_RUN_LABEL) or "") != str(run_id)
    ):
        raise LiveVerificationError(
            "Cloud Run revision is not immutably labelled with this merge and workflow run",
            code="deployment_revision_lineage_mismatch",
        )
    expected_workflow_ref = (
        f"{repo}/{_CLAUSEYE_DEPLOY_WORKFLOW_PATH}@refs/heads/main"
    )
    if not isinstance(annotations, dict) or (
        str(annotations.get(_REVISION_CANDIDATE_ANNOTATION) or "").lower()
        != candidate_sha
        or str(annotations.get(_REVISION_MERGE_ANNOTATION) or "").lower()
        != merge_sha
        or str(annotations.get(_REVISION_RUN_ANNOTATION) or "") != str(run_id)
        or str(annotations.get(_REVISION_RUN_ATTEMPT_ANNOTATION) or "")
        != str(run_attempt)
        or str(annotations.get(_REVISION_WORKFLOW_ANNOTATION) or "")
        != expected_workflow_ref
        or str(annotations.get(_REVISION_WORKFLOW_SHA_ANNOTATION) or "").lower()
        != workflow_source_sha
        or str(annotations.get(_REVISION_MODE_ANNOTATION) or "") != "promotion"
    ):
        raise LiveVerificationError(
            "Cloud Run revision annotations do not prove exact deployment lineage",
            code="deployment_revision_lineage_mismatch",
        )
    revision_status = (
        revision_json.get("status") if isinstance(revision_json, dict) else {}
    )
    revision_ready = any(
        isinstance(item, dict)
        and item.get("type") == "Ready"
        and str(item.get("status")) == "True"
        for item in ((revision_status or {}).get("conditions") or [])
    )
    image_digest = str((revision_status or {}).get("imageDigest") or "")
    containers = (
        (revision_json.get("spec") or {}).get("containers")
        if isinstance(revision_json, dict)
        and isinstance(revision_json.get("spec"), dict)
        else None
    )
    deployed_image = str(
        containers[0].get("image")
        if isinstance(containers, list)
        and len(containers) == 1
        and isinstance(containers[0], dict)
        else ""
    )
    digest_match = re.search(r"@(sha256:[0-9a-f]{64})$", image_digest)
    if (
        not revision_ready
        or digest_match is None
        or deployed_image != image_digest
        or str(annotations.get(_REVISION_IMAGE_ANNOTATION) or "")
        != digest_match.group(1)
    ):
        raise LiveVerificationError(
            "Cloud Run revision lacks a ready immutable image digest",
            code="deployment_revision_image_unverified",
        )
    smoke = max(smoke_jobs, key=lambda item: int(item.get("id") or 0))
    checked_at = _utc_now()
    return {
        "environment": "production",
        "revision": claimed_revision,
        "source_sha": merge_sha,
        "workflow_run_id": run_id,
        "workflow_run_url": run["html_url"],
        "workflow_name": run["name"],
        "health_status": "healthy",
        "health_checked_at": checked_at,
        "health_reference": smoke.get("html_url") or run["html_url"],
        "verification": {
            "verifier": "clauseye-production-probe",
            "status": "passed",
            "reference": smoke.get("html_url") or run["html_url"],
        },
        "cloud_run": {
            "project": project,
            "region": region,
            "service": service,
            "traffic_percent": 100,
            "candidate_sha_label": candidate_sha,
            "merge_sha_label": merge_sha,
            "workflow_run_id_label": str(run_id),
            "workflow_run_attempt": run_attempt,
            "workflow_ref": expected_workflow_ref,
            "workflow_source_sha": workflow_source_sha,
            "protected_control_plane": control_plane,
            "deployed_image": deployed_image,
            "image_digest": image_digest,
        },
    }


def verify_reviewer_merge_ready(
    task: Any,
    policy: Mapping[str, Any],
    submission: Mapping[str, Any],
    *,
    kanban_home: Path,
) -> dict[str, Any]:
    """Re-derive exact-head policy, checks, threads, and acceptance pre-merge.

    The merge endpoint is an irreversible boundary.  Terminal verification
    remains authoritative after the merge, but the control plane must not
    merge first and discover a failed acceptance command afterwards.
    """

    candidate = {
        key: submission.get(key)
        for key in ("pr_url", "pr_number", "head_sha", "candidate_ref")
    }
    verified: dict[str, Any] = {
        "submission": verify_submission(task, policy, candidate),
    }
    workspace = _workspace(task)
    repo = _repo_from_pr_url(str(candidate["pr_url"]))
    project_root = _require_registered_clauseye_project(task, workspace, repo)
    pr = _pull_request(repo, int(candidate["pr_number"]), cwd=workspace)
    default_branch = _default_branch_for_pr(repo, pr, cwd=workspace)
    head_sha = str(candidate["head_sha"] or "").lower()
    verified["ruleset"] = _required_ruleset_evidence(
        repo,
        default_branch,
        head_sha,
        int(candidate["pr_number"]),
        cwd=workspace,
    )
    contract_hash = str(policy.get("contract_hash") or "")
    if not contract_hash or str(submission.get("contract_hash") or "") != contract_hash:
        raise LiveVerificationError(
            "review submission is not bound to the current acceptance contract",
            code="acceptance_contract_mismatch",
        )
    verified["acceptance"] = _authoritative_acceptance(
        task,
        submission,
        contract_hash=contract_hash,
        candidate_sha=head_sha,
        base_sha=str(verified["submission"].get("base_sha") or "").lower(),
        base_ref=str(verified["submission"].get("base_ref") or default_branch),
        kanban_home=kanban_home,
        workspace=workspace,
        project_root=project_root,
    )
    post_head = _run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], cwd=workspace,
    ).strip().lower()
    post_dirty = _run(
        ["git", "-C", str(workspace), "status", "--porcelain"], cwd=workspace,
    ).strip()
    if post_head != head_sha or post_dirty:
        raise LiveVerificationError(
            "canonical acceptance mutated the exact-head review workspace",
            code="review_workspace_mutated_by_acceptance",
        )
    final_policy = _revalidate_terminal_policy(
        repo,
        default_branch,
        str(verified["ruleset"].get("policy_claim_sha256") or ""),
    )
    verified["ruleset"]["policy_revalidated_at"] = final_policy["issued_at"]
    verified["ruleset"]["policy_expires_at"] = final_policy["expires_at"]
    verified["ruleset"]["policy_key_id"] = final_policy["key_id"]
    return verified


def verify_terminal(
    task: Any,
    policy: Mapping[str, Any],
    submission: Mapping[str, Any],
    delivery: Mapping[str, Any],
    *,
    kanban_home: Path,
) -> dict[str, Any]:
    """Verify merged PR, exact-head gate, reviewer evidence, and deployment."""
    workspace = _workspace(task)
    repo = _repo_from_pr_url(str(delivery["pr_url"]))
    if repo.casefold() != _CLAUSEYE_REPOSITORY:
        raise LiveVerificationError(
            f"no trusted terminal delivery verifier is registered for {repo!r}",
            code="repository_verifier_unregistered",
        )
    project_root = _require_registered_clauseye_project(task, workspace, repo)
    origin = _run(
        ["git", "-C", str(workspace), "remote", "get-url", "origin"],
        cwd=workspace,
    ).strip()
    origin_repo = _repo_from_remote(origin)
    if origin_repo is None or origin_repo.casefold() != repo.casefold():
        raise LiveVerificationError(
            "task worktree origin no longer matches the submitted PR repository",
            code="pr_repository_mismatch",
        )
    pr = _pull_request(repo, int(delivery["pr_number"]), cwd=workspace)
    head_sha = str(delivery["head_sha"]).lower()
    merge_sha = str(delivery["merge_sha"]).lower()
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    default_branch = _default_branch_for_pr(repo, pr, cwd=workspace)
    if (
        str(pr.get("state") or "").casefold() != "closed"
        or pr.get("merged") is not True
        or not pr.get("merged_at")
        or str(head.get("sha") or "").lower() != head_sha
        or str(pr.get("merge_commit_sha") or "").lower() != merge_sha
        or str(head_repo.get("full_name") or "").casefold() != repo.casefold()
        or int(head_repo.get("id") or 0) != _CLAUSEYE_REPOSITORY_ID
        or int(base_repo.get("id") or 0) != _CLAUSEYE_REPOSITORY_ID
    ):
        raise LiveVerificationError(
            "GitHub does not confirm this exact submitted head and merge SHA as merged",
            code="pr_merge_state_mismatch",
        )
    if head_sha != str(submission.get("head_sha") or "").lower():
        raise LiveVerificationError(
            "terminal PR head differs from submitted review head",
            code="review_candidate_mismatch",
        )
    submitted_ref = str(submission.get("candidate_ref") or "").removeprefix(
        "refs/heads/"
    )
    if (
        not submitted_ref.startswith(_CLAUSEYE_AUTONOMOUS_BRANCH_PREFIX)
        or str(head.get("ref") or "") != submitted_ref
    ):
        raise LiveVerificationError(
            "merged PR branch is not the exact canonical submitted hermes/* branch",
            code="autonomous_branch_mismatch",
        )
    local_head = _run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], cwd=workspace,
    ).strip().lower()
    dirty = _run(
        ["git", "-C", str(workspace), "status", "--porcelain"], cwd=workspace,
    ).strip()
    if local_head != head_sha or dirty:
        raise LiveVerificationError(
            "review workspace must remain clean at the exact submitted head",
            code="review_workspace_drift",
        )
    ancestry = _json(
        [
            str(TRUSTED_GH_PATH), "api",
            f"repos/{repo}/compare/{merge_sha}...{default_branch}",
        ],
        cwd=workspace,
    )
    if not isinstance(ancestry, dict) or int(ancestry.get("behind_by") or 0) != 0:
        raise LiveVerificationError(
            "merge commit is not reachable from the repository default branch",
            code="merge_not_on_default_branch",
        )

    verified: dict[str, Any] = {
        "verified_at": _utc_now(),
        "repository": repo,
        "pr": {
            "number": int(delivery["pr_number"]),
            "head_sha": head_sha,
            "merge_sha": merge_sha,
            "merged_at": pr.get("merged_at"),
        },
    }
    verified["ruleset"] = _required_ruleset_evidence(
        repo,
        default_branch,
        head_sha,
        int(delivery["pr_number"]),
        cwd=workspace,
    )
    trusted_workflow = verified["ruleset"].get("required_workflow") or {}
    verified["autonomous_merge_gate"] = _successful_autonomous_gate(
        repo,
        head_sha,
        int(delivery["pr_number"]),
        head_ref=submitted_ref,
        base_ref=default_branch,
        merge_sha=merge_sha,
        trusted_workflow_sha=str(
            trusted_workflow.get("workflow_source_sha") or ""
        ),
        cwd=workspace,
    )
    contract_hash = policy.get("contract_hash")
    if contract_hash:
        verified["acceptance"] = _authoritative_acceptance(
            task,
            submission,
            contract_hash=str(contract_hash),
            candidate_sha=head_sha,
            # The PR API's base can move after merging. Reuse the immutable
            # squash parent already bound to the exact successful gate.
            base_sha=str(
                verified["autonomous_merge_gate"].get("base_sha") or ""
            ).lower(),
            base_ref=default_branch,
            kanban_home=kanban_home,
            workspace=workspace,
            project_root=project_root,
        )
        post_acceptance_head = _run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            cwd=workspace,
        ).strip().lower()
        post_acceptance_dirty = _run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            cwd=workspace,
        ).strip()
        if post_acceptance_head != head_sha or post_acceptance_dirty:
            raise LiveVerificationError(
                "canonical acceptance mutated the exact-head review workspace",
                code="review_workspace_mutated_by_acceptance",
            )

    if policy.get("deployment_required"):
        deployment = delivery.get("deployment")
        if not isinstance(deployment, Mapping):
            raise LiveVerificationError(
                "deployment claim is missing", code="deployment_evidence_missing"
            )
        verifier = str(policy.get("deployment_verifier") or "")
        if verifier != "clauseye-production-probe":
            raise LiveVerificationError(
                f"no trusted live verifier is registered for {verifier!r}",
                code="deployment_verifier_unregistered",
            )
        verified["deployment"] = _verify_clauseye_production(
            repo,
            head_sha,
            merge_sha,
            deployment,
            pr_number=int(delivery["pr_number"]),
            merged_at=pr.get("merged_at"),
            trusted_control_sha=str(
                trusted_workflow.get("workflow_source_sha") or ""
            ),
            cwd=workspace,
        )
    final_policy = _revalidate_terminal_policy(
        repo,
        default_branch,
        str(verified["ruleset"].get("policy_claim_sha256") or ""),
    )
    verified["ruleset"]["policy_revalidated_at"] = final_policy["issued_at"]
    verified["ruleset"]["policy_expires_at"] = final_policy["expires_at"]
    verified["ruleset"]["policy_key_id"] = final_policy["key_id"]
    return verified
