"""Explicit, local repair of missing historical delivery records.

Preview is read-only. Apply accepts only the reviewed, unchanged snapshots and
defers whole graphs with uncertain ownership. This module neither verifies old
deliveries nor stops workers; the existing owning controller must drain them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_board_identity import board_binding
from hermes_cli.legacy_delivery_audit import audit_legacy_done, audit_legacy_done_task


MANIFEST_SCHEMA = "hermes-legacy-delivery-repair/v1"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 1000
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def require_operator() -> None:
    """Run before reading a manifest path, and again at the native mutation."""
    kb._assert_not_delegated_child_mutation()
    if any(os.environ.get(key) for key in (
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_TERMINAL_SANDBOX", "HERMES_KANBAN_DELIVERY_CONTROL",
    )):
        raise PermissionError("delivery repair requires an operator outside a worker run")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@contextlib.contextmanager
def _read_snapshot(conn: sqlite3.Connection):
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            conn.rollback()


def preview_delivery_repair(conn: sqlite3.Connection, *, board: str,
                            task_ids=None, limit: int = MAX_ENTRIES) -> dict:
    """Create a deterministic manifest without migration or external work."""
    require_operator()
    board = kb._normalize_board_slug(board)
    if not board or type(limit) is not int or not 1 <= limit <= MAX_ENTRIES:
        raise ValueError("a board and a limit between 1 and 1000 are required")
    with _read_snapshot(conn):
        binding = board_binding(conn, required=True)
        if binding["state"] != "active":
            raise ValueError("retired boards cannot be repaired")
        report = audit_legacy_done(conn, task_ids=task_ids, limit=limit)
        if binding != board_binding(conn, required=True):
            raise ValueError("board identity changed during preview")
        manifest = {"schema": MANIFEST_SCHEMA, "board": board,
                    "database": binding, "audit": report}
        manifest["digest"] = _digest(manifest)
    if len(_canonical(manifest)) > MAX_MANIFEST_BYTES:
        raise ValueError("repair manifest is too large; select fewer tasks")
    return manifest


def _validate_manifest(manifest: object, *, board: str) -> list[dict]:
    if not isinstance(manifest, dict) or len(_canonical(manifest)) > MAX_MANIFEST_BYTES:
        raise ValueError("invalid repair manifest")
    payload = {key: value for key, value in manifest.items() if key != "digest"}
    if (manifest.get("schema") != MANIFEST_SCHEMA
            or manifest.get("board") != kb._normalize_board_slug(board)
            or not isinstance(manifest.get("database"), dict)
            or manifest.get("digest") != _digest(payload)):
        raise ValueError("repair manifest integrity or board binding does not match")
    report = manifest.get("audit")
    if not isinstance(report, dict):
        raise ValueError("repair manifest has no audit")
    entries = report.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise ValueError("invalid repair entry count")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid repair entry")
        task_id, digest = entry.get("task_id"), entry.get("snapshot_digest")
        if (not isinstance(task_id, str) or not task_id or len(task_id) > 200
                or task_id in seen or not isinstance(digest, str)
                or not _DIGEST.fullmatch(digest)):
            raise ValueError("invalid or duplicate repair identity")
        seen.add(task_id)
    return entries


def apply_delivery_repair(conn: sqlite3.Connection, manifest: dict, *, board: str) -> dict:
    """Hold only listed, unchanged gaps whose entire graph is inactive.

    Each graph commits independently. Changed, overlapping and active graphs
    stay unchanged. No worker signaling, notifications, cleanup or promotion
    occurs here. Consistent records remain unverified, never automatically
    invalidated merely because no independent historical audit has run.
    """
    require_operator()
    entries = _validate_manifest(manifest, board=board)
    if conn.in_transaction:
        raise RuntimeError("repair requires its own write transaction")
    if board_binding(conn, required=True) != manifest["database"]:
        raise ValueError("repair manifest belongs to a different database")
    results = []
    for expected in entries:
        task_id = expected["task_id"]
        with kb.write_txn(conn):
            require_operator()
            if board_binding(conn, required=True) != manifest["database"]:
                raise ValueError("board identity changed before repair")
            current = audit_legacy_done_task(conn, task_id)
            if current is None or current["snapshot_digest"] != expected["snapshot_digest"]:
                results.append({"task_id": task_id, "outcome": "changed_or_missing"})
                continue
            if current["ownership_fences"]:
                results.append({"task_id": task_id, "outcome": "ownership_fenced"})
                continue
            if current["classification"] not in {"record_missing", "record_inconsistent"}:
                results.append({"task_id": task_id, "outcome": "no_local_repair"})
                continue
            # Human-owned gates are preserved even when their surrounding
            # dependency graph otherwise has no worker ownership.
            if any(kb.get_task(conn, node["id"]).block_kind == "needs_input"
                   for node in current["nodes"]):
                results.append({"task_id": task_id, "outcome": "human_gate"})
                continue
            now = int(time.time())
            reason = "delivery-proof-missing: historical completion needs verified delivery evidence"
            cursor = conn.execute(
                "UPDATE tasks SET status = 'blocked', block_kind = 'capability', "
                "completed_at = NULL, last_failure_error = ? WHERE id = ? AND status = 'done'",
                (reason, task_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("delivery repair lost its task snapshot")
            payload = {"manifest_digest": manifest["digest"],
                       "snapshot_digest": current["snapshot_digest"],
                       "reason_codes": current["reason_codes"],
                       "block_kind": "capability", "resume_status": "review"}
            kb._append_event(conn, task_id, "legacy_delivery_gap", payload)
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "delivery-reconciler",
                 "Moved out of Done because its recorded delivery evidence is missing or "
                 "inconsistent. The original result and run history are preserved. "
                 "Independent delivery verification is required before completion.", now),
            )
            invalidated = []
            for node in current["nodes"]:
                child_id, status = node["id"], node["status"]
                if child_id == task_id or status not in {"ready", "review", "done"}:
                    continue
                resume = "review" if status == "review" else kb._resume_status_from_events(conn, child_id)
                if status == "done":
                    completed_run = conn.execute(
                        "SELECT id FROM task_runs WHERE task_id = ? AND outcome = 'completed' "
                        "AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1", (child_id,),
                    ).fetchone()
                    if completed_run is not None:
                        resume = kb._retry_status_for_run(conn, child_id, completed_run["id"])
                changed = conn.execute(
                    "UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ? AND status = ?",
                    (child_id, status),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("delivery repair lost its descendant snapshot")
                kb._append_event(conn, child_id, "dependency_wait", {
                    "parent": task_id, "reason": "legacy_delivery_gap",
                    "manifest_digest": manifest["digest"], "source_status": status,
                    "resume_status": resume,
                })
                invalidated.append(child_id)
            results.append({"task_id": task_id, "outcome": "repaired",
                            "invalidated_descendants": invalidated})
    return {"schema": MANIFEST_SCHEMA, "manifest_digest": manifest["digest"],
            "repaired_count": sum(row["outcome"] == "repaired" for row in results),
            "results": results}
