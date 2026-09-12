"""Historical claims are diagnosed locally, never promoted to verified proof."""

import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import legacy_delivery_audit as audit
from tests.hermes_cli import test_delivery_control as fixtures

board = fixtures.board


def _done(conn, *, noncode=False, deploy=False, body=None):
    if body is None:
        body = fixtures._contract(deploy=deploy)
        if noncode:
            body = body.replace("domain: coding", "domain: audit").replace("target: github-merge", "target: artifact-file")
    task_id = kb.create_task(conn, title="historical fixture", body=body)
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    policy = kb._completion_delivery_policy(kb.get_task(conn, task_id))
    builder = conn.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome) VALUES (?,'builder','review',1,2,'submitted_for_review')",
        (task_id,),
    ).lastrowid
    delivery = {
        "classification": "merged_pr", "pr_url": "https://github.com/example/project/pull/17",
        "pr_number": 17, "head_sha": fixtures.HEAD, "merge_sha": fixtures.MERGE,
        "live_verification": {"verified_at": "2020-01-01T00:00:00Z", "acceptance": {
            "verdict": "pass", "contract_hash": policy["contract_hash"],
        }},
    }
    if noncode:
        delivery = kb._normalize_no_merge_expected({
            "classification": "no_merge_expected", "reason": "Read-only audit report attached to the task.",
        }, policy=policy)
    if deploy:
        delivery["deployment"] = {
            "environment": "production", "revision": "historical-revision",
            "health_status": "healthy", "health_reference": "local-record",
            "health_checked_at": "2020-01-01T00:00:00Z",
            "workflow_run_url": "https://github.com/example/project/actions/runs/8",
            "workflow_run_id": 8, "source_sha": fixtures.MERGE,
            "verification": {"verifier": "clauseye-production-probe", "status": "passed", "reference": "local-record"},
        }
    run_id = conn.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) VALUES (?,'reviewer','done',3,4,'completed',?)",
        (task_id, json.dumps({"delivery": delivery})),
    ).lastrowid
    submission = {
        "pr_url": delivery.get("pr_url"), "pr_number": delivery.get("pr_number"),
        "head_sha": fixtures.HEAD, "candidate_ref": "hermes/candidate",
        "contract_hash": policy["contract_hash"], "builder_run_id": builder,
        "executor_assignee": "builder", "reviewer_assignee": "reviewer",
    }
    kb._append_event(conn, task_id, "submitted_for_review", submission, run_id=builder)
    conn.execute("UPDATE task_runs SET metadata = ? WHERE id = ?", (json.dumps({"review_submission": submission}), builder))
    kb._append_event(conn, task_id, "claimed", {"source_status": "review"}, run_id=run_id)
    kb._append_event(conn, task_id, "completed", {"delivery": delivery, "delivery_policy": policy}, run_id=run_id)
    return task_id, run_id


def _rewrite_delivery(conn, task_id, run_id, change):
    run = json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0])
    change(run["delivery"])
    conn.execute("UPDATE task_runs SET metadata = ? WHERE id = ?", (json.dumps(run), run_id))
    row = conn.execute("SELECT id,payload FROM task_events WHERE task_id = ? AND kind = 'completed'", (task_id,)).fetchone()
    event = json.loads(row["payload"])
    event["delivery"] = run["delivery"]
    conn.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(event), row["id"]))


def _operation(conn, task_id, run_id, state="committed", **changes):
    values = {
        "op_id": "fixture-op", "task_id": task_id, "run_id": run_id,
        "action": "reviewer_complete", "request_sha256": "a" * 64,
        "claim_sha256": "b" * 64, "summary": "fixture", "candidate_head": fixtures.HEAD,
        "submission_event_id": conn.execute("SELECT id FROM task_events WHERE task_id = ? AND kind = 'submitted_for_review'", (task_id,)).fetchone()[0],
        "peer_uid": 1000, "owner_instance": "fixture", "state": state, "created_at": 1, "updated_at": 1,
        **changes,
    }
    conn.execute(
        f"INSERT INTO delivery_control_operations ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )


def _rows(conn):
    return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in audit._TABLES}


