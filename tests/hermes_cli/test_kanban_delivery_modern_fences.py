"""Native delivery transitions preserve current runs, held workers and task bases."""

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_delivery_gate as delivery_fixture
from tests.hermes_cli.test_kanban_delivery_gate import delivery_board as delivery_board


BASE_A = "d" * 40
BASE_B = "e" * 40


@pytest.fixture
def review(delivery_board, tmp_path, monkeypatch):
    # These fixtures verify database boundaries only, never a live checkout.
    monkeypatch.setattr(kb, "_cleanup_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(kb, "_unlock_task_worktree", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(kb, "_cleanup_worker_tmux", lambda *_args, **_kwargs: None)
    with kb.connect() as conn:
        task_id, builder_run = delivery_fixture._create_claimed_code_task(conn)
        conn.execute("UPDATE tasks SET workspace_path = ?, worktree_base_sha = ? WHERE id = ?",
                     (str(tmp_path / "fixture-checkout"), BASE_A, task_id))
        conn.commit()
        run_id = delivery_fixture._submit_and_claim_review(conn, task_id, builder_run)
        yield SimpleNamespace(conn=conn, task_id=task_id, run_id=run_id)


def snapshot(review):
    conn = review.conn
    return (
        dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (review.task_id,)).fetchone()),
        [dict(row) for row in conn.execute("SELECT * FROM task_runs ORDER BY id")],
        [dict(row) for row in conn.execute("SELECT * FROM task_events ORDER BY id")],
        [dict(row) for row in conn.execute("SELECT * FROM delivery_control_operations ORDER BY op_id")],
    )


def forbid_verification(*_args, **_kwargs):
    raise AssertionError("A refused native identity must not call the verifier")


def complete(review, verifier, *, run_id=None, delivery=None):
    return kb.complete_task(
        review.conn, review.task_id, summary="fixture exact review result",
        delivery=delivery_fixture._merged_delivery() if delivery is None else delivery,
        expected_run_id=review.run_id if run_id is None else run_id,
        terminal_verifier=verifier,
    )


def test_code_review_without_delivery_proof_is_refused_before_verifier(review):
    before = snapshot(review)
    with pytest.raises(kb.DeliveryEvidenceError) as error:
        kb.complete_task(review.conn, review.task_id, summary="tests passed but no delivery proof",
                         expected_run_id=review.run_id, terminal_verifier=forbid_verification)
    assert error.value.code == "delivery_missing"
    after = snapshot(review)
    assert after[:2] == before[:2]
    assert not any(row["kind"] == "completed" for row in after[2])


def test_exact_review_run_ledgers_the_same_verified_delivery_on_run_and_event(review):
    calls = []

    def verify(task, policy, submission, delivery, **_kwargs):
        calls.append((task.current_run_id, submission["_event_id"]))
        assert task.current_run_id == review.run_id
        assert task.worktree_base_sha == BASE_A
        assert submission["head_sha"] == delivery["head_sha"] == delivery_fixture.HEAD_A
        return {"verified_at": "2026-09-12T18:00:00Z", "contract_hash": policy["contract_hash"]}

    assert complete(review, verify)
    task = kb.get_task(review.conn, review.task_id)
    run = kb.get_run(review.conn, review.run_id)
    event, = [event for event in kb.list_events(review.conn, review.task_id) if event.kind == "completed"]
    assert len(calls) == 1
    assert task.status == "done" and task.current_run_id is None
    assert run.outcome == "completed" and run.ended_at is not None
    metadata = json.loads(review.conn.execute("SELECT metadata FROM task_runs WHERE id = ?",
                                             (review.run_id,)).fetchone()[0])
    assert metadata["delivery"] == event.payload["delivery"]
    assert event.run_id == review.run_id
    assert event.payload["delivery"]["merge_sha"] == delivery_fixture.MERGE


