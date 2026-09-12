"""Dashboard writes preserve held workers and trusted delivery ownership."""

from __future__ import annotations

from contextlib import closing, contextmanager
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import delivery_verifier
from hermes_cli import kanban_db as kb


PREFIX = "/api/plugins/kanban"
FENCES = ("held", "delivery_running", "delivery_shipping", "shipping_without_operation")
TABLES = (
    "tasks", "task_runs", "task_events", "task_comments", "task_links",
    "task_attachments", "delivery_control_operations", "kanban_notify_subs",
)
CODE_CONTRACT = """```acceptance-contract
domain: coding
target: github-merge
tier1:
  - cmd: "python -m pytest -q"
    expect_exit: 0
tier2:
  - "The exact candidate was independently reviewed."
tier3: "The requested change is merged."
```"""


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for name in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_DELIVERY_CONTROL", "HERMES_KANBAN_TERMINAL_SANDBOX",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    fixture = SimpleNamespace(home=home, updates=[], forbidden=[], terminations=[])

    def tripwire(name):
        def reject(*_args, **_kwargs):
            fixture.forbidden.append(name)
            raise AssertionError(f"dashboard crossed protected boundary: {name}")
        return reject

    def terminate(pid, claim_lock):
        fixture.terminations.append((pid, claim_lock))
        if pid is not None:
            tripwire("worker termination")(pid, claim_lock)

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", terminate)
    monkeypatch.setattr(delivery_verifier, "verify_terminal", tripwire("terminal verifier"))
    monkeypatch.setattr(delivery_verifier, "verify_submission", tripwire("submission verifier"))
    for name in ("_cleanup_workspace", "_unlock_task_worktree", "_cleanup_worker_tmux"):
        monkeypatch.setattr(kb, name, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        kb, "notify_task_updated",
        lambda _conn, tid, fields, **_kw: fixture.updates.append((tid, tuple(fields))),
    )
    source = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("dashboard_delivery_fences_fixture", source)
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)
    spec.loader.exec_module(plugin)
    app = FastAPI()
    app.include_router(plugin.router, prefix=PREFIX)
    fixture.plugin = plugin
    with TestClient(app) as client:
        fixture.client = client
        yield fixture
    assert fixture.forbidden == []


def _snapshot(task_id=None, *, conn=None):
    if conn is None:
        with closing(kb.connect()) as opened:
            return _snapshot(task_id, conn=opened)
    result = {}
    for table in TABLES:
        condition, args = "", ()
        if task_id is not None:
            if table == "tasks":
                condition, args = " WHERE id = ?", (task_id,)
            elif table == "task_links":
                condition, args = " WHERE parent_id = ? OR child_id = ?", (task_id, task_id)
            else:
                condition, args = " WHERE task_id = ?", (task_id,)
        result[table] = [tuple(row) for row in conn.execute(
            f"SELECT * FROM {table}{condition} ORDER BY rowid", args,
        )]
    return result


def _task(dashboard, *, claim=True, code=False):
    kwargs = {}
    if code:
        kwargs = {
            "body": CODE_CONTRACT, "workspace_kind": "worktree",
            "workspace_path": str(dashboard.home / "fixture-worktree"),
            "branch_name": "hermes/dashboard-fixture",
        }
    with closing(kb.connect()) as conn:
        tid = kb.create_task(conn, title="Original title", assignee="builder", **kwargs)
        if kb.get_task(conn, tid).status == "triage":
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            conn.commit()
        if claim:
            task = kb.claim_task(conn, tid, claimer="dashboard-fixture-claim", ttl_seconds=600)
            assert task is not None and task.current_run_id is not None
        else:
            task = kb.get_task(conn, tid)
    dashboard.updates.clear()
    return task


