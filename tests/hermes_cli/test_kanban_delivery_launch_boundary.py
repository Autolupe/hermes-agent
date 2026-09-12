"""A valid code-delivery project cannot substitute for a protected launcher."""

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_policy as policy
from tests.hermes_cli import test_kanban_delivery_gate as delivery_fixture
from tests.hermes_cli.test_kanban_delivery_gate import delivery_board as delivery_board


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_code_dispatch_without_required_policy_stops_before_workspace_or_launch(
    delivery_board, tmp_path, monkeypatch, lane,
):
    inspected = []
    resolved = []
    spawned = []
    monkeypatch.setattr(policy, "select_required_policy", lambda: None)
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    # Every read-only project and mirror check would succeed. That still
    # cannot authorize the ordinary worker path without a required policy.
    monkeypatch.setattr(kb, "_trusted_worktree_materialization_preflight",
                        lambda *_a, **_kw: inspected.append("project") or True)
    monkeypatch.setattr(kb, "_validate_materialization_config",
                        lambda *_a, **_kw: inspected.append("config"))
    monkeypatch.setattr(kb, "_trusted_materialization_mirror",
                        lambda *_a, **_kw: inspected.append("mirror") or tmp_path)

    def resolve(*_args, **_kwargs):
        resolved.append(True)
        raise AssertionError("Missing launch policy must stop before workspace setup")

    def spawn(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("Missing launch policy must stop before worker launch")

    monkeypatch.setattr(kb, "_resolve_worktree_workspace", resolve)
    monkeypatch.setattr(kb, "resolve_workspace", resolve)
    monkeypatch.setattr(kb, "_default_spawn", spawn)
    workspace = str(tmp_path / "candidate-worktree")
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="exact code delivery", body=delivery_fixture._contract(),
            assignee="builder", workspace_kind="worktree", workspace_path=workspace,
            branch_name=delivery_fixture.BRANCH,
        )
        if lane == "review":
            claimed = kb.claim_task(conn, task_id, claimer="fixture-builder")
            assert claimed is not None
            assert kb.submit_task_for_review(
                conn, task_id, expected_run_id=claimed.current_run_id,
                pull_request=delivery_fixture._pull_request(), reviewer_assignee="reviewer",
            )
        assert kb.get_task(conn, task_id).status == lane

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)

        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert task.status == "triage" and task.current_run_id is None
        assert task.worker_pid is None and task.claim_lock is None
        assert task.workspace_path == workspace
        assert task.branch_name == delivery_fixture.BRANCH
        assert run.ended_at is not None and run.outcome == "blocked"
        assert run.worker_pid is None
        assert result.spawned == []
        assert result.delivery_triaged == [(task_id, "delivery_control_ineligible")]
        events = [event for event in kb.list_events(conn, task_id)
                  if event.kind == "delivery_control_ineligible"]
        assert len(events) == 1
        assert events[0].run_id == run.id
        assert events[0].payload["code"] == "delivery_control_ineligible"

    assert inspected == []
    assert resolved == []
    assert spawned == []


@pytest.mark.parametrize("selection", ["absent", "error"])
def test_delivery_preflight_denies_missing_policy_before_project_inspection(
    delivery_board, monkeypatch, selection,
):
    inspected = []

    def select():
        if selection == "error":
            raise RuntimeError("fixture policy unavailable")
        return None

    monkeypatch.setattr(policy, "select_required_policy", select)
    monkeypatch.setattr(kb, "_trusted_worktree_materialization_preflight",
                        lambda *_a, **_kw: inspected.append(True) or True)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="exact code delivery", body=delivery_fixture._contract(),
            workspace_kind="worktree", branch_name=delivery_fixture.BRANCH,
        )
        task = kb.get_task(conn, task_id)
        assert kb._task_requires_trusted_delivery_control(task)
        assert kb._delivery_control_worker_preflight_eligible(task) is False
    assert inspected == []