@pytest.mark.parametrize("damage", [
    "stale_token", "foreign_pointer", "ended", "missing", "claim", "pid", "foreign_owner",
])
def test_invalid_current_review_run_is_refused_before_verifier(review, damage):
    conn = review.conn
    other_id = kb.create_task(conn, title="unrelated fixture", assignee="other")
    other = kb.claim_task(conn, other_id, claimer="other-claim")
    assert other is not None
    expected_run = review.run_id
    if damage == "stale_token":
        expected_run = other.current_run_id
    elif damage == "foreign_pointer":
        conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?",
                     (other.current_run_id, review.task_id))
        expected_run = other.current_run_id
    elif damage == "ended":
        conn.execute("UPDATE task_runs SET status = 'blocked', outcome = 'blocked', ended_at = 1 WHERE id = ?",
                     (review.run_id,))
    elif damage == "missing":
        expected_run += 999999
        conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (expected_run, review.task_id))
    elif damage == "claim":
        conn.execute("UPDATE task_runs SET claim_lock = 'changed-claim' WHERE id = ?", (review.run_id,))
    elif damage == "pid":
        conn.execute("UPDATE task_runs SET worker_pid = 991234 WHERE id = ?", (review.run_id,))
    else:
        conn.execute("UPDATE task_runs SET task_id = ? WHERE id = ?", (other_id, review.run_id))
    conn.commit()
    before = snapshot(review)
    assert complete(review, forbid_verification, run_id=expected_run) is False
    assert snapshot(review) == before


@pytest.mark.parametrize("arrival", ["before_verifier", "during_verifier"])
def test_pending_held_worker_prevents_verified_completion(review, arrival):
    conn = review.conn
    calls = []
    protected = []

    def hold():
        with kb.write_txn(conn):
            kb._append_event(conn, review.task_id, "controlled_worker_held",
                             {"request_id": "fixture-negative-fence"}, run_id=review.run_id)
        protected.append(snapshot(review))

    def verify(*_args, **_kwargs):
        calls.append(True)
        hold()
        return {"verified_at": "2026-09-12T18:00:00Z"}

    if arrival == "before_verifier":
        hold()
    assert complete(review, verify if arrival == "during_verifier" else forbid_verification) is False
    assert calls == ([True] if arrival == "during_verifier" else [])
    assert kb._controlled_worker_pending(conn, review.task_id)
    assert snapshot(review) == protected[-1]


@pytest.mark.parametrize("arrival", ["before_verifier", "during_verifier"])
def test_reopened_parent_prevents_verified_completion(review, arrival):
    conn = review.conn
    parent_id = kb.create_task(conn, title="fixture prerequisite")
    assert kb.complete_task(conn, parent_id, summary="fixture prerequisite complete")
    kb.link_tasks(conn, parent_id, review.task_id)
    calls = []
    protected = []

    def reopen():
        conn.execute("UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?", (parent_id,))
        conn.commit()
        protected.append(snapshot(review))

    def verify(*_args, **_kwargs):
        calls.append(True)
        reopen()
        return {"verified_at": "2026-09-12T18:00:00Z"}

    if arrival == "before_verifier":
        reopen()
    assert complete(review, verify if arrival == "during_verifier" else forbid_verification) is False
    assert calls == ([True] if arrival == "during_verifier" else [])
    assert snapshot(review) == protected[-1]
    assert kb.get_task(conn, parent_id).status == "todo"


def test_base_changed_during_verification_cannot_commit_completion(review):
    protected = []

    def verify(task, *_args, **_kwargs):
        assert task.worktree_base_sha == BASE_A
        review.conn.execute("UPDATE tasks SET worktree_base_sha = ? WHERE id = ?", (BASE_B, review.task_id))
        review.conn.commit()
        protected.append(snapshot(review))
        return {"verified_at": "2026-09-12T18:00:00Z"}

    assert complete(review, verify) is False
    assert snapshot(review) == protected[-1]
    assert kb.get_task(review.conn, review.task_id).worktree_base_sha == BASE_B


@pytest.mark.parametrize("damage", ["ended", "claim", "pid", "foreign_owner"])
def test_run_changed_during_verification_cannot_commit_completion(review, damage):
    conn = review.conn
    other_id = kb.create_task(conn, title="unrelated fixture")
    protected = []

    def verify(*_args, **_kwargs):
        if damage == "ended":
            conn.execute("UPDATE task_runs SET status = 'blocked', outcome = 'blocked', ended_at = 1 WHERE id = ?",
                         (review.run_id,))
        elif damage == "claim":
            conn.execute("UPDATE task_runs SET claim_lock = 'changed-claim' WHERE id = ?", (review.run_id,))
        elif damage == "pid":
            conn.execute("UPDATE task_runs SET worker_pid = 991234 WHERE id = ?", (review.run_id,))
        else:
            conn.execute("UPDATE task_runs SET task_id = ? WHERE id = ?", (other_id, review.run_id))
        conn.commit()
        protected.append(snapshot(review))
        return {"verified_at": "2026-09-12T18:00:00Z"}

    assert complete(review, verify) is False
    assert snapshot(review) == protected[-1]