def _protect(task, fence, *, conn=None):
    if conn is None:
        with closing(kb.connect()) as opened:
            return _protect(task, fence, conn=opened)
    if fence == "held":
        kb._append_event(
            conn, task.id, "controlled_worker_held",
            {"request_id": "unresolved-dashboard-fixture"}, run_id=task.current_run_id,
        )
    elif fence != "shipping_without_operation":
        conn.execute("""
            INSERT INTO delivery_control_operations (
                op_id, task_id, run_id, action, request_sha256, claim_sha256,
                summary, candidate_head, peer_uid, owner_instance, state,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'builder_publish', ?, ?, ?, ?, 1000, ?, 'in_progress', 1, 1)
        """, (
            "fixture-op-" + task.id, task.id, task.current_run_id,
            "a" * 64, "b" * 64, "Pending fixture delivery", "c" * 40, "fixture-controller",
        ))
    if fence in {"delivery_shipping", "shipping_without_operation"}:
        conn.execute("UPDATE tasks SET status = 'shipping' WHERE id = ?", (task.id,))
    conn.commit()


def _assert_unchanged(dashboard, before, *, task_id=None):
    assert _snapshot(task_id) == before
    assert dashboard.updates == []
    assert dashboard.forbidden == []
    assert dashboard.terminations == []


@pytest.mark.parametrize("helper", ["_set_status_direct", "_set_status_direct_guarded"])
@pytest.mark.parametrize("target", ["todo", "triage", "ready", "done"])
@pytest.mark.parametrize("fence", FENCES)
def test_direct_status_preserves_protected_task(dashboard, helper, target, fence):
    task = _task(dashboard)
    _protect(task, fence)
    before = _snapshot()
    with closing(kb.connect()) as conn:
        assert getattr(dashboard.plugin, helper)(conn, task.id, target) is False
    _assert_unchanged(dashboard, before)


@pytest.mark.parametrize("fence", FENCES)
@pytest.mark.parametrize("payload", [
    {"status": "todo"}, {"title": "Edited title"}, {"body": "Edited body"},
    {"priority": 9}, {"model_override": "fixture-model"},
    {"reasoning_effort": "high"}, {"assignee": "reviewer"},
    {"status": "done", "summary": "Caller claims completion"},
    {"status": "triage", "assignee": "reviewer", "priority": 9,
     "title": "Edited title", "body": "Edited body", "model_override": "fixture-model",
     "reasoning_effort": "high"},
])
def test_single_patch_refuses_protected_fields_before_any_mutation(dashboard, fence, payload):
    task = _task(dashboard)
    _protect(task, fence)
    before = _snapshot()
    response = dashboard.client.patch(f"{PREFIX}/tasks/{task.id}", json=payload)
    assert response.status_code == 409, response.text
    _assert_unchanged(dashboard, before)


@pytest.mark.parametrize("fence", FENCES)
@pytest.mark.parametrize("payload", [
    {"priority": 7}, {"status": "ready"}, {"archive": True},
    {"status": "triage", "assignee": "reviewer", "priority": 7},
])
def test_bulk_refuses_protected_item_but_updates_eligible_sibling(dashboard, fence, payload):
    protected = _task(dashboard)
    sibling = _task(dashboard, claim=False)
    _protect(protected, fence)
    before = _snapshot(protected.id)
    response = dashboard.client.post(
        f"{PREFIX}/tasks/bulk", json={"ids": [protected.id, sibling.id], **payload},
    )
    assert response.status_code == 200, response.text
    results = {entry["id"]: entry for entry in response.json()["results"]}
    assert results[protected.id]["ok"] is False
    assert results[protected.id].get("error")
    assert results[sibling.id]["ok"] is True
    assert _snapshot(protected.id) == before
    assert all(tid != protected.id for tid, _fields in dashboard.updates)
    with closing(kb.connect()) as conn:
        updated = kb.get_task(conn, sibling.id)
    for key, value in payload.items():
        if key == "archive":
            assert updated.status == "archived"
        else:
            assert getattr(updated, key) == value


