"""Tests for the specifier module + `hermes kanban specify` CLI surface.

The auxiliary LLM client is mocked — these tests don't hit any network or
real provider. They exercise the prompt plumbing, response parsing, DB
writes, and CLI flag surface.
"""

from __future__ import annotations

import argparse
import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_specify as spec


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    """Build a minimal object shaped like an OpenAI chat.completions result.

    The specifier only reads ``resp.choices[0].message.content``, so we
    avoid importing the openai SDK and build the tree with MagicMock.
    """
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    """Patch call_llm at its source module — specify_task now routes through
    it (#35566) instead of building a raw client. Returns (patcher, mock) so
    callers can still assert on the call.
    """
    mock_fn = MagicMock(return_value=_fake_aux_response(content))
    return patch("agent.auxiliary_client.call_llm", mock_fn), mock_fn


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# specify_task (module-level entry point)
# ---------------------------------------------------------------------------

def test_specify_task_happy_path(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)

    content = jsonlib.dumps({
        "title": "Refined rough",
        "body": "**Goal**\nA concrete goal.",
    })
    p, _ = _patch_aux_client(content)
    with p:
        outcome = spec.specify_task(tid, author="ace")

    assert outcome.ok is True
    assert outcome.task_id == tid
    assert outcome.new_title == "Refined rough"

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    # Parent-free → recompute_ready promotes to ready.
    assert task.status == "ready"
    assert task.title == "Refined rough"
    assert "**Goal**" in (task.body or "")


def _engage_planning_brake(home: Path, brake: str) -> Path:
    if brake == "estop":
        path = home / "ESTOP"
    else:
        path = home / "state" / "halt.json"
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("brake", ["halt", "estop"])
def test_specify_task_existing_stop_blocks_model_and_mutation(
    kanban_home, monkeypatch, brake
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)
    _engage_planning_brake(kanban_home, brake)
    content = jsonlib.dumps({"title": "must not land", "body": "blocked"})
    patcher, model_call = _patch_aux_client(content)

    with patcher:
        outcome = spec.specify_task(tid, author="ace")

    assert outcome.ok is False
    assert "paused or halted" in outcome.reason
    model_call.assert_not_called()
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None and task.status == "triage"
    assert task.title == "rough"


@pytest.mark.parametrize("brake", ["halt", "estop"])
def test_specify_task_stop_during_model_blocks_response_mutation(
    kanban_home, monkeypatch, brake
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)
    content = jsonlib.dumps({"title": "must not land", "body": "blocked"})

    def stop_during_model(**_kwargs):
        _engage_planning_brake(kanban_home, brake)
        return _fake_aux_response(content)

    model_call = MagicMock(side_effect=stop_during_model)
    with patch("agent.auxiliary_client.call_llm", model_call):
        outcome = spec.specify_task(tid, author="ace")

    assert outcome.ok is False
    assert "paused or halted" in outcome.reason
    model_call.assert_called_once()
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None and task.status == "triage"
    assert task.title == "rough"


def test_specify_task_reports_transaction_boundary_stop_as_paused(
    kanban_home, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)
    content = jsonlib.dumps({"title": "must not land", "body": "blocked"})
    patcher, _model_call = _patch_aux_client(content)

    def stopped_at_write_boundary(*_args, **_kwargs):
        raise kb.DispatchPausedError("stop engaged after lock wait")

    monkeypatch.setattr(kb, "specify_triage_task", stopped_at_write_boundary)
    with patcher:
        outcome = spec.specify_task(tid, author="ace")

    assert outcome.ok is False
    assert "paused or halted" in outcome.reason






# ---------------------------------------------------------------------------
# CLI wiring — argparse + _cmd_specify
# ---------------------------------------------------------------------------

def _run_cli(*argv: str) -> int:
    """Invoke the `hermes kanban …` argparse surface directly."""
    root = argparse.ArgumentParser()
    subp = root.add_subparsers(dest="cmd")
    kanban_cli.build_parser(subp)
    ns = root.parse_args(["kanban", *argv])
    return kanban_cli.kanban_command(ns)




def test_cli_specify_tenant_filter(kanban_home, capsys):
    with kb.connect() as conn:
        outside = kb.create_task(conn, title="outside", triage=True)
        inside = kb.create_task(
            conn, title="inside", triage=True, tenant="proj-a",
        )

    content = jsonlib.dumps({"title": "spec", "body": "body"})
    p, _ = _patch_aux_client(content)
    with p:
        rc = _run_cli("specify", "--all", "--tenant", "proj-a", "--json")
    assert rc == 0
    lines = [
        jsonlib.loads(l)
        for l in capsys.readouterr().out.strip().splitlines()
        if l
    ]
    ids = {row["task_id"] for row in lines}
    assert ids == {inside}

    # The outside task stays in triage.
    with kb.connect() as conn:
        assert kb.get_task(conn, outside).status == "triage"
        # The inside task was promoted.
        assert kb.get_task(conn, inside).status in {"todo", "ready"}

