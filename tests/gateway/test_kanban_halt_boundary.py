"""Shared Kanban brakes stop gateway decomposition and dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import types

import pytest

from agent import estop
from gateway import kanban_watchers
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp


def _write_brake(root, name):
    path = root / "state" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


def _decompose_response():
    content = json.dumps(
        {
            "fanout": False,
            "rationale": "single test task",
            "title": "tightened test task",
            "body": "must stay in triage while halted",
            "assignee": "default",
        }
    )
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content=content)
            )
        ]
    )


def _fanout_response():
    content = json.dumps(
        {
            "fanout": True,
            "rationale": "one isolated child",
            "tasks": [
                {
                    "title": "child that must not be created",
                    "body": "the connection-entry brake keeps this absent",
                    "assignee": "default",
                    "parents": [],
                }
            ],
        }
    )
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content=content)
            )
        ]
    )


def _prepare_decompose_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb.init_db()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="decompose edge", triage=True)
    monkeypatch.setattr(decomp, "_load_config", lambda: {})
    monkeypatch.setattr(
        decomp, "_resolve_orchestrator_profile", lambda _cfg: "default"
    )
    monkeypatch.setattr(
        decomp, "_resolve_default_assignee", lambda _cfg: "default"
    )
    fake_aux = types.ModuleType("agent.auxiliary_client")
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake_aux)
    return task_id, fake_aux


@pytest.mark.parametrize("brake_name", ["dispatch_pause.json", "halt.json"])
def test_gateway_dispatch_gate_honors_shared_kanban_brakes(
    tmp_path, monkeypatch, brake_name
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))

    assert kanban_watchers._kanban_dispatch_allowed() is True
    _write_brake(tmp_path, brake_name)
    assert kanban_watchers._kanban_dispatch_allowed() is False


def test_gateway_dispatch_gate_fails_closed_on_boundary_lookup_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))

    def failed_boundary_lookup():
        raise OSError("shared dispatch state unavailable")

    monkeypatch.setattr(kb, "dispatch_is_paused", failed_boundary_lookup)
    assert kanban_watchers._kanban_dispatch_allowed() is False


def test_decomposer_halt_during_setup_blocks_model_call_and_task_mutation(
    tmp_path, monkeypatch
):
    task_id, fake_aux = _prepare_decompose_probe(tmp_path, monkeypatch)
    model_calls = []

    def fake_call_llm(**_kwargs):
        model_calls.append(True)
        return _decompose_response()

    fake_aux.call_llm = fake_call_llm

    def halt_during_roster():
        _write_brake(tmp_path, "halt.json")
        return (
            [
                {
                    "name": "default",
                    "description": "test",
                    "has_description": True,
                }
            ],
            {"default"},
        )

    monkeypatch.setattr(decomp, "_build_roster", halt_during_roster)

    outcome = decomp.decompose_task(task_id, author="auto-decomposer")

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert outcome.ok is False
    assert "paused or halted" in outcome.reason
    assert model_calls == []
    assert task is not None and task.status == "triage"
    assert task_count == 1


def test_decomposer_halt_during_model_call_blocks_response_mutation(
    tmp_path, monkeypatch
):
    task_id, fake_aux = _prepare_decompose_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(
        decomp,
        "_build_roster",
        lambda: (
            [
                {
                    "name": "default",
                    "description": "test",
                    "has_description": True,
                }
            ],
            {"default"},
        ),
    )
    model_calls = []

    def halt_during_model(**_kwargs):
        model_calls.append(True)
        _write_brake(tmp_path, "halt.json")
        return _decompose_response()

    fake_aux.call_llm = halt_during_model

    outcome = decomp.decompose_task(task_id, author="auto-decomposer")

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert outcome.ok is False
    assert "paused or halted" in outcome.reason
    assert model_calls == [True]
    assert task is not None and task.status == "triage"
    assert task_count == 1


def test_decomposer_estop_during_setup_blocks_model_call_and_task_mutation(
    tmp_path, monkeypatch
):
    task_id, fake_aux = _prepare_decompose_probe(tmp_path, monkeypatch)
    engaged = False
    model_calls = []

    monkeypatch.setattr(
        estop, "check_paused", lambda _component, _logger: engaged
    )

    def forbidden_model_call(**_kwargs):
        model_calls.append(True)
        return _decompose_response()

    fake_aux.call_llm = forbidden_model_call

    def engage_during_roster():
        nonlocal engaged
        engaged = True
        return (
            [
                {
                    "name": "default",
                    "description": "test",
                    "has_description": True,
                }
            ],
            {"default"},
        )

    monkeypatch.setattr(decomp, "_build_roster", engage_during_roster)

    outcome = decomp.decompose_task(task_id, author="auto-decomposer")

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert outcome.ok is False
    assert "paused or halted" in outcome.reason
    assert model_calls == []
    assert task is not None and task.status == "triage"
    assert task_count == 1


def test_decomposer_estop_during_model_call_blocks_response_mutation(
    tmp_path, monkeypatch
):
    task_id, fake_aux = _prepare_decompose_probe(tmp_path, monkeypatch)
    engaged = False
    model_calls = []
    monkeypatch.setattr(
        estop, "check_paused", lambda _component, _logger: engaged
    )
    monkeypatch.setattr(
        decomp,
        "_build_roster",
        lambda: (
            [
                {
                    "name": "default",
                    "description": "test",
                    "has_description": True,
                }
            ],
            {"default"},
        ),
    )

    def engage_during_model(**_kwargs):
        nonlocal engaged
        model_calls.append(True)
        engaged = True
        return _decompose_response()

    fake_aux.call_llm = engage_during_model

    outcome = decomp.decompose_task(task_id, author="auto-decomposer")

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert outcome.ok is False
    assert "paused or halted" in outcome.reason
    assert model_calls == [True]
    assert task is not None and task.status == "triage"
    assert task_count == 1


def test_decomposer_estop_lookup_error_fails_closed(tmp_path, monkeypatch):
    task_id, fake_aux = _prepare_decompose_probe(tmp_path, monkeypatch)
    model_calls = []

    def failed_estop_lookup(_component, _logger):
        raise OSError("emergency-stop state unavailable")

    monkeypatch.setattr(estop, "check_paused", failed_estop_lookup)
    fake_aux.call_llm = lambda **_kwargs: model_calls.append(True)

    outcome = decomp.decompose_task(task_id, author="auto-decomposer")

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert outcome.ok is False
    assert "paused or halted" in outcome.reason
    assert model_calls == []
    assert task is not None and task.status == "triage"
    assert task_count == 1


def _assert_connection_entry_brake_blocks_mutation(
    tmp_path, monkeypatch, model_response
):
    task_id, fake_aux = _prepare_decompose_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(
        decomp,
        "_build_roster",
        lambda: (
            [
                {
                    "name": "default",
                    "description": "test",
                    "has_description": True,
                }
            ],
            {"default"},
        ),
    )
    fake_aux.call_llm = lambda **_kwargs: model_response()
    real_connect_closing = kb.connect_closing
    connection_entries = 0

    @contextlib.contextmanager
    def brake_during_second_connection(*args, **kwargs):
        nonlocal connection_entries
        with real_connect_closing(*args, **kwargs) as conn:
            connection_entries += 1
            if connection_entries == 2:
                _write_brake(tmp_path, "halt.json")
            yield conn

    monkeypatch.setattr(kb, "connect_closing", brake_during_second_connection)

    outcome = decomp.decompose_task(task_id, author="auto-decomposer")

    monkeypatch.setattr(kb, "connect_closing", real_connect_closing)
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert connection_entries == 2
    assert outcome.ok is False
    assert "paused or halted" in outcome.reason
    assert task is not None and task.status == "triage"
    assert task_count == 1


def test_decomposer_single_task_brake_during_connection_entry_blocks_mutation(
    tmp_path, monkeypatch
):
    _assert_connection_entry_brake_blocks_mutation(
        tmp_path, monkeypatch, _decompose_response
    )


def test_decomposer_fanout_brake_during_connection_entry_blocks_mutation(
    tmp_path, monkeypatch
):
    _assert_connection_entry_brake_blocks_mutation(
        tmp_path, monkeypatch, _fanout_response
    )


@pytest.mark.parametrize(
    ("model_response", "helper_name"),
    [
        (_decompose_response, "specify_triage_task"),
        (_fanout_response, "decompose_triage_task"),
    ],
)
def test_decomposer_reports_transaction_boundary_stop_as_paused(
    tmp_path, monkeypatch, model_response, helper_name
):
    task_id, fake_aux = _prepare_decompose_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(
        decomp,
        "_build_roster",
        lambda: (
            [
                {
                    "name": "default",
                    "description": "test",
                    "has_description": True,
                }
            ],
            {"default"},
        ),
    )
    fake_aux.call_llm = lambda **_kwargs: model_response()

    def stopped_at_write_boundary(*_args, **_kwargs):
        raise kb.DispatchPausedError("stop engaged after lock wait")

    monkeypatch.setattr(kb, helper_name, stopped_at_write_boundary)

    outcome = decomp.decompose_task(task_id, author="auto-decomposer")

    assert outcome.ok is False
    assert "paused or halted" in outcome.reason
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert task is not None and task.status == "triage"
    assert task_count == 1


@pytest.mark.parametrize("brake_name", ["dispatch_pause.json", "halt.json"])
def test_gateway_brake_suppresses_auto_decompose_and_dispatch_tick(
    tmp_path, monkeypatch, brake_name
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "1")
    _write_brake(tmp_path, brake_name)

    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": True,
            }
        },
    )
    monkeypatch.setattr(kb, "dispatch_kwargs_from_config", lambda: {})
    monkeypatch.setattr(
        kanban_watchers,
        "_acquire_singleton_lock",
        lambda _path: (None, "unavailable"),
    )

    def reap_only():
        return []

    monkeypatch.setattr(kb, "reap_worker_zombies", reap_only)

    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    sleep_calls = []
    thread_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        if len(sleep_calls) >= 2:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        thread_calls.append(fn)
        return fn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    asyncio.run(runner._kanban_dispatcher_watcher())

    assert thread_calls == [reap_only]
    assert sleep_calls == [5, 1.0]