@pytest.mark.parametrize("noncode,deploy", [(False, False), (True, False), (False, True)])
def test_matching_claims_are_never_verified_and_do_not_write(board, noncode, deploy):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, _ = _done(conn, noncode=noncode, deploy=deploy)
        before, changes = _rows(conn), conn.total_changes
        report = audit.audit_legacy_done(conn)
        entry = report["entries"][0]
        assert entry["classification"] == "verification_required", entry
        assert entry["ownership_fences"] == []
        assert entry["task_id"] == task_id
        assert report == audit.audit_legacy_done(conn)
        assert _rows(conn) == before and conn.total_changes == changes
        assert conn.in_transaction is False


def test_native_controller_records_are_consistent_without_calling_verifier_again(board, monkeypatch):
    from hermes_cli.delivery_control import DeliveryControl

    path, root = board
    task_id, request = fixtures._claimed_builder(path, root)
    control = DeliveryControl(path, fixtures.FakeGitHub(), allowed_worker_uid=fixtures.UID)
    control.handle(request, peer_pid=fixtures.PID, peer_uid=fixtures.UID)
    request = fixtures._claim_review(path, task_id)
    control.handle(request, peer_pid=fixtures.PID, peer_uid=fixtures.UID)
    monkeypatch.setattr(fixtures.FakeGitHub, "verify_terminal", lambda *_a, **_k: pytest.fail("historical active verifier called"))
    with kb.connect_closing(path) as conn:
        entry = audit.audit_legacy_done_task(conn, task_id)
        assert entry["classification"] == "verification_required", entry
        assert entry["ownership_fences"] == []


@pytest.mark.parametrize("table,where,reason", [
    ("task_runs", "outcome = 'completed'", "completed_run_missing"),
    ("task_events", "kind = 'completed'", "completed_event_missing"),
    ("task_events", "kind = 'submitted_for_review'", "review_submission_missing"),
    ("task_events", "kind = 'claimed'", "review_claim_record_missing"),
])
def test_missing_local_records_are_reported(board, table, where, reason):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, _ = _done(conn)
        conn.execute(f"DELETE FROM {table} WHERE {where}")
        entry = audit.audit_legacy_done_task(conn, task_id)
        assert entry["classification"] in {"record_missing", "record_inconsistent"}
        assert reason in entry["reason_codes"]


@pytest.mark.parametrize("change,reason", [
    (lambda d: d.update(merge_sha="not merged"), "merge_sha_invalid"),
    (lambda d: d.update(head_sha="c" * 40), "review_candidate_mismatch"),
    (lambda d: d.update(contract_hash="c" * 64), "contract_hash_mismatch"),
    (lambda d: d["live_verification"]["acceptance"].update(review_run_id=999), "acceptance_binding_mismatch"),
    (lambda d: d["live_verification"]["acceptance"].update(review_profile="builder"), "acceptance_binding_mismatch"),
    (lambda d: d["live_verification"]["acceptance"].update(verdict="fail"), "acceptance_not_passed"),
])
def test_conflicting_matching_json_does_not_escape_diagnosis(board, change, reason):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, run_id = _done(conn)
        _rewrite_delivery(conn, task_id, run_id, change)
        entry = audit.audit_legacy_done_task(conn, task_id)
        assert entry["classification"] == "record_inconsistent"
        assert reason in entry["reason_codes"]


@pytest.mark.parametrize("reason", ["not merged", "zero merged PR", "x"])
def test_no_merge_reason_must_describe_actual_deliverable(board, reason):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, run_id = _done(conn, noncode=True)
        _rewrite_delivery(conn, task_id, run_id, lambda d: d.update(reason=reason))
        assert audit.audit_legacy_done_task(conn, task_id)["classification"] == "record_inconsistent"


