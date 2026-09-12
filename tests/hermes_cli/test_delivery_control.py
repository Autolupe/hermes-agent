"""Hermetic native-controller cases extracted from protected 339a5398.

Installation, worker launch, credential scrubbing, and live services are outside
this source-integration fixture suite. Remote delivery uses explicit fakes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import sqlite3
import stat
import struct
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.delivery_control import (
    PROTOCOL_SCHEMA,
    ControlRequest,
    DeliveryControl,
    DeliveryControlError,
    PublishResult,
    ReviewResult,
    SubprocessDeliveryBackend,
    _response_for_error,
    _submission_sha256,
    handle_connection,
    parse_request,
)

HEAD = "a" * 40
MERGE = "b" * 40
PID = os.getpid()
UID = os.getuid()


def _contract(*, deploy: bool = False) -> str:
    target = "github-merge-and-deploy" if deploy else "github-merge"
    deployment = (
        "deployment: production\n"
        "deployment-environment: production\n"
        "deployment-verifier: clauseye-production-probe\n"
        if deploy else ""
    )
    return f"""```acceptance-contract
domain: coding
target: {target}
{deployment}tier1:
  - cmd: "scripts/run_tests.sh -q"
    expect_exit: 0
tier2:
  - "reviewed independently"
tier3: "merged delivery is verified"
```"""


class FakeGitHub:
    def __init__(self) -> None:
        self.publish_calls = 0
        self.review_calls = 0
        self.submission_verifications = 0
        self.terminal_verifications = 0
        self.mutate_on_publish = None
        self.mutate_on_review = None
        self.pending = False

    def candidate_identity(self, task):
        return HEAD

    def publish(self, task, policy, *, run_id, guard):
        self.publish_calls += 1
        guard()
        if self.mutate_on_publish:
            self.mutate_on_publish()
        guard()
        pull_request = {
            "pr_url": "https://github.com/clauseye-com/clauseye-contra-rope/pull/17",
            "pr_number": 17,
            "head_sha": HEAD,
            "candidate_ref": task.branch_name,
        }
        guard("pull_request", {"pull_request": pull_request})
        return PublishResult(pull_request)

    def verify_submission(self, task, policy, pull_request):
        self.submission_verifications += 1
        assert pull_request["head_sha"] == HEAD
        return {
            "repository": "clauseye-com/clauseye-contra-rope",
            "contract_hash": policy["contract_hash"],
            "local_head_sha": HEAD,
        }

    def review(self, task, policy, submission, *, guard, kanban_home):
        self.review_calls += 1
        guard()
        assert submission["_event_id"] > 0
        assert submission["review_round"] >= 1
        assert submission["contract_hash"] == policy["contract_hash"]
        if self.pending:
            raise DeliveryControlError(
                "checks_pending", "exact-head checks are pending", pending=True,
            )
        if self.mutate_on_review:
            self.mutate_on_review()
        guard()
        delivery = {
            "classification": "merged_pr",
            "pr_url": submission["pr_url"],
            "pr_number": submission["pr_number"],
            "head_sha": submission["head_sha"],
            "merge_sha": MERGE,
        }
        guard("merge", {"delivery": delivery})
        return ReviewResult(delivery)

    def verify_terminal(
        self, task, policy, submission, delivery, *, kanban_home,
    ):
        self.terminal_verifications += 1
        assert submission["_event_id"] > 0
        assert delivery["head_sha"] == submission["head_sha"]
        assert delivery["merge_sha"] == MERGE
        return {
            "repository": "clauseye-com/clauseye-contra-rope",
            "pr": {"head_sha": HEAD, "merge_sha": MERGE},
            "acceptance": {
                "verdict": "pass",
                "contract_hash": policy["contract_hash"],
            },
        }


class CrashRecoveringGitHub(FakeGitHub):
    """Stateful fake for ambiguous remote-write/restart boundaries."""

    def __init__(self, crash_point: str) -> None:
        super().__init__()
        self.crash_point = crash_point
        self.did_crash = False
        self.remote_pushes = 0
        self.pull_request_creates = 0
        self.remote_branch = False
        self.remote_pull_request = False

    def publish(self, task, policy, *, run_id, guard):
        self.publish_calls += 1
        guard()
        if not self.remote_branch:
            self.remote_branch = True
            self.remote_pushes += 1
        if self.crash_point == "after_push" and not self.did_crash:
            self.did_crash = True
            raise RuntimeError("simulated broker death after push")
        if not self.remote_pull_request:
            self.remote_pull_request = True
            self.pull_request_creates += 1
        if self.crash_point == "after_pr" and not self.did_crash:
            self.did_crash = True
            raise RuntimeError("simulated broker death after PR create")
        pull_request = {
            "pr_url": "https://github.com/clauseye-com/clauseye-contra-rope/pull/17",
            "pr_number": 17,
            "head_sha": HEAD,
            "candidate_ref": task.branch_name,
        }
        guard("pull_request", {"pull_request": pull_request})
        return PublishResult(pull_request)


class CrashRecoveringReviewGitHub(FakeGitHub):
    """Fake exact-head merge with a durable pre-merge authorization receipt."""

    def __init__(self, crash_point: str, *, externally_merged: bool = False) -> None:
        super().__init__()
        self.crash_point = crash_point
        self.did_crash = False
        self.merged = externally_merged
        self.merge_calls = 0

    def review(self, task, policy, submission, *, guard, kanban_home):
        self.review_calls += 1
        guard()
        if not self.merged:
            guard("premerge", {
                "verification_sha256": "c" * 64,
                "policy_claim_sha256": "d" * 64,
                "acceptance_verdict": "pass",
                "contract_hash": policy["contract_hash"],
                "head_sha": HEAD,
                "submission_event_id": int(submission["_event_id"]),
            })
            if self.crash_point == "before_put" and not self.did_crash:
                self.did_crash = True
                raise RuntimeError("simulated broker death before merge PUT")
            self.merge_calls += 1
            self.merged = True
            if self.crash_point == "after_put" and not self.did_crash:
                self.did_crash = True
                raise RuntimeError("simulated broker death after merge PUT")
        else:
            guard("require_premerge")
        delivery = {
            "classification": "merged_pr",
            "pr_url": submission["pr_url"],
            "pr_number": submission["pr_number"],
            "head_sha": submission["head_sha"],
            "merge_sha": MERGE,
        }
        guard("merge", {"delivery": delivery})
        if self.crash_point == "deployment_pending" and not self.did_crash:
            self.did_crash = True
            raise DeliveryControlError(
                "deployment_pending",
                "exact deployment evidence is pending",
                pending=True,
            )
        return ReviewResult(delivery)


class AcceptanceRejectingGitHub(FakeGitHub):
    """Reviewer fake that durably records the PR, then fails exact tier 1."""

    def __init__(
        self,
        *,
        evidence_overrides=None,
        ledger_attack=None,
        mutate_after_receipt=None,
    ) -> None:
        super().__init__()
        self.evidence_overrides = dict(evidence_overrides or {})
        self.ledger_attack = ledger_attack
        self.mutate_after_receipt = mutate_after_receipt

    def review(self, task, policy, submission, *, guard, kanban_home):
        del kanban_home
        self.review_calls += 1
        guard("pull_request", {"pull_request": {
            "pr_url": submission["pr_url"],
            "pr_number": submission["pr_number"],
            "head_sha": submission["head_sha"],
            "candidate_ref": submission["candidate_ref"],
            "body_sha256": "c" * 64,
        }})
        if self.mutate_after_receipt is not None:
            self.mutate_after_receipt(task, submission)
        from hermes_cli.delivery_verifier import LiveVerificationError

        created_at = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        ledger = {
            "schema": "hermes-evidence/v1",
            "task_id": task.id,
            "domain": "coding",
            "contract_hash": policy["contract_hash"],
            "candidate_sha": submission["head_sha"],
            "results": [{
                "cmd": "scripts/run_tests.sh -q",
                "exit": 1,
                "expect_ok": False,
                "ms": 1,
            }],
            "env_fingerprint": kb._DELIVERY_ACCEPTANCE_SANDBOX_FINGERPRINT,
            "run_by": "reviewer",
            "review_run_id": int(task.current_run_id),
            "review_profile": task.assignee,
            "submission_event_id": int(submission["_event_id"]),
            "verdict": "fail",
            "created_at": created_at,
        }
        evidence_dir = kb._DELIVERY_ACCEPTANCE_EVIDENCE_ROOT / task.id
        evidence_dir.mkdir(mode=0o700)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        ledger_path = evidence_dir / f"acceptance-{timestamp}-deadbeef.json"
        raw = (json.dumps(ledger, indent=2, sort_keys=True) + "\n").encode()
        ledger_path.write_bytes(raw)
        ledger_path.chmod(0o400)
        evidence = {
            "schema": "hermes-acceptance-failure/v1",
            "path": str(ledger_path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "contract_hash": policy["contract_hash"],
            "candidate_sha": submission["head_sha"],
            "review_run_id": int(task.current_run_id),
            "review_profile": task.assignee,
            "submission_event_id": int(submission["_event_id"]),
            "created_at": created_at,
            "failed_indexes": [0],
            "observed_exits": [1],
        }
        if self.ledger_attack == "missing":
            ledger_path.unlink()
        elif self.ledger_attack == "tamper":
            ledger_path.chmod(0o600)
            ledger_path.write_bytes(raw + b" ")
            ledger_path.chmod(0o400)
        elif self.ledger_attack == "symlink":
            target = evidence_dir / "symlink-target.json"
            ledger_path.rename(target)
            ledger_path.symlink_to(target.name)
        elif self.ledger_attack == "hardlink":
            os.link(ledger_path, evidence_dir / "hardlink-alias.json")
        elif self.ledger_attack == "wrong_content":
            changed = {**ledger, "candidate_sha": "f" * 40}
            changed_raw = (
                json.dumps(changed, indent=2, sort_keys=True) + "\n"
            ).encode()
            ledger_path.chmod(0o600)
            ledger_path.write_bytes(changed_raw)
            ledger_path.chmod(0o400)
            evidence["sha256"] = hashlib.sha256(changed_raw).hexdigest()
        elif self.ledger_attack is not None:
            raise AssertionError(self.ledger_attack)
        evidence.update(self.evidence_overrides)
        raise LiveVerificationError(
            "canonical tier1 failed",
            code="acceptance_tier1_failed",
            evidence=evidence,
        )


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    monkeypatch.setattr(kb, "_cleanup_worker_tmux", lambda *_args: None)
    evidence_root = tmp_path / "controller-evidence"
    evidence_root.mkdir(mode=0o700)
    monkeypatch.setattr(kb, "_DELIVERY_ACCEPTANCE_EVIDENCE_ROOT", evidence_root)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect_closing(db_path):
        pass
    yield db_path, tmp_path
    kb._INITIALIZED_PATHS.clear()


def _claimed_builder(db_path: Path, root: Path):
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="ship exact candidate",
            body=_contract(),
            assignee="builder",
            workspace_kind="worktree",
            workspace_path=str(root / "placeholder"),
            branch_name="hermes/placeholder",
        )
        workspace = root / ".worktrees" / task_id
        workspace.mkdir(parents=True)
        branch = f"hermes/{task_id}-ship"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_path = ?, branch_name = ?, "
                "project_id = ? WHERE id = ?",
                (str(workspace), branch, "p_clauseye", task_id),
            )
        claimed = kb.claim_task(conn, task_id, claimer="builder-lock")
        assert claimed is not None
        run_id = int(claimed.current_run_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?", (PID, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?", (PID, run_id),
            )
        task = kb.get_task(conn, task_id)
    request = ControlRequest(
        "builder_publish", task_id, run_id, str(task.claim_lock), "candidate ready",
    )
    return task_id, request


def _claim_review(db_path: Path, task_id: str) -> ControlRequest:
    with kb.connect_closing(db_path) as conn:
        claimed = kb.claim_review_task(conn, task_id, claimer="review-lock")
        assert claimed is not None
        run_id = int(claimed.current_run_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?", (PID, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?", (PID, run_id),
            )
        task = kb.get_task(conn, task_id)
    return ControlRequest(
        "reviewer_complete", task_id, run_id, str(task.claim_lock), "review accepted",
    )


def _linked_worktree(root: Path, task_id: str = "t_12345678"):
    project = root / "project"
    workspace = project / ".worktrees" / task_id
    common = project / ".git"
    admin = common / "worktrees" / task_id
    workspace.mkdir(parents=True)
    admin.mkdir(parents=True)
    (workspace / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{workspace / '.git'}\n", encoding="utf-8")
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    return project, workspace, common, admin


def _base_receipt_fixture(monkeypatch, tmp_path):
    from hermes_cli import delivery_control

    base = tmp_path / "state" / "base"
    base.mkdir(parents=True, mode=0o700)
    base.chmod(0o700)
    mirror = base / "repository.git"
    subprocess.run(
        ["/usr/bin/git", "init", "--bare", "--initial-branch=main", str(mirror)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "/usr/bin/git", "-C", str(mirror), "config",
            "core.sharedRepository", "0600",
        ],
        check=True,
        capture_output=True,
    )
    for current, directories, files in os.walk(mirror):
        Path(current).chmod(0o700)
        for name in directories:
            (Path(current) / name).chmod(0o700)
        for name in files:
            (Path(current) / name).chmod(0o600)
    receipt = base / "clauseye-main.json"
    monkeypatch.setattr(delivery_control, "BASE_STATE_ROOT", base)
    monkeypatch.setattr(delivery_control, "BASE_REPOSITORY_MIRROR", mirror)
    monkeypatch.setattr(delivery_control, "BASE_REFRESH_RECEIPT", receipt)
    return delivery_control, base, mirror, receipt


def _base_payload(delivery_control, sha: str, fetched_at: int) -> dict:
    return {
        "schema": delivery_control.BASE_REFRESH_SCHEMA,
        "repository": delivery_control.FIXED_REPOSITORY,
        "ref": "refs/heads/main",
        "sha": sha,
        "fetched_at": fetched_at,
    }


def test_base_receipt_rotation_and_expiry_are_deterministic(monkeypatch, tmp_path):
    delivery_control, _base, mirror, _receipt = _base_receipt_fixture(
        monkeypatch, tmp_path,
    )
    now = int(time.time())
    first = "1" * 40
    second = "2" * 40
    delivery_control._publish_base_receipt(_base_payload(delivery_control, first, now))
    assert delivery_control.read_fresh_base_receipt(mirror, now=now) == first
    delivery_control._publish_base_receipt(_base_payload(delivery_control, second, now + 1))
    assert delivery_control.read_fresh_base_receipt(mirror, now=now + 1) == second
    with pytest.raises(DeliveryControlError) as expired:
        delivery_control.read_fresh_base_receipt(
            mirror,
            now=now + delivery_control.BASE_REFRESH_MAX_AGE_SECONDS + 2,
        )
    assert expired.value.code == "base_refresh_stale"


def test_base_receipt_symlink_and_replacement_race_are_rejected(monkeypatch, tmp_path):
    delivery_control, _base, mirror, receipt = _base_receipt_fixture(
        monkeypatch, tmp_path,
    )
    now = int(time.time())
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_base_payload(delivery_control, "3" * 40, now)))
    outside.chmod(0o600)
    receipt.symlink_to(outside)
    with pytest.raises(DeliveryControlError):
        delivery_control.read_fresh_base_receipt(mirror, now=now)
    receipt.unlink()
    delivery_control._publish_base_receipt(
        _base_payload(delivery_control, "4" * 40, now),
    )
    replacement = tmp_path / "replacement.json"
    replacement.write_text(
        json.dumps(_base_payload(delivery_control, "5" * 40, now)),
        encoding="ascii",
    )
    replacement.chmod(0o600)
    real_open = delivery_control.os.open
    receipt_opens = 0

    def racing_open(path, flags, *args, **kwargs):
        nonlocal receipt_opens
        if path == receipt.name:
            receipt_opens += 1
            if receipt_opens == 2:
                os.replace(replacement, receipt)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(delivery_control.os, "open", racing_open)
    with pytest.raises(DeliveryControlError) as raced:
        delivery_control.read_fresh_base_receipt(mirror, now=now)
    assert raced.value.code == "base_refresh_raced"


def test_serve_rejects_manual_root_identity(monkeypatch):
    from hermes_cli import delivery_control

    monkeypatch.setattr(delivery_control.os, "geteuid", lambda: 0)
    with pytest.raises(DeliveryControlError) as rejected:
        delivery_control.serve_forever(SimpleNamespace())
    assert rejected.value.code == "service_identity_invalid"


def test_controller_refreshes_exact_main_into_config_free_mirror(
    monkeypatch, tmp_path,
):
    from hermes_cli import delivery_control, delivery_verifier

    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(
        ["/usr/bin/git", "init", "-b", "main", str(source)],
        check=True,
        capture_output=True,
    )
    (source / "README.md").write_text("exact base\n", encoding="utf-8")
    subprocess.run(
        [
            "/usr/bin/git", "-C", str(source),
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "add", "README.md",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "/usr/bin/git", "-C", str(source),
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false", "commit", "-m", "base",
        ],
        check=True,
        capture_output=True,
    )
    expected = subprocess.run(
        ["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    state = tmp_path / "state"
    transactions = state / "transactions"
    base = state / "base"
    transactions.mkdir(parents=True, mode=0o700)
    base.mkdir(mode=0o700)
    transactions.chmod(0o700)
    base.chmod(0o700)
    mirror = base / "repository.git"
    receipt = base / "clauseye-main.json"
    monkeypatch.setattr(delivery_control, "STATE_ROOT", state)
    monkeypatch.setattr(delivery_control, "BASE_STATE_ROOT", base)
    monkeypatch.setattr(delivery_control, "BASE_REPOSITORY_MIRROR", mirror)
    monkeypatch.setattr(delivery_control, "BASE_REFRESH_RECEIPT", receipt)
    monkeypatch.setattr(delivery_control, "FIXED_REPOSITORY_URL", str(source))
    monkeypatch.setattr(
        delivery_verifier,
        "_repo_from_remote",
        lambda _remote: delivery_control.FIXED_REPOSITORY,
    )
    backend = SubprocessDeliveryBackend("github-secret")

    assert backend.refresh_default_branch() == expected
    assert delivery_control.read_fresh_base_receipt(mirror) == expected
    assert subprocess.run(
        ["/usr/bin/git", "-C", str(mirror), "rev-parse", "origin/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == expected
    assert not (source / ".git" / "refs" / "remotes" / "origin" / "main").exists()


def test_real_sqlite_fake_github_publish_review_complete_is_idempotent(board):
    db_path, root = board
    backend = FakeGitHub()
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    task_id, builder_request = _claimed_builder(db_path, root)

    submitted = control.handle(builder_request, peer_pid=PID, peer_uid=UID)
    submitted_retry = control.handle(builder_request, peer_pid=PID, peer_uid=UID)
    assert submitted["state"] == "submitted"
    assert submitted_retry["idempotent"] is True
    assert backend.publish_calls == 1
    assert backend.submission_verifications == 1

    review_request = _claim_review(db_path, task_id)
    completed = control.handle(review_request, peer_pid=PID, peer_uid=UID)
    completed_retry = control.handle(review_request, peer_pid=PID, peer_uid=UID)
    assert completed["state"] == "completed"
    assert completed_retry["idempotent"] is True
    assert backend.review_calls == 1
    assert backend.terminal_verifications == 1
    with kb.connect_closing(db_path) as conn:
        assert kb.get_task(conn, task_id).status == "done"
        event_kinds = [event.kind for event in kb.list_events(conn, task_id)]
    assert "submitted_for_review" in event_kinds
    assert "acceptance_verified" in event_kinds
    assert "completed" in event_kinds


def test_exact_tier1_failure_atomically_rejects_review_into_triage(board):
    db_path, root = board
    builder = FakeGitHub()
    task_id, builder_request = _claimed_builder(db_path, root)
    DeliveryControl(db_path, builder, allowed_worker_uid=UID).handle(
        builder_request, peer_pid=PID, peer_uid=UID,
    )
    review_request = _claim_review(db_path, task_id)
    backend = AcceptanceRejectingGitHub()
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)

    rejected = control.handle(review_request, peer_pid=PID, peer_uid=UID)
    stale_retry = control.handle(review_request, peer_pid=PID, peer_uid=UID)

    assert rejected == {
        "schema": PROTOCOL_SCHEMA,
        "ok": False,
        "state": "rejected",
        "code": "acceptance_tier1_failed",
        "message": "canonical tier1 acceptance requires task rework",
        "task_id": task_id,
        "run_id": review_request.run_id,
        "idempotent": False,
    }
    assert stale_retry == {**rejected, "idempotent": True}
    assert backend.review_calls == 1

    with kb.connect_closing(db_path) as conn:
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT * FROM task_runs WHERE id = ?",
            (review_request.run_id,),
        ).fetchone()
        operation = conn.execute(
            "SELECT * FROM delivery_control_operations WHERE task_id = ? "
            "AND run_id = ? AND action = 'reviewer_complete'",
            (task_id, review_request.run_id),
        ).fetchone()
        events = kb.list_events(conn, task_id)

    assert task.status == "triage"
    assert task.assignee == "builder"
    assert task.current_run_id is None
    assert task.claim_lock is None
    assert task.claim_expires is None
    assert task.worker_pid is None
    assert task.block_kind is None
    assert run["status"] == "triage"
    assert run["outcome"] == "acceptance_rejected"
    assert run["error"] == "acceptance_tier1_failed"
    assert run["ended_at"] is not None
    assert run["claim_lock"] is None
    assert run["claim_expires"] is None
    assert run["worker_pid"] is None
    assert operation["state"] == "rejected"
    assert operation["stage"] == "native_rejected"

    receipt = json.loads(operation["receipt"])
    assert set(receipt) == {"stages"}
    assert set(receipt["stages"]) == {"pull_request", "acceptance_rejected"}
    assert receipt["stages"]["pull_request"] == {"pull_request": {
        "pr_url": "https://github.com/clauseye-com/clauseye-contra-rope/pull/17",
        "pr_number": 17,
        "head_sha": HEAD,
        "candidate_ref": f"hermes/{task_id}-ship",
        "body_sha256": "c" * 64,
    }}
    rejection = receipt["stages"]["acceptance_rejected"]
    assert set(rejection) == {
        "code", "evidence_path", "evidence_sha256", "contract_hash",
        "candidate_head", "review_run_id", "submission_event_id",
        "failed_indexes", "observed_exits",
    }
    assert rejection["code"] == "acceptance_tier1_failed"
    assert rejection["candidate_head"] == HEAD
    assert rejection["review_run_id"] == review_request.run_id
    assert rejection["failed_indexes"] == [0]
    assert rejection["observed_exits"] == [1]

    rework_events = [
        event for event in events if event.kind == "acceptance_rework_required"
    ]
    assert len(rework_events) == 1
    assert rework_events[0].run_id == review_request.run_id
    assert rework_events[0].payload == {
        **rejection,
        "operation_id": operation["op_id"],
        "next_status": "triage",
        "executor_assignee": "builder",
    }
    serialized_audit = operation["receipt"] + json.dumps(rework_events[0].payload)
    assert review_request.claim_lock not in serialized_audit
    assert review_request.summary not in serialized_audit


def test_reviewer_cutover_reconciles_pull_receipt_into_acceptance_rework(board):
    db_path, root = board
    task_id, builder_request = _claimed_builder(db_path, root)
    DeliveryControl(db_path, FakeGitHub(), allowed_worker_uid=UID).handle(
        builder_request, peer_pid=PID, peer_uid=UID,
    )
    review_request = _claim_review(db_path, task_id)

    def simulate_cutover(_task, _submission):
        raise RuntimeError("simulated cutover after pull-request receipt")

    interrupted_backend = AcceptanceRejectingGitHub(
        mutate_after_receipt=simulate_cutover,
    )
    interrupted = DeliveryControl(
        db_path, interrupted_backend, allowed_worker_uid=UID,
    )
    with pytest.raises(RuntimeError, match="simulated cutover"):
        interrupted.handle(review_request, peer_pid=PID, peer_uid=UID)

    with kb.connect_closing(db_path) as conn:
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT * FROM task_runs WHERE id = ?", (review_request.run_id,),
        ).fetchone()
        operation = conn.execute(
            "SELECT * FROM delivery_control_operations WHERE task_id = ? "
            "AND run_id = ? AND action = 'reviewer_complete'",
            (task_id, review_request.run_id),
        ).fetchone()
    assert task.status == "shipping"
    assert task.current_run_id == review_request.run_id
    assert run["status"] == "running"
    assert run["ended_at"] is None
    assert tuple(operation[key] for key in ("state", "stage")) == (
        "remote_applied", "pull_request",
    )
    original_pull_stage = json.loads(operation["receipt"])["stages"][
        "pull_request"
    ]
    assert set(json.loads(operation["receipt"])["stages"]) == {"pull_request"}
    assert interrupted_backend.review_calls == 1

    recovered_backend = AcceptanceRejectingGitHub()
    restarted = DeliveryControl(
        db_path, recovered_backend, allowed_worker_uid=UID,
    )
    restarted.prepare_recovery()
    assert restarted.reconcile_orphans() == {
        "attempted": 1,
        "completed": 1,
        "pending": 0,
        "quarantined": 0,
    }
    assert recovered_backend.review_calls == 1

    with kb.connect_closing(db_path) as conn:
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT * FROM task_runs WHERE id = ?", (review_request.run_id,),
        ).fetchone()
        operation = conn.execute(
            "SELECT * FROM delivery_control_operations WHERE task_id = ? "
            "AND run_id = ? AND action = 'reviewer_complete'",
            (task_id, review_request.run_id),
        ).fetchone()
        rework_events = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "acceptance_rework_required"
        ]

    assert task.status == "triage"
    assert task.assignee == "builder"
    assert task.current_run_id is None
    assert task.claim_lock is None
    assert task.claim_expires is None
    assert task.worker_pid is None
    assert run["status"] == "triage"
    assert run["outcome"] == "acceptance_rejected"
    assert run["error"] == "acceptance_tier1_failed"
    assert run["ended_at"] is not None
    assert run["claim_lock"] is None
    assert run["claim_expires"] is None
    assert run["worker_pid"] is None
    assert tuple(operation[key] for key in ("state", "stage")) == (
        "rejected", "native_rejected",
    )
    stages = json.loads(operation["receipt"])["stages"]
    assert set(stages) == {"pull_request", "acceptance_rejected"}
    assert stages["pull_request"] == original_pull_stage
    assert len(rework_events) == 1

    assert restarted.reconcile_orphans() == {
        "attempted": 0,
        "completed": 0,
        "pending": 0,
        "quarantined": 0,
    }
    assert recovered_backend.review_calls == 1


@pytest.mark.parametrize(
    "evidence_overrides",
    [
        {"candidate_sha": "f" * 40},
        {"failed_indexes": [1]},
        {"unexpected": "field"},
    ],
)
def test_acceptance_rejection_evidence_drift_leaves_delivery_fence_untouched(
    board, evidence_overrides,
):
    db_path, root = board
    task_id, builder_request = _claimed_builder(db_path, root)
    DeliveryControl(db_path, FakeGitHub(), allowed_worker_uid=UID).handle(
        builder_request, peer_pid=PID, peer_uid=UID,
    )
    review_request = _claim_review(db_path, task_id)
    backend = AcceptanceRejectingGitHub(evidence_overrides=evidence_overrides)
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)

    with pytest.raises(kb.DeliveryOperationInProgressError):
        control.handle(review_request, peer_pid=PID, peer_uid=UID)

    with kb.connect_closing(db_path) as conn:
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT * FROM task_runs WHERE id = ?", (review_request.run_id,),
        ).fetchone()
        operation = conn.execute(
            "SELECT * FROM delivery_control_operations WHERE task_id = ? "
            "AND run_id = ? AND action = 'reviewer_complete'",
            (task_id, review_request.run_id),
        ).fetchone()
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'acceptance_rework_required'",
            (task_id,),
        ).fetchone()[0]

    assert task.status == "shipping"
    assert task.current_run_id == review_request.run_id
    assert task.claim_lock == review_request.claim_lock
    assert run["status"] == "running"
    assert run["ended_at"] is None
    assert operation["state"] == "remote_applied"
    assert operation["stage"] == "pull_request"
    assert set(json.loads(operation["receipt"])["stages"]) == {"pull_request"}
    assert event_count == 0


@pytest.mark.parametrize(
    "ledger_attack",
    ["missing", "tamper", "symlink", "hardlink", "wrong_content"],
)
def test_acceptance_rejection_unsafe_ledger_rolls_back_delivery_fence(
    board, ledger_attack,
):
    db_path, root = board
    task_id, builder_request = _claimed_builder(db_path, root)
    DeliveryControl(db_path, FakeGitHub(), allowed_worker_uid=UID).handle(
        builder_request, peer_pid=PID, peer_uid=UID,
    )
    review_request = _claim_review(db_path, task_id)
    backend = AcceptanceRejectingGitHub(ledger_attack=ledger_attack)
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)

    with pytest.raises(kb.DeliveryOperationInProgressError):
        control.handle(review_request, peer_pid=PID, peer_uid=UID)

    with kb.connect_closing(db_path) as conn:
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT * FROM task_runs WHERE id = ?", (review_request.run_id,),
        ).fetchone()
        operation = conn.execute(
            "SELECT * FROM delivery_control_operations WHERE task_id = ? "
            "AND run_id = ? AND action = 'reviewer_complete'",
            (task_id, review_request.run_id),
        ).fetchone()
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'acceptance_rework_required'",
            (task_id,),
        ).fetchone()[0]

    assert task.status == "shipping"
    assert task.current_run_id == review_request.run_id
    assert task.claim_lock == review_request.claim_lock
    assert run["status"] == "running"
    assert run["ended_at"] is None
    assert operation["state"] == "remote_applied"
    assert operation["stage"] == "pull_request"
    assert set(json.loads(operation["receipt"])["stages"]) == {"pull_request"}
    assert event_count == 0


def test_acceptance_rejection_task_drift_leaves_pull_request_fence_untouched(board):
    db_path, root = board
    task_id, builder_request = _claimed_builder(db_path, root)
    DeliveryControl(db_path, FakeGitHub(), allowed_worker_uid=UID).handle(
        builder_request, peer_pid=PID, peer_uid=UID,
    )
    review_request = _claim_review(db_path, task_id)

    def drift_title(_task, _submission):
        with kb.connect_closing(db_path) as conn, kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET title = 'concurrent edit' WHERE id = ?",
                (task_id,),
            )

    backend = AcceptanceRejectingGitHub(mutate_after_receipt=drift_title)
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    with pytest.raises(kb.DeliveryOperationInProgressError):
        control.handle(review_request, peer_pid=PID, peer_uid=UID)

    with kb.connect_closing(db_path) as conn:
        task = kb.get_task(conn, task_id)
        operation = conn.execute(
            "SELECT state, stage, receipt FROM delivery_control_operations "
            "WHERE task_id = ? AND run_id = ? AND action = 'reviewer_complete'",
            (task_id, review_request.run_id),
        ).fetchone()
        rework_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'acceptance_rework_required'",
            (task_id,),
        ).fetchone()[0]

    assert task.title == "concurrent edit"
    assert task.status == "shipping"
    assert task.current_run_id == review_request.run_id
    assert tuple(operation)[:2] == ("remote_applied", "pull_request")
    assert set(json.loads(operation["receipt"])["stages"]) == {"pull_request"}
    assert rework_count == 0


def test_rejected_acceptance_retry_survives_rework_and_successor_submission(board):
    db_path, root = board
    task_id, builder_request = _claimed_builder(db_path, root)
    DeliveryControl(db_path, FakeGitHub(), allowed_worker_uid=UID).handle(
        builder_request, peer_pid=PID, peer_uid=UID,
    )
    review_request = _claim_review(db_path, task_id)
    backend = AcceptanceRejectingGitHub()
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)

    rejected = control.handle(review_request, peer_pid=PID, peer_uid=UID)
    corrected_body = _contract().replace(
        "scripts/run_tests.sh -q",
        "scripts/run_tests.sh tests/hermes_cli -q",
    )
    with kb.connect_closing(db_path) as conn:
        assert kb.specify_triage_task(
            conn,
            task_id,
            body=corrected_body,
            assignee="builder",
            author="recovery",
        )
        assert kb.get_task(conn, task_id).status == "ready"

    after_rework = control.handle(
        review_request, peer_pid=PID, peer_uid=UID,
    )
    with kb.connect_closing(db_path) as conn:
        successor = kb.claim_task(conn, task_id, claimer="successor-lock")
        assert successor is not None
        assert successor.current_run_id != review_request.run_id
        successor_run_id = int(successor.current_run_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (PID, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                (PID, successor_run_id),
            )
        successor = kb.get_task(conn, task_id)
    successor_request = ControlRequest(
        "builder_publish",
        task_id,
        successor_run_id,
        str(successor.claim_lock),
        "corrected contract candidate",
    )
    successor_response = DeliveryControl(
        db_path, FakeGitHub(), allowed_worker_uid=UID,
    ).handle(successor_request, peer_pid=PID, peer_uid=UID)
    assert successor_response["state"] == "submitted"

    after_successor = control.handle(
        review_request, peer_pid=PID, peer_uid=UID,
    )

    expected = {**rejected, "idempotent": True}
    assert after_rework == expected
    assert after_successor == expected
    assert backend.review_calls == 1


@pytest.mark.parametrize(
    "drift", ["missing", "payload", "digest", "semantic_binding"],
)
def test_rejected_acceptance_retry_requires_exact_historical_submission(
    board, drift,
):
    db_path, root = board
    task_id, builder_request = _claimed_builder(db_path, root)
    DeliveryControl(db_path, FakeGitHub(), allowed_worker_uid=UID).handle(
        builder_request, peer_pid=PID, peer_uid=UID,
    )
    review_request = _claim_review(db_path, task_id)
    control = DeliveryControl(
        db_path, AcceptanceRejectingGitHub(), allowed_worker_uid=UID,
    )
    control.handle(review_request, peer_pid=PID, peer_uid=UID)

    with kb.connect_closing(db_path) as conn:
        operation = conn.execute(
            "SELECT submission_event_id FROM delivery_control_operations "
            "WHERE task_id = ? AND run_id = ? AND action = 'reviewer_complete'",
            (task_id, review_request.run_id),
        ).fetchone()
        historical_submission_id = int(operation["submission_event_id"])
        assert kb.specify_triage_task(
            conn,
            task_id,
            body=_contract().replace(
                "scripts/run_tests.sh -q",
                "scripts/run_tests.sh tests/hermes_cli -q",
            ),
            assignee="builder",
            author="recovery",
        )
        successor = kb.claim_task(conn, task_id, claimer="successor-lock")
        assert successor is not None
        successor_run_id = int(successor.current_run_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (PID, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                (PID, successor_run_id),
            )
        successor = kb.get_task(conn, task_id)
    successor_request = ControlRequest(
        "builder_publish",
        task_id,
        successor_run_id,
        str(successor.claim_lock),
        "corrected contract candidate",
    )
    DeliveryControl(db_path, FakeGitHub(), allowed_worker_uid=UID).handle(
        successor_request, peer_pid=PID, peer_uid=UID,
    )

    with kb.connect_closing(db_path) as conn, kb.write_txn(conn):
        if drift == "missing":
            conn.execute(
                "DELETE FROM task_events WHERE id = ?",
                (historical_submission_id,),
            )
        elif drift == "payload":
            conn.execute(
                "UPDATE task_events SET payload = ? WHERE id = ?",
                (json.dumps({"drifted": True}), historical_submission_id),
            )
        elif drift == "digest":
            conn.execute(
                "UPDATE delivery_control_operations SET submission_sha256 = ? "
                "WHERE task_id = ? AND run_id = ? "
                "AND action = 'reviewer_complete'",
                ("f" * 64, task_id, review_request.run_id),
            )
        else:
            row = conn.execute(
                "SELECT payload FROM task_events WHERE id = ?",
                (historical_submission_id,),
            ).fetchone()
            submission = json.loads(row["payload"])
            submission["candidate_ref"] = "hermes/t_deadbeef-drifted"
            conn.execute(
                "UPDATE task_events SET payload = ? WHERE id = ?",
                (json.dumps(submission), historical_submission_id),
            )
            conn.execute(
                "UPDATE delivery_control_operations SET submission_sha256 = ? "
                "WHERE task_id = ? AND run_id = ? "
                "AND action = 'reviewer_complete'",
                (
                    _submission_sha256(historical_submission_id, submission),
                    task_id,
                    review_request.run_id,
                ),
            )

    with pytest.raises(DeliveryControlError) as rejected_replay:
        control.handle(review_request, peer_pid=PID, peer_uid=UID)
    assert rejected_replay.value.code == "operation_receipt_invalid"


@pytest.mark.parametrize(
    "drift",
    [
        "profile",
        "status",
        "outcome",
        "ended_at",
        "claim_lock",
        "worker_pid",
        "operation_claim",
    ],
)
def test_acceptance_rejection_reviewer_run_drift_preserves_pull_fence(
    board, drift,
):
    db_path, root = board
    task_id, builder_request = _claimed_builder(db_path, root)
    DeliveryControl(db_path, FakeGitHub(), allowed_worker_uid=UID).handle(
        builder_request, peer_pid=PID, peer_uid=UID,
    )
    review_request = _claim_review(db_path, task_id)

    def drift_reviewer_run(_task, _submission):
        with kb.connect_closing(db_path) as conn, kb.write_txn(conn):
            if drift == "operation_claim":
                conn.execute(
                    "UPDATE delivery_control_operations SET claim_sha256 = ? "
                    "WHERE task_id = ? AND run_id = ? "
                    "AND action = 'reviewer_complete'",
                    ("f" * 64, task_id, review_request.run_id),
                )
                return
            column, value = {
                "profile": ("profile", "security"),
                "status": ("status", "blocked"),
                "outcome": ("outcome", "completed"),
                "ended_at": ("ended_at", int(time.time())),
                "claim_lock": ("claim_lock", "concurrent-claim"),
                "worker_pid": ("worker_pid", PID + 1),
            }[drift]
            conn.execute(
                f"UPDATE task_runs SET {column} = ? WHERE id = ? AND task_id = ?",
                (value, review_request.run_id, task_id),
            )

    backend = AcceptanceRejectingGitHub(mutate_after_receipt=drift_reviewer_run)
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    snapshot_drift = drift in {"status", "outcome", "ended_at", "claim_lock", "worker_pid"}
    expected_error = DeliveryControlError if snapshot_drift else kb.DeliveryOperationInProgressError
    with pytest.raises(expected_error) as refused:
        control.handle(review_request, peer_pid=PID, peer_uid=UID)
    assert refused.value.code == (
        "task_drifted" if snapshot_drift else "delivery_operation_in_progress"
    )

    with kb.connect_closing(db_path) as conn:
        task = kb.get_task(conn, task_id)
        operation = conn.execute(
            "SELECT state, stage, receipt FROM delivery_control_operations "
            "WHERE task_id = ? AND run_id = ? AND action = 'reviewer_complete'",
            (task_id, review_request.run_id),
        ).fetchone()
        rework_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'acceptance_rework_required'",
            (task_id,),
        ).fetchone()[0]

    assert task.status == "shipping"
    assert task.current_run_id == review_request.run_id
    assert task.claim_lock == review_request.claim_lock
    assert tuple(operation)[:2] == ("remote_applied", "pull_request")
    assert set(json.loads(operation["receipt"])["stages"]) == {"pull_request"}
    assert rework_count == 0


def test_controller_publish_uses_local_sqlite_and_certified_reviewer(
    board, monkeypatch,
):
    """Masked profile state and the general DB initializer are not dependencies."""

    db_path, root = board
    backend = FakeGitHub()
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    _task_id, request = _claimed_builder(db_path, root)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("general profile/agent state must stay masked")

    monkeypatch.setattr("hermes_cli.profiles.profile_exists", forbidden)
    monkeypatch.setattr(kb, "connect_closing", forbidden)

    submitted = control.handle(request, peer_pid=PID, peer_uid=UID)
    assert submitted["state"] == "submitted"
    assert backend.submission_verifications == 1


def test_controller_refuses_sqlite_trigger_side_effects(board):
    db_path, _root = board
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TRIGGER unexpected AFTER UPDATE ON tasks BEGIN SELECT 1; END"
        )
    control = DeliveryControl(db_path, FakeGitHub(), allowed_worker_uid=UID)
    with pytest.raises(DeliveryControlError) as raised:
        control.prepare_recovery()
    assert raised.value.code == "database_schema_unsupported"


def test_socket_protocol_uses_real_peer_credentials_and_no_cloud_claims(
    board, caplog,
):
    db_path, root = board
    backend = FakeGitHub()
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    _task_id, request = _claimed_builder(db_path, root)
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    thread = threading.Thread(target=handle_connection, args=(server, control))
    thread.start()
    client.sendall(json.dumps({
        "schema": PROTOCOL_SCHEMA,
        "action": request.action,
        "task_id": request.task_id,
        "run_id": request.run_id,
        "claim_lock": request.claim_lock,
        "summary": request.summary,
    }).encode())
    client.shutdown(socket.SHUT_WR)
    response = json.loads(client.recv(32 * 1024))
    thread.join(timeout=5)
    client.close()
    server.close()

    assert response["ok"] is True
    assert response["state"] == "submitted"
    assert "token" not in json.dumps(response).lower()
    assert request.claim_lock not in json.dumps(response)
    assert request.claim_lock not in caplog.text
    assert "classification" not in response
    assert "merge_sha" not in response


def test_socket_request_read_is_timed_and_fails_safely():
    class TimedOutConnection:
        timeout = None
        response = b""

        def settimeout(self, value):
            self.timeout = value

        def getsockopt(self, *_args):
            return struct.pack("3i", PID, UID, os.getgid())

        def recv(self, _size):
            raise socket.timeout("slow client")

        def sendall(self, raw):
            self.response = raw

    connection = TimedOutConnection()
    handle_connection(connection, SimpleNamespace())
    response = json.loads(connection.response)
    assert connection.timeout == 5.0
    assert response["ok"] is False
    assert response["state"] == "rejected"
    assert "slow client" not in json.dumps(response)


def test_disconnected_client_cannot_crash_control_after_durable_work():
    raw = json.dumps({
        "schema": PROTOCOL_SCHEMA,
        "action": "builder_publish",
        "task_id": "t_12345678",
        "run_id": 42,
        "claim_lock": "worker-lock",
        "summary": "ready",
    }).encode()

    class DisconnectedConnection:
        chunks = [raw, b""]

        def settimeout(self, _value):
            pass

        def getsockopt(self, *_args):
            return struct.pack("3i", PID, UID, os.getgid())

        def recv(self, _size):
            return self.chunks.pop(0)

        def sendall(self, _raw):
            raise BrokenPipeError("peer left")

    calls = []
    control = SimpleNamespace(handle=lambda request, **peer: calls.append(
        (request, peer)
    ) or {
        "schema": PROTOCOL_SCHEMA,
        "ok": True,
        "state": "submitted",
        "task_id": "t_12345678",
        "run_id": 42,
    })
    handle_connection(DisconnectedConnection(), control)
    assert len(calls) == 1
    assert calls[0][0].claim_lock == "worker-lock"


@pytest.mark.parametrize(
    "change",
    [
        {"action": "shell"},
        {"task_id": "../../root"},
        {"run_id": "1; id"},
        {"claim_lock": "bad\ncapability"},
        {"extra": "field"},
    ],
)
def test_request_parser_rejects_injection_and_extra_fields(change):
    value = {
        "schema": PROTOCOL_SCHEMA,
        "action": "builder_publish",
        "task_id": "t_12345678",
        "run_id": 1,
        "claim_lock": "worker-lock",
        "summary": "ready",
    }
    value.update(change)
    with pytest.raises(DeliveryControlError):
        parse_request(json.dumps(value).encode())


def test_stale_claim_and_wrong_peer_cannot_publish(board):
    db_path, root = board
    backend = FakeGitHub()
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    _task_id, request = _claimed_builder(db_path, root)
    stale = ControlRequest(
        request.action, request.task_id, request.run_id, "different-lock", request.summary,
    )
    with pytest.raises(DeliveryControlError, match="stale"):
        control.handle(stale, peer_pid=PID, peer_uid=UID)
    with pytest.raises(DeliveryControlError, match="active worker"):
        control.handle(request, peer_pid=PID + 1, peer_uid=UID)
    with pytest.raises(DeliveryControlError, match="not authorized"):
        control.handle(request, peer_pid=PID, peer_uid=UID + 1)
    assert backend.publish_calls == 0


def test_branch_contract_and_source_are_rederived_not_worker_supplied(board):
    db_path, root = board
    backend = FakeGitHub()
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    task_id, request = _claimed_builder(db_path, root)
    with kb.connect_closing(db_path) as conn, kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET branch_name = ? WHERE id = ?",
            ("hermes/other-task;touch-pwned", task_id),
        )
    with pytest.raises(DeliveryControlError, match="canonical"):
        control.handle(request, peer_pid=PID, peer_uid=UID)
    assert backend.publish_calls == 0


def test_publish_race_detects_task_drift_before_native_submit(board):
    db_path, root = board
    backend = FakeGitHub()
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    task_id, request = _claimed_builder(db_path, root)

    def mutate():
        with kb.connect_closing(db_path) as conn, kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET title = ? WHERE id = ?", ("drifted", task_id),
            )

    backend.mutate_on_publish = mutate
    with pytest.raises(DeliveryControlError, match="changed"):
        control.handle(request, peer_pid=PID, peer_uid=UID)
    with kb.connect_closing(db_path) as conn:
        task = kb.get_task(conn, task_id)
        submissions = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "submitted_for_review"
        ]
    assert task.status == "shipping"
    assert submissions == []


def test_shipping_fence_refuses_every_ordinary_task_mutation(board):
    db_path, root = board
    backend = FakeGitHub()
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    task_id, request = _claimed_builder(db_path, root)
    with kb.connect_closing(db_path) as conn:
        other_id = kb.create_task(conn, title="unrelated dependency")

    refused: list[str] = []

    def assert_fenced():
        mutations = {
            "archive": lambda conn: kb.archive_task(conn, task_id),
            "delete": lambda conn: kb.delete_task(conn, task_id),
            "assign": lambda conn: kb.assign_task(conn, task_id, "other"),
            "reassign": lambda conn: kb.reassign_task(
                conn, task_id, "other", reclaim_first=True,
            ),
            "priority": lambda conn: kb.set_task_priority(conn, task_id, 99),
            "edit": lambda conn: kb.edit_task_fields(
                conn, task_id, title="drifted", body="drifted",
            ),
            "model": lambda conn: kb.set_model_override(conn, task_id, "other"),
            "reasoning": lambda conn: kb.set_reasoning_effort(conn, task_id, "low"),
            "workspace": lambda conn: kb.set_workspace_path(
                conn, task_id, root / "other",
            ),
            "branch": lambda conn: kb.set_branch_name(
                conn, task_id, "hermes/other",
            ),
            "schedule": lambda conn: kb.schedule_task(conn, task_id),
            "block": lambda conn: kb.block_task(conn, task_id, reason="stop"),
            "reclaim": lambda conn: kb.reclaim_task(conn, task_id, reason="stop"),
            "claim": lambda conn: kb.claim_task(conn, task_id),
            "review_claim": lambda conn: kb.claim_review_task(conn, task_id),
            "submit": lambda conn: kb.submit_task_for_review(
                conn,
                task_id,
                pull_request={
                    "pr_url": "https://github.com/clauseye-com/clauseye-contra-rope/pull/99",
                    "pr_number": 99,
                    "head_sha": HEAD,
                    "candidate_ref": "hermes/other",
                },
                reviewer_assignee="reviewer",
                expected_run_id=request.run_id,
            ),
            "complete": lambda conn: kb.complete_task(
                conn, task_id, result="not controller-owned",
            ),
            "request_changes": lambda conn: kb.request_task_changes(
                conn,
                task_id,
                reason="reject",
                reviewed_head_sha=HEAD,
                expected_run_id=request.run_id,
            ),
            "link": lambda conn: kb.link_tasks(conn, other_id, task_id),
            "unlink": lambda conn: kb.unlink_tasks(conn, other_id, task_id),
        }
        for name, mutation in mutations.items():
            with kb.connect_closing(db_path) as conn:
                with pytest.raises(kb.DeliveryOperationInProgressError) as raised:
                    mutation(conn)
            assert raised.value.code == "delivery_operation_in_progress"
            refused.append(name)
        with kb.connect_closing(db_path) as conn:
            task = kb.get_task(conn, task_id)
            run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (request.run_id,),
            ).fetchone()
            assert task.status == "shipping"
            assert task.title == "ship exact candidate"
            assert task.assignee == "builder"
            assert task.current_run_id == request.run_id
            assert run["ended_at"] is None
            assert kb.heartbeat_claim(
                conn,
                task_id,
                claimer=request.claim_lock,
                expected_run_id=request.run_id,
            )
            assert kb.heartbeat_worker(
                conn, task_id, expected_run_id=request.run_id,
            )

    backend.mutate_on_publish = assert_fenced
    response = control.handle(request, peer_pid=PID, peer_uid=UID)
    assert response["state"] == "submitted"
    assert set(refused) == {
        "archive", "delete", "assign", "reassign", "priority", "edit",
        "model", "reasoning", "workspace", "branch", "schedule", "block",
        "reclaim", "claim", "review_claim", "submit", "complete",
        "request_changes", "link", "unlink",
    }


@pytest.mark.parametrize("crash_point", ["after_push", "after_pr"])
def test_builder_restart_reconciles_ambiguous_remote_write_without_duplicate(
    board, crash_point,
):
    db_path, root = board
    backend = CrashRecoveringGitHub(crash_point)
    first = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    task_id, request = _claimed_builder(db_path, root)

    with pytest.raises(RuntimeError, match="simulated broker death"):
        first.handle(request, peer_pid=PID, peer_uid=UID)
    with kb.connect_closing(db_path) as conn:
        assert kb.get_task(conn, task_id).status == "shipping"

    restarted = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    restarted.prepare_recovery()
    result = restarted.reconcile_orphans()
    assert result == {
        "attempted": 1, "completed": 1, "pending": 0, "quarantined": 0,
    }
    assert backend.remote_pushes == 1
    assert backend.pull_request_creates == 1
    with kb.connect_closing(db_path) as conn:
        assert kb.get_task(conn, task_id).status == "review"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'submitted_for_review'",
            (task_id,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "crash_point", ["before_put", "after_put", "deployment_pending"],
)
def test_reviewer_restart_uses_durable_premerge_receipt_and_completes(
    board, crash_point,
):
    db_path, root = board
    builder = FakeGitHub()
    task_id, builder_request = _claimed_builder(db_path, root)
    DeliveryControl(db_path, builder, allowed_worker_uid=UID).handle(
        builder_request, peer_pid=PID, peer_uid=UID,
    )
    review_request = _claim_review(db_path, task_id)
    backend = CrashRecoveringReviewGitHub(crash_point)
    first = DeliveryControl(db_path, backend, allowed_worker_uid=UID)

    expected_error = DeliveryControlError if crash_point == "deployment_pending" else RuntimeError
    with pytest.raises(expected_error):
        first.handle(review_request, peer_pid=PID, peer_uid=UID)
    with kb.connect_closing(db_path) as conn:
        operation = conn.execute(
            "SELECT * FROM delivery_control_operations WHERE task_id = ? "
            "AND action = 'reviewer_complete'",
            (task_id,),
        ).fetchone()
        stages = json.loads(operation["receipt"])["stages"]
        assert stages["premerge"]["head_sha"] == HEAD
        assert kb.get_task(conn, task_id).status == "shipping"

    restarted = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    restarted.prepare_recovery()
    result = restarted.reconcile_orphans()
    assert result["completed"] == 1
    assert result["quarantined"] == 0
    assert backend.merge_calls == 1
    with kb.connect_closing(db_path) as conn:
        assert kb.get_task(conn, task_id).status == "done"


def test_externally_premerged_without_premerge_receipt_is_quarantined(board):
    db_path, root = board
    task_id, builder_request = _claimed_builder(db_path, root)
    DeliveryControl(db_path, FakeGitHub(), allowed_worker_uid=UID).handle(
        builder_request, peer_pid=PID, peer_uid=UID,
    )
    review_request = _claim_review(db_path, task_id)
    backend = CrashRecoveringReviewGitHub("none", externally_merged=True)
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)

    with pytest.raises(DeliveryControlError) as rejected:
        control.handle(review_request, peer_pid=PID, peer_uid=UID)
    assert rejected.value.code == "premerge_receipt_missing"
    with kb.connect_closing(db_path) as conn:
        operation = conn.execute(
            "SELECT * FROM delivery_control_operations WHERE task_id = ? "
            "AND action = 'reviewer_complete'",
            (task_id,),
        ).fetchone()
        assert operation["state"] == "quarantined"
        assert kb.get_task(conn, task_id).status == "shipping"
    assert backend.merge_calls == 0


def test_second_broker_cannot_steal_live_operation(board):
    db_path, root = board
    backend = FakeGitHub()
    first = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    second = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    _task_id, request = _claimed_builder(db_path, root)
    second_result = []

    def race_second_broker():
        with pytest.raises(DeliveryControlError) as pending:
            second.handle(request, peer_pid=PID, peer_uid=UID)
        second_result.append((pending.value.code, pending.value.pending))

    backend.mutate_on_publish = race_second_broker
    assert first.handle(request, peer_pid=PID, peer_uid=UID)["state"] == "submitted"
    assert second_result == [("delivery_operation_in_progress", True)]
    assert backend.publish_calls == 1


def test_missing_task_orphan_is_surfaced_and_quarantined(board):
    db_path, root = board
    backend = CrashRecoveringGitHub("after_push")
    first = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    task_id, request = _claimed_builder(db_path, root)
    with pytest.raises(RuntimeError):
        first.handle(request, peer_pid=PID, peer_uid=UID)
    # Simulate corruption/direct SQL outside supported lifecycle writers. The
    # broker must still surface the orphan instead of hiding it via an INNER JOIN.
    with kb.connect_closing(db_path) as conn, kb.write_txn(conn):
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    restarted = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    restarted.prepare_recovery()
    result = restarted.reconcile_orphans()
    assert result["quarantined"] == 1
    assert result["completed"] == 0
    with kb.connect_closing(db_path) as conn:
        operation = conn.execute(
            "SELECT state FROM delivery_control_operations WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert operation["state"] == "quarantined"
    assert backend.pull_request_creates == 0


def test_shipping_counts_against_global_and_profile_wip_caps(board):
    db_path, root = board
    backend = CrashRecoveringGitHub("after_push")
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    _shipping_id, request = _claimed_builder(db_path, root)
    with pytest.raises(RuntimeError):
        control.handle(request, peer_pid=PID, peer_uid=UID)
    with kb.connect_closing(db_path) as conn:
        kb.create_task(conn, title="next", assignee="builder")
        kb.recompute_ready(conn)
        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace, board=None: spawned.append(task.id),
            max_spawn=1,
            max_in_progress_per_profile=1,
        )
    assert spawned == []
    assert result.spawned == []


def test_reviewer_pending_is_retryable_and_does_not_complete(board):
    db_path, root = board
    backend = FakeGitHub()
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    task_id, builder_request = _claimed_builder(db_path, root)
    control.handle(builder_request, peer_pid=PID, peer_uid=UID)
    review_request = _claim_review(db_path, task_id)
    backend.pending = True

    with pytest.raises(DeliveryControlError) as raised:
        control.handle(review_request, peer_pid=PID, peer_uid=UID)
    assert raised.value.pending is True
    assert _response_for_error(raised.value)["state"] == "pending"
    with kb.connect_closing(db_path) as conn:
        assert kb.get_task(conn, task_id).status == "shipping"


@pytest.mark.parametrize("field,value", [("status", "done"), ("outcome", "completed")])
def test_controller_refuses_nonopen_run_before_candidate_inspection(board, field, value):
    db_path, root = board
    task_id, request = _claimed_builder(db_path, root)
    backend = FakeGitHub()
    backend.candidate_identity = lambda _task: pytest.fail("inspected inconsistent run")
    with kb.connect_closing(db_path) as conn, kb.write_txn(conn):
        conn.execute(
            f"UPDATE task_runs SET {field} = ? WHERE id = ?",
            (value, request.run_id),
        )
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    with pytest.raises(DeliveryControlError) as refused:
        control.handle(request, peer_pid=PID, peer_uid=UID)
    assert refused.value.code == "run_stale"
    assert backend.publish_calls == 0
    with kb.connect_closing(db_path) as conn:
        assert kb.get_task(conn, task_id).status == "running"
        assert conn.execute("SELECT COUNT(*) FROM delivery_control_operations").fetchone()[0] == 0


def test_controller_preserves_held_worker_before_candidate_inspection(board):
    db_path, root = board
    task_id, request = _claimed_builder(db_path, root)
    with kb.connect_closing(db_path) as conn, kb.write_txn(conn):
        kb._append_event(
            conn, task_id, "controlled_worker_held", {"request_id": "held-old-attempt"},
            run_id=request.run_id,
        )
    backend = FakeGitHub()
    backend.candidate_identity = lambda _task: pytest.fail("inspected held worker")
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    with pytest.raises(DeliveryControlError) as refused:
        control.handle(request, peer_pid=PID, peer_uid=UID)
    assert refused.value.code == "worker_cleanup_pending"
    assert refused.value.pending
    assert backend.publish_calls == 0
    with kb.connect_closing(db_path) as conn:
        assert kb.get_task(conn, task_id).status == "running"
        assert kb._controlled_worker_pending(conn, task_id)
        assert conn.execute("SELECT COUNT(*) FROM delivery_control_operations").fetchone()[0] == 0


def test_controller_retains_shipping_fence_when_creation_base_changes(board):
    db_path, root = board
    task_id, request = _claimed_builder(db_path, root)
    with kb.connect_closing(db_path) as conn, kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worktree_base_sha = ? WHERE id = ?",
            ("c" * 40, task_id),
        )
    backend = FakeGitHub()

    def mutate_base():
        with kb.connect_closing(db_path) as conn, kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worktree_base_sha = ? WHERE id = ?",
                ("d" * 40, task_id),
            )

    backend.mutate_on_publish = mutate_base
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    with pytest.raises(DeliveryControlError) as refused:
        control.handle(request, peer_pid=PID, peer_uid=UID)
    assert refused.value.code == "task_drifted"
    assert backend.submission_verifications == 0
    with kb.connect_closing(db_path) as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "shipping"
        assert task.worktree_base_sha == "d" * 40
        assert kb._latest_review_submission_record(conn, task_id) is None
        operation = kb._active_delivery_operation(conn, task_id)
        assert operation is not None
        assert operation["state"] == "in_progress"


@pytest.mark.parametrize("drift", ["held_worker", "run_status", "run_outcome"])
def test_controller_retains_operation_when_worker_changes_during_publish(board, drift):
    db_path, root = board
    task_id, request = _claimed_builder(db_path, root)
    backend = FakeGitHub()

    def change_worker():
        with kb.connect_closing(db_path) as conn, kb.write_txn(conn):
            if drift == "held_worker":
                kb._append_event(
                    conn, task_id, "controlled_worker_held", {"request_id": "pending"},
                    run_id=request.run_id,
                )
            elif drift == "run_status":
                conn.execute("UPDATE task_runs SET status = 'done' WHERE id = ?", (request.run_id,))
            else:
                conn.execute("UPDATE task_runs SET outcome = 'completed' WHERE id = ?", (request.run_id,))

    backend.mutate_on_publish = change_worker
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    with pytest.raises(DeliveryControlError) as refused:
        control.handle(request, peer_pid=PID, peer_uid=UID)
    assert refused.value.code == ("worker_cleanup_pending" if drift == "held_worker" else "run_stale")
    assert backend.submission_verifications == 0
    with kb.connect_closing(db_path) as conn:
        assert kb.get_task(conn, task_id).status == "shipping"
        assert kb._latest_review_submission_record(conn, task_id) is None
        assert kb._active_delivery_operation(conn, task_id)["state"] == "in_progress"


def test_controller_rejects_missing_creation_base_column_before_inspection(board):
    db_path, root = board
    _task_id, request = _claimed_builder(db_path, root)
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE tasks DROP COLUMN worktree_base_sha")
    backend = FakeGitHub()
    backend.candidate_identity = lambda _task: pytest.fail("inspected incomplete schema")
    control = DeliveryControl(db_path, backend, allowed_worker_uid=UID)
    with pytest.raises(DeliveryControlError) as refused:
        control.handle(request, peer_pid=PID, peer_uid=UID)
    assert refused.value.code == "database_schema_unsupported"
    assert backend.publish_calls == 0
