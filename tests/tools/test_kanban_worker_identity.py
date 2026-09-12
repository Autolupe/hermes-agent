"""Worker lifecycle identity must fail before judging or touching a real claim."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt


LIFECYCLE = {
    "complete": {"summary": "Verified local result"},
    "block": {"reason": "An external answer is needed", "kind": "needs_input"},
    "request_review": {"summary": "Implementation and checks are ready"},
    "request_changes": {"reason": "Add the missing boundary check"},
    "heartbeat": {"note": "Still working"},
    "auto_heartbeat": {},
}


@pytest.fixture
def worker(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_SESSION_ID",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "builder")
    monkeypatch.setattr(kt, "_auto_heartbeat_last_attempt", 0.0)
    kb._INITIALIZED_PATHS.clear()
    with closing(kb.connect()) as conn:
        tid = kb.create_task(conn, title="Lifecycle identity", assignee="builder")
        task = kb.claim_task(conn, tid, claimer="dispatcher:123")
        assert task is not None and task.current_run_id is not None
        # Make an unintended claim extension observable without sleeping.
        conn.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (tid,))
        conn.execute("UPDATE task_runs SET claim_expires=1 WHERE id=?", (task.current_run_id,))
        conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock)
    return task


def _prepare_surface(worker, monkeypatch, surface):
    if surface != "request_changes":
        return worker
    with closing(kb.connect()) as conn:
        assert kb.request_review(conn, worker.id, summary="Ready", expected_run_id=worker.current_run_id)
        review = kb.claim_review_task(conn, worker.id, claimer="dispatcher:123")
        assert review is not None
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", review.claim_lock)
    return review


def _state():
    with closing(kb.connect()) as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("tasks", "task_runs", "task_events", "task_comments", "task_attachments")
        }


def _call(surface, **extra):
    if surface == "auto_heartbeat":
        return kt.heartbeat_current_worker_from_env()
    return json.loads(getattr(kt, f"_handle_{surface}")({**LIFECYCLE[surface], **extra}))


def _assert_refused(surface, result):
    if surface == "auto_heartbeat":
        assert result is False
    else:
        assert isinstance(result.get("error"), str), result
        assert not result.get("ok")


def _judge_tripwire(monkeypatch):
    calls = []
    monkeypatch.setattr(kt, "_goal_judge_available", lambda: calls.append("availability") or True)
    monkeypatch.setattr(kt, "judge_goal", lambda **kw: calls.append("judge") or ("done", "", False, None, False))
    with closing(kb.connect()) as conn:
        conn.execute("UPDATE tasks SET goal_mode=1")
        conn.commit()
    return calls


@pytest.mark.parametrize("surface", LIFECYCLE)
@pytest.mark.parametrize("run_id", [None, "", "invalid", "0", "-1", "1.0", "1e3", "9" * 100], ids=["unset_id", "empty", "text", "zero", "negative", "fraction", "exponent", "oversized"])
def test_invalid_identity_has_no_database_or_judge_effect(worker, monkeypatch, surface, run_id):
    worker = _prepare_surface(worker, monkeypatch, surface)
    if run_id is None:
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    else:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    judge_calls = _judge_tripwire(monkeypatch)
    before = _state()
    result = _call(surface)
    _assert_refused(surface, result)
    assert _state() == before
    assert judge_calls == []


def _new_attempt(tid):
    with closing(kb.connect()) as conn:
        assert kb.block_task(conn, tid, reason="Retry fixture", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        claim = (
            kb.claim_review_task
            if kb.get_task(conn, tid).status == "review"
            else kb.claim_task
        )
        task = claim(conn, tid, claimer="dispatcher:123")
        assert task is not None
        conn.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (tid,))
        conn.execute("UPDATE task_runs SET claim_expires=1 WHERE id=?", (task.current_run_id,))
        conn.commit()
        return task


@pytest.mark.parametrize("surface", LIFECYCLE)
def test_stale_run_cannot_touch_a_new_attempt(worker, monkeypatch, surface):
    worker = _prepare_surface(worker, monkeypatch, surface)
    newer = _new_attempt(worker.id)
    assert newer.current_run_id != worker.current_run_id
    # The dispatcher process/claim string can be reused; the run cannot.
    assert newer.claim_lock == worker.claim_lock
    judge_calls = _judge_tripwire(monkeypatch)
    before = _state()
    _assert_refused(surface, _call(surface))
    assert _state() == before
    assert judge_calls == []


@pytest.mark.parametrize("surface", [name for name in LIFECYCLE if name != "auto_heartbeat"])
def test_worker_cannot_target_a_foreign_task(worker, surface):
    with closing(kb.connect()) as conn:
        tid = kb.create_task(conn, title="Foreign task", assignee="builder")
        assert kb.claim_task(conn, tid, claimer=worker.claim_lock)
    before = _state()
    _assert_refused(surface, _call(surface, task_id=tid))
    assert _state() == before


@pytest.mark.parametrize("surface", ["complete", "block", "request_review", "request_changes", "heartbeat", "auto_heartbeat"])
def test_current_run_can_use_lifecycle_tools(worker, monkeypatch, surface):
    worker = _prepare_surface(worker, monkeypatch, surface)
    result = _call(surface)
    if surface == "auto_heartbeat":
        assert result is True
    else:
        assert result.get("ok") is True, result
    with closing(kb.connect()) as conn:
        task = kb.get_task(conn, worker.id)
        assert task.status == {
            "complete": "done", "block": "blocked", "request_review": "review",
            "request_changes": "ready", "heartbeat": "running", "auto_heartbeat": "running",
        }[surface]
        if surface in {"heartbeat", "auto_heartbeat"}:
            run = kb.get_run(conn, worker.current_run_id)
            assert task.claim_expires > 1
            assert task.claim_expires == run.claim_expires
            assert task.last_heartbeat_at == run.last_heartbeat_at
            assert kb.list_events(conn, worker.id)[-1].kind == "heartbeat"


@pytest.mark.parametrize("surface", ["complete", "block", "heartbeat"])
def test_orchestrator_without_task_identity_keeps_existing_behavior(worker, monkeypatch, surface):
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    assert _call(surface, task_id=worker.id).get("ok") is True


@pytest.mark.parametrize("context_name", ["delegated_child_context", "non_dispatcher_owned_context"])
def test_automatic_heartbeat_ignores_inherited_worker_environment(worker, context_name):
    from agent import delegation_context

    before = _state()
    with getattr(delegation_context, context_name)():
        assert kt.heartbeat_current_worker_from_env() is False
    assert _state() == before


@pytest.mark.parametrize("surface", ["heartbeat", "auto_heartbeat"])
def test_heartbeat_race_cannot_extend_replacement_claim(worker, monkeypatch, surface):
    original = kb.heartbeat_worker
    after_replacement = []

    def replace_then_heartbeat(conn, tid, **kwargs):
        newer = _new_attempt(tid)
        assert newer.current_run_id != worker.current_run_id
        after_replacement.append(_state())
        return original(conn, tid, **kwargs)

    monkeypatch.setattr(kb, "heartbeat_worker", replace_then_heartbeat)
    _assert_refused(surface, _call(surface))
    assert len(after_replacement) == 1
    assert _state() == after_replacement[0]


def test_orchestrator_can_record_heartbeat_without_owning_claim(worker, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "different-operator:456")
    assert _call("heartbeat", task_id=worker.id).get("ok") is True
    with closing(kb.connect()) as conn:
        assert kb.get_task(conn, worker.id).claim_expires == 1
        assert kb.get_run(conn, worker.current_run_id).claim_expires == 1
        assert kb.list_events(conn, worker.id)[-1].kind == "heartbeat"


@pytest.mark.parametrize("surface", ["complete", "request_review"])
def test_run_replaced_during_judge_cannot_receive_old_handoff(worker, monkeypatch, surface):
    with closing(kb.connect()) as conn:
        conn.execute("UPDATE tasks SET goal_mode=1 WHERE id=?", (worker.id,))
        conn.commit()
    monkeypatch.setattr(kt, "_goal_judge_available", lambda: True)
    replaced = []

    def judge_and_replace(**kwargs):
        newer = _new_attempt(worker.id)
        # Even a changed process environment cannot replace the identity
        # captured before the judge started.
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(newer.current_run_id))
        replaced.append(_state())
        return "done", "Verified", False, None, False

    monkeypatch.setattr(kt, "judge_goal", judge_and_replace)
    _assert_refused(surface, _call(surface))
    assert len(replaced) == 1
    assert _state() == replaced[0]