@pytest.mark.parametrize("fence", FENCES)
def test_delete_returns_conflict_without_losing_protected_evidence(dashboard, fence):
    task = _task(dashboard)
    _protect(task, fence)
    before = _snapshot()
    response = dashboard.client.delete(f"{PREFIX}/tasks/{task.id}")
    assert response.status_code == 409, response.text
    _assert_unchanged(dashboard, before)


@pytest.mark.parametrize("payload,fields", [
    ({"title": "Edited title"}, ("title",)),
    ({"body": "Edited body"}, ("body",)),
    ({"title": "Edited title", "body": "Edited body"}, ("title", "body")),
    ({"priority": 8}, ("priority",)),
])
def test_successful_edit_emits_one_notification_with_exact_fields(dashboard, payload, fields):
    task = _task(dashboard, claim=False)
    response = dashboard.client.patch(f"{PREFIX}/tasks/{task.id}", json=payload)
    assert response.status_code == 200, response.text
    assert dashboard.updates == [(task.id, fields)]
    for key, value in payload.items():
        assert response.json()["task"][key] == value


@pytest.mark.parametrize("helper", ["_set_status_direct", "_set_status_direct_guarded"])
def test_code_task_cannot_finish_through_direct_sql(dashboard, helper):
    task = _task(dashboard, code=True)
    before = _snapshot(task.id)
    with closing(kb.connect()) as conn:
        assert getattr(dashboard.plugin, helper)(conn, task.id, "done") is False
        assert kb.get_task(conn, task.id).status == "running"
        assert kb.get_run(conn, task.current_run_id).ended_at is None
        assert not any(e.kind in {"completed", "status"} for e in kb.list_events(conn, task.id))
    after = _snapshot(task.id)
    assert after["tasks"] == before["tasks"]
    assert after["task_runs"] == before["task_runs"]


def test_code_patch_done_still_requires_native_delivery_verification(dashboard, monkeypatch):
    task = _task(dashboard, code=True)
    native_complete = kb.complete_task
    calls = []

    def complete(conn, tid, **kwargs):
        calls.append((tid, kwargs))
        return native_complete(conn, tid, **kwargs)

    monkeypatch.setattr(kb, "complete_task", complete)
    before = _snapshot(task.id)
    response = dashboard.client.patch(
        f"{PREFIX}/tasks/{task.id}",
        json={"status": "done", "summary": "A summary is not delivery proof."},
    )
    assert response.status_code == 409, response.text
    assert len(calls) == 1 and calls[0][0] == task.id
    after = _snapshot(task.id)
    assert after["tasks"] == before["tasks"]
    assert after["task_runs"] == before["task_runs"]
    with closing(kb.connect()) as conn:
        assert kb.get_task(conn, task.id).status == "running"
        assert not any(e.kind in {"completed", "status"} for e in kb.list_events(conn, task.id))


@pytest.mark.parametrize("helper", ["_set_status_direct", "_set_status_direct_guarded"])
@pytest.mark.parametrize("fence", ["held", "delivery_running"])
def test_direct_write_rechecks_fence_after_initial_read(dashboard, monkeypatch, helper, fence):
    task = _task(dashboard)
    native_txn = kb.write_txn
    captured = []

    @contextmanager
    def hold_before_lock(conn, **kwargs):
        if not captured:
            _protect(task, fence, conn=conn)
            captured.append(_snapshot(conn=conn))
        with native_txn(conn, **kwargs):
            yield conn

    monkeypatch.setattr(kb, "write_txn", hold_before_lock)
    with closing(kb.connect()) as conn:
        assert getattr(dashboard.plugin, helper)(conn, task.id, "ready") is False
    assert len(captured) == 1
    _assert_unchanged(dashboard, captured[0])