@pytest.fixture(params=["submit", "changes"])
def transition(request, delivery_board, tmp_path):
    with kb.connect() as conn:
        task_id, run_id = delivery_fixture._create_claimed_code_task(conn)
        conn.execute("UPDATE tasks SET workspace_path = ?, worktree_base_sha = ? WHERE id = ?",
                     (str(tmp_path / "fixture-checkout"), BASE_A, task_id))
        conn.commit()
        if request.param == "changes":
            run_id = delivery_fixture._submit_and_claim_review(conn, task_id, run_id)
        other_id = kb.create_task(conn, title="unrelated transition fixture")
        other = kb.claim_task(conn, other_id, claimer="unrelated-claim")
        assert other is not None
        yield SimpleNamespace(conn=conn, task_id=task_id, run_id=run_id,
                              kind=request.param, other=other)


def apply_transition(transition, verifier, *, run_id=None):
    expected = transition.run_id if run_id is None else run_id
    if transition.kind == "submit":
        return kb.submit_task_for_review(
            transition.conn, transition.task_id, summary="fixture candidate ready",
            expected_run_id=expected, pull_request=delivery_fixture._pull_request(),
            reviewer_assignee="reviewer", submission_verifier=verifier,
        )
    return kb.request_task_changes(
        transition.conn, transition.task_id, reason="fixture review needs correction",
        reviewed_head_sha=delivery_fixture.HEAD_A, expected_run_id=expected,
        candidate_verifier=verifier,
    )


def damage_transition(transition, damage):
    conn, task_id, run_id = transition.conn, transition.task_id, transition.run_id
    expected = run_id
    if damage == "stale_token":
        expected = transition.other.current_run_id
    elif damage in {"foreign_pointer", "missing"}:
        expected = transition.other.current_run_id if damage == "foreign_pointer" else run_id + 999999
        conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (expected, task_id))
    elif damage == "ended":
        conn.execute("UPDATE task_runs SET status = 'blocked', outcome = 'blocked', ended_at = 1 WHERE id = ?",
                     (run_id,))
    elif damage == "claim":
        conn.execute("UPDATE task_runs SET claim_lock = 'changed-claim' WHERE id = ?", (run_id,))
    elif damage == "pid":
        conn.execute("UPDATE task_runs SET worker_pid = 991234 WHERE id = ?", (run_id,))
    elif damage == "foreign_owner":
        conn.execute("UPDATE task_runs SET task_id = ? WHERE id = ?", (transition.other.id, run_id))
    elif damage == "held":
        kb._append_event(conn, task_id, "controlled_worker_held",
                         {"request_id": "fixture-transition-fence"}, run_id=run_id)
    elif damage == "base":
        conn.execute("UPDATE tasks SET worktree_base_sha = ? WHERE id = ?", (BASE_B, task_id))
    else:
        raise AssertionError(f"Unknown fixture damage: {damage}")
    conn.commit()
    return expected


@pytest.mark.parametrize("damage", [
    "stale_token", "foreign_pointer", "ended", "missing", "claim", "pid", "foreign_owner", "held",
])
def test_submission_and_changes_refuse_invalid_run_before_verifier(transition, damage):
    expected = damage_transition(transition, damage)
    before = snapshot(transition)
    assert apply_transition(transition, forbid_verification, run_id=expected) in (False, None)
    assert snapshot(transition) == before


@pytest.mark.parametrize("damage", [
    "foreign_pointer", "ended", "missing", "claim", "pid", "foreign_owner", "held", "base",
])
def test_submission_and_changes_cannot_commit_after_verifier_drift(transition, damage):
    protected = []

    def verify(task, *_args):
        assert task.current_run_id == transition.run_id
        assert task.worktree_base_sha == BASE_A
        damage_transition(transition, damage)
        protected.append(snapshot(transition))
        return {"verified_at": "2026-09-12T18:00:00Z"}

    assert apply_transition(transition, verify) in (False, None)
    assert len(protected) == 1
    assert snapshot(transition) == protected[0]