@pytest.mark.parametrize("mutation,reason", [
    (lambda p: p.pop("delivery_policy"), "completion_policy_missing"),
    (lambda p: p["delivery_policy"].pop("contract_hash"), "completion_policy_incomplete"),
    (lambda p: p["delivery_policy"].update(target="read-only"), "completion_policy_mismatch"),
])
def test_contract_backfill_cannot_rebind_old_completion(board, mutation, reason):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, _ = _done(conn, noncode=True)
        row = conn.execute("SELECT id,payload FROM task_events WHERE kind = 'completed'").fetchone()
        payload = json.loads(row["payload"])
        mutation(payload)
        conn.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
        entry = audit.audit_legacy_done_task(conn, task_id)
        assert entry["classification"] in {"record_missing", "record_inconsistent"}
        assert reason in entry["reason_codes"]


def test_required_deployment_is_distinct_from_superseded_historical_claim(board):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, run_id = _done(conn, deploy=True)
        assert audit.audit_legacy_done_task(conn, task_id)["classification"] == "verification_required"
        _rewrite_delivery(conn, task_id, run_id, lambda d: d.pop("deployment"))
        entry = audit.audit_legacy_done_task(conn, task_id)
        assert entry["classification"] == "record_missing"
        assert "deployment_record_missing" in entry["reason_codes"]


@pytest.mark.parametrize("status", ["ready", "review", "done", "shipping", "running", "archived"])
def test_every_held_descendant_is_in_graph_and_fenced(board, status):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, _ = _done(conn)
        child = kb.create_task(conn, title="descendant")
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, child))
        conn.execute("INSERT INTO task_links VALUES (?,?)", (task_id, child))
        kb._append_event(conn, child, "controlled_worker_held", {"request_id": "owner"}, run_id=99)
        entry = audit.audit_legacy_done_task(conn, task_id)
        node = next(n for n in entry["nodes"] if n["id"] == child)
        assert node["status"] == status and "held_worker" in node["ownership_fences"]
        assert "held_worker" in entry["ownership_fences"]
        if status in {"shipping", "running"}:
            assert status in node["ownership_fences"]


@pytest.mark.parametrize("state,fence", [
    ("in_progress", "delivery_operation"), ("remote_applied", "delivery_operation"),
    ("quarantined", "delivery_operation"), ("failed", "unknown_operation_state"),
    ("committed", None), ("rejected", None),
])
def test_controller_states_follow_native_ownership(board, state, fence):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, run_id = _done(conn)
        _operation(conn, task_id, run_id, state)
        entry = audit.audit_legacy_done_task(conn, task_id)
        assert entry["ownership_fences"] == ([fence] if fence else [])


def test_committed_controller_record_is_locally_bound(board):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, run_id = _done(conn)
        _operation(conn, task_id, run_id, candidate_head="c" * 40)
        assert "controller_candidate_mismatch" in audit.audit_legacy_done_task(conn, task_id)["reason_codes"]


def test_ended_history_is_not_worker_authority_but_open_ownership_is_fenced(board):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, run_id = _done(conn)
        conn.execute("UPDATE task_runs SET claim_lock = 'historical-secret', worker_pid = 42 WHERE id = ?", (run_id,))
        entry = audit.audit_legacy_done_task(conn, task_id)
        assert entry["ownership_fences"] == []
        conn.execute("UPDATE task_runs SET ended_at = NULL WHERE id = ?", (run_id,))
        changed = audit.audit_legacy_done_task(conn, task_id)
        assert {"claim_present", "worker_pid_present", "open_run"} <= set(changed["ownership_fences"])
        assert changed["snapshot_digest"] != entry["snapshot_digest"]
        assert "historical-secret" not in json.dumps(changed)


@pytest.mark.parametrize("table,sql", [
    ("task_comments", "INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,'fixture','secret-proof-backfill',1)"),
    ("task_attachments", "INSERT INTO task_attachments(task_id,filename,stored_path,created_at) VALUES (?,'proof','secret-path',1)"),
    ("task_events", "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'proof','secret-evidence',1)"),
    ("task_runs", "INSERT INTO task_runs(task_id,status,started_at,ended_at,outcome,claim_lock) VALUES (?,'failed',1,2,'failed','secret-old-claim')"),
])
def test_all_evidence_changes_digest_without_echoing_content(board, table, sql):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, _ = _done(conn)
        before = audit.audit_legacy_done_task(conn, task_id)
        conn.execute(sql, (task_id,))
        after = audit.audit_legacy_done_task(conn, task_id)
        assert before["snapshot_digest"] != after["snapshot_digest"], table
        assert "secret-" not in json.dumps(after)


