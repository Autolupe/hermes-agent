"""Behavior tests for the fail-closed Kanban code-delivery lifecycle."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


HEAD_A = "a" * 40
HEAD_B = "b" * 40
MERGE = "c" * 40
PR_URL = "https://github.com/acme/widgets/pull/17"
BRANCH = "hermes/t_delivery"


@pytest.fixture
def delivery_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    from hermes_cli import delivery_verifier
    monkeypatch.setattr(
        delivery_verifier,
        "verify_submission",
        lambda _task, policy, _pr: {
            "verified_at": "2026-08-09T15:00:00Z",
            "contract_hash": policy.get("contract_hash"),
            "repository": "acme/widgets",
        },
    )
    monkeypatch.setattr(
        delivery_verifier,
        "verify_terminal",
        lambda _task, _policy, _submission, delivery, **_kwargs: {
            "verified_at": "2026-08-09T15:01:00Z",
            **(
                {"deployment": dict(delivery["deployment"])}
                if isinstance(delivery.get("deployment"), dict)
                else {}
            ),
        },
    )
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _pull_request(*, head: str = HEAD_A, branch: str = BRANCH) -> dict:
    return {
        "pr_url": PR_URL,
        "pr_number": 17,
        "head_sha": head,
        "candidate_ref": branch,
    }


def _merged_delivery(*, head: str = HEAD_A, deploy: dict | None = None) -> dict:
    value = {
        "classification": "merged_pr",
        "pr_url": PR_URL,
        "pr_number": 17,
        "head_sha": head,
        "merge_sha": MERGE,
    }
    if deploy is not None:
        value["deployment"] = deploy
    return value


def _contract(
    *,
    domain: str = "coding",
    target: str = "github-merge",
    deployment: bool = False,
) -> str:
    deploy_fields = ""
    if deployment:
        target = "github-merge-and-deploy"
        deploy_fields = """deployment: production
