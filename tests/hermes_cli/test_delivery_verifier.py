"""Direct tests for the trusted GitHub/acceptance/deployment verifier."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import stat
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import delivery_verifier as verifier


HEAD = "a" * 40
MERGE = "b" * 40
BASE = "d" * 40
WORKFLOW_SHA = "e" * 40
DEPLOY_WORKFLOW_SHA = "c" * 40
BRANCH = "hermes/project/t_deadbeef-change"
REPO = "clauseye-com/clauseye-contra-rope"
PR_URL = f"https://github.com/{REPO}/pull/592"


def test_delivery_policy_cache_path_is_fixed():
    assert verifier._policy_attestation_path() == Path(
        "/var/lib/hermes-delivery-control/attestations/clauseye-production.json"
    )


def _contract() -> str:
    return """```acceptance-contract
domain: coding
target: github-merge
tier1:
  - cmd: "python -m pytest -q"
    expect_exit: 0
tier2:
  - "independent review"
tier3: "The exact candidate is merged."
```"""


def _task(tmp_path: Path, **overrides):
    values = {
        "id": "t_deadbeef",
        "body": _contract(),
        "workspace_path": str(tmp_path),
        "branch_name": BRANCH,
        "current_run_id": 42,
        "assignee": "reviewer",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _pr(**overrides):
    value = {
        "html_url": PR_URL,
        "state": "open",
        "merged": False,
        "merged_at": None,
        "draft": False,
        "head": {
            "sha": HEAD,
            "ref": BRANCH,
            "repo": {"id": 1182097307, "full_name": REPO},
        },
        "base": {
            "ref": "main",
            "sha": "d" * 40,
            "repo": {
                "id": 1182097307,
                "full_name": REPO,
                "default_branch": "main",
            },
        },
    }
    value.update(overrides)
    return value


def _submission_claim():
    return {
        "pr_url": PR_URL,
        "pr_number": 592,
        "head_sha": HEAD,
        "candidate_ref": BRANCH,
    }


def _git_run(argv, **_kwargs):
    joined = " ".join(argv)
    if "remote get-url origin" in joined:
        return f"https://github.com/{REPO}.git\n"
    if "rev-parse HEAD" in joined:
        return HEAD + "\n"
    if "status --porcelain" in joined:
        return ""
    raise AssertionError(f"unexpected command: {argv}")


def _stub_registered_project(monkeypatch):
    monkeypatch.setattr(
        verifier,
        "_require_registered_clauseye_project",
        lambda _task, workspace, _repo: workspace.parent,
    )


def test_project_binding_rejects_remote_substitution_and_foreign_workspace(
    tmp_path, monkeypatch,
):
    import hermes_constants
    import sqlite3

    project_root = tmp_path / "clauseye"
    workspace = project_root / ".worktrees" / "t_deadbeef"
    workspace.mkdir(parents=True)
    registry_root = tmp_path / "root"
    registry_root.mkdir()
    registry = registry_root / "projects.db"
    with sqlite3.connect(registry) as connection:
        connection.execute(
            "CREATE TABLE projects (id TEXT PRIMARY KEY, slug TEXT NOT NULL, "
            "primary_path TEXT, archived INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO projects VALUES (?, ?, ?, 0)",
            ("p_trusted", "clauseye-production-readiness", str(project_root)),
        )

    monkeypatch.setattr(
        hermes_constants, "get_default_hermes_root", lambda: registry_root,
    )
    monkeypatch.setattr(
        verifier,
        "_run",
        lambda *_args, **_kwargs: f"https://github.com/{REPO}.git\n",
    )
    task = SimpleNamespace(project_id="p_trusted")
    verifier._require_registered_clauseye_project(task, workspace, REPO)

    with pytest.raises(verifier.LiveVerificationError) as foreign:
        verifier._require_registered_clauseye_project(
            task, tmp_path / "foreign", REPO,
        )
    assert foreign.value.code == "project_workspace_mismatch"

    monkeypatch.setattr(
        verifier,
        "_run",
        lambda *_args, **_kwargs: "https://github.com/acme/widgets.git\n",
    )
    with pytest.raises(verifier.LiveVerificationError) as substituted:
        verifier._require_registered_clauseye_project(task, workspace, REPO)
    assert substituted.value.code == "project_repository_mismatch"


def test_project_identity_uses_shared_root_registry_from_reviewer_profile(
    tmp_path, monkeypatch,
):
    from hermes_cli import projects_db

    hermes_root = tmp_path / "hermes-root"
    reviewer_home = hermes_root / "profiles" / "reviewer"
    reviewer_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(reviewer_home))
    project_root = tmp_path / "clauseye"
    workspace = project_root / ".worktrees" / "t_deadbeef"
    workspace.mkdir(parents=True)
    with projects_db.connect_closing(
        db_path=hermes_root / "projects.db",
    ) as conn:
        project_id = projects_db.create_project(
            conn,
            name="ClausEye Production Readiness",
            slug="clauseye-production-readiness",
            primary_path=str(project_root),
        )
    with projects_db.connect_closing(
        db_path=reviewer_home / "projects.db",
    ) as conn:
        assert projects_db.get_project(conn, project_id) is None
    monkeypatch.setattr(
        verifier,
        "_run",
        lambda *_a, **_k: f"https://github.com/{REPO}.git\n",
    )

    observed = verifier._require_registered_clauseye_project(
        SimpleNamespace(project_id=project_id), workspace, REPO,
    )
    assert observed == project_root.resolve()


def test_verify_submission_binds_repo_head_ref_default_and_non_draft(
    tmp_path, monkeypatch,
):
    _stub_registered_project(monkeypatch)
    monkeypatch.setattr(verifier, "_run", _git_run)
    monkeypatch.setattr(verifier, "_pull_request", lambda *_a, **_k: _pr())

    observed = verifier.verify_submission(
        _task(tmp_path), {"contract_hash": "contract"}, _submission_claim(),
    )

    assert observed["repository"] == REPO
    assert observed["local_head_sha"] == HEAD
    assert observed["base_ref"] == "main"
    assert observed["pr_draft"] is False


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"draft": True}, "pr_is_draft"),
        (
            {"base": {"ref": "release", "repo": {
                "id": 1182097307,
                "full_name": REPO,
                "default_branch": "main",
            }}},
            "pr_base_branch_mismatch",
        ),
        (
            {"base": {"ref": "main", "repo": {
                "id": 9,
                "full_name": "other/repo",
                "default_branch": "main",
            }}},
            "pr_base_repository_mismatch",
        ),
    ],
)
def test_verify_submission_rejects_unsafe_pr_target(
    tmp_path, monkeypatch, mutation, code,
):
    _stub_registered_project(monkeypatch)
    monkeypatch.setattr(verifier, "_run", _git_run)
    monkeypatch.setattr(
        verifier, "_pull_request", lambda *_a, **_k: _pr(**mutation),
    )
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier.verify_submission(_task(tmp_path), {}, _submission_claim())
    assert rejected.value.code == code


def test_verify_submission_rejects_nonhermes_and_unregistered_repo(
    tmp_path, monkeypatch,
):
    _stub_registered_project(monkeypatch)
    monkeypatch.setattr(verifier, "_run", _git_run)
    monkeypatch.setattr(
        verifier,
        "_pull_request",
        lambda *_a, **_k: _pr(head={
            "sha": HEAD,
            "ref": "feature/untrusted",
            "repo": {"id": 1182097307, "full_name": REPO},
        }),
    )
    claim = _submission_claim()
    claim["candidate_ref"] = "feature/untrusted"
    with pytest.raises(verifier.LiveVerificationError) as branch_rejected:
        verifier.verify_submission(_task(tmp_path), {}, claim)
    assert branch_rejected.value.code == "autonomous_branch_required"

    other = dict(claim)
    other.update({
        "pr_url": "https://github.com/acme/widgets/pull/1",
        "pr_number": 1,
    })
    with pytest.raises(verifier.LiveVerificationError) as repo_rejected:
        verifier.verify_submission(_task(tmp_path), {}, other)
    assert repo_rejected.value.code == "repository_verifier_unregistered"


def _gate_external_id(*, candidate=HEAD, base=BASE, workflow=WORKFLOW_SHA):
    return (
        "hermes-autonomy/v1:repo:1182097307:pr:592:"
        f"candidate:{candidate}:base:{base}:workflow:{workflow}:"
        "run:700:attempt:2"
    )


def _gate_check(*, external_id=None, status="completed", conclusion="success"):
    return {
        "id": 900,
        "name": "Autonomous Merge Gate",
        "head_sha": HEAD,
        "status": status,
        "conclusion": conclusion,
        "details_url": f"https://github.com/{REPO}/actions/runs/700/attempts/2",
        "external_id": external_id or _gate_external_id(),
        "app": {"slug": "github-actions", "id": 15368},
    }


def _gate_association(**overrides):
    value = {
        "number": 592,
        "head": {
            "sha": HEAD,
            "ref": BRANCH,
            "repo": {"id": 1182097307},
        },
        "base": {
            "sha": BASE,
            "ref": "main",
            "repo": {"id": 1182097307},
        },
    }
    value.update(overrides)
    return value


def _gate_run(**overrides):
    value = {
        "id": 700,
        "run_attempt": 2,
        "workflow_id": 330662330,
        "workflow_url": (
            f"https://api.github.com/repos/{REPO}/actions/required_workflows/330662330"
        ),
        "html_url": f"https://github.com/{REPO}/actions/runs/700",
        "name": "Autonomous Merge Gate",
        "path": ".github/workflows/autonomous-merge-gate.yml",
        "event": "pull_request_target",
        "status": "completed",
        "conclusion": "success",
        "head_sha": HEAD,
        "head_branch": BRANCH,
        "repository": {"id": 1182097307, "full_name": REPO},
        "pull_requests": [_gate_association()],
    }
    value.update(overrides)
    return value


def _gate_attestation(**overrides):
    value = {
        "schema": "hermes-autonomy-attestation/v1",
        "repository_id": 1182097307,
        "repository": REPO,
        "pull_request_number": 592,
        "candidate_sha": HEAD,
        "candidate_repository_id": 1182097307,
        "base_sha": BASE,
        "base_ref": "main",
        "workflow_path": ".github/workflows/autonomous-merge-gate.yml",
        "workflow_sha": WORKFLOW_SHA,
        "workflow_run_id": 700,
        "workflow_run_attempt": 2,
        "event": "pull_request_target",
    }
    value.update(overrides)
    return value


def _gate_attestation_zip(
    *,
    payload=None,
    filename="attestation.json",
    symlink=False,
    second_entry=False,
):
    output = io.BytesIO()
    info = zipfile.ZipInfo(filename)
    if symlink:
        info.create_system = 3
        info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(info, json.dumps(payload or _gate_attestation()))
        if second_entry:
            bundle.writestr("extra.json", "{}")
    return output.getvalue()


def _gate_artifacts(*items):
    values = list(items) or [{
        "id": 901,
        "name": "hermes-autonomy-attestation-v1",
        "expired": False,
        "size_in_bytes": len(_gate_attestation_zip()),
    }]
    return {"total_count": len(values), "artifacts": values}


def test_autonomous_gate_is_bound_to_native_required_workflow_and_pr(
    monkeypatch, tmp_path,
):
    def fake_json(argv, **_kwargs):
        joined = " ".join(argv)
        if f"commits/{MERGE}" in joined:
            return {"parents": [{"sha": BASE}]}
        if "/actions/runs/700/artifacts" in joined:
            return _gate_artifacts()
        if joined.endswith("/actions/runs/700"):
            return _gate_run()
        if "check-runs" in joined:
            return {"check_runs": [_gate_check()]}
        raise AssertionError(argv)

    monkeypatch.setattr(verifier, "_json", fake_json)
    monkeypatch.setattr(verifier, "_bytes", lambda *_a, **_k: _gate_attestation_zip())
    monkeypatch.setattr(
        verifier,
        "_protected_control_plane_unchanged",
        lambda *_a, **_k: {"manifest_sha256": "f" * 64},
    )
    observed = verifier._successful_autonomous_gate(
        REPO,
        HEAD,
        592,
        head_ref=BRANCH,
        base_ref="main",
        merge_sha=MERGE,
        trusted_workflow_sha=WORKFLOW_SHA,
        cwd=tmp_path,
    )
    assert observed["workflow_run_id"] == 700
    assert observed["workflow_id"] == 330662330
    assert observed["authoritative_source"] == "native_required_workflow"
    assert observed["event"] == "pull_request_target"
    assert observed["workflow_path"].endswith("autonomous-merge-gate.yml")
    assert observed["attestation"]["candidate_sha"] == HEAD
    assert observed["attestation"]["artifact_id"] == 901


def test_autonomous_gate_allows_github_post_merge_association_elision(
    monkeypatch, tmp_path,
):
    def fake_json(argv, **_kwargs):
        joined = " ".join(argv)
        if f"commits/{MERGE}" in joined:
            return {"parents": [{"sha": BASE}]}
        if "/actions/runs/700/artifacts" in joined:
            return _gate_artifacts()
        if joined.endswith("/actions/runs/700"):
            return _gate_run(pull_requests=[])
        if "check-runs" in joined:
            return {"check_runs": [_gate_check()]}
        raise AssertionError(argv)

    monkeypatch.setattr(verifier, "_json", fake_json)
    monkeypatch.setattr(verifier, "_bytes", lambda *_a, **_k: _gate_attestation_zip())
    monkeypatch.setattr(
        verifier,
        "_protected_control_plane_unchanged",
        lambda *_a, **_k: {"manifest_sha256": "f" * 64},
    )
    observed = verifier._successful_autonomous_gate(
        REPO,
        HEAD,
        592,
        head_ref=BRANCH,
        base_ref="main",
        merge_sha=MERGE,
        trusted_workflow_sha=WORKFLOW_SHA,
        cwd=tmp_path,
    )
    assert observed["workflow_run_id"] == 700
    assert observed["attestation"]["candidate_sha"] == HEAD


@pytest.mark.parametrize(
    ("check", "run_overrides", "code"),
    [
        (
            _gate_check(external_id=_gate_external_id(workflow="f" * 40)),
            {},
            "autonomous_merge_gate_correlation_missing",
        ),
        (
            _gate_check(),
            {"event": "pull_request"},
            "autonomous_required_workflow_missing",
        ),
        (
            _gate_check(),
            {"pull_requests": [{"number": 591}]},
            "autonomous_required_workflow_missing",
        ),
        (
            _gate_check(),
            {"conclusion": "failure"},
            "autonomous_required_workflow_failed",
        ),
    ],
)
def test_autonomous_gate_rejects_spoofed_or_mixed_provenance(
    monkeypatch, tmp_path, check, run_overrides, code,
):
    def fake_json(argv, **_kwargs):
        joined = " ".join(argv)
        if f"commits/{MERGE}" in joined:
            return {"parents": [{"sha": BASE}]}
        if "/actions/runs/700/artifacts" in joined:
            return _gate_artifacts()
        if joined.endswith("/actions/runs/700"):
            return _gate_run(**run_overrides)
        if "check-runs" in joined:
            return {"check_runs": [check]}
        raise AssertionError(argv)

    monkeypatch.setattr(verifier, "_json", fake_json)
    monkeypatch.setattr(verifier, "_bytes", lambda *_a, **_k: _gate_attestation_zip())
    monkeypatch.setattr(
        verifier,
        "_protected_control_plane_unchanged",
        lambda *_a, **_k: {"manifest_sha256": "f" * 64},
    )
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._successful_autonomous_gate(
            REPO,
            HEAD,
            592,
            head_ref=BRANCH,
            base_ref="main",
            merge_sha=MERGE,
            trusted_workflow_sha=WORKFLOW_SHA,
            cwd=tmp_path,
        )
    assert rejected.value.code == code


@pytest.mark.parametrize(
    "run_overrides",
    [
        {"workflow_url": "https://api.github.com/repos/acme/widgets/actions/runs/700"},
        {"head_sha": BASE},
        {"head_branch": "main"},
        {"workflow_id": 0},
    ],
)
def test_autonomous_gate_rejects_required_workflow_run_identity_drift(
    monkeypatch, tmp_path, run_overrides,
):
    def fake_json(argv, **_kwargs):
        joined = " ".join(argv)
        if f"commits/{MERGE}" in joined:
            return {"parents": [{"sha": BASE}]}
        if "/actions/runs/700/artifacts" in joined:
            return _gate_artifacts()
        if joined.endswith("/actions/runs/700"):
            return _gate_run(**run_overrides)
        if "check-runs" in joined:
            return {"check_runs": [_gate_check()]}
        raise AssertionError(argv)

    monkeypatch.setattr(verifier, "_json", fake_json)
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._successful_autonomous_gate(
            REPO,
            HEAD,
            592,
            head_ref=BRANCH,
            base_ref="main",
            merge_sha=MERGE,
            trusted_workflow_sha=WORKFLOW_SHA,
            cwd=tmp_path,
        )
    assert rejected.value.code == "autonomous_required_workflow_missing"


@pytest.mark.parametrize(
    "associations",
    [
        [_gate_association(number=591)],
        [_gate_association(), _gate_association()],
        (_gate_association(),),
    ],
    ids=["wrong", "duplicate", "non-list"],
)
def test_autonomous_gate_rejects_nonexact_run_associations(
    monkeypatch, tmp_path, associations,
):
    def fake_json(argv, **_kwargs):
        joined = " ".join(argv)
        if f"commits/{MERGE}" in joined:
            return {"parents": [{"sha": BASE}]}
        if joined.endswith("/actions/runs/700"):
            return _gate_run(pull_requests=associations)
        if "check-runs" in joined:
            return {"check_runs": [_gate_check()]}
        raise AssertionError(argv)

    monkeypatch.setattr(verifier, "_json", fake_json)
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._successful_autonomous_gate(
            REPO,
            HEAD,
            592,
            head_ref=BRANCH,
            base_ref="main",
            merge_sha=MERGE,
            trusted_workflow_sha=WORKFLOW_SHA,
            cwd=tmp_path,
        )
    assert rejected.value.code == "autonomous_required_workflow_missing"


def test_autonomous_gate_rejects_ambiguous_successful_correlation_checks(
    monkeypatch, tmp_path,
):
    second = _gate_check()
    second["id"] = 902

    def fake_json(argv, **_kwargs):
        joined = " ".join(argv)
        if f"commits/{MERGE}" in joined:
            return {"parents": [{"sha": BASE}]}
        if "check-runs" in joined:
            return {"check_runs": [_gate_check(), second]}
        raise AssertionError(argv)

    monkeypatch.setattr(verifier, "_json", fake_json)
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._successful_autonomous_gate(
            REPO,
            HEAD,
            592,
            head_ref=BRANCH,
            base_ref="main",
            merge_sha=MERGE,
            trusted_workflow_sha=WORKFLOW_SHA,
            cwd=tmp_path,
        )
    assert rejected.value.code == "autonomous_merge_gate_correlation_missing"


@pytest.mark.parametrize(
    ("artifacts", "archive", "code"),
    [
        (
            _gate_artifacts({
                "id": 901,
                "name": "hermes-autonomy-attestation-v1",
                "expired": True,
                "size_in_bytes": 100,
            }),
            _gate_attestation_zip(),
            "autonomous_gate_attestation_missing",
        ),
        (
            _gate_artifacts(
                {
                    "id": 901,
                    "name": "hermes-autonomy-attestation-v1",
                    "expired": False,
                    "size_in_bytes": 100,
                },
                {
                    "id": 902,
                    "name": "hermes-autonomy-attestation-v1",
                    "expired": False,
                    "size_in_bytes": 100,
                },
            ),
            _gate_attestation_zip(),
            "autonomous_gate_attestation_missing",
        ),
        (
            _gate_artifacts(),
            _gate_attestation_zip(filename="../attestation.json"),
            "autonomous_gate_attestation_invalid",
        ),
        (
            _gate_artifacts(),
            _gate_attestation_zip(symlink=True),
            "autonomous_gate_attestation_invalid",
        ),
        (
            _gate_artifacts(),
            _gate_attestation_zip(second_entry=True),
            "autonomous_gate_attestation_invalid",
        ),
        (
            _gate_artifacts(),
            _gate_attestation_zip(
                payload={**_gate_attestation(), "untrusted": True},
            ),
            "autonomous_gate_attestation_mismatch",
        ),
        (
            _gate_artifacts(),
            _gate_attestation_zip(
                payload={**_gate_attestation(), "padding": "x" * 20_000},
            ),
            "autonomous_gate_attestation_invalid",
        ),
    ],
)
def test_gate_attestation_rejects_expired_duplicate_or_unsafe_artifact(
    monkeypatch, tmp_path, artifacts, archive, code,
):
    monkeypatch.setattr(verifier, "_json", lambda *_a, **_k: artifacts)
    monkeypatch.setattr(verifier, "_bytes", lambda *_a, **_k: archive)
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._trusted_gate_attestation(
            REPO,
            run_id=700,
            run_attempt=2,
            pr_number=592,
            candidate_sha=HEAD,
            base_sha=BASE,
            base_ref="main",
            workflow_sha=WORKFLOW_SHA,
            cwd=tmp_path,
        )
    assert rejected.value.code == code


def _control_tree(**overrides):
    paths = sorted(
        verifier._CLAUSEYE_PROTECTED_CONTROL_PATHS
        | {"backend/deploy/terraform/main.tf"}
    )
    blobs = {
        path: f"{index + 1:040x}"
        for index, path in enumerate(paths)
    }
    blobs.update(overrides)
    return {
        "truncated": False,
        "tree": [
            {"path": path, "type": "blob", "sha": sha}
            for path, sha in blobs.items()
        ],
    }


def test_protected_control_plane_must_match_immutable_workflow_source(
    monkeypatch, tmp_path,
):
    baseline = _control_tree()
    merged = _control_tree()

    def same_json(argv, **_kwargs):
        return baseline if WORKFLOW_SHA in " ".join(argv) else merged

    monkeypatch.setattr(verifier, "_json", same_json)
    observed = verifier._protected_control_plane_unchanged(
        REPO,
        baseline_sha=WORKFLOW_SHA,
        merge_sha=MERGE,
        cwd=tmp_path,
    )
    assert observed["path_count"] == len(baseline["tree"])

    changed_path = ".github/workflows/auto-deploy-production.yml"
    changed = _control_tree(**{changed_path: "f" * 40})

    def changed_json(argv, **_kwargs):
        return baseline if WORKFLOW_SHA in " ".join(argv) else changed

    monkeypatch.setattr(verifier, "_json", changed_json)
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._protected_control_plane_unchanged(
            REPO,
            baseline_sha=WORKFLOW_SHA,
            merge_sha=MERGE,
            cwd=tmp_path,
        )
    assert rejected.value.code == "protected_control_plane_changed"


def _ruleset(
    *,
    bypass=None,
    integration_id=15368,
    strict=True,
    contexts=("Autonomous Merge Gate", "Backend"),
    omit_rule=None,
):
    rules = [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {"type": "required_linear_history"},
        {
            "type": "pull_request",
            "parameters": {
                "dismiss_stale_reviews_on_push": True,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_approving_review_count": 0,
                "required_review_thread_resolution": True,
                "allowed_merge_methods": ["squash"],
            },
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": strict,
                "required_status_checks": [
                    {"context": context, "integration_id": integration_id}
                    for context in contexts
                ],
            },
        },
    ]
    return {
        "id": 15579823,
        "name": "main-protection",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [] if bypass is None else bypass,
        "conditions": {
            "ref_name": {"include": ["refs/heads/main"], "exclude": []},
        },
        "rules": [rule for rule in rules if rule["type"] != omit_rule],
    }


def _ruleset_template(**kwargs):
    value = _ruleset(**kwargs)
    value.pop("id")
    return value


def _contents_payload(value):
    raw = (json.dumps(value, sort_keys=True) + "\n").encode()
    return {
        "type": "file",
        "encoding": "base64",
        "size": len(raw),
        "content": base64.b64encode(raw).decode(),
    }


def _applied_rules(*, workflow_sha=WORKFLOW_SHA):
    return [
        {
            "type": "pull_request",
            "parameters": {"required_review_thread_resolution": True},
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [
                    {"context": "Autonomous Merge Gate"},
                    {"context": "Backend"},
                ],
            },
        },
        {
            "type": "workflows",
            "parameters": {
                "do_not_enforce_on_create": False,
                "workflows": [{
                    "repository_id": 1182097307,
                    "path": ".github/workflows/autonomous-merge-gate.yml",
                    "sha": workflow_sha,
                }],
            },
        },
    ]


def _org_ruleset(
    *, workflow_sha=WORKFLOW_SHA, bypass=None, do_not_enforce_on_create=False,
):
    return {
        "id": 321,
        "name": "clauseye-autonomous-merge-workflow",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [] if bypass is None else bypass,
        "conditions": {
            "repository_id": {"repository_ids": [1182097307]},
            "ref_name": {"include": ["refs/heads/main"]},
        },
        "rules": [{
            "type": "workflows",
            "parameters": {
                "do_not_enforce_on_create": do_not_enforce_on_create,
                "workflows": [{
                    "repository_id": 1182097307,
                    "path": ".github/workflows/autonomous-merge-gate.yml",
                    "sha": workflow_sha,
                }],
            },
        }],
    }


def _configure_policy_cache(tmp_path, monkeypatch):
    system_root = tmp_path / "var-lib"
    control = system_root / "hermes-delivery-control"
    directory = control / "attestations"
    directory.mkdir(parents=True, exist_ok=True)
    system_root.chmod(0o700)
    control.chmod(0o755)
    directory.chmod(0o755)
    path = directory / "clauseye-production.json"
    monkeypatch.setattr(
        verifier, "_POLICY_ATTESTATION_SYSTEM_ROOT", system_root,
    )
    monkeypatch.setattr(verifier, "_POLICY_ATTESTATION_DIRECTORY", directory)
    monkeypatch.setattr(verifier, "_POLICY_ATTESTATION_PATH", path)
    monkeypatch.setattr(verifier, "_POLICY_ATTESTATION_OWNER_UID", os.geteuid())
    monkeypatch.setattr(verifier, "_POLICY_ATTESTATION_OWNER_GID", os.getegid())
    return path


def _policy_private_key(monkeypatch, private_key_pem=None):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    key = (
        Ed25519PrivateKey.generate()
        if private_key_pem is None
        else serialization.load_pem_private_key(private_key_pem, password=None)
    )
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setattr(
        verifier,
        "_CLAUSEYE_POLICY_ATTESTATION_PUBLIC_KEY_B64",
        base64.b64encode(public).decode("ascii"),
    )
    return private_key_pem or key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _write_policy_attestation(
    tmp_path,
    monkeypatch,
    *,
    ruleset=None,
    org_ruleset=None,
    source_access="organization",
    now=None,
    ttl_seconds=5 * 60,
    private_key_pem=None,
    template=None,
    publish=True,
):
    _configure_policy_cache(tmp_path, monkeypatch)
    private_key = _policy_private_key(monkeypatch, private_key_pem)

    def admin_json(argv, **_kwargs):
        joined = " ".join(argv)
        if "actions/permissions/access" in joined:
            return {"access_level": source_access}
        if "repos/" in joined and "rulesets/15579823" in joined:
            return ruleset or _ruleset()
        if "orgs/clauseye-com/rulesets?" in joined:
            return [{"id": 321, "name": "clauseye-autonomous-merge-workflow"}]
        if "orgs/clauseye-com/rulesets/321" in joined:
            return org_ruleset or _org_ruleset()
        if "contents/.github/rulesets/main-protection.json" in joined:
            return _contents_payload(template or _ruleset_template())
        raise AssertionError(argv)

    monkeypatch.setattr(verifier, "_json", admin_json)
    envelope = verifier.produce_clauseye_policy_attestation(
        REPO,
        "main",
        cwd=tmp_path,
        private_key_pem=private_key,
        now=now,
        ttl_seconds=ttl_seconds,
    )
    if publish:
        published = verifier.publish_clauseye_policy_attestation(
            envelope,
            now=now,
        )
    else:
        verified = verifier._verify_policy_attestation_envelope(
            envelope,
            REPO,
            "main",
            now=now,
        )
        published = {
            "path": str(verifier._policy_attestation_path()),
            "policy_claim_sha256": verified["policy_claim_sha256"],
        }
    return {**published, "envelope": envelope, "private_key_pem": private_key}


def test_terminal_ruleset_rechecks_all_contexts_threads_and_no_bypass(
    tmp_path, monkeypatch,
):
    _write_policy_attestation(tmp_path, monkeypatch)
    worker_calls = []

    def fake_json(argv, **_kwargs):
        joined = " ".join(argv)
        worker_calls.append(joined)
        if "rules/branches/main" in joined:
            return _applied_rules()
        if "contents/.github/rulesets/main-protection.json" in joined:
            return _contents_payload(_ruleset_template())
        if "check-runs" in joined:
            return {"check_runs": [
                _gate_check(),
                {
                    **_gate_check(),
                    "id": 901,
                    "name": "Backend",
                },
            ]}
        if "graphql" in joined:
            return {"data": {"repository": {"pullRequest": {
                "reviewThreads": {
                    "nodes": [{"isResolved": True}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }}}}
        raise AssertionError(argv)

    monkeypatch.setattr(verifier, "_json", fake_json)
    observed = verifier._required_ruleset_evidence(
        REPO, "main", HEAD, 592, cwd=tmp_path,
    )
    assert observed["bypass_actors"] == 0
    assert set(observed["required_checks"]) == {
        "Autonomous Merge Gate", "Backend",
    }
    assert observed["required_workflow"]["workflow_source_sha"] == WORKFLOW_SHA
    assert observed["required_workflow"]["source_access_level"] == "organization"
    assert not any(
        endpoint in call
        for call in worker_calls
        for endpoint in (
            "actions/permissions/access",
            "orgs/clauseye-com/rulesets",
            "repos/clauseye-com/clauseye-contra-rope/rulesets/",
        )
    )


def test_policy_attestation_fails_closed_without_pinned_producer_key(
    tmp_path, monkeypatch,
):
    private_key = _policy_private_key(monkeypatch)
    monkeypatch.setattr(
        verifier, "_CLAUSEYE_POLICY_ATTESTATION_PUBLIC_KEY_B64", "",
    )
    monkeypatch.setattr(
        verifier,
        "_json",
        lambda *_a, **_k: pytest.fail("admin API must not run without trust"),
    )
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier.produce_clauseye_policy_attestation(
            REPO,
            "main",
            cwd=tmp_path,
            private_key_pem=private_key,
        )
    assert rejected.value.code == "delivery_policy_trust_unconfigured"


def test_policy_attestation_rejects_tampering_and_expiry(
    tmp_path, monkeypatch,
):
    issued = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    published = _write_policy_attestation(
        tmp_path, monkeypatch, now=issued,
    )
    path = Path(published["path"])
    observed = verifier._load_policy_attestation(
        REPO,
        "main",
        now=issued,
    )
    assert observed["payload"]["main_ruleset"]["bypass_actors"] == 0
    file_stat = path.stat()
    assert stat.S_IMODE(file_stat.st_mode) == 0o644
    assert file_stat.st_uid == os.geteuid()
    assert file_stat.st_gid == os.getegid()
    assert not list(path.parent.glob(".*.tmp"))

    with pytest.raises(verifier.LiveVerificationError) as stale:
        verifier._load_policy_attestation(
            REPO,
            "main",
            now=issued + timedelta(minutes=6),
        )
    assert stale.value.code == "delivery_policy_attestation_stale"

    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["main_ruleset"]["bypass_actors"] = 1
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(verifier.LiveVerificationError) as tampered:
        verifier._load_policy_attestation(
            REPO,
            "main",
            now=issued,
        )
    assert tampered.value.code == "delivery_policy_attestation_signature_invalid"


def test_policy_cache_consumer_and_publisher_reject_symlink(
    tmp_path, monkeypatch,
):
    published = _write_policy_attestation(tmp_path, monkeypatch)
    path = Path(published["path"])
    outside = tmp_path / "outside.json"
    path.replace(outside)
    expected = outside.read_bytes()
    path.symlink_to(outside)

    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._load_policy_attestation(
            REPO,
            "main",
        )
    assert rejected.value.code == "delivery_policy_cache_untrusted"

    with pytest.raises(verifier.LiveVerificationError) as publish_rejected:
        verifier.publish_clauseye_policy_attestation(published["envelope"])
    assert publish_rejected.value.code == (
        "delivery_policy_attestation_publish_failed"
    )
    assert path.is_symlink()
    assert outside.read_bytes() == expected


def test_policy_cache_rejects_worker_writable_directory(tmp_path, monkeypatch):
    published = _write_policy_attestation(tmp_path, monkeypatch)
    path = Path(published["path"])
    path.parent.chmod(0o777)

    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._load_policy_attestation(REPO, "main")
    assert rejected.value.code == "delivery_policy_cache_untrusted"

    with pytest.raises(verifier.LiveVerificationError) as publish_rejected:
        verifier.publish_clauseye_policy_attestation(published["envelope"])
    assert publish_rejected.value.code == (
        "delivery_policy_attestation_publish_failed"
    )


def test_policy_cache_rejects_worker_writable_file(tmp_path, monkeypatch):
    published = _write_policy_attestation(tmp_path, monkeypatch)
    path = Path(published["path"])
    path.chmod(0o666)

    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._load_policy_attestation(REPO, "main")
    assert rejected.value.code == "delivery_policy_cache_untrusted"

    with pytest.raises(verifier.LiveVerificationError) as publish_rejected:
        verifier.publish_clauseye_policy_attestation(published["envelope"])
    assert publish_rejected.value.code == (
        "delivery_policy_attestation_publish_failed"
    )


def test_policy_publisher_replace_failure_preserves_previous_envelope(
    tmp_path, monkeypatch,
):
    issued = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    initial = _write_policy_attestation(
        tmp_path,
        monkeypatch,
        now=issued,
    )
    candidate = _write_policy_attestation(
        tmp_path,
        monkeypatch,
        now=issued + timedelta(minutes=1),
        private_key_pem=initial["private_key_pem"],
        publish=False,
    )
    path = Path(initial["path"])
    previous = path.read_bytes()

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(verifier.os, "replace", fail_replace)
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier.publish_clauseye_policy_attestation(
            candidate["envelope"],
            now=issued + timedelta(minutes=1),
        )
    assert rejected.value.code == "delivery_policy_attestation_publish_failed"
    assert path.read_bytes() == previous
    assert not list(path.parent.glob(".*.tmp"))


def test_policy_publisher_exclusive_create_preserves_colliding_file(
    tmp_path, monkeypatch,
):
    issued = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    initial = _write_policy_attestation(
        tmp_path,
        monkeypatch,
        now=issued,
    )
    candidate = _write_policy_attestation(
        tmp_path,
        monkeypatch,
        now=issued + timedelta(minutes=1),
        private_key_pem=initial["private_key_pem"],
        publish=False,
    )
    path = Path(initial["path"])
    previous = path.read_bytes()
    token = "f" * 32
    monkeypatch.setattr(verifier.secrets, "token_hex", lambda _size: token)
    collision = path.parent / f".{path.name}.{os.getpid()}.{token}.tmp"
    collision.write_bytes(b"pre-existing")

    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier.publish_clauseye_policy_attestation(
            candidate["envelope"],
            now=issued + timedelta(minutes=1),
        )
    assert rejected.value.code == "delivery_policy_attestation_publish_failed"
    assert path.read_bytes() == previous
    assert collision.read_bytes() == b"pre-existing"


def test_terminal_policy_revalidation_accepts_fresh_timestamp_rotation(
    tmp_path, monkeypatch,
):
    issued = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    initial = _write_policy_attestation(
        tmp_path,
        monkeypatch,
        now=issued,
    )
    rotated = _write_policy_attestation(
        tmp_path,
        monkeypatch,
        now=issued + timedelta(minutes=4),
        private_key_pem=initial["private_key_pem"],
    )
    assert rotated["policy_claim_sha256"] == initial["policy_claim_sha256"]

    observed = verifier._revalidate_terminal_policy(
        REPO,
        "main",
        initial["policy_claim_sha256"],
        now=issued + timedelta(minutes=4),
    )
    assert observed["issued_at"] == "2026-08-09T20:04:00Z"


def test_terminal_policy_revalidation_rejects_expiry_race(
    tmp_path, monkeypatch,
):
    issued = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    initial = _write_policy_attestation(
        tmp_path,
        monkeypatch,
        now=issued,
    )

    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._revalidate_terminal_policy(
            REPO,
            "main",
            initial["policy_claim_sha256"],
            now=issued + timedelta(minutes=6),
        )
    assert rejected.value.code == "delivery_policy_attestation_stale"


def test_terminal_policy_revalidation_rejects_cache_revocation(
    tmp_path, monkeypatch,
):
    issued = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    initial = _write_policy_attestation(
        tmp_path,
        monkeypatch,
        now=issued,
    )
    Path(initial["path"]).unlink()

    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._revalidate_terminal_policy(
            REPO,
            "main",
            initial["policy_claim_sha256"],
            now=issued + timedelta(minutes=1),
        )
    assert rejected.value.code == "delivery_policy_attestation_missing"


def test_terminal_policy_revalidation_rejects_policy_drift_race(
    tmp_path, monkeypatch,
):
    issued = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    initial = _write_policy_attestation(
        tmp_path,
        monkeypatch,
        now=issued,
    )
    changed_ruleset = _ruleset(
        contexts=("Autonomous Merge Gate", "Backend", "Security"),
    )
    _write_policy_attestation(
        tmp_path,
        monkeypatch,
        now=issued + timedelta(minutes=1),
        private_key_pem=initial["private_key_pem"],
        ruleset=changed_ruleset,
        template=_ruleset_template(
            contexts=("Autonomous Merge Gate", "Backend", "Security"),
        ),
    )

    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._revalidate_terminal_policy(
            REPO,
            "main",
            initial["policy_claim_sha256"],
            now=issued + timedelta(minutes=1),
        )
    assert rejected.value.code == "delivery_policy_attestation_changed"


@pytest.mark.parametrize(
    ("applied", "org_ruleset"),
    [
        (_applied_rules()[:-1], _org_ruleset()),
        (_applied_rules(), _org_ruleset(workflow_sha="f" * 40)),
    ],
)
def test_worker_required_workflow_fails_closed_against_signed_policy(
    tmp_path, monkeypatch, applied, org_ruleset,
):
    _write_policy_attestation(
        tmp_path, monkeypatch, org_ruleset=org_ruleset,
    )
    attested = verifier._load_policy_attestation(
        REPO, "main",
    )
    monkeypatch.setattr(verifier, "_json", lambda *_a, **_k: applied)
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._required_workflow_policy(
            REPO,
            "main",
            required_contexts=["Autonomous Merge Gate", "Backend"],
            attested=attested,
            cwd=tmp_path,
        )
    assert rejected.value.code == "required_workflow_rule_missing"


@pytest.mark.parametrize(
    ("org_ruleset", "source_access", "code"),
    [
        (
            _org_ruleset(bypass=[{
                "actor_id": 1,
                "actor_type": "User",
                "bypass_mode": "always",
            }]),
            "organization",
            "required_workflow_policy_mismatch",
        ),
        (
            _org_ruleset(),
            "none",
            "required_workflow_source_access_mismatch",
        ),
        (
            _org_ruleset(do_not_enforce_on_create=True),
            "organization",
            "required_workflow_policy_mismatch",
        ),
    ],
)
def test_trusted_policy_producer_fails_closed_on_admin_policy_weakness(
    tmp_path, monkeypatch, org_ruleset, source_access, code,
):
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        _write_policy_attestation(
            tmp_path,
            monkeypatch,
            org_ruleset=org_ruleset,
            source_access=source_access,
        )
    assert rejected.value.code == code


@pytest.mark.parametrize(
    ("ruleset", "code"),
    [
        (
            _ruleset(bypass=[{
                "actor_id": 1, "actor_type": "User", "bypass_mode": "always",
            }]),
            "ruleset_bypass_enabled",
        ),
        (_ruleset(integration_id=0), "ruleset_check_integration_unbound"),
        (_ruleset(strict=False), "ruleset_policy_mismatch"),
    ],
)
def test_trusted_policy_producer_fails_closed_on_main_ruleset_weakness(
    tmp_path, monkeypatch, ruleset, code,
):
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        _write_policy_attestation(
            tmp_path, monkeypatch, ruleset=ruleset,
        )
    assert rejected.value.code == code


@pytest.mark.parametrize(
    "live_ruleset",
    [
        _ruleset(contexts=("Autonomous Merge Gate",)),
        _ruleset(omit_rule="deletion"),
        _ruleset(omit_rule="non_fast_forward"),
        _ruleset(omit_rule="required_linear_history"),
    ],
)
def test_terminal_ruleset_must_exactly_match_protected_template(
    tmp_path, monkeypatch, live_ruleset,
):
    _write_policy_attestation(tmp_path, monkeypatch)

    def fake_json(argv, **_kwargs):
        joined = " ".join(argv)
        if "rules/branches/main" in joined:
            return _applied_rules()
        if "contents/.github/rulesets/main-protection.json" in joined:
            changed = dict(live_ruleset)
            changed.pop("id", None)
            return _contents_payload(changed)
        raise AssertionError(argv)

    monkeypatch.setattr(verifier, "_json", fake_json)
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._required_ruleset_evidence(
            REPO, "main", HEAD, 592, cwd=tmp_path,
        )
    assert rejected.value.code == "ruleset_template_mismatch"


def _write_acceptance(
    root: Path,
    task,
    *,
    results=None,
    review_run_id=42,
    submission_event_id=77,
):
    from hermes_cli.acceptance_contract import lint_body

    evidence_dir = root / "coding" / "evidence" / task.id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence = {
        "schema": "hermes-evidence/v1",
        "task_id": task.id,
        "domain": "coding",
        "contract_hash": lint_body(task.body)["contract_hash"],
        "candidate_sha": HEAD,
        "results": results or [{
            "cmd": "python -m pytest -q",
            "exit": 0,
            "expect_ok": True,
            "ms": 1,
        }],
        "env_fingerprint": (
            "systemd sandbox/v2: private network,pids,home,tmp,devices; "
            "project read-only; bounded cgroup"
        ),
        "run_by": "reviewer",
        "review_run_id": review_run_id,
        "review_profile": "reviewer",
        "submission_event_id": submission_event_id,
        "verdict": "pass",
        "created_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
    }
    path = evidence_dir / "acceptance-test.json"
    raw = (json.dumps(evidence, indent=2) + "\n").encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    (evidence_dir / "SHA256SUMS").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8",
    )
    return path, evidence


def test_detached_acceptance_never_links_mutable_project_dependencies(
    tmp_path, monkeypatch,
):
    project = tmp_path / "project"
    detached = tmp_path / "detached"
    (project / "node_modules").mkdir(parents=True)
    (project / "backend" / ".venv").mkdir(parents=True)
    detached.mkdir()
    observed = object()
    monkeypatch.setattr(
        "tools.environments.local.resolve_trusted_dependency_projections",
        lambda workspace: observed if Path(workspace) == detached else None,
    )

    assert verifier._link_acceptance_dependencies(project, detached) is observed

    assert not (detached / "node_modules").exists()
    assert not (detached / "backend" / ".venv").exists()


def test_authoritative_acceptance_runs_exact_snapshot_body_and_tier1(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    (home / "state" / "tmp").mkdir(parents=True)
    evidence_root = tmp_path / "controller-evidence"
    evidence_root.mkdir(mode=0o700)
    monkeypatch.setattr(verifier, "_ACCEPTANCE_EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(
        "tools.environments.local.delivery_acceptance_candidate",
        lambda **_kwargs: contextlib.nullcontext((tmp_path, home, object())),
    )
    task = _task(tmp_path)
    observed_commands = []
    monkeypatch.setattr(
        verifier,
        "_run_acceptance_tier1_in_sandbox",
        lambda _workspace, _home, tier1, **_kwargs: (
            observed_commands.extend(item["cmd"] for item in tier1)
            or [{
                "cmd": "python -m pytest -q",
                "exit": 0,
                "expect_ok": True,
                "ms": 1,
            }]
        ),
    )

    def fake_run(argv, **_kwargs):
        if "rev-parse" in argv:
            return HEAD
        return ""

    monkeypatch.setattr(verifier, "_run", fake_run)
    from hermes_cli.acceptance_contract import lint_body

    observed = verifier._authoritative_acceptance(
        task,
        {"_event_id": 77, "submitted_at": 1},
        contract_hash=lint_body(task.body)["contract_hash"],
        candidate_sha=HEAD,
        base_sha=BASE,
        base_ref="main",
        kanban_home=home,
        workspace=tmp_path,
        project_root=tmp_path,
    )
    assert observed["review_run_id"] == 42
    assert observed["submission_event_id"] == 77
    assert observed["commands_verified"] == 1
    assert observed_commands == ["python -m pytest -q"]
    assert Path(observed["path"]).parent == evidence_root / task.id
    assert not (Path(observed["path"]).stat().st_mode & 0o222)


def test_authoritative_acceptance_reports_exact_validated_tier1_failure(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    (home / "state" / "tmp").mkdir(parents=True)
    evidence_root = tmp_path / "controller-evidence"
    evidence_root.mkdir(mode=0o700)
    monkeypatch.setattr(verifier, "_ACCEPTANCE_EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(
        "tools.environments.local.delivery_acceptance_candidate",
        lambda **_kwargs: contextlib.nullcontext((tmp_path, home, object())),
    )
    task = _task(tmp_path)
    monkeypatch.setattr(
        verifier,
        "_run",
        lambda argv, **_k: HEAD if "rev-parse" in argv else "",
    )
    monkeypatch.setattr(
        verifier,
        "_run_acceptance_tier1_in_sandbox",
        lambda *_a, **_k: [{
            "cmd": "python -m pytest -q",
            "exit": 1,
            "expect_ok": False,
            "ms": 1,
        }],
    )
    from hermes_cli.acceptance_contract import lint_body

    contract_hash = lint_body(task.body)["contract_hash"]
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._authoritative_acceptance(
            task,
            {"_event_id": 77, "submitted_at": 1},
            contract_hash=contract_hash,
            candidate_sha=HEAD,
            base_sha=BASE,
            base_ref="main",
            kanban_home=home,
            workspace=tmp_path,
            project_root=tmp_path,
        )

    assert rejected.value.code == "acceptance_tier1_failed"
    evidence = rejected.value.evidence
    assert evidence is not None
    assert set(evidence) == {
        "schema", "path", "sha256", "contract_hash", "candidate_sha",
        "review_run_id", "review_profile", "submission_event_id",
        "created_at", "failed_indexes", "observed_exits",
    }
    assert evidence == {
        **evidence,
        "schema": "hermes-acceptance-failure/v1",
        "contract_hash": contract_hash,
        "candidate_sha": HEAD,
        "review_run_id": 42,
        "review_profile": "reviewer",
        "submission_event_id": 77,
        "failed_indexes": [0],
        "observed_exits": [1],
    }
    evidence_path = Path(evidence["path"])
    raw = evidence_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == evidence["sha256"]
    ledger = json.loads(raw)
    assert ledger["verdict"] == "fail"
    assert ledger["results"] == [{
        "cmd": "python -m pytest -q",
        "exit": 1,
        "expect_ok": False,
        "ms": 1,
    }]
    assert not (evidence_path.stat().st_mode & 0o222)


def test_authoritative_acceptance_validates_failure_ledger_checksum_first(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    (home / "state" / "tmp").mkdir(parents=True)
    evidence_root = tmp_path / "controller-evidence"
    evidence_root.mkdir(mode=0o700)
    monkeypatch.setattr(verifier, "_ACCEPTANCE_EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(
        "tools.environments.local.delivery_acceptance_candidate",
        lambda **_kwargs: contextlib.nullcontext((tmp_path, home, object())),
    )
    task = _task(tmp_path)
    monkeypatch.setattr(
        verifier,
        "_run",
        lambda argv, **_k: HEAD if "rev-parse" in argv else "",
    )
    monkeypatch.setattr(
        verifier,
        "_run_acceptance_tier1_in_sandbox",
        lambda *_a, **_k: [{
            "cmd": "python -m pytest -q",
            "exit": 1,
            "expect_ok": False,
            "ms": 1,
        }],
    )
    real_publish = verifier._publish_acceptance_evidence

    def publish_with_inconsistent_raw(task_id, evidence):
        path, raw, digest = real_publish(task_id, evidence)
        return path, raw + b" ", digest

    monkeypatch.setattr(
        verifier, "_publish_acceptance_evidence", publish_with_inconsistent_raw,
    )
    from hermes_cli.acceptance_contract import lint_body

    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._authoritative_acceptance(
            task,
            {"_event_id": 77, "submitted_at": 1},
            contract_hash=lint_body(task.body)["contract_hash"],
            candidate_sha=HEAD,
            base_sha=BASE,
            base_ref="main",
            kanban_home=home,
            workspace=tmp_path,
            project_root=tmp_path,
        )
    assert rejected.value.code == "acceptance_evidence_checksum_mismatch"
    assert rejected.value.evidence is None


def test_authoritative_acceptance_validates_serialized_ledger_identity(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    (home / "state" / "tmp").mkdir(parents=True)
    evidence_root = tmp_path / "controller-evidence"
    evidence_root.mkdir(mode=0o700)
    monkeypatch.setattr(verifier, "_ACCEPTANCE_EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(
        "tools.environments.local.delivery_acceptance_candidate",
        lambda **_kwargs: contextlib.nullcontext((tmp_path, home, object())),
    )
    task = _task(tmp_path)
    monkeypatch.setattr(
        verifier,
        "_run_acceptance_tier1_in_sandbox",
        lambda *_a, **_k: [{
            "cmd": "python -m pytest -q",
            "exit": 1,
            "expect_ok": False,
            "ms": 1,
        }],
    )
    real_publish = verifier._publish_acceptance_evidence

    def publish_with_wrong_serialized_identity(task_id, evidence):
        path, _raw, _digest = real_publish(task_id, evidence)
        changed = {**evidence, "candidate_sha": "f" * 40}
        raw = (json.dumps(changed, indent=2, sort_keys=True) + "\n").encode()
        return path, raw, hashlib.sha256(raw).hexdigest()

    monkeypatch.setattr(
        verifier,
        "_publish_acceptance_evidence",
        publish_with_wrong_serialized_identity,
    )
    from hermes_cli.acceptance_contract import lint_body

    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._authoritative_acceptance(
            task,
            {"_event_id": 77, "submitted_at": 1},
            contract_hash=lint_body(task.body)["contract_hash"],
            candidate_sha=HEAD,
            base_sha=BASE,
            base_ref="main",
            kanban_home=home,
            workspace=tmp_path,
            project_root=tmp_path,
        )
    assert rejected.value.code == "acceptance_evidence_mismatch"
    assert rejected.value.evidence is None


@pytest.mark.parametrize(
    "results",
    [
        [{"cmd": "true", "exit": 0, "expect_ok": True}],
        [{"cmd": "python -m pytest -q", "exit": 1, "expect_ok": True}],
        [{"cmd": "python -m pytest -q", "exit": False, "expect_ok": False}],
        [],
    ],
)
def test_authoritative_acceptance_rejects_noncanonical_results(
    tmp_path, monkeypatch, results,
):
    home = tmp_path / "home"
    (home / "state" / "tmp").mkdir(parents=True)
    evidence_root = tmp_path / "controller-evidence"
    evidence_root.mkdir(mode=0o700)
    monkeypatch.setattr(verifier, "_ACCEPTANCE_EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(
        "tools.environments.local.delivery_acceptance_candidate",
        lambda **_kwargs: contextlib.nullcontext((tmp_path, home, object())),
    )
    task = _task(tmp_path)
    monkeypatch.setattr(
        verifier,
        "_run",
        lambda argv, **_k: HEAD if "rev-parse" in argv else "",
    )
    monkeypatch.setattr(
        verifier,
        "_run_acceptance_tier1_in_sandbox",
        lambda *_a, **_k: results,
    )
    from hermes_cli.acceptance_contract import lint_body

    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._authoritative_acceptance(
            task,
            {"_event_id": 77, "submitted_at": 1},
            contract_hash=lint_body(task.body)["contract_hash"],
            candidate_sha=HEAD,
            base_sha=BASE,
            base_ref="main",
            kanban_home=home,
            workspace=tmp_path,
            project_root=tmp_path,
        )
    assert rejected.value.code == "acceptance_results_mismatch"


def test_acceptance_tier1_uses_shared_hidden_host_no_network_boundary(
    tmp_path, monkeypatch,
):
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    sandbox_home = tmp_path / "sandbox-home"
    calls = []
    def fake_subprocess_run(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(verifier.subprocess, "run", fake_subprocess_run)
    results = verifier._run_acceptance_tier1_in_sandbox(
        workspace,
        sandbox_home,
        [{"cmd": "python -c 'print(1)'", "expect_exit": 0}],
        task_id="t_deadbeef",
        candidate_sha=HEAD,
        base_sha=BASE,
        base_ref="main",
        git_overlay=None,
    )

    systemd_argv, systemd_kwargs = calls[0]
    joined = " ".join(systemd_argv)
    assert "PrivateNetwork=yes" in systemd_argv
    assert "InaccessiblePaths=-/run -/etc -/sys/fs/cgroup" in joined
    assert "TemporaryFileSystem=/var:ro,nodev,nosuid,noexec,mode=0755" in joined
    assert f"BindReadOnlyPaths={workspace}" in systemd_argv
    assert f"ReadWritePaths={workspace}" not in systemd_argv
    assert systemd_argv[-3:] == ["/bin/bash", "-c", "python -c 'print(1)'"]
    assert systemd_kwargs["env"] == {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": os.environ.get("HOME", "/nonexistent-hermes-worker-home"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
    }
    assert "GH_TOKEN" not in joined
    assert "CLOUDSDK_CONFIG" not in joined
    assert results[0]["expect_ok"] is True
    assert calls[1][0][:3] == ["/usr/bin/systemctl", "--user", "stop"]


def _deploy_run(**overrides):
    value = {
        "id": 700,
        "name": "Auto Deploy Production",
        "path": ".github/workflows/auto-deploy-production.yml",
        "workflow_id": 261021157,
        "event": "workflow_run",
        "status": "completed",
        "conclusion": "success",
        "head_sha": DEPLOY_WORKFLOW_SHA,
        "head_branch": "main",
        "repository": {"id": 1182097307, "full_name": REPO},
        "head_repository": {"id": 1182097307, "full_name": REPO},
        "html_url": f"https://github.com/{REPO}/actions/runs/700",
        "run_attempt": 2,
        "created_at": "2026-08-09T20:01:00Z",
    }
    value.update(overrides)
    return value


def _service():
    return {
        "status": {
            "latestReadyRevisionName": "service-00042-abc",
            "traffic": [{"revisionName": "service-00042-abc", "percent": 100}],
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def _revision(*, _annotation_overrides=None, **label_overrides):
    labels = {
        "clauseye-candidate-sha": HEAD,
        "clauseye-merge-sha": MERGE,
        "clauseye-github-run-id": "700",
    }
    labels.update(label_overrides)
    digest = "registry/image@sha256:" + "c" * 64
    annotations = {
        "clauseye.dev/candidate-sha": HEAD,
        "clauseye.dev/merge-sha": MERGE,
        "clauseye.dev/github-run-id": "700",
        "clauseye.dev/github-run-attempt": "2",
        "clauseye.dev/github-workflow-ref": (
            f"{REPO}/.github/workflows/auto-deploy-production.yml"
            "@refs/heads/main"
        ),
        "clauseye.dev/github-workflow-sha": DEPLOY_WORKFLOW_SHA,
        "clauseye.dev/image-digest": "sha256:" + "c" * 64,
        "clauseye.dev/deployment-mode": "promotion",
    }
    annotations.update(_annotation_overrides or {})
    return {
        "metadata": {"labels": labels, "annotations": annotations},
        "spec": {"containers": [{"image": digest}]},
        "status": {
            "imageDigest": digest,
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def _merge_pr_association(**overrides):
    value = {
        "number": 592,
        "state": "closed",
        "merged": None,
        "merged_at": "2026-08-09T20:00:00Z",
        "merge_commit_sha": MERGE,
        "head": {"sha": HEAD},
        "base": {"ref": "main"},
    }
    value.update(overrides)
    return [value]


@pytest.mark.parametrize(
    "association_merged", [None, True], ids=["github-null", "explicit-true"],
)
def test_production_verifier_binds_workflow_revision_labels_and_digest(
    tmp_path, monkeypatch, association_merged,
):
    def fake_json(argv, **_kwargs):
        joined = " ".join(argv)
        if "/actions/runs/700/jobs" in joined:
            return {"jobs": [{
                "id": 9,
                "name": "Cloud Run Smoke Test",
                "conclusion": "success",
                "html_url": "https://github.com/smoke",
            }]}
        if "/actions/runs/700" in joined:
            return _deploy_run()
        if f"/commits/{MERGE}/pulls" in joined:
            return _merge_pr_association(merged=association_merged)
        if "services describe" in joined:
            return _service()
        if "revisions describe" in joined:
            return _revision()
        raise AssertionError(argv)

    monkeypatch.setattr(verifier, "_json", fake_json)
    monkeypatch.setattr(
        verifier,
        "_variables",
        lambda _repo, env, **_kwargs: (
            {"CLOUD_RUN_SERVICE_NAME": "service"}
            if env
            else {"GCP_PROJECT_ID": "project", "GCP_REGION": "region"}
        ),
    )
    monkeypatch.setattr(
        verifier,
        "_protected_control_plane_unchanged",
        lambda *_a, **_k: {"manifest_sha256": "f" * 64},
    )
    observed = verifier._verify_clauseye_production(
        REPO,
        HEAD,
        MERGE,
        {
            "workflow_run_id": 700,
            "workflow_run_url": f"https://github.com/{REPO}/actions/runs/700",
            "revision": "service-00042-abc",
        },
        pr_number=592,
        merged_at="2026-08-09T20:00:00Z",
        trusted_control_sha=WORKFLOW_SHA,
        cwd=tmp_path,
    )
    assert observed["source_sha"] == MERGE
    assert observed["cloud_run"]["workflow_run_id_label"] == "700"
    assert observed["cloud_run"]["workflow_source_sha"] == DEPLOY_WORKFLOW_SHA
    assert observed["cloud_run"]["image_digest"].endswith("c" * 64)


@pytest.mark.parametrize(
    "associations",
    [
        _merge_pr_association(state="open"),
        _merge_pr_association(merged_at=None),
        _merge_pr_association(merged_at="2026-08-09T20:00:01Z"),
        _merge_pr_association(merged_at=20260809),
        _merge_pr_association(merged=False),
        _merge_pr_association(number=591),
        _merge_pr_association(merge_commit_sha=HEAD),
        _merge_pr_association(head={"sha": MERGE}),
        _merge_pr_association(base={"ref": "release"}),
        _merge_pr_association() * 2,
    ],
    ids=[
        "open",
        "merged-at-missing",
        "merged-at-wrong",
        "merged-at-wrong-type",
        "explicitly-not-merged",
        "wrong-pr",
        "wrong-merge",
        "wrong-head",
        "wrong-base",
        "multiple",
    ],
)
def test_production_verifier_rejects_nonexact_merge_association(
    tmp_path, monkeypatch, associations,
):
    def fake_json(argv, **_kwargs):
        joined = " ".join(argv)
        if f"/commits/{MERGE}/pulls" in joined:
            return associations
        if "/actions/runs/700" in joined:
            return _deploy_run()
        raise AssertionError(argv)

    monkeypatch.setattr(verifier, "_json", fake_json)
    monkeypatch.setattr(
        verifier,
        "_protected_control_plane_unchanged",
        lambda *_a, **_k: {"manifest_sha256": "f" * 64},
    )
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._verify_clauseye_production(
            REPO,
            HEAD,
            MERGE,
            {
                "workflow_run_id": 700,
                "workflow_run_url": (
                    f"https://github.com/{REPO}/actions/runs/700"
                ),
                "revision": "service-00042-abc",
            },
            pr_number=592,
            merged_at="2026-08-09T20:00:00Z",
            trusted_control_sha=WORKFLOW_SHA,
            cwd=tmp_path,
        )
    assert rejected.value.code == "deployment_pr_lineage_mismatch"


@pytest.mark.parametrize("merged", [False, None], ids=["false", "missing"])
def test_terminal_requires_canonical_pr_api_merged_true(
    tmp_path, monkeypatch, merged,
):
    _stub_registered_project(monkeypatch)
    merged_pr = _pr(
        state="closed",
        merged=merged,
        merged_at="2026-08-09T20:00:00Z",
        merge_commit_sha=MERGE,
    )
    monkeypatch.setattr(verifier, "_run", _git_run)
    monkeypatch.setattr(verifier, "_pull_request", lambda *_a, **_k: merged_pr)
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier.verify_terminal(
            _task(tmp_path),
            {"deployment_required": False},
            {"head_sha": HEAD, "candidate_ref": BRANCH},
            {
                "pr_url": PR_URL,
                "pr_number": 592,
                "head_sha": HEAD,
                "merge_sha": MERGE,
            },
            kanban_home=tmp_path / "home",
        )
    assert rejected.value.code == "pr_merge_state_mismatch"


def test_terminal_passes_exact_candidate_merge_and_pr_lineage_to_deploy_verifier(
    tmp_path, monkeypatch,
):
    policy_digest = "1" * 64
    boundary_events = []
    _stub_registered_project(monkeypatch)
    merged_pr = _pr(
        state="closed",
        merged=True,
        merged_at="2026-08-09T20:00:00Z",
        merge_commit_sha=MERGE,
    )
    monkeypatch.setattr(verifier, "_run", _git_run)
    monkeypatch.setattr(verifier, "_pull_request", lambda *_a, **_k: merged_pr)
    monkeypatch.setattr(
        verifier,
        "_json",
        lambda argv, **_kwargs: (
            {"behind_by": 0}
            if "/compare/" in " ".join(argv)
            else {}
        ),
    )
    monkeypatch.setattr(
        verifier,
        "_required_ruleset_evidence",
        lambda *_a, **_k: {
            "required_workflow": {"workflow_source_sha": WORKFLOW_SHA},
            "policy_claim_sha256": policy_digest,
        },
    )
    monkeypatch.setattr(
        verifier, "_successful_autonomous_gate", lambda *_a, **_k: {},
    )
    captured = {}

    def fake_deploy(repo, candidate, merge, deployment, **kwargs):
        boundary_events.append("deployment")
        captured.update({
            "repo": repo,
            "candidate": candidate,
            "merge": merge,
            "deployment": deployment,
            **kwargs,
        })
        return {"revision": deployment["revision"]}

    monkeypatch.setattr(verifier, "_verify_clauseye_production", fake_deploy)

    def fake_revalidate(repo, branch, expected):
        boundary_events.append("policy-revalidation")
        assert (repo, branch, expected) == (REPO, "main", policy_digest)
        return {
            "key_id": "trusted-key",
            "policy_claim_sha256": policy_digest,
            "issued_at": "2026-08-09T20:04:00Z",
            "expires_at": "2026-08-09T20:09:00Z",
        }

    monkeypatch.setattr(
        verifier, "_revalidate_terminal_policy", fake_revalidate,
    )
    deployment = {
        "workflow_run_id": 700,
        "workflow_run_url": f"https://github.com/{REPO}/actions/runs/700",
        "revision": "service-00042-abc",
    }
    observed = verifier.verify_terminal(
        _task(tmp_path),
        {
            "deployment_required": True,
            "deployment_verifier": "clauseye-production-probe",
        },
        {"head_sha": HEAD, "candidate_ref": BRANCH},
        {
            "pr_url": PR_URL,
            "pr_number": 592,
            "head_sha": HEAD,
            "merge_sha": MERGE,
            "deployment": deployment,
        },
        kanban_home=tmp_path / "home",
    )
    assert captured == {
        "repo": REPO,
        "candidate": HEAD,
        "merge": MERGE,
        "deployment": deployment,
        "pr_number": 592,
        "merged_at": "2026-08-09T20:00:00Z",
        "trusted_control_sha": WORKFLOW_SHA,
        "cwd": tmp_path,
    }
    assert observed["deployment"] == {"revision": "service-00042-abc"}
    assert boundary_events == ["deployment", "policy-revalidation"]
    assert observed["ruleset"]["policy_revalidated_at"] == (
        "2026-08-09T20:04:00Z"
    )


@pytest.mark.parametrize(
    ("run_overrides", "label_overrides", "code"),
    [
        ({"path": ".github/workflows/other.yml"}, {}, "deployment_run_mismatch"),
        ({"head_branch": "feature"}, {}, "deployment_run_mismatch"),
        ({}, {"clauseye-candidate-sha": MERGE}, "deployment_revision_lineage_mismatch"),
        ({}, {"clauseye-merge-sha": HEAD}, "deployment_revision_lineage_mismatch"),
        ({}, {"clauseye-github-run-id": "699"}, "deployment_revision_lineage_mismatch"),
        (
            {},
            {"_annotation_overrides": {
                "clauseye.dev/github-workflow-sha": HEAD,
            }},
            "deployment_revision_lineage_mismatch",
        ),
    ],
)
def test_production_verifier_rejects_wrong_workflow_or_revision_lineage(
    tmp_path, monkeypatch, run_overrides, label_overrides, code,
):
    def fake_json(argv, **_kwargs):
        joined = " ".join(argv)
        if "/actions/runs/700/jobs" in joined:
            return {"jobs": [{
                "id": 9, "name": "Cloud Run Smoke Test", "conclusion": "success",
            }]}
        if "/actions/runs/700" in joined:
            return _deploy_run(**run_overrides)
        if f"/commits/{MERGE}/pulls" in joined:
            return _merge_pr_association()
        if "services describe" in joined:
            return _service()
        if "revisions describe" in joined:
            return _revision(**label_overrides)
        raise AssertionError(argv)

    monkeypatch.setattr(verifier, "_json", fake_json)
    monkeypatch.setattr(
        verifier,
        "_variables",
        lambda _repo, env, **_kwargs: (
            {"CLOUD_RUN_SERVICE_NAME": "service"}
            if env
            else {"GCP_PROJECT_ID": "project", "GCP_REGION": "region"}
        ),
    )
    monkeypatch.setattr(
        verifier,
        "_protected_control_plane_unchanged",
        lambda *_a, **_k: {"manifest_sha256": "f" * 64},
    )
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._verify_clauseye_production(
            REPO,
            HEAD,
            MERGE,
            {
                "workflow_run_id": 700,
                "workflow_run_url": f"https://github.com/{REPO}/actions/runs/700",
                "revision": "service-00042-abc",
            },
            pr_number=592,
            merged_at="2026-08-09T20:00:00Z",
            trusted_control_sha=WORKFLOW_SHA,
            cwd=tmp_path,
        )
    assert rejected.value.code == code


def test_production_verifier_rejects_changed_deploy_workflow_source(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(verifier, "_json", lambda *_a, **_k: _deploy_run())

    def changed_control(*_args, **_kwargs):
        raise verifier.LiveVerificationError(
            "deploy control plane changed",
            code="protected_control_plane_changed",
        )

    monkeypatch.setattr(
        verifier, "_protected_control_plane_unchanged", changed_control,
    )
    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier._verify_clauseye_production(
            REPO,
            HEAD,
            MERGE,
            {
                "workflow_run_id": 700,
                "workflow_run_url": f"https://github.com/{REPO}/actions/runs/700",
                "revision": "service-00042-abc",
            },
            pr_number=592,
            merged_at="2026-08-09T20:00:00Z",
            trusted_control_sha=WORKFLOW_SHA,
            cwd=tmp_path,
        )
    assert rejected.value.code == "protected_control_plane_changed"


def test_terminal_rejects_acceptance_that_mutates_review_workspace(
    tmp_path, monkeypatch,
):
    _stub_registered_project(monkeypatch)
    dirty = {"value": False}

    def fake_run(argv, **_kwargs):
        joined = " ".join(argv)
        if "remote get-url origin" in joined:
            return f"https://github.com/{REPO}.git\n"
        if "rev-parse HEAD" in joined:
            return HEAD + "\n"
        if "status --porcelain" in joined:
            return " M trusted.py\n" if dirty["value"] else ""
        raise AssertionError(argv)

    merged_pr = _pr(
        state="closed",
        merged=True,
        merged_at="2026-08-09T20:00:00Z",
        merge_commit_sha=MERGE,
    )
    monkeypatch.setattr(verifier, "_run", fake_run)
    monkeypatch.setattr(verifier, "_pull_request", lambda *_a, **_k: merged_pr)
    monkeypatch.setattr(
        verifier,
        "_json",
        lambda argv, **_kwargs: (
            {"behind_by": 0}
            if "/compare/" in " ".join(argv)
            else {}
        ),
    )
    monkeypatch.setattr(
        verifier, "_successful_autonomous_gate", lambda *_a, **_k: {},
    )
    monkeypatch.setattr(
        verifier, "_required_ruleset_evidence", lambda *_a, **_k: {},
    )

    def mutating_acceptance(*_args, **_kwargs):
        dirty["value"] = True
        return {"verdict": "pass"}

    monkeypatch.setattr(verifier, "_authoritative_acceptance", mutating_acceptance)
    from hermes_cli.acceptance_contract import lint_body

    with pytest.raises(verifier.LiveVerificationError) as rejected:
        verifier.verify_terminal(
            _task(tmp_path),
            {
                "contract_hash": lint_body(_contract())["contract_hash"],
                "deployment_required": False,
            },
            {
                "head_sha": HEAD,
                "candidate_ref": BRANCH,
                "_event_id": 77,
                "submitted_at": 1,
            },
            {
                "pr_url": PR_URL,
                "pr_number": 592,
                "head_sha": HEAD,
                "merge_sha": MERGE,
            },
            kanban_home=tmp_path / "home",
        )
    assert rejected.value.code == "review_workspace_mutated_by_acceptance"
