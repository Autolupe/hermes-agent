"""``hermes kanban claim`` must respect the claim guard (plan P1.3 CLI half).

A terminal pulling a card by hand is the one dispatch path with no
dispatcher in front of it, so the CLI asks ``check_claim_guard`` itself:
defer reasons refuse unless ``--force``; block reasons always refuse.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_cli_claim_guard_")
    os.makedirs(os.path.join(test_home, "profiles", "default"), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    yield test_home


def _wire(monkeypatch, kanban_db, reason):
    """Install a guard returning ``reason``; a sibling change adds the real one
    (``raising=False`` keeps the test meaningful until it lands)."""
    monkeypatch.setattr(
        kanban_db, "check_claim_guard",
        lambda conn, task_id, *, board=None: reason, raising=False,
    )
    monkeypatch.setattr(
        kanban_db, "CLAIM_GUARD_BLOCK_REASONS",
        frozenset({"branch_policy", "protected_path"}), raising=False,
    )
    claimed = []

    def fake_claim(conn, task_id, *, ttl_seconds=None, claimer=None):
        claimed.append(task_id)
        return None  # let _cmd_claim take its "cannot claim" branch harmlessly

    monkeypatch.setattr(kanban_db, "claim_task", fake_claim)
    return claimed


def _args(task_id, force=False):
    return argparse.Namespace(task_id=task_id, ttl=900, force=force)


def test_defer_reason_refuses_without_force(isolated_kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    kanban_db.init_db()
    claimed = _wire(monkeypatch, kanban_db, "branch_conflict:t_11111111")
    assert kb_cli._cmd_claim(_args("t_00000000")) == 1
    assert claimed == [], "claim_task must not run when the guard defers"
    err = capsys.readouterr().err
    assert "branch_conflict:t_11111111" in err and "--force" in err


def test_defer_reason_proceeds_with_force(isolated_kanban_home, monkeypatch):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    kanban_db.init_db()
    claimed = _wire(monkeypatch, kanban_db, "scope_overlap:t_11111111:src/")
    kb_cli._cmd_claim(_args("t_00000000", force=True))
    assert claimed == ["t_00000000"]


@pytest.mark.parametrize("reason", ["branch_policy", "protected_path:ops/autonomy/"])
def test_block_reason_refuses_even_with_force(isolated_kanban_home, monkeypatch, capsys, reason):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    kanban_db.init_db()
    claimed = _wire(monkeypatch, kanban_db, reason)
    assert kb_cli._cmd_claim(_args("t_00000000", force=True)) == 1
    assert claimed == []
    assert "cannot be forced" in capsys.readouterr().err


def test_no_reason_claims_normally(isolated_kanban_home, monkeypatch):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    kanban_db.init_db()
    claimed = _wire(monkeypatch, kanban_db, None)
    kb_cli._cmd_claim(_args("t_00000000"))
    assert claimed == ["t_00000000"]


def test_claim_parser_accepts_force(isolated_kanban_home):
    from hermes_cli import kanban as kb_cli

    root = argparse.ArgumentParser()
    kb_cli.build_parser(root.add_subparsers(dest="command"))
    ns = root.parse_args(["kanban", "claim", "t_00000000", "--force"])
    assert ns.force is True
    ns = root.parse_args(["kanban", "claim", "t_00000000"])
    assert ns.force is False
    # --failure-limit now defaults to None so kanban.failure_limit from config wins.
    ns = root.parse_args(["kanban", "dispatch", "--dry-run"])
    assert ns.failure_limit is None
