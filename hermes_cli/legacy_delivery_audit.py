"""Cheap, read-only diagnosis of locally recorded historical Done deliveries.

Matching database records are claims, not independent proof. This module never
returns ``verified`` and never calls the active-attempt delivery verifier. An
operator may use an unchanged snapshot to quarantine missing/inconsistent local
records; ownership fences require the owning lifecycle to finish first.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

from hermes_cli import kanban_db as kb

SCHEMA = "hermes-legacy-delivery-audit/v1"
CLASSIFICATIONS = (
    "record_missing", "record_inconsistent", "verification_required", "not_applicable",
)
MAX_TASKS = 1000
MAX_GRAPH_NODES = 10000
MAX_SNAPSHOT_ROWS = 100000
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
_TABLES = (
    "tasks", "task_runs", "task_events", "task_comments", "task_attachments",
    "delivery_control_operations", "task_links",
)


class AuditSnapshotError(ValueError):
    """No complete audit snapshot could be produced; safe to display verbatim."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@contextmanager
def _read_snapshot(conn: sqlite3.Connection):
    # Apply recomputes this report within its own write transaction. Never
    # commit or roll back a transaction owned by the caller.
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN DEFERRED")
    try:
        yield
    except sqlite3.DatabaseError as exc:
        raise AuditSnapshotError("snapshot_unavailable") from exc
    finally:
        if own_transaction and conn.in_transaction:
            conn.rollback()


def _rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    rows = cursor.fetchmany(MAX_SNAPSHOT_ROWS + 1)
    if len(rows) > MAX_SNAPSHOT_ROWS:
        raise AuditSnapshotError("snapshot_too_large")
    return [dict(zip(names, row)) for row in rows]


