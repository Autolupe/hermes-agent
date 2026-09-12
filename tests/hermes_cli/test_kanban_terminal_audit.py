"""Read-only event counters, using disposable native SQLite boards."""

import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_terminal_audit import main, terminal_audit


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "board.db"
    with kb.connect_closing(path) as conn:
        yield path, conn


def event(conn, kind, task_id="task-a", run_id=None):
    return conn.execute(
        "INSERT INTO task_events(task_id,run_id,kind,created_at,payload) VALUES (?,?,?,1,?)",
        (task_id, run_id, kind, '{"secret":"do not print"}'),
    ).lastrowid


def test_baseline_and_subsequent_counts_preserve_database_and_hide_payloads(board):
    _, conn = board
    conn.execute("INSERT INTO task_runs(id,task_id,status,outcome,started_at,ended_at) "
                 "VALUES (1,'task-a','blocked','blocked',1,2)")
    old = event(conn, "terminal_reconciled", run_id=1)
    baseline = terminal_audit(conn)
    assert baseline["mode"] == "baseline" and baseline["through_event_id"] == old
    assert "event_counts" not in baseline
    event(conn, "completed")
    event(conn, "blocked")
    event(conn, "terminal_reconciled", run_id=1)
    event(conn, "terminal_reconciled", task_id="other-task", run_id=1)
    event(conn, "terminal_reconciled")
    event(conn, "commented")
    before = tuple(conn.iterdump())
    report = terminal_audit(conn, since_event_id=old)
    assert report["event_counts"] == {
        "terminal_reconciled": 3, "completed": 1, "blocked": 1,
        "crashed": 0, "timed_out": 0, "rate_limited": 0,
    }
    assert report["repeated_reconciliation_events"] == 1
    assert report["unbound_reconciliation_events"] == 2
    assert "do not print" not in json.dumps(report)
    assert tuple(conn.iterdump()) == before
    assert terminal_audit(conn, since_event_id=old) == report


@pytest.mark.parametrize("value", [-1, True, 1.5, "0", 999])
def test_invalid_or_future_baseline_is_not_a_zero_success(board, value):
    _, conn = board
    before = tuple(conn.iterdump())
    with pytest.raises(ValueError):
        terminal_audit(conn, since_event_id=value)
    assert tuple(conn.iterdump()) == before
    assert not conn.in_transaction


def test_caller_transaction_is_not_committed_or_rolled_back(board):
    _, conn = board
    conn.execute("BEGIN IMMEDIATE")
    event(conn, "completed")
    assert terminal_audit(conn, since_event_id=0)["event_counts"]["completed"] == 1
    assert conn.in_transaction
    conn.rollback()
    assert terminal_audit(conn)["through_event_id"] == 0


def test_counts_use_one_snapshot_while_another_connection_appends(board):
    path, conn = board
    first = event(conn, "completed")
    inserted = []
    with kb.connect_closing(path) as writer:
        def append_during_count(sql):
            if sql.startswith("SELECT kind, COUNT(*)") and not inserted:
                inserted.append(event(writer, "completed"))

        conn.set_trace_callback(append_during_count)
        try:
            report = terminal_audit(conn, since_event_id=0)
        finally:
            conn.set_trace_callback(None)
    assert len(inserted) == 1 and inserted[0] > first
    assert report["through_event_id"] == first
    assert report["event_counts"]["completed"] == 1
    assert terminal_audit(conn, since_event_id=0)["event_counts"]["completed"] == 2


def test_cli_reads_only_existing_database_and_does_not_initialize(tmp_path, board, capsys):
    path, conn = board
    event(conn, "crashed")
    before = tuple(conn.iterdump())
    assert main(["--database", str(path), "--since-event-id", "0"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["database"] == str(path)
    assert report["event_counts"]["crashed"] == 1
    assert tuple(conn.iterdump()) == before
    missing = tmp_path / "missing.db"
    assert main(["--database", str(missing)]) == 1
    assert not missing.exists()
    assert capsys.readouterr().out == ""
    legacy = tmp_path / "unrelated.db"
    with sqlite3.connect(legacy) as other:
        other.execute("CREATE TABLE unrelated(value)")
    before_bytes = legacy.read_bytes()
    assert main(["--database", str(legacy)]) == 1
    assert legacy.read_bytes() == before_bytes
