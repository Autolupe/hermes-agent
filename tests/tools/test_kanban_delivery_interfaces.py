"""Marked delivery tools validate ownership before asking the fixed controller.

The board is a temporary SQLite fixture. Controller requests and native writes
are intercepted; these tests never contact a service or load operator config.
"""

from __future__ import annotations

from contextlib import closing
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agent import delegation_context
from hermes_cli import delivery_control
from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt


LANES = {
    "builder": ("_handle_submit_for_review", "builder_publish"),
    "reviewer": ("_handle_complete", "reviewer_complete"),
}
TASK_ID = "t_0123abcd"
RUN_ID = 41
CLAIM = "dispatcher:fixture-active-claim"
SUMMARY = "The exact local candidate passed its checks."


def _open(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _state(worker):
    with closing(_open(worker.path)) as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("tasks", "task_runs", "delivery_control_operations")
        }


def _change(worker, sql, parameters=()):
    with closing(_open(worker.path)) as conn:
        conn.execute(sql, parameters)
        conn.commit()


@pytest.fixture
def worker(monkeypatch, tmp_path):
    for name in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_SESSION_ID",
        "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_DELIVERY_CONTROL",
        "HERMES_KANBAN_TERMINAL_SANDBOX",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_TASK", TASK_ID)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(RUN_ID))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", CLAIM)
    monkeypatch.setenv("HERMES_KANBAN_DELIVERY_CONTROL", "clauseye-v1")
    monkeypatch.setenv("HERMES_KANBAN_TERMINAL_SANDBOX", "systemd-v1")
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: False)

    fixture = SimpleNamespace(
        path=tmp_path / "board.sqlite", calls=[], mutations=[], boards=[],
        response={"ok": True, "state": "accepted"},
    )
    with closing(_open(fixture.path)) as conn:
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, status TEXT, current_run_id INTEGER,
                claim_lock TEXT, assignee TEXT
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY, task_id TEXT, status TEXT,
                ended_at INTEGER, outcome TEXT
            );
            CREATE TABLE delivery_control_operations (
                id TEXT PRIMARY KEY, task_id TEXT, run_id INTEGER,
                action TEXT, state TEXT, created_at INTEGER
            );
        """)
        conn.execute(
            "INSERT INTO tasks VALUES (?, 'running', ?, ?, 'builder')",
            (TASK_ID, RUN_ID, CLAIM),
        )
        conn.execute(
            "INSERT INTO task_runs VALUES (?, ?, 'running', NULL, NULL)",
            (RUN_ID, TASK_ID),
        )
        conn.commit()

    def connect(board=None):
        fixture.boards.append(board)
        assert board is None
        return kb, _open(fixture.path)

    def row_object(conn, table, key):
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (key,)).fetchone()
        return SimpleNamespace(**dict(row)) if row else None

    monkeypatch.setattr(kt, "_connect", connect)
    monkeypatch.setattr(kb, "get_task", lambda conn, tid: row_object(conn, "tasks", tid))
    monkeypatch.setattr(kb, "get_run", lambda conn, rid: row_object(conn, "task_runs", rid))

    def request(**kwargs):
        fixture.calls.append(kwargs)
        if isinstance(fixture.response, Exception):
            raise fixture.response
        return dict(fixture.response)

    monkeypatch.setattr(delivery_control, "request_worker_action", request)

    def forbidden(name):
        def reject(*args, **kwargs):
            fixture.mutations.append(name)
            raise AssertionError(f"marked delivery called native or provider path: {name}")
        return reject

    for name in (
        "complete_task", "request_review", "submit_for_review", "block_task",
        "heartbeat_worker", "heartbeat_claim", "add_comment",
    ):
        monkeypatch.setattr(kb, name, forbidden(name), raising=False)
    monkeypatch.setattr(kt, "_goal_judge_available", forbidden("goal_judge_available"))
    monkeypatch.setattr(kt, "judge_goal", forbidden("judge_goal"))
    monkeypatch.setattr(kt, "load_config", forbidden("load_config"))
    return fixture


def _call(monkeypatch, lane, **arguments):
    monkeypatch.setenv("HERMES_PROFILE", lane)
    return json.loads(getattr(kt, LANES[lane][0])({"summary": SUMMARY, **arguments}))


def _assert_refused(worker, before, response):
    assert response.get("error"), response
    assert not response.get("ok"), response
    assert worker.calls == []
    assert worker.mutations == []
    assert _state(worker) == before


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("run_id", [None, "", "bad", "0", "-1", "1.0", "٤١", "9" * 100])
def test_bad_worker_run_is_refused_before_controller(worker, monkeypatch, lane, run_id):
    if run_id is None:
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    else:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    before = _state(worker)
    _assert_refused(worker, before, _call(monkeypatch, lane))


@pytest.mark.parametrize("lane", LANES)
def test_worker_cannot_target_a_foreign_task(worker, monkeypatch, lane):
    before = _state(worker)
    _assert_refused(worker, before, _call(monkeypatch, lane, task_id="t_deadbeef"))


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("change", [
    "UPDATE tasks SET current_run_id = 42",
    "UPDATE tasks SET status = 'ready'",
    "UPDATE task_runs SET task_id = 't_deadbeef'",
    "UPDATE task_runs SET status = 'ended'",
    "UPDATE task_runs SET ended_at = 1",
    "UPDATE task_runs SET outcome = 'completed'",
    "DELETE FROM task_runs",
])
def test_replaced_or_closed_run_cannot_reach_controller(worker, monkeypatch, lane, change):
    _change(worker, change)
    before = _state(worker)
    _assert_refused(worker, before, _call(monkeypatch, lane))


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("claim", [None, ""])
def test_missing_claim_is_refused(worker, monkeypatch, lane, claim):
    if claim is None:
        monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK")
    else:
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claim)
    before = _state(worker)
    _assert_refused(worker, before, _call(monkeypatch, lane))


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("context_name", ["delegated_child_context", "non_dispatcher_owned_context"])
def test_inherited_worker_context_cannot_reach_controller(worker, monkeypatch, lane, context_name):
    before = _state(worker)
    with getattr(delegation_context, context_name)():
        response = _call(monkeypatch, lane, task_id=TASK_ID)
    _assert_refused(worker, before, response)


@pytest.mark.parametrize("lane", LANES)
def test_summary_routes_only_fixed_action_and_validated_identity(worker, monkeypatch, lane):
    before = _state(worker)
    result = _call(monkeypatch, lane)
    assert result == worker.response
    assert worker.calls == [{
        "action": LANES[lane][1], "task_id": TASK_ID,
        "run_id": RUN_ID, "claim_lock": CLAIM, "summary": SUMMARY,
    }]
    assert worker.boards == [None]
    assert worker.mutations == []
    assert _state(worker) == before


@pytest.mark.parametrize("lane,field", [
    ("builder", "pull_request"), ("builder", "metadata"), ("builder", "board"),
    ("reviewer", "delivery"), ("reviewer", "metadata"),
    ("reviewer", "created_cards"), ("reviewer", "artifacts"), ("reviewer", "board"),
])
def test_worker_cannot_supply_trusted_evidence(worker, monkeypatch, lane, field):
    before = _state(worker)
    value = [] if field in {"created_cards", "artifacts"} else {}
    if field == "board":
        value = "fixture"
    _assert_refused(worker, before, _call(monkeypatch, lane, **{field: value}))


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("summary", ["", "   "])
def test_empty_summary_never_reaches_controller(worker, monkeypatch, lane, summary):
    before = _state(worker)
    _assert_refused(worker, before, _call(monkeypatch, lane, summary=summary))


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("response_kind", ["pending_response", "pending_error", "rejected_response", "rejected_error"])
def test_controller_pending_or_error_keeps_native_state_in_flight(worker, monkeypatch, lane, response_kind):
    pending = response_kind.startswith("pending")
    if response_kind.endswith("error"):
        worker.response = delivery_control.DeliveryControlError(
            "fixture_pending" if pending else "fixture_rejected",
            "The controller has not finished.", pending=pending,
        )
    else:
        worker.response = {
            "ok": False, "state": "pending" if pending else "rejected",
            "code": "fixture_pending" if pending else "fixture_rejected",
            "message": "The controller has not finished.",
        }
    before = _state(worker)
    result = _call(monkeypatch, lane)
    if pending:
        assert result.get("ok") is True
        assert result.get("state") == "pending"
        assert result.get("retryable") is True
        assert result.get("next_action") == "kanban_heartbeat_then_retry"
        assert result.get("task_id") == TASK_ID
        assert result.get("run_id") == RUN_ID
    else:
        assert result.get("error"), result
        assert not result.get("ok")
    assert len(worker.calls) == 1
    assert worker.mutations == []
    assert _state(worker) == before


def _shipping(worker, *, lane, task_id=TASK_ID, run_id=RUN_ID, action=None):
    _change(worker, "UPDATE tasks SET status = 'shipping'")
    _change(
        worker,
        "INSERT INTO delivery_control_operations VALUES (?, ?, ?, ?, 'in_progress', 1)",
        ("fixture-operation", task_id, run_id, action or LANES[lane][1]),
    )


@pytest.mark.parametrize("lane", LANES)
def test_shipping_retry_requires_same_open_run_and_matching_operation(worker, monkeypatch, lane):
    _shipping(worker, lane=lane)
    before = _state(worker)
    assert _call(monkeypatch, lane) == worker.response
    assert len(worker.calls) == 1
    assert worker.calls[0]["action"] == LANES[lane][1]
    assert worker.calls[0]["run_id"] == RUN_ID
    assert worker.mutations == []
    assert _state(worker) == before


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("mismatch", ["missing", "task", "run", "action"])
def test_shipping_without_exact_operation_is_refused(worker, monkeypatch, lane, mismatch):
    _shipping(
        worker, lane=lane,
        task_id="t_deadbeef" if mismatch == "task" else TASK_ID,
        run_id=RUN_ID + 1 if mismatch == "run" else RUN_ID,
        action="different_action" if mismatch == "action" else None,
    )
    if mismatch == "missing":
        _change(worker, "DELETE FROM delivery_control_operations")
    before = _state(worker)
    _assert_refused(worker, before, _call(monkeypatch, lane))


def test_ordinary_worker_identity_still_rejects_shipping(worker):
    _shipping(worker, lane="builder")
    with closing(_open(worker.path)) as conn:
        with pytest.raises(ValueError, match="running"):
            kt._worker_run_id(TASK_ID, kb=kb, conn=conn)


@pytest.mark.parametrize("lane,override_name,allowed", [
    ("builder", "_submit_for_review_schema_overrides", {"task_id", "summary"}),
    ("reviewer", "_complete_schema_overrides", {"task_id", "summary", "result"}),
])
def test_marked_schema_hides_caller_supplied_evidence(worker, monkeypatch, lane, override_name, allowed):
    monkeypatch.setenv("HERMES_PROFILE", lane)
    schema = getattr(kt, override_name)()
    properties = schema["parameters"]["properties"]
    assert set(properties) == allowed
    assert {"pull_request", "delivery", "metadata", "created_cards", "artifacts", "socket"}.isdisjoint(properties)
    monkeypatch.delenv("HERMES_KANBAN_DELIVERY_CONTROL")
    assert getattr(kt, override_name)() == {}


def test_generic_systemd_worker_has_no_delivery_mode(worker, monkeypatch):
    assert kt._check_kanban_delivery_mode() is True
    monkeypatch.delenv("HERMES_KANBAN_DELIVERY_CONTROL")
    assert kt._check_kanban_delivery_mode() is False


@pytest.mark.parametrize("context_name", ["delegated_child_context", "non_dispatcher_owned_context"])
def test_inherited_marked_context_has_no_delivery_mode(worker, context_name):
    with getattr(delegation_context, context_name)():
        assert kt._check_kanban_delivery_mode() is False


def _heartbeat(monkeypatch, surface):
    monkeypatch.setattr(kt, "_auto_heartbeat_last_attempt", 0.0)
    monkeypatch.setattr(kt, "_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS", 0)
    if surface == "auto":
        return kt.heartbeat_current_worker_from_env()
    return json.loads(kt._handle_heartbeat({"note": "Delivery is still being checked."}))


@pytest.mark.parametrize("surface", ["explicit", "auto"])
def test_marked_shipping_heartbeat_forwards_exact_run_and_claim(worker, monkeypatch, surface):
    _shipping(worker, lane="builder")
    heartbeats = []

    def heartbeat_worker(conn, task_id, **kwargs):
        assert kb.get_task(conn, task_id).status == "shipping"
        operation = kb._active_delivery_operation(conn, task_id, kwargs["expected_run_id"])
        assert operation is not None and operation["action"] == "builder_publish"
        heartbeats.append((task_id, kwargs))
        return True

    monkeypatch.setattr(kb, "heartbeat_worker", heartbeat_worker)
    before = _state(worker)
    result = _heartbeat(monkeypatch, surface)
    if surface == "auto":
        assert result is True
    else:
        assert result.get("ok") is True
        assert result.get("task_id") == TASK_ID
    assert heartbeats == [(TASK_ID, {
        "note": None if surface == "auto" else "Delivery is still being checked.",
        "expected_run_id": RUN_ID, "extend_claim": True, "claimer": CLAIM,
    })]
    assert worker.calls == []
    assert worker.mutations == []
    assert _state(worker) == before


@pytest.mark.parametrize("surface", ["explicit", "auto"])
@pytest.mark.parametrize("invalid", ["missing_operation", "stale_run", "unmarked"])
def test_shipping_heartbeat_refuses_unowned_or_unmarked_run(worker, monkeypatch, surface, invalid):
    _shipping(worker, lane="builder")
    if invalid == "missing_operation":
        _change(worker, "DELETE FROM delivery_control_operations")
    elif invalid == "stale_run":
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(RUN_ID + 1))
    else:
        monkeypatch.delenv("HERMES_KANBAN_DELIVERY_CONTROL")
    heartbeats = []
    monkeypatch.setattr(
        kb, "heartbeat_worker",
        lambda *args, **kwargs: heartbeats.append((args, kwargs)) or True,
    )
    before = _state(worker)
    result = _heartbeat(monkeypatch, surface)
    if surface == "auto":
        assert result is False
    else:
        assert result.get("error"), result
        assert not result.get("ok")
    assert heartbeats == []
    assert worker.calls == []
    assert worker.mutations == []
    assert _state(worker) == before