def test_incoming_descendant_links_and_cycles_are_bound(board):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, _ = _done(conn)
        child = kb.create_task(conn, title="child")
        other = kb.create_task(conn, title="outside parent")
        conn.execute("INSERT INTO task_links VALUES (?,?)", (task_id, child))
        first = audit.audit_legacy_done_task(conn, task_id)
        conn.execute("INSERT INTO task_links VALUES (?,?)", (other, child))
        second = audit.audit_legacy_done_task(conn, task_id)
        assert second["snapshot_digest"] != first["snapshot_digest"]
        assert {n["id"] for n in second["nodes"]} == {task_id, child}
        conn.execute("INSERT INTO task_links VALUES (?,?)", (child, task_id))
        assert len(audit.audit_legacy_done_task(conn, task_id)["nodes"]) == 2


def test_read_transaction_is_consistent_and_does_not_finish_callers_transaction(board, monkeypatch):
    path, _ = board
    with kb.connect_closing(path) as conn, kb.connect_closing(path) as other:
        task_id, _ = _done(conn)
        original = audit.audit_legacy_done_task(conn, task_id)
        capture = audit._snapshot
        changed = False

        def concurrent_change(connection, selected):
            nonlocal changed
            if not changed:
                other.execute("INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,'fixture','concurrent',1)", (task_id,))
                changed = True
            return capture(connection, selected)

        monkeypatch.setattr(audit, "_snapshot", concurrent_change)
        assert audit.audit_legacy_done_task(conn, task_id) == original
        assert audit.audit_legacy_done_task(conn, task_id)["snapshot_digest"] != original["snapshot_digest"]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE tasks SET title = 'caller uncommitted' WHERE id = ?", (task_id,))
        audit.audit_legacy_done_task(conn, task_id)
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == "historical fixture"


def test_read_only_connection_needs_no_initializer_and_bounded_failures(board, monkeypatch):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, _ = _done(conn)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        assert audit.audit_legacy_done_task(conn, task_id)["classification"] == "verification_required"
        monkeypatch.setattr(audit, "MAX_SNAPSHOT_BYTES", 1)
        with pytest.raises(audit.AuditSnapshotError, match="^snapshot_too_large$"):
            audit.audit_legacy_done_task(conn, task_id)
        assert not conn.in_transaction


def test_plain_scratch_and_selection_are_explicit_and_limited(board):
    path, _ = board
    with kb.connect_closing(path) as conn:
        scratch, _ = _done(conn, body="Unclassified old scratch note")
        code, _ = _done(conn)
        todo = kb.create_task(conn, title="unfinished")
        assert audit.audit_legacy_done_task(conn, scratch)["classification"] == "not_applicable"
        assert audit.audit_legacy_done_task(conn, todo) is None
        assert audit.audit_legacy_done_task(conn, "missing") is None
        report = audit.audit_legacy_done(conn, limit=1)
        assert report["scanned_count"] == 1 and report["limited"]
        selected = audit.audit_legacy_done(conn, task_ids=[code, code, todo])
        assert selected["scanned_count"] == 1 and not selected["limited"]
        assert audit.audit_legacy_done(conn, task_ids=[])["entries"] == []


def test_malformed_body_and_missing_descendant_are_bounded_errors(board):
    path, _ = board
    with kb.connect_closing(path) as conn:
        task_id, _ = _done(conn)
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (b"secret-invalid-body", task_id))
        with pytest.raises(audit.AuditSnapshotError, match="^task_body_invalid$"):
            audit.audit_legacy_done_task(conn, task_id)
        conn.execute("UPDATE tasks SET body = NULL WHERE id = ?", (task_id,))
        conn.execute("INSERT INTO task_links VALUES (?,'missing-child')", (task_id,))
        with pytest.raises(audit.AuditSnapshotError, match="^snapshot_incomplete$"):
            audit.audit_legacy_done_task(conn, task_id)