def _canonical(value):
    # SQLite can contain BLOBs even in TEXT columns. Preserve their exact bytes
    # in the digest without returning them or guessing their encoding.
    if isinstance(value, bytes):
        return {"sqlite_blob_hex": value.hex()}
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _digest(snapshot):
    raw = json.dumps(
        _canonical(snapshot), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("ascii")
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise AuditSnapshotError("snapshot_too_large")
    return hashlib.sha256(raw).hexdigest()


def _task_id(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise AuditSnapshotError("task_id_invalid")
    return value


def _snapshot(conn, task_id):
    reachable = _rows(conn, """
        WITH RECURSIVE affected(id) AS (
            SELECT ? UNION
            SELECT child_id FROM task_links JOIN affected ON parent_id = affected.id
        ) SELECT id FROM affected LIMIT ?
    """, (task_id, MAX_GRAPH_NODES + 1))
    if len(reachable) > MAX_GRAPH_NODES:
        raise AuditSnapshotError("snapshot_too_large")
    ids = sorted(_task_id(row["id"]) for row in reachable)
    snapshot = {"schema": SCHEMA, "root_task_id": task_id, **{t: [] for t in _TABLES}}
    for offset in range(0, len(ids), 400):
        batch = ids[offset:offset + 400]
        marks = ",".join("?" for _ in batch)
        for table in _TABLES:
            if table == "task_links":
                sql = f"SELECT * FROM {table} WHERE parent_id IN ({marks}) OR child_id IN ({marks})"
                params = batch + batch
            else:
                key = "id" if table == "tasks" else "task_id"
                sql = f"SELECT * FROM {table} WHERE {key} IN ({marks})"
                params = batch
            snapshot[table].extend(_rows(conn, sql, params))
        if sum(len(snapshot[t]) for t in _TABLES) > MAX_SNAPSHOT_ROWS:
            raise AuditSnapshotError("snapshot_too_large")
    # A task may point to a foreign run. Bind that row too, so drift of the
    # foreign ownership cannot leave an unchanged manifest digest.
    captured_runs = {row["id"] for row in snapshot["task_runs"]}
    for task in snapshot["tasks"]:
        run_id = task.get("current_run_id")
        if run_id is not None and run_id not in captured_runs:
            rows = _rows(conn, "SELECT * FROM task_runs WHERE id = ?", (run_id,))
            snapshot["task_runs"].extend(rows)
            captured_runs.add(run_id)
    if {row["id"] for row in snapshot["tasks"]} != set(ids):
        raise AuditSnapshotError("snapshot_incomplete")
    for table in _TABLES:
        keys = ("parent_id", "child_id") if table == "task_links" else (
            ("op_id",) if table == "delivery_control_operations" else ("id",)
        )
        dedup = {tuple(row[k] for k in keys): row for row in snapshot[table]}
        snapshot[table] = [dedup[key] for key in sorted(dedup)]
    if sum(len(snapshot[t]) for t in _TABLES) > MAX_SNAPSHOT_ROWS:
        raise AuditSnapshotError("snapshot_too_large")
    return snapshot


def _ownership(conn, snapshot):
    nodes = []
    all_runs = {row["id"]: row for row in snapshot["task_runs"]}
    for task in snapshot["tasks"]:
        task_id = task["id"]
        fences = set()
        status = task["status"]
        if status not in kb.VALID_STATUSES:
            fences.add("unknown_status")
            status = "unknown"
        if status in {"running", "shipping"}:
            fences.add(status)
        if kb._controlled_worker_pending(conn, task_id):
            fences.add("held_worker")
        if kb._active_delivery_operation(conn, task_id) is not None:
            fences.add("delivery_operation")
        operations = [r for r in snapshot["delivery_control_operations"] if r["task_id"] == task_id]
        if any(r["state"] not in {"in_progress", "remote_applied", "quarantined", "committed", "rejected"} for r in operations):
            fences.add("unknown_operation_state")
        runs = [r for r in all_runs.values() if r["task_id"] == task_id]
        # Historical terminal runs may retain their old PID/claim. They bind
        # the snapshot but cannot establish current worker ownership.
        uncertain_runs = [r for r in runs if r["ended_at"] is None or r["status"] == "running" or r["outcome"] is None]
        for row in [task, *uncertain_runs]:
            if row.get("claim_lock") is not None or row.get("claim_expires") is not None:
                fences.add("claim_present")
            if row.get("worker_pid") is not None:
                fences.add("worker_pid_present")
        if any(r["ended_at"] is None for r in runs):
            fences.add("open_run")
        if any(r["ended_at"] is not None and (r["status"] == "running" or r["outcome"] is None) for r in runs):
            fences.add("run_identity_mismatch")
        current_id = task.get("current_run_id")
        if current_id is not None:
            fences.add("current_run_pointer")
            current = all_runs.get(current_id)
            if current is None:
                fences.add("missing_run")
            elif current["task_id"] != task_id:
                fences.add("foreign_run")
            elif current["ended_at"] is not None:
                fences.add("ended_run_pointer")
            elif any(task.get(k) != current.get(k) for k in ("claim_lock", "worker_pid")):
                fences.add("run_identity_mismatch")
        nodes.append({"id": task_id, "status": status, "ownership_fences": sorted(fences)})
    return nodes


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _classify(snapshot, entry):
    task_id = entry["task_id"]
    task = next(row for row in snapshot["tasks"] if row["id"] == task_id)
    missing, inconsistent = set(), set()
    if task.get("body") is not None and not isinstance(task["body"], str):
        raise AuditSnapshotError("task_body_invalid")
    policy = kb._completion_delivery_policy(task)
    entry["contract_hash"] = policy["contract_hash"]
    if policy["pr_gate"] == "none" and not policy["contract_present"] and not policy["deployment_required"]:
        entry.update(classification="not_applicable", reason_codes=["no_delivery_contract_or_code_workspace"])
        return
    if policy["contract_present"] and not policy["contract_valid"]:
        inconsistent.add("delivery_contract_invalid")
    elif policy["pr_gate"] == "merge" and not policy["contract_valid"]:
        missing.add("delivery_contract_missing")

    events = [r for r in snapshot["task_events"] if r["task_id"] == task_id]
    runs = [r for r in snapshot["task_runs"] if r["task_id"] == task_id]
    completed = [r for r in runs if r["outcome"] == "completed"]
    run = max(completed, key=lambda r: r["id"], default=None)
    completion = max((e for e in events if e["kind"] == "completed"), key=lambda e: e["id"], default=None)
    submission = max((e for e in events if e["kind"] == "submitted_for_review"), key=lambda e: e["id"], default=None)
    entry.update(
        completion_run_id=run["id"] if run else None,
        completion_event_id=completion["id"] if completion else None,
        submission_event_id=submission["id"] if submission else None,
    )
    if run is None:
        missing.add("completed_run_missing")
    elif run["ended_at"] is None or run["status"] not in {"done", "completed"}:
        inconsistent.add("completed_run_not_terminal")
    if completion is None:
        missing.add("completed_event_missing")
    elif run is None or completion["run_id"] != run["id"]:
        inconsistent.add("completion_run_mismatch")
    metadata = _object(run["metadata"]) if run else None
    payload = _object(completion["payload"]) if completion else None
    if run and run["metadata"] is not None and metadata is None:
        inconsistent.add("run_metadata_invalid")
    if completion and payload is None:
        inconsistent.add("completed_payload_invalid")
    delivery = metadata.get("delivery") if metadata else None
    event_delivery = payload.get("delivery") if payload else None
    if delivery is None or event_delivery is None:
        missing.add("delivery_record_missing")
    elif not isinstance(delivery, dict) or not isinstance(event_delivery, dict):
        inconsistent.add("delivery_record_invalid")
    elif delivery != event_delivery:
        inconsistent.add("delivery_record_mismatch")

    def contract_binding(record):
        if isinstance(record, dict) and "contract_hash" in record and record["contract_hash"] != policy["contract_hash"]:
            inconsistent.add("contract_hash_mismatch")

    recorded_policy = payload.get("delivery_policy") if payload else None
    if policy["contract_present"]:
        if not isinstance(recorded_policy, dict):
            missing.add("completion_policy_missing")
        else:
            for key in ("contract_hash", "domain", "target", "pr_gate", "deployment_required"):
                if key not in recorded_policy:
                    missing.add("completion_policy_incomplete")
                elif recorded_policy[key] != policy[key]:
                    inconsistent.add("completion_policy_mismatch")
    contract_binding(recorded_policy)
    if isinstance(delivery, dict):
        contract_binding(delivery)
        gate = policy["pr_gate"]
        expected = "merged_pr" if gate == "merge" else "reviewed_pr" if gate == "review" else (
            "deployed_revision" if policy["deployment_required"] else "no_merge_expected"
        )
        if delivery.get("classification") != expected:
            inconsistent.add("delivery_classification_mismatch")
        if expected == "no_merge_expected":
            if policy["contract_present"] and not delivery.get("contract_hash"):
                missing.add("delivery_contract_hash_missing")
            try:
                kb._normalize_no_merge_expected(delivery, policy=policy)
            except kb.DeliveryEvidenceError as exc:
                inconsistent.add(exc.code)
        if gate in {"merge", "review"}:
            try:
                kb._canonical_pr_record(delivery, policy=policy, require_candidate_ref=False)
            except kb.DeliveryEvidenceError as exc:
                inconsistent.add(exc.code)
        if gate == "review" and delivery.get("verdict") not in {"approved", "changes_requested", "commented"}:
            inconsistent.add("review_verdict_invalid")
        if gate == "merge":
            if not kb._IMMUTABLE_GIT_OID_RE.fullmatch(str(delivery.get("merge_sha") or "")):
                inconsistent.add("merge_sha_invalid")
            sub = _object(submission["payload"]) if submission else None
            if submission is None:
                missing.add("review_submission_missing")
            elif sub is None:
                inconsistent.add("review_submission_invalid")
            else:
                contract_binding(sub)
                if not sub.get("contract_hash"):
                    missing.add("submission_contract_hash_missing")
                if completion and submission["id"] >= completion["id"]:
                    inconsistent.add("submission_after_completion")
                if any(sub.get(key) != delivery.get(key) for key in ("pr_url", "pr_number", "head_sha")):
                    inconsistent.add("review_candidate_mismatch")
                builder_id = sub.get("builder_run_id")
                builder = next((r for r in runs if r["id"] == builder_id), None)
                if builder is None:
                    missing.add("submission_builder_run_missing")
                elif submission["run_id"] != builder_id or builder["ended_at"] is None:
                    inconsistent.add("submission_builder_run_mismatch")
                if builder:
                    builder_metadata = _object(builder["metadata"])
                    if builder_metadata and "review_submission" in builder_metadata and builder_metadata["review_submission"] != sub:
                        inconsistent.add("submission_record_mismatch")
                if run and (builder_id == run["id"] or (
                    sub.get("executor_assignee") and sub.get("executor_assignee") == run.get("profile")
                )):
                    inconsistent.add("review_not_independent")
                if run and sub.get("reviewer_assignee") != run.get("profile"):
                    inconsistent.add("review_profile_mismatch")
            claimed = [e for e in events if e["kind"] == "claimed" and run and e["run_id"] == run["id"]]
            claim = _object(claimed[-1]["payload"]) if claimed else None
            if claim is None:
                missing.add("review_claim_record_missing")
            elif claim.get("source_status") != "review":
                inconsistent.add("completion_not_review_attempt")
        if policy["deployment_required"]:
            if delivery.get("deployment") is None:
                missing.add("deployment_record_missing")
            else:
                try:
                    kb._normalize_deployment_evidence(
                        delivery["deployment"], policy=policy,
                        source_sha=delivery.get("merge_sha") or delivery.get("source_sha"),
                        pr_url=delivery.get("pr_url"),
                    )
                except kb.DeliveryEvidenceError as exc:
                    inconsistent.add(exc.code)
        elif "deployment" in delivery:
            inconsistent.add("unexpected_deployment_evidence")
        verification = delivery.get("live_verification")
        if verification is not None and not isinstance(verification, dict):
            inconsistent.add("verification_record_invalid")
        if isinstance(verification, dict):
            contract_binding(verification)
            pr = verification.get("pr")
            if isinstance(pr, dict) and any(key in pr and pr[key] != delivery.get(key) for key in ("head_sha", "merge_sha")):
                inconsistent.add("verification_candidate_mismatch")
            acceptance = verification.get("acceptance")
            if acceptance is not None and not isinstance(acceptance, dict):
                inconsistent.add("acceptance_record_invalid")
            if isinstance(acceptance, dict):
                contract_binding(acceptance)
                bindings = {
                    "task_id": task_id, "candidate_sha": delivery.get("head_sha"),
                    "review_run_id": run["id"] if run else None,
                    "review_profile": run["profile"] if run else None,
                    "submission_event_id": submission["id"] if submission else None,
                }
                if any(key in acceptance and acceptance[key] != value for key, value in bindings.items()):
                    inconsistent.add("acceptance_binding_mismatch")
                if acceptance.get("verdict") not in {None, "pass"}:
                    inconsistent.add("acceptance_not_passed")
                for event in events:
                    if event["kind"] == "acceptance_verified" and run and event["run_id"] == run["id"]:
                        expected_acceptance = {**acceptance, "submission_event_id": submission["id"] if submission else None}
                        if _object(event["payload"]) != expected_acceptance:
                            inconsistent.add("acceptance_record_mismatch")
    # Optional controller receipts can reveal contradictions, but their absence
    # is not retroactively treated as a historical failure.
    for op in snapshot["delivery_control_operations"]:
        if op["task_id"] != task_id or op["state"] != "committed" or op["action"] != "reviewer_complete":
            continue
        if run and op["run_id"] == run["id"]:
            if isinstance(delivery, dict) and op["candidate_head"] != delivery.get("head_sha"):
                inconsistent.add("controller_candidate_mismatch")
            if submission and op["submission_event_id"] != submission["id"]:
                inconsistent.add("controller_submission_mismatch")
    entry["classification"] = "record_inconsistent" if inconsistent else "record_missing" if missing else "verification_required"
    entry["reason_codes"] = sorted(inconsistent | missing) or ["independent_historical_verification_required"]


def _audit_task(conn, task_id):
    task = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None or task[0] != "done":
        return None
    snapshot = _snapshot(conn, task_id)
    nodes = _ownership(conn, snapshot)
    entry = {
        "task_id": task_id, "classification": None, "reason_codes": [],
        "contract_hash": None, "completion_run_id": None,
        "completion_event_id": None, "submission_event_id": None,
        "snapshot_digest": _digest(snapshot), "nodes": nodes,
        "ownership_fences": sorted({code for node in nodes for code in node["ownership_fences"]}),
    }
    _classify(snapshot, entry)
    return entry


def audit_legacy_done_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    """Audit one Done task and its entire graph without changing any record."""
    task_id = _task_id(task_id)
    with _read_snapshot(conn):
        return _audit_task(conn, task_id)


def audit_legacy_done(
    conn: sqlite3.Connection, *, task_ids: Iterable[str] | None = None, limit: int = 1000,
) -> dict[str, Any]:
    """Read-only, deterministic, JSON-safe report from one connection snapshot.

    ``limited`` signals more Done rows than requested. Oversized individual
    graphs raise instead of emitting a partial digest usable by reconciliation.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_TASKS:
        raise AuditSnapshotError("audit_limit_invalid")
    selected = None
    if task_ids is not None:
        if isinstance(task_ids, (str, bytes)):
            raise AuditSnapshotError("task_ids_invalid")
        selected = set()
        for index, task_id in enumerate(task_ids):
            if index >= MAX_TASKS:
                raise AuditSnapshotError("audit_limit_invalid")
            selected.add(_task_id(task_id))
    with _read_snapshot(conn):
        if selected is None:
            ids = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE status = 'done' ORDER BY id LIMIT ?", (limit + 1,))]
        else:
            ids = []
            for task_id in sorted(selected):
                row = conn.execute("SELECT id FROM tasks WHERE id = ? AND status = 'done'", (task_id,)).fetchone()
                if row:
                    ids.append(row[0])
        entries = [_audit_task(conn, _task_id(task_id)) for task_id in ids[:limit]]
        return {
            "schema": SCHEMA, "scanned_count": len(entries), "limited": len(ids) > limit,
            "counts": {kind: sum(e["classification"] == kind for e in entries) for kind in CLASSIFICATIONS},
            "entries": entries,
        }