@pytest.mark.parametrize("target", ["todo", "triage", "ready"])
@pytest.mark.parametrize("damage", ["ended", "outcome", "foreign_run", "missing_pointer_with_pid"])
def test_direct_write_preserves_uncertain_running_identity(dashboard, target, damage):
    task = _task(dashboard)
    with closing(kb.connect()) as conn:
        if damage == "ended":
            conn.execute("UPDATE task_runs SET ended_at = 1 WHERE id = ?", (task.current_run_id,))
        elif damage == "outcome":
            conn.execute("UPDATE task_runs SET outcome = 'completed' WHERE id = ?", (task.current_run_id,))
        elif damage == "foreign_run":
            conn.execute("UPDATE task_runs SET task_id = 't_deadbeef' WHERE id = ?", (task.current_run_id,))
        else:
            conn.execute("UPDATE tasks SET current_run_id = NULL, worker_pid = 991234 WHERE id = ?", (task.id,))
        conn.commit()
        before = _snapshot(conn=conn)
        assert dashboard.plugin._set_status_direct_guarded(conn, task.id, target) is False
    _assert_unchanged(dashboard, before)


@pytest.mark.parametrize("target", ["todo", "triage", "ready"])
def test_pid_free_historical_untracked_task_can_still_move(dashboard, target):
    task = _task(dashboard, claim=False)
    with closing(kb.connect()) as conn:
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task.id,))
        conn.commit()
        assert dashboard.plugin._set_status_direct_guarded(conn, task.id, target) is True
        assert kb.get_task(conn, task.id).status == target


@pytest.mark.parametrize("fence", FENCES)
@pytest.mark.parametrize("action", ["reclaim", "reassign", "terminate"])
def test_recovery_routes_preserve_protected_task(dashboard, fence, action):
    task = _task(dashboard)
    _protect(task, fence)
    before = _snapshot()
    path = f"/runs/{task.current_run_id}/terminate" if action == "terminate" else f"/tasks/{task.id}/{action}"
    payload = {"profile": "reviewer", "reclaim_first": True} if action == "reassign" else {"reason": "Fixture recovery"}
    response = dashboard.client.post(PREFIX + path, json=payload)
    assert response.status_code == 409, response.text
    _assert_unchanged(dashboard, before)


@pytest.mark.parametrize("fence", ["held", "delivery_shipping"])
@pytest.mark.parametrize("method", ["post", "delete"])
@pytest.mark.parametrize("protected_end", ["parent", "child"])
def test_link_routes_guard_both_endpoints(dashboard, fence, method, protected_end):
    task = _task(dashboard)
    sibling = _task(dashboard, claim=False)
    _protect(task, fence)
    parent, child = (task.id, sibling.id) if protected_end == "parent" else (sibling.id, task.id)
    if method == "delete":
        with closing(kb.connect()) as conn:
            conn.execute("INSERT INTO task_links VALUES (?, ?)", (parent, child))
            conn.commit()
    before = _snapshot()
    payload = {"parent_id": parent, "child_id": child}
    if method == "post":
        response = dashboard.client.post(f"{PREFIX}/links", json=payload)
    else:
        response = dashboard.client.delete(f"{PREFIX}/links", params=payload)
    assert response.status_code == 409, response.text
    _assert_unchanged(dashboard, before)


@pytest.mark.parametrize("current", [False, True])
def test_run_termination_requires_current_attempt_and_forwards_exact_run(dashboard, monkeypatch, current):
    task = _task(dashboard)
    if not current:
        with closing(kb.connect()) as conn:
            conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (task.current_run_id + 1, task.id))
            conn.commit()
    calls = []
    monkeypatch.setattr(kb, "reclaim_task", lambda conn, tid, **kwargs: calls.append((tid, kwargs)) or True)
    before = _snapshot()
    response = dashboard.client.post(
        f"{PREFIX}/runs/{task.current_run_id}/terminate", json={"reason": "Fixture termination"},
    )
    if current:
        assert response.status_code == 200, response.text
        assert calls == [(task.id, {"reason": "Fixture termination", "expected_run_id": task.current_run_id})]
    else:
        assert response.status_code == 409, response.text
        assert calls == []
    _assert_unchanged(dashboard, before)