def test_submission_and_changes_accept_exact_unchanged_run(transition):
    calls = []

    def verify(task, *_args):
        calls.append(task.current_run_id)
        return {"verified_at": "2026-09-12T18:00:00Z"}

    expected_status = "review" if transition.kind == "submit" else "ready"
    expected_outcome = "submitted_for_review" if transition.kind == "submit" else "changes_requested"
    expected_return = True if transition.kind == "submit" else "ready"
    assert apply_transition(transition, verify) == expected_return
    assert calls == [transition.run_id]
    task = kb.get_task(transition.conn, transition.task_id)
    run = kb.get_run(transition.conn, transition.run_id)
    assert task.status == expected_status and task.current_run_id is None
    assert run.ended_at is not None and run.outcome == expected_outcome


@pytest.mark.parametrize("arrival", ["before_verifier", "during_verifier"])
def test_submission_refuses_and_changes_parks_when_parent_reopens(transition, arrival):
    conn = transition.conn
    parent = kb.create_task(conn, title="transition prerequisite")
    assert kb.complete_task(conn, parent, summary="fixture prerequisite complete")
    kb.link_tasks(conn, parent, transition.task_id)
    calls = []
    protected = []

    def reopen():
        conn.execute("UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?", (parent,))
        conn.commit()
        protected.append(snapshot(transition))

    def verify(*_args):
        calls.append(True)
        if arrival == "during_verifier":
            reopen()
        return {"verified_at": "2026-09-12T18:00:00Z"}

    if arrival == "before_verifier":
        reopen()
    if transition.kind == "submit":
        verifier = forbid_verification if arrival == "before_verifier" else verify
        assert apply_transition(transition, verifier) is False
        assert snapshot(transition) == protected[-1]
        assert calls == ([] if arrival == "before_verifier" else [True])
    else:
        assert apply_transition(transition, verify) == "todo"
        assert calls == [True]
        task = kb.get_task(conn, transition.task_id)
        assert task.status == "todo" and task.assignee == "builder"
        assert task.current_run_id is None
        assert kb.get_run(conn, transition.run_id).outcome == "changes_requested"
    assert kb.get_task(conn, parent).status == "todo"


def add_active_operation(review, state):
    with kb.write_txn(review.conn):
        review.conn.execute("UPDATE tasks SET status = 'shipping' WHERE id = ?", (review.task_id,))
        review.conn.execute(
            "INSERT INTO delivery_control_operations "
            "(op_id, task_id, run_id, action, request_sha256, claim_sha256, summary, "
            "candidate_head, peer_uid, owner_instance, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("fixture-operation", review.task_id, review.run_id, "reviewer_accept", "a" * 64,
             "b" * 64, "fixture protected delivery", delivery_fixture.HEAD_A, 1000,
             "fixture-owner", state, 1, 1),
        )


@pytest.mark.parametrize("state", ["in_progress", "remote_applied", "quarantined"])
@pytest.mark.parametrize("operation", ["complete", "changes", "block", "reclaim", "edit", "assign"])
def test_active_delivery_operation_preserves_shipping_and_exact_run(review, state, operation):
    add_active_operation(review, state)
    before = snapshot(review)
    conn, task_id = review.conn, review.task_id
    actions = {
        "complete": lambda: complete(review, forbid_verification),
        "changes": lambda: kb.request_task_changes(
            conn, task_id, reason="must remain fenced", reviewed_head_sha=delivery_fixture.HEAD_A,
            expected_run_id=review.run_id, candidate_verifier=forbid_verification),
        "block": lambda: kb.block_task(conn, task_id, reason="must remain fenced", expected_run_id=review.run_id),
        "reclaim": lambda: kb.reclaim_task(conn, task_id, signal_fn=forbid_verification),
        "edit": lambda: kb.edit_task_fields(conn, task_id, body="must remain fenced"),
        "assign": lambda: kb.assign_task(conn, task_id, "replacement"),
    }
    with pytest.raises(kb.DeliveryOperationInProgressError):
        actions[operation]()
    assert snapshot(review) == before