deployment-environment: production
deployment-verifier: clauseye-production-probe
"""
    return f"""```acceptance-contract
domain: {domain}
target: {target}
{deploy_fields}tier1:
  - cmd: "python -m pytest -q"
    expect_exit: 0
tier2:
  - "candidate is independently reviewed"
tier3: "The requested delivery is verifiably complete."
```"""


def _create_claimed_code_task(conn, *, body: str | None = None, assignee="builder"):
    effective_body = _contract() if body is None else body
    task_id = kb.create_task(
        conn,
        title="ship exact change",
        body=effective_body,
        assignee=assignee,
        workspace_kind="worktree",
        workspace_path="/tmp/test-delivery-worktree",
        branch_name=BRANCH,
    )
    # Invalid-contract cases below model legacy rows created before the
    # planning-boundary router existed. New worktree tasks remain in triage.
    if kb.get_task(conn, task_id).status == "triage":
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,),
            )
    claimed = kb.claim_task(conn, task_id, claimer="builder-lock")
    assert claimed is not None
    return task_id, claimed.current_run_id


@pytest.mark.parametrize("workspace_kind", ["worktree", "dir"])
def test_new_code_workspace_without_valid_contract_routes_to_triage(
    delivery_board,
    workspace_kind,
):
    with kb.connect() as conn:
        missing = kb.create_task(
            conn,
            title="unspecified code",
            workspace_kind=workspace_kind,
            workspace_path="/tmp/unspecified",
        )
        invalid = kb.create_task(
            conn,
            title="invalid code contract",
            body="```acceptance-contract\ntarget: github-merge\n```",
            workspace_kind=workspace_kind,
            workspace_path="/tmp/invalid",
        )
        missing_task = kb.get_task(conn, missing)
        invalid_task = kb.get_task(conn, invalid)
        events = kb.list_events(conn, missing)

    assert missing_task.status == "triage"
    assert invalid_task.status == "triage"
    assert events[-1].kind == "delivery_contract_triage"
    assert events[-1].payload["reason"] == "missing"


def test_specify_worktree_requires_valid_contract_before_promotion(delivery_board):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="unspecified code",
            workspace_kind="worktree",
            workspace_path="/tmp/unspecified",
        )

        with pytest.raises(ValueError, match="remains in triage"):
            kb.specify_triage_task(conn, task_id, title="still unspecified")

        unchanged = kb.get_task(conn, task_id)
        assert unchanged.status == "triage"
        assert unchanged.title == "unspecified code"

        assert kb.specify_triage_task(conn, task_id, body=_contract())
        promoted = kb.get_task(conn, task_id)

    assert promoted.status == "ready"
    assert promoted.body == _contract()


def test_merge_gated_dir_is_triaged_at_create_and_both_claim_boundaries(
    delivery_board,
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="legacy checkout cannot publish",
            body=_contract(),
            assignee="builder",
            workspace_kind="dir",
            workspace_path="/tmp/legacy-checkout",
        )
        created = kb.get_task(conn, task_id)
        create_events = kb.list_events(conn, task_id)

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,),
            )
        assert kb.claim_task(conn, task_id, claimer="must-not-run") is None
        ready_rejected = kb.get_task(conn, task_id)

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review', claim_lock = NULL "
                "WHERE id = ?",
                (task_id,),
            )
        assert kb.claim_review_task(conn, task_id, claimer="must-not-review") is None
        review_rejected = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert created.status == "triage"
    assert create_events[-1].kind == "delivery_control_ineligible"
    assert create_events[-1].payload["code"] == "delivery_worktree_required"
    assert ready_rejected.status == "triage"
    assert review_rejected.status == "triage"
    rejected = [event for event in events if event.kind == "claim_rejected"]
    assert [event.payload["reason"] for event in rejected[-2:]] == [
        "delivery_worktree_required",
        "delivery_worktree_required",
    ]


def test_dispatch_triages_merge_worktree_when_controller_is_ineligible(
    delivery_board, monkeypatch, tmp_path,
):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    workspace = tmp_path / "project" / ".worktrees" / "placeholder"
    workspace.mkdir(parents=True)
    spawned = []
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="wrong board or project",
            body=_contract(),
            assignee="builder",
            workspace_kind="worktree",
            workspace_path=str(workspace),
            branch_name="hermes/placeholder",
        )
        exact_workspace = workspace.with_name(task_id)
        exact_workspace.mkdir()
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_path = ? WHERE id = ?",
                (str(exact_workspace), task_id),
            )
        monkeypatch.setattr(
            kb,
            "_resolve_worktree_workspace",
            lambda task, *, board=None, **_kwargs: (exact_workspace, task.branch_name),
        )
        monkeypatch.setattr(
            kb, "_delivery_control_worker_eligible", lambda *_a, **_k: False,
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *args, **kwargs: spawned.append(args) or 999,
            max_spawn=1,
        )
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert spawned == []
    assert result.delivery_triaged == [(task_id, "delivery_control_ineligible")]
    assert task.status == "triage"
    assert task.current_run_id is None
    assert run.outcome == "blocked"
    assert events[-1].payload["code"] == "delivery_control_ineligible"


@pytest.mark.parametrize("workspace_kind", ["worktree", "dir"])
def test_claim_routes_legacy_contractless_code_workspace_back_to_triage(
    delivery_board,
    workspace_kind,
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="legacy unspecified code",
            workspace_kind=workspace_kind,
            workspace_path="/tmp/legacy-unspecified",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,),
            )

        assert kb.claim_task(conn, task_id, claimer="must-not-run") is None
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert task.status == "triage"
    assert task.current_run_id is None
    assert events[-2].kind == "delivery_contract_triage"
    assert events[-2].payload["source"] == "claim"
    assert events[-1].kind == "claim_rejected"
    assert events[-1].payload["reason"] == "delivery_contract_invalid"


def _submit_and_claim_review(
    conn,
    task_id: str,
    run_id: int,
    *,
    head=HEAD_A,
    claimer="review-lock",
):
    assert kb.submit_task_for_review(
        conn,
        task_id,
        pull_request=_pull_request(head=head),
        summary="candidate ready",
        reviewer_assignee="reviewer",
        expected_run_id=run_id,
    )
    claimed = kb.claim_review_task(conn, task_id, claimer=claimer)
    assert claimed is not None
    return claimed.current_run_id


def test_submit_for_review_records_exact_candidate_and_provenance(delivery_board):
    with kb.connect() as conn:
        task_id, run_id = _create_claimed_code_task(conn)
        assert kb.submit_task_for_review(
            conn,
            task_id,
            pull_request=_pull_request(),
            summary="tests green; ready for independent review",
            reviewer_assignee="reviewer",
            expected_run_id=run_id,
        )
        task = kb.get_task(conn, task_id)
        event = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "submitted_for_review"
        ][-1]
        run = kb.latest_run(conn, task_id)

    assert task.status == "review"
    assert task.assignee == "reviewer"
    assert task.current_run_id is None
    assert event.payload["pr_url"] == PR_URL
    assert event.payload["pr_number"] == 17
    assert event.payload["head_sha"] == HEAD_A
    assert event.payload["candidate_ref"] == BRANCH
    assert event.payload["executor_assignee"] == "builder"
    assert event.payload["builder_run_id"] == run_id
    assert run.outcome == "submitted_for_review"


def test_submit_for_review_rejects_wrong_status_and_non_code(delivery_board):
    with kb.connect() as conn:
        ready = kb.create_task(
            conn,
            title="not claimed",
            assignee="builder",
            workspace_kind="worktree",
            branch_name=BRANCH,
        )
        assert kb.submit_task_for_review(
            conn, ready, pull_request=_pull_request()
        ) is False

        scratch = kb.create_task(conn, title="research", assignee="builder")
        scratch_claim = kb.claim_task(conn, scratch)
        assert scratch_claim is not None
        with pytest.raises(kb.DeliveryEvidenceError) as exc:
            kb.submit_task_for_review(
                conn,
                scratch,
                pull_request=_pull_request(),
                expected_run_id=scratch_claim.current_run_id,
            )
        assert exc.value.code == "review_not_applicable"
        assert kb.get_task(conn, scratch).status == "running"


def test_submit_for_review_requires_configured_distinct_reviewer(delivery_board):
    with kb.connect() as conn:
        task_id, run_id = _create_claimed_code_task(conn)
        with pytest.raises(kb.DeliveryEvidenceError) as unconfigured:
            kb.submit_task_for_review(
                conn,
                task_id,
                pull_request=_pull_request(),
                expected_run_id=run_id,
            )
        with pytest.raises(kb.DeliveryEvidenceError) as same_profile:
            kb.submit_task_for_review(
                conn,
                task_id,
                pull_request=_pull_request(),
                reviewer_assignee="builder",
                expected_run_id=run_id,
            )
    assert unconfigured.value.code == "reviewer_profile_unconfigured"
    assert same_profile.value.code == "reviewer_profile_invalid"


def test_submit_for_review_rejects_candidate_ref_mismatch_auditably(delivery_board):
    with kb.connect() as conn:
        task_id, run_id = _create_claimed_code_task(conn)
        with pytest.raises(kb.DeliveryEvidenceError) as exc:
            kb.submit_task_for_review(
                conn,
                task_id,
                pull_request=_pull_request(branch="hermes/other-task"),
                expected_run_id=run_id,
            )
        events = kb.list_events(conn, task_id)
        task = kb.get_task(conn, task_id)

    assert exc.value.code == "candidate_ref_mismatch"
    assert task.status == "running"
    rejected = [event for event in events if event.kind == "review_submission_rejected"]
    assert rejected[-1].payload["code"] == "candidate_ref_mismatch"


def test_builder_cannot_bypass_review_with_prose_or_merge_claim(delivery_board):
    with kb.connect() as conn:
        task_id, run_id = _create_claimed_code_task(conn)
        with pytest.raises(kb.DeliveryEvidenceError) as missing:
            kb.complete_task(
                conn, task_id, summary="implemented, shipped, done",
                expected_run_id=run_id,
            )
        with pytest.raises(kb.DeliveryEvidenceError) as bypass:
            kb.complete_task(
                conn,
                task_id,
                summary="I merged it",
                delivery=_merged_delivery(),
                expected_run_id=run_id,
            )
        task = kb.get_task(conn, task_id)
        rejected = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "completion_blocked_delivery"
        ]

    assert missing.value.code == "delivery_missing"
    assert bypass.value.code == "review_required"
    assert task.status == "running"
    assert [event.payload["code"] for event in rejected[-2:]] == [
        "delivery_missing", "review_required",
    ]


@pytest.mark.parametrize(
    "body",
    [
        "```acceptance-contract\n\n```",
        """```acceptance-contract
tier1:
  - cmd: "true"
    expect_exit: 0
```""",
        """```acceptance-contract
domain: coding
target: github-merg
tier1:
  - cmd: "true"
    expect_exit: 0
tier2: []
tier3: "done"
```""",
        """```acceptance-contract
domain: [not-valid-yaml
```""",
        """```acceptance-contract
domain: coding
target: github-merge
target: artifact-file
tier1:
  - cmd: "true"
    expect_exit: 0
tier2: []
tier3: "done"
```""",
        """```acceptance-contract
domain: coding
target: github-merge
1: invalid-key
tier1:
  - cmd: "true"
    expect_exit: 0
tier2: []
tier3: "done"
```""",
        """```acceptance-contract
domain: coding
target: github-merge
tier1:
  - cmd: "true"
    cmd: "false"
    expect_exit: 0
tier2: []
tier3: "done"
```""",
        """```acceptance-contract
domain: coding
target: github-merge
tier1:
  - cmd: "true"
    expect_exit: 0
    1: invalid-nested-key
tier2: []
tier3: "done"
```""",
        """```acceptance-contract
domain: coding
target: github-merge
tier1:
  - cmd: "true"
    expect_exit: 0
tier2: &loop
  - *loop
tier3: "done"
```""",
        _contract() + "\n" + _contract(),
    ],
)
def test_present_but_invalid_contract_never_bypasses_worktree_gate(
    delivery_board, body,
):
    with kb.connect() as conn:
        # A legacy/direct writer can still corrupt a contract after the
        # planning-boundary claim. Terminal transitions must independently
        # fail closed against that changed body.
        task_id, run_id = _create_claimed_code_task(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, task_id))
        with pytest.raises(kb.DeliveryEvidenceError) as rejected:
            kb.complete_task(
                conn, task_id, summary="prose is not delivery",
                expected_run_id=run_id,
            )
        assert kb.get_task(conn, task_id).status == "running"

    assert rejected.value.code == "delivery_contract_invalid"


def test_inline_yaml_comments_use_canonical_parser_and_still_gate(delivery_board):
    body = """```acceptance-contract
domain: coding  # code delivery
target: github-merge  # exact PR merge
tier1:
  - cmd: "true"
    expect_exit: 0
tier2:
  - "reviewed"
tier3: "The change is merged."
```"""
    with kb.connect() as conn:
        task_id, run_id = _create_claimed_code_task(conn, body=body)
        with pytest.raises(kb.DeliveryEvidenceError) as rejected:
            kb.complete_task(
                conn, task_id, summary="not merged", expected_run_id=run_id,
            )

    assert rejected.value.code == "delivery_missing"


def test_coding_cannot_use_artifact_target_to_bypass_merge(delivery_board):
    with kb.connect() as conn:
        rejected = kb.create_task(
            conn,
            title="render evidence",
            body=_contract(domain="coding", target="artifact-file"),
            workspace_kind="worktree",
            branch_name="evidence/render",
        )
        audit = kb.create_task(
            conn,
            title="inspect exact source without changing it",
            body=_contract(domain="audit", target="artifact-file"),
            workspace_kind="worktree",
            branch_name="audit/exact-tree",
        )
        with pytest.raises(kb.DeliveryEvidenceError) as missing_classification:
            kb.complete_task(conn, audit, summary="not merged; zero merged PR")
        assert missing_classification.value.code == "no_merge_expected_required"
        assert kb.complete_task(
            conn,
            audit,
            summary="artifact recorded",
            delivery={
                "classification": "no_merge_expected",
                "reason": "read-only exact-source audit artifact",
            },
        )
        rejected_task = kb.get_task(conn, rejected)
        audit_task = kb.get_task(conn, audit)

    assert rejected_task.status == "triage"
    assert audit_task.status == "done"


def test_contract_parser_rejects_alias_expansion_and_oversize_input():
    from hermes_cli.acceptance_contract import lint_body

    aliased = """```acceptance-contract
domain: coding
target: github-merge
tier1:
  - &command
    cmd: "true"
    expect_exit: 0
  - *command
tier2: []
tier3: "done"
```"""
    alias_lint = lint_body(aliased)
    assert alias_lint["valid"] is False
    assert "anchors and aliases" in alias_lint["errors"][0]

    oversize = "```acceptance-contract\n" + ("x" * 65537) + "\n```"
    size_lint = lint_body(oversize)
    assert size_lint["valid"] is False
    assert "exceeds" in size_lint["errors"][0]

    nested = (
        "```acceptance-contract\n"
        "domain: coding\n"
        "target: github-merge\n"
        "tier1:\n  - cmd: \"true\"\n    expect_exit: 0\n"
        "tier2: []\n"
        "tier3: " + ("[" * 500) + "\"done\"" + ("]" * 500) + "\n```"
    )
    nested_lint = lint_body(nested)
    assert nested_lint["valid"] is False
    assert "nesting exceeds" in nested_lint["errors"][0]


@pytest.mark.parametrize("target", ["github-merge", "artifact-file"])
def test_deployment_fields_require_merge_and_deploy_target(delivery_board, target):
    with kb.connect() as conn:
        task_id, run_id = _create_claimed_code_task(conn)
        invalid_body = _contract(target=target, deployment=True).replace(
            "target: github-merge-and-deploy", f"target: {target}",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET body = ? WHERE id = ?", (invalid_body, task_id),
            )
        with pytest.raises(kb.DeliveryEvidenceError) as rejected:
            kb.complete_task(
                conn, task_id, summary="invalid deployment contract",
                expected_run_id=run_id,
            )
    assert rejected.value.code == "delivery_contract_invalid"


def test_optional_deployment_claim_is_rejected(delivery_board):
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(
            conn, body=_contract(target="github-merge"),
        )
        review_run = _submit_and_claim_review(conn, task_id, builder_run)
        with pytest.raises(kb.DeliveryEvidenceError) as rejected:
            kb.complete_task(
                conn,
                task_id,
                summary="must not ledger unverified optional deployment",
                delivery=_merged_delivery(deploy={"environment": "production"}),
                expected_run_id=review_run,
            )
    assert rejected.value.code == "unexpected_deployment_evidence"


def test_review_completion_requires_and_ledgers_exact_submitted_head(delivery_board):
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        review_run = _submit_and_claim_review(conn, task_id, builder_run)
        with pytest.raises(kb.DeliveryEvidenceError) as mismatch:
            kb.complete_task(
                conn,
                task_id,
                summary="reviewed stale head",
                delivery=_merged_delivery(head=HEAD_B),
                expected_run_id=review_run,
            )
        assert mismatch.value.code == "review_candidate_mismatch"
        assert kb.get_task(conn, task_id).status == "running"

        assert kb.complete_task(
            conn,
            task_id,
            summary="exact head reviewed and merged",
            delivery=_merged_delivery(),
            expected_run_id=review_run,
        )
        task = kb.get_task(conn, task_id)
        completed = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "completed"
        ][-1]

    assert task.status == "done"
    assert completed.payload["delivery"]["head_sha"] == HEAD_A
    assert completed.payload["delivery"]["merge_sha"] == MERGE
    assert completed.payload["delivery_policy"]["pr_gate"] == "merge"


def test_stale_terminal_verification_cannot_complete_new_review_round(delivery_board):
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        review_run = _submit_and_claim_review(conn, task_id, builder_run)

        def advance_round(_task, _policy, _submission, _delivery, **_kwargs):
            with kb.connect() as other:
                assert kb.request_task_changes(
                    other,
                    task_id,
                    reason="round one needs a fix",
                    reviewed_head_sha=HEAD_A,
                    expected_run_id=review_run,
                    candidate_verifier=lambda *_args: {"verified": True},
                ) == "ready"
                builder_two = kb.claim_task(other, task_id, claimer="builder-two")
                assert builder_two is not None
                assert kb.submit_task_for_review(
                    other,
                    task_id,
                    pull_request=_pull_request(head=HEAD_B),
                    reviewer_assignee="reviewer",
                    expected_run_id=builder_two.current_run_id,
                    submission_verifier=lambda *_args: {"verified": True},
                )
            return {"verified_at": "2026-08-09T15:05:00Z"}

        assert kb.complete_task(
            conn,
            task_id,
            summary="stale round one result",
            delivery=_merged_delivery(),
            expected_run_id=review_run,
            terminal_verifier=advance_round,
        ) is False
        task = kb.get_task(conn, task_id)
        completed = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "completed"
        ]
        latest = kb._latest_review_submission(conn, task_id)

    assert task.status == "review"
    assert task.assignee == "reviewer"
    assert latest["head_sha"] == HEAD_B
    assert completed == []


def test_submission_verifier_race_cannot_land_stale_handoff(delivery_board):
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)

        def reclaim_during_verification(*_args):
            with kb.connect() as other:
                assert kb.reclaim_task(
                    other,
                    task_id,
                    reason="simulated concurrent reclaim",
                    signal_fn=lambda *_signal_args: None,
                )
            return {"verified": True}

        assert kb.submit_task_for_review(
            conn,
            task_id,
            pull_request=_pull_request(),
            reviewer_assignee="reviewer",
            expected_run_id=builder_run,
            submission_verifier=reclaim_during_verification,
        ) is False
        task = kb.get_task(conn, task_id)
        submissions = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "submitted_for_review"
        ]

    assert task.status == "ready"
    assert submissions == []


def test_terminal_verifier_internal_typeerror_is_not_retried(delivery_board):
    calls = 0

    def broken_verifier(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise TypeError("internal kanban_home invariant failed")

    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        review_run = _submit_and_claim_review(conn, task_id, builder_run)
        with pytest.raises(kb.DeliveryEvidenceError) as rejected:
            kb.complete_task(
                conn,
                task_id,
                delivery=_merged_delivery(),
                expected_run_id=review_run,
                terminal_verifier=broken_verifier,
            )
        task = kb.get_task(conn, task_id)

    assert rejected.value.code == "live_delivery_unverified"
    assert calls == 1
    assert task.status == "running"


def test_stale_change_request_cannot_move_new_review_round(delivery_board):
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        review_run = _submit_and_claim_review(conn, task_id, builder_run)

        def advance_round(_task, _policy, _submission):
            with kb.connect() as other:
                assert kb.request_task_changes(
                    other,
                    task_id,
                    reason="real round-one decision",
                    reviewed_head_sha=HEAD_A,
                    expected_run_id=review_run,
                    candidate_verifier=lambda *_args: {"verified": True},
                ) == "ready"
                builder_two = kb.claim_task(other, task_id, claimer="builder-two")
                assert builder_two is not None
                assert kb.submit_task_for_review(
                    other,
                    task_id,
                    pull_request=_pull_request(head=HEAD_B),
                    reviewer_assignee="reviewer",
                    expected_run_id=builder_two.current_run_id,
                    submission_verifier=lambda *_args: {"verified": True},
                )
            return {"verified": True}

        assert kb.request_task_changes(
            conn,
            task_id,
            reason="stale round-one decision",
            reviewed_head_sha=HEAD_A,
            expected_run_id=review_run,
            candidate_verifier=advance_round,
        ) is None
        task = kb.get_task(conn, task_id)
        latest = kb._latest_review_submission(conn, task_id)
        changes = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "changes_requested"
        ]

    assert task.status == "review"
    assert task.assignee == "reviewer"
    assert latest["head_sha"] == HEAD_B
    assert len(changes) == 1
    assert changes[0].payload["reason"] == "real round-one decision"


def test_explicit_deployment_contract_requires_revision_and_health(delivery_board):
    body = _contract(deployment=True)
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn, body=body)
        review_run = _submit_and_claim_review(conn, task_id, builder_run)
        with pytest.raises(kb.DeliveryEvidenceError) as missing:
            kb.complete_task(
                conn,
                task_id,
                summary="merged only",
                delivery=_merged_delivery(),
                expected_run_id=review_run,
            )
        assert missing.value.code == "deployment_evidence_missing"
        with pytest.raises(kb.DeliveryEvidenceError) as wrong_repo:
            kb.complete_task(
                conn,
                task_id,
                summary="unrelated workflow is not delivery proof",
                delivery=_merged_delivery(
                    deploy={
                        "environment": "production",
                        "revision": "widgets-production-00042-abc",
                        "health_status": "healthy",
                        "health_reference": "https://widgets.example/health",
                        "health_checked_at": "2026-08-09T15:00:00Z",
                        "workflow_run_url": (
                            "https://github.com/acme/other/actions/runs/9001"
                        ),
                        "workflow_run_id": 9001,
                        "source_sha": MERGE,
                        "verification": {
                            "verifier": "clauseye-production-probe",
                            "status": "passed",
                            "reference": "evidence://deploy/9001",
                        },
                    }
                ),
                expected_run_id=review_run,
            )
        assert wrong_repo.value.code == "deployment_workflow_repository_mismatch"
        assert kb.complete_task(
            conn,
            task_id,
            summary="merged and production revision healthy",
            delivery=_merged_delivery(
                deploy={
                    "environment": "production",
                    "revision": "widgets-production-00042-abc",
                    "health_status": "healthy",
                    "health_reference": "https://widgets.example/health",
                    "health_checked_at": "2026-08-09T15:00:00Z",
                    "workflow_run_url": (
                        "https://github.com/acme/widgets/actions/runs/9001"
                    ),
                    "workflow_run_id": 9001,
                    "source_sha": MERGE,
                    "verification": {
                        "verifier": "clauseye-production-probe",
                        "status": "passed",
                        "reference": "evidence://deploy/9001",
                    },
                }
            ),
            expected_run_id=review_run,
        )


def test_deployment_contract_without_named_verifier_fails_closed(delivery_board):
    body = """```acceptance-contract
domain: coding
target: github-merge
deployment: production
tier1:
  - cmd: "python -m pytest -q"
    expect_exit: 0
tier2:
  - "independent review"
    tier3: "Production is healthy."
    ```"""
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, task_id))
        with pytest.raises(kb.DeliveryEvidenceError) as incomplete:
            kb.submit_task_for_review(
                conn,
                task_id,
                pull_request=_pull_request(),
                expected_run_id=builder_run,
            )

    assert incomplete.value.code == "delivery_contract_invalid"


def test_non_code_scratch_and_explicit_artifact_worktree_remain_compatible(delivery_board):
    artifact_contract = _contract(domain="research", target="artifact-file")
    with kb.connect() as conn:
        scratch = kb.create_task(conn, title="plain scratch")
        artifact = kb.create_task(
            conn,
            title="review artifact",
            body=artifact_contract,
            workspace_kind="worktree",
            branch_name="review/artifact",
        )
        assert kb.complete_task(conn, scratch, summary="done")
        with pytest.raises(kb.DeliveryEvidenceError) as missing_classification:
            kb.complete_task(conn, artifact, summary="artifact attached")
        assert missing_classification.value.code == "no_merge_expected_required"
        with pytest.raises(kb.DeliveryEvidenceError) as ambiguous_reason:
            kb.complete_task(
                conn,
                artifact,
                summary="artifact attached",
                delivery={
                    "classification": "no_merge_expected",
                    "reason": "zero merged PR",
                },
            )
        assert ambiguous_reason.value.code == "no_merge_reason_ambiguous"
        assert kb.complete_task(
            conn,
            artifact,
            summary="artifact attached",
            delivery={
                "classification": "no_merge_expected",
                "reason": "research artifact has no repository mutation",
            },
        )
        assert kb.get_task(conn, scratch).status == "done"
        assert kb.get_task(conn, artifact).status == "done"


def test_project_link_alone_does_not_make_scratch_task_code_delivery(delivery_board):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="project-linked research",
            body="Compare implementation options; do not change code.",
            workspace_kind="scratch",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET project_id = ? WHERE id = ?",
                ("p_research", task_id),
            )
        assert kb.complete_task(conn, task_id, summary="research attached")
        assert kb.get_task(conn, task_id).status == "done"


def test_request_changes_restores_executor_and_blocks_at_round_limit(delivery_board):
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        review_run = _submit_and_claim_review(conn, task_id, builder_run)
        assert kb.request_task_changes(
            conn,
            task_id,
            reason="add a tenant isolation negative",
            reviewed_head_sha=HEAD_A,
            max_review_rounds=2,
            expected_run_id=review_run,
        ) == "ready"
        task = kb.get_task(conn, task_id)
        assert task.assignee == "builder"

        second_builder = kb.claim_task(conn, task_id, claimer="builder-two")
        assert second_builder is not None
        second_review = _submit_and_claim_review(
            conn, task_id, second_builder.current_run_id, head=HEAD_B,
        )
        assert kb.request_task_changes(
            conn,
            task_id,
            reason="still unsafe",
            reviewed_head_sha=HEAD_B,
            max_review_rounds=2,
            expected_run_id=second_review,
        ) == "blocked"
        task = kb.get_task(conn, task_id)

    assert task.status == "blocked"
    assert task.assignee == "builder"
    assert task.block_kind == "capability"


def test_requested_changes_bypass_active_pr_guard_for_exact_round(
    delivery_board, monkeypatch,
):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        review_run = _submit_and_claim_review(conn, task_id, builder_run)
        kb.add_comment(
            conn,
            task_id,
            "reviewer",
            f"Reviewing the submitted candidate at {PR_URL}",
        )
        assert kb.check_respawn_guard(conn, task_id) == "active_pr"
        assert kb.request_task_changes(
            conn,
            task_id,
            reason="fix the failing boundary test",
            reviewed_head_sha=HEAD_A,
            max_review_rounds=2,
            expected_run_id=review_run,
        ) == "ready"
        assert kb.check_respawn_guard(conn, task_id) is None
        result = kb.dispatch_once(conn, dry_run=True, max_in_progress=1)
        kb.add_comment(
            conn,
            task_id,
            "builder",
            "Replacement candidate mentioned at https://github.com/acme/widgets/pull/18",
        )
        assert kb.check_respawn_guard(conn, task_id) == "active_pr"

    assert [row[0] for row in result.spawned] == [task_id]


@pytest.mark.parametrize(
    "recovery",
    [
        "spawn_failure",
        "manual_reclaim",
        "expired_claim",
        "max_runtime",
        "stale_heartbeat",
        "crash",
        "rate_limit",
    ],
)
def test_review_attempt_recovery_returns_to_review_without_new_round(
    delivery_board, monkeypatch, recovery,
):
    host = kb._claimer_id().split(":", 1)[0]
    claimer = f"{host}:review-test"
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        review_run = _submit_and_claim_review(
            conn, task_id, builder_run, claimer=claimer,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET max_retries = 10 WHERE id = ?",
                (task_id,),
            )

        if recovery == "spawn_failure":
            assert not kb._record_spawn_failure(
                conn, task_id, "spawn failed", failure_limit=10,
            )
        elif recovery == "manual_reclaim":
            assert kb.reclaim_task(
                conn, task_id, reason="operator retry", signal_fn=lambda *_: None,
            )
        elif recovery == "expired_claim":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                    (int(time.time()) - 10, task_id),
                )
            assert kb.release_stale_claims(
                conn, signal_fn=lambda *_: None,
            ) == 1
        elif recovery == "max_runtime":
            now = int(time.time())
            kb._set_worker_pid(conn, task_id, 71001)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET max_runtime_seconds = 1 WHERE id = ?",
                    (task_id,),
                )
                conn.execute(
                    "UPDATE task_runs SET started_at = ? WHERE id = ?",
                    (now - 30, review_run),
                )
            assert task_id in kb.enforce_max_runtime(
                conn, signal_fn=lambda *_: None,
            )
        elif recovery == "stale_heartbeat":
            now = int(time.time())
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET started_at = ? WHERE id = ?",
                    (now - 7200, review_run),
                )
                conn.execute(
                    "UPDATE tasks SET last_heartbeat_at = NULL WHERE id = ?",
                    (task_id,),
                )
            assert task_id in kb.detect_stale_running(
                conn, stale_timeout_seconds=60, signal_fn=lambda *_: None,
            )
        else:
            pid = 72001 if recovery == "crash" else 72002
            kb._set_worker_pid(conn, task_id, pid)
            if recovery == "rate_limit":
                kb._record_worker_exit(
                    pid, kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8,
                )
            assert (
                task_id in kb.detect_crashed_workers(conn)
            ) is (recovery == "crash")

        task = kb.get_task(conn, task_id)
        submissions = conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review'",
            (task_id,),
        ).fetchone()[0]
        assert task.status == "review"
        assert task.assignee == "reviewer"
        assert task.current_run_id is None
        assert submissions == 1

        retried = kb.claim_review_task(conn, task_id, claimer=f"{host}:retry")
        assert retried is not None
        assert kb._run_source_status(
            conn, task_id, retried.current_run_id,
        ) == "review"


def test_rate_limited_review_retry_obeys_respawn_cooldown(
    delivery_board, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    now = int(time.time())
    # Keep setup in the same clock epoch as the injected reviewer exit. If a
    # second ticks during setup, the older builder can otherwise appear to
    # have ended after that reviewer and this tests the wrong latest run.
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        review_run = _submit_and_claim_review(conn, task_id, builder_run)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET outcome = 'rate_limited', "
                "status = 'rate_limited', ended_at = ? WHERE id = ?",
                (now, review_run),
            )
            conn.execute(
                "UPDATE tasks SET status = 'review', current_run_id = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
                "last_failure_error = ? WHERE id = ?",
                ("reviewer rate-limited (quota wall)", task_id),
            )
        monkeypatch.setattr(kb.time, "time", lambda: now + 100)
        result = kb.dispatch_once(conn, dry_run=True, max_in_progress=1)

    assert result.spawned == []
    assert (task_id, "rate_limit_cooldown") in result.respawn_guarded


def test_review_block_unblock_resumes_review_queue(delivery_board):
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        review_run = _submit_and_claim_review(conn, task_id, builder_run)
        assert kb.block_task(
            conn,
            task_id,
            reason="temporary reviewer capability outage",
            kind="capability",
            expected_run_id=review_run,
        )
        assert kb.get_task(conn, task_id).status == "blocked"
        assert kb.unblock_task(conn, task_id)
        held = kb.get_task(conn, task_id)
        assert held.status == "review"
        retried = kb.claim_review_task(conn, task_id, claimer="review-retry")
        assert retried is not None
        assert kb._run_source_status(
            conn, task_id, retried.current_run_id,
        ) == "review"


def test_review_circuit_breaker_unblock_resumes_review_queue(delivery_board):
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        _submit_and_claim_review(conn, task_id, builder_run)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET max_retries = 1 WHERE id = ?", (task_id,),
            )
        assert kb._record_spawn_failure(
            conn, task_id, "reviewer spawn failed", failure_limit=3,
        )
        blocked = kb.get_task(conn, task_id)
        assert blocked.status == "blocked"
        gave_up = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "gave_up"
        ][-1]
        assert gave_up.payload["resume_status"] == "review"
        assert kb.unblock_task(conn, task_id)
        resumed = kb.get_task(conn, task_id)

    assert resumed.status == "review"
    assert resumed.assignee == "reviewer"


def test_ended_review_failure_breaker_preserves_resume_queue(delivery_board):
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        _submit_and_claim_review(conn, task_id, builder_run)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET max_retries = 1 WHERE id = ?", (task_id,),
            )
            kb._end_run(
                conn,
                task_id,
                outcome="timed_out",
                status="timed_out",
                error="review timed out",
            )
            # Failure accounting receives a fully released terminal attempt,
            # as the real timeout/crash caller leaves it. A dangling claim is
            # intentionally refused by the independent reopen boundary.
            conn.execute(
                "UPDATE tasks SET status = 'review', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL WHERE id = ?", (task_id,),
            )
        assert kb._record_task_failure(
            conn,
            task_id,
            "review timed out",
            outcome="timed_out",
            failure_limit=3,
            release_claim=False,
            end_run=False,
        )
        assert kb.unblock_task(conn, task_id)
        resumed = kb.get_task(conn, task_id)

    assert resumed.status == "review"
    assert resumed.assignee == "reviewer"


def test_scheduled_review_unblocks_back_to_review(delivery_board):
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        review_run = _submit_and_claim_review(conn, task_id, builder_run)
        assert kb.schedule_task(
            conn,
            task_id,
            reason="wait for a provider window",
            expected_run_id=review_run,
        )
        assert kb.get_task(conn, task_id).status == "scheduled"
        assert kb.unblock_task(conn, task_id)
        resumed = kb.get_task(conn, task_id)

    assert resumed.status == "review"
    assert resumed.assignee == "reviewer"

def test_review_dependency_hold_promotes_back_to_review(delivery_board):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="external prerequisite")
        assert kb.complete_task(conn, parent, summary="prerequisite initially done")
        task_id, builder_run = _create_claimed_code_task(conn)
        kb.link_tasks(conn, parent, task_id)
        review_run = _submit_and_claim_review(conn, task_id, builder_run)
        # The valid review claim precedes the prerequisite reopening. A claim
        # beside an already-undone parent must still be refused.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?", (parent,))
        assert kb.block_task(
            conn,
            task_id,
            reason="waiting for prerequisite",
            kind="dependency",
            expected_run_id=review_run,
        )
        assert kb.get_task(conn, task_id).status == "todo"
        assert kb.complete_task(conn, parent, summary="prerequisite done")
        # Parent completion may run recompute_ready immediately; an explicit
        # later pass remains idempotent.
        kb.recompute_ready(conn)
        held = kb.get_task(conn, task_id)

    assert held.status == "review"
    assert held.assignee == "reviewer"


def test_dispatch_prioritizes_review_with_single_slot_and_ready_backlog(
    delivery_board, monkeypatch,
):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
        assert kb.submit_task_for_review(
            conn,
            task_id,
            pull_request=_pull_request(),
            reviewer_assignee="reviewer",
            expected_run_id=builder_run,
        )
        for index in range(4):
            kb.create_task(conn, title=f"ready {index}", assignee="builder")
        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_in_progress=1,
            max_in_progress_per_profile=1,
        )

    assert [row[0] for row in result.spawned] == [task_id]


def test_review_only_queue_obeys_global_and_per_profile_caps(
    delivery_board, monkeypatch,
):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    with kb.connect() as conn:
        review_ids = []
        for index in range(2):
            task_id, run_id = _create_claimed_code_task(
                conn, assignee=f"builder-{index}",
            )
            assert kb.submit_task_for_review(
                conn,
                task_id,
                pull_request={
                    **_pull_request(),
                    "pr_url": f"https://github.com/acme/widgets/pull/{20 + index}",
                    "pr_number": 20 + index,
                },
                reviewer_assignee="reviewer",
                expected_run_id=run_id,
            )
            review_ids.append(task_id)
        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_in_progress=3,
            max_in_progress_per_profile=1,
        )

    assert len(result.spawned) == 1
    assert result.spawned[0][0] in review_ids
    assert len(result.skipped_per_profile_capped) == 1


def test_worker_toolset_exposes_review_lifecycle_schemas(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_schema")
    import tools.kanban_tools  # noqa: F401 - registration side effect
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    definitions = registry.get_definitions(
        set(resolve_toolset("hermes-cli")), quiet=True,
    )
    schemas = {
        item["function"]["name"]: item["function"]
        for item in definitions
        if "function" in item
    }

    assert "kanban_submit_for_review" in schemas
    assert "pull_request" in schemas["kanban_submit_for_review"]["parameters"]["required"]
    submit_description = schemas["kanban_submit_for_review"]["description"]
    assert "Hermes-Task-ID: $HERMES_KANBAN_TASK" in submit_description
    assert "Rollback:" in submit_description
    assert "delivery" in schemas["kanban_complete"]["parameters"]["properties"]
    assert "kanban_request_changes" in schemas
    from agent.prompt_builder import KANBAN_GUIDANCE

    assert "Hermes-Task-ID: $HERMES_KANBAN_TASK" in KANBAN_GUIDANCE
    assert "Rollback:" in KANBAN_GUIDANCE


def test_worker_tools_land_submit_and_terminal_review_transitions(
    delivery_board, monkeypatch,
):
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(builder_run))
    monkeypatch.setenv("HERMES_SESSION_ID", "builder-session")
    monkeypatch.setattr(
        kt,
        "load_config",
        lambda: {"kanban": {"reviewer_profile": "reviewer"}},
    )

    submitted = json.loads(
        kt._handle_submit_for_review(
            {
                "pull_request": _pull_request(),
                "summary": "candidate ready",
            }
        )
    )
    assert submitted["ok"] is True

    with kb.connect() as conn:
        review = kb.claim_review_task(conn, task_id, claimer="review-tool")
        assert review is not None
        review_run = review.current_run_id
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review_run))
    monkeypatch.setenv("HERMES_SESSION_ID", "reviewer-session")
    completed = json.loads(
        kt._handle_complete(
            {
                "summary": "verified exact head and merge",
                "delivery": _merged_delivery(),
            }
        )
    )

    assert completed["ok"] is True
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "done"


def test_worker_review_routing_uses_shared_root_config_from_backend_profile(
    delivery_board, tmp_path, monkeypatch,
):
    from tools import kanban_tools as kt

    hermes_root = tmp_path / "hermes-root"
    backend_home = hermes_root / "profiles" / "backend"
    backend_home.mkdir(parents=True)
    (hermes_root / "config.yaml").write_text(
        "kanban:\n  reviewer_profile: reviewer\n  max_review_rounds: 3\n",
        encoding="utf-8",
    )
    (backend_home / "config.yaml").write_text(
        "kanban:\n  reviewer_profile: ''\n  max_review_rounds: 99\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(backend_home))
    with kb.connect() as conn:
        task_id, builder_run = _create_claimed_code_task(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(builder_run))

    assert kt._shared_kanban_config()["reviewer_profile"] == "reviewer"
    assert kt._shared_kanban_config()["max_review_rounds"] == 3
    submitted = json.loads(kt._handle_submit_for_review({
        "pull_request": _pull_request(),
        "summary": "candidate ready",
    }))
    assert submitted["ok"] is True
    assert submitted["reviewer"] == "reviewer"
