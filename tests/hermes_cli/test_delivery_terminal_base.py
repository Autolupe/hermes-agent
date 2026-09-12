"""Terminal acceptance uses the immutable base already proved by the merge gate."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import delivery_verifier as verifier


@pytest.mark.parametrize("reported_base", ["d" * 40, "f" * 40])
def test_terminal_acceptance_binds_verified_merge_parent(tmp_path, monkeypatch, reported_base):
    repo = "clauseye-com/clauseye-contra-rope"
    head_sha, merge_sha, merge_parent = "a" * 40, "b" * 40, "d" * 40
    branch = "hermes/t_deadbeef-fix"
    task = SimpleNamespace(
        id="t_deadbeef", workspace_path=str(tmp_path), current_run_id=42,
        branch_name=branch, assignee="reviewer",
    )
    repository = {
        "id": 1182097307, "full_name": repo, "default_branch": "main",
    }
    pull_request = {
        "state": "closed", "merged": True, "merged_at": "2026-08-09T20:00:00Z",
        "merge_commit_sha": merge_sha,
        "head": {"sha": head_sha, "ref": branch, "repo": repository},
        "base": {"sha": reported_base, "ref": "main", "repo": repository},
    }

    def git_read(argv, **_kwargs):
        command = argv[3:]
        if command == ["remote", "get-url", "origin"]:
            return f"https://github.com/{repo}.git\n"
        if command == ["rev-parse", "HEAD"]:
            return head_sha + "\n"
        if command == ["status", "--porcelain"]:
            return ""
        pytest.fail(f"unexpected process request: {argv}")

    def github_read(argv, **_kwargs):
        assert argv[-1] == f"repos/{repo}/compare/{merge_sha}...main"
        return {"behind_by": 0}

    monkeypatch.setattr(verifier, "_run", git_read)
    monkeypatch.setattr(verifier, "_json", github_read)
    monkeypatch.setattr(verifier, "_pull_request", lambda *_a, **_kw: pull_request)
    monkeypatch.setattr(
        verifier, "_require_registered_clauseye_project",
        lambda *_args: Path(tmp_path),
    )
    monkeypatch.setattr(verifier, "_required_ruleset_evidence", lambda *_a, **_kw: {})
    monkeypatch.setattr(
        verifier, "_successful_autonomous_gate",
        lambda *_args, **_kwargs: {"base_sha": merge_parent},
    )
    monkeypatch.setattr(verifier, "_revalidate_terminal_policy", lambda *_args: {
        "issued_at": "fixture", "expires_at": "fixture", "key_id": "fixture",
    })
    acceptance_inputs = []

    def record_acceptance(*_args, **kwargs):
        acceptance_inputs.append((kwargs["candidate_sha"], kwargs["base_sha"], kwargs["base_ref"]))
        return {"verdict": "pass"}

    monkeypatch.setattr(verifier, "_authoritative_acceptance", record_acceptance)
    observed = verifier.verify_terminal(
        task, {"contract_hash": "c" * 64, "deployment_required": False},
        {"head_sha": head_sha, "candidate_ref": branch},
        {
            "pr_url": f"https://github.com/{repo}/pull/17", "pr_number": 17,
            "head_sha": head_sha, "merge_sha": merge_sha,
        },
        kanban_home=tmp_path / "home",
    )
    assert observed["acceptance"]["verdict"] == "pass"
    assert acceptance_inputs == [(head_sha, merge_parent, "main")]
