"""Durable board binding, retirement and removal preserve worker ownership."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

from hermes_cli import kanban_board_identity as identity
from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("HERMES_KANBAN_HOME", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(name, raising=False)
    kb.create_board("identity-fixture")
    return kb.board_dir("identity-fixture")


def _snapshot(conn):
    return {
        name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name}")]
        for name in ("tasks", "task_runs", "task_events", "delivery_control_operations", "kanban_board_identity")
    }


def _retire(conn, token="fixture-retirement"):
    with kb.write_txn(conn):
        return identity.retire_board_identity(conn, token)


def test_native_initialization_has_one_stable_uuid_and_different_boards_differ(board):
    path = board / "kanban.db"
    with kb.connect_closing(path) as conn:
        first = identity.read_board_identity(conn, required=True)
        assert str(uuid.UUID(first["board_uuid"])) == first["board_uuid"]
        assert first["state"] == "active"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO kanban_board_identity (singleton, board_uuid, created_at) VALUES (2, ?, 1)",
                (str(uuid.uuid4()),),
            )
    kb.init_db(path)
    with kb.connect_closing(path) as conn:
        assert identity.read_board_identity(conn, required=True) == first
        assert conn.execute("SELECT count(*) FROM kanban_board_identity").fetchone()[0] == 1
    with kb.connect_closing(board.parent / "other.db") as conn:
        assert identity.read_board_identity(conn, required=True)["board_uuid"] != first["board_uuid"]


def test_missing_identity_can_be_read_without_mutating_legacy_database(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE tasks (id TEXT)")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        assert identity.read_board_identity(conn) is None
        binding = identity.board_binding(conn, required=False)
        assert binding["board_uuid"] is None and binding["state"] is None
        assert binding["db_path"] == str(path)
        with pytest.raises(identity.BoardIdentityError, match="persisted identity"):
            identity.board_binding(conn)
        assert conn.total_changes == 0
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == [("tasks",)]


def test_binding_uses_existing_connection_and_stat_without_new_descriptor(board, monkeypatch):
    path = board / "kanban.db"
    with kb.connect_closing(path) as conn:
        expected = identity.read_board_identity(conn, required=True)

        def forbidden(*_args, **_kwargs):
            raise AssertionError("identity reads must not open another database descriptor")

        with monkeypatch.context() as patch:
            patch.setattr("builtins.open", forbidden)
            patch.setattr(Path, "open", forbidden)
            patch.setattr(sqlite3, "connect", forbidden)
            binding = identity.board_binding(conn)
        observed = path.stat()
        assert binding == {
            "board_uuid": expected["board_uuid"], "state": "active",
            "db_path": str(path.resolve()), "device": observed.st_dev, "inode": observed.st_ino,
        }


def test_retirement_survives_reinitialization_and_keeps_reads_available(board):
    path = board / "kanban.db"
    with kb.connect_closing(path) as conn:
        task = kb.create_task(conn, title="preserved audit")
        retired = _retire(conn)
        with pytest.raises(identity.BoardRetiredError):
            kb.create_task(conn, title="refused")
        assert not conn.in_transaction
        assert kb.get_task(conn, task).title == "preserved audit"
    kb.init_db(path)
    with kb.connect_closing(path) as conn:
        assert identity.read_board_identity(conn, required=True) == retired
        assert kb.get_task(conn, task).title == "preserved audit"
        with pytest.raises(identity.BoardRetiredError):
            kb.add_comment(conn, task, author="fixture", body="refused")


@pytest.mark.parametrize("damage", [
    "running", "shipping", "current_run", "claim", "claim_expiry", "pid", "open_run",
])
@pytest.mark.parametrize("archive", [False, True])
def test_remove_refuses_owned_or_unfinished_runs_without_changes(board, damage, archive):
    with kb.connect_closing(board / "kanban.db") as conn:
        task = kb.create_task(conn, title="owned fixture")
        if damage in ("running", "shipping"):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (damage, task))
        elif damage == "current_run":
            conn.execute("UPDATE tasks SET current_run_id=919 WHERE id=?", (task,))
        elif damage == "claim":
            conn.execute("UPDATE tasks SET claim_lock='owner' WHERE id=?", (task,))
        elif damage == "claim_expiry":
            conn.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (task,))
        elif damage == "pid":
            conn.execute("UPDATE tasks SET worker_pid=919 WHERE id=?", (task,))
        else:
            # Even a pointerless run on a terminal-looking card retains ownership.
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task,))
            conn.execute(
                "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', 1)",
                (task,),
            )
        before = _snapshot(conn)
        with pytest.raises(ValueError, match="unfinished worker run"):
            kb.remove_board("identity-fixture", archive=archive)
        assert _snapshot(conn) == before
        assert board.exists()


@pytest.mark.parametrize("state", ["in_progress", "remote_applied", "quarantined", "failed", "unknown"])
def test_remove_preserves_unresolved_operation_even_without_current_task(board, state):
    with kb.connect_closing(board / "kanban.db") as conn:
        conn.execute(
            "INSERT INTO delivery_control_operations "
            "(op_id,task_id,run_id,action,request_sha256,claim_sha256,summary,candidate_head,"
            "peer_uid,owner_instance,state,created_at,updated_at) "
            "VALUES ('fixture','missing-task',1,'complete','request','claim','summary','head',"
            "1000,'owner',?,1,1)", (state,),
        )
        before = _snapshot(conn)
        with pytest.raises(ValueError, match="unresolved delivery operation"):
            kb.remove_board("identity-fixture")
        assert _snapshot(conn) == before


@pytest.mark.parametrize("drain", [None, "wrong_request", "wrong_event", "malformed"])
def test_remove_preserves_pending_held_worker_even_without_task_or_pid(board, drain):
    with kb.connect_closing(board / "kanban.db") as conn:
        held = conn.execute(
            "INSERT INTO task_events (task_id,run_id,kind,payload,created_at) "
            "VALUES ('missing-task',7,'controlled_worker_held',?,1)",
            (json.dumps({"request_id": "held-request"}),),
        ).lastrowid
        if drain:
            payload = {"held_event_id": held, "request_id": "held-request"}
            if drain == "wrong_request":
                payload["request_id"] = "other"
            elif drain == "wrong_event":
                payload["held_event_id"] += 1
            conn.execute(
                "INSERT INTO task_events (task_id,run_id,kind,payload,created_at) "
                "VALUES ('missing-task',7,'controlled_worker_drained',?,2)",
                ("invalid json" if drain == "malformed" else json.dumps(payload),),
            )
        before = _snapshot(conn)
        with pytest.raises(ValueError, match="held worker"):
            kb.remove_board("identity-fixture")
        assert _snapshot(conn) == before


@pytest.mark.parametrize("archive", [False, True])
def test_remove_and_recreate_have_different_identity_and_old_connection_cannot_write(board, archive):
    path = board / "kanban.db"
    with kb.connect_closing(path) as old:
        task = kb.create_task(old, title="old data")
        before = identity.board_binding(old)
        result = kb.remove_board("identity-fixture", archive=archive)
        with kb.connect_closing(path) as fresh:
            assert identity.read_board_identity(fresh, required=True)["board_uuid"] != before["board_uuid"]
            assert kb.get_task(fresh, task) is None
            kb.create_task(fresh, title="new board")
        with pytest.raises((identity.BoardRetiredError, sqlite3.OperationalError)):
            kb.create_task(old, title="must not enter either board")
        if archive:
            archived_path = Path(result["new_path"]) / "kanban.db"
            with kb.connect_closing(archived_path) as archived:
                assert identity.read_board_identity(archived, required=True)["state"] == "retired"
                assert kb.get_task(archived, task).title == "old data"
                with pytest.raises(identity.BoardRetiredError):
                    kb.create_task(archived, title="refused archive write")


def test_failed_rename_restores_same_identity_and_current_selection(board, monkeypatch):
    kb.set_current_board("identity-fixture")
    path = board / "kanban.db"
    with kb.connect_closing(path) as conn:
        before = identity.read_board_identity(conn, required=True)

    def fail_rename(self, target):
        assert self == board
        raise OSError("synthetic rename failure")

    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(OSError, match="synthetic rename failure"):
        kb.remove_board("identity-fixture")
    with kb.connect_closing(path) as conn:
        assert identity.read_board_identity(conn, required=True) == before
        kb.create_task(conn, title="retry is safe")
    assert kb.get_current_board() == "identity-fixture"


def test_failed_delete_never_unretires_or_deletes_recreated_board(board, monkeypatch):
    real_rmtree = kb.shutil.rmtree
    preserved = {}
    (board / "discarded-before-failure.txt").write_text("partial deletion fixture")

    def fail_delete(target, *args, **kwargs):
        # The original slug is now free; another ordinary connection may create it.
        if Path(target).parent.name == "_archived":
            preserved["target"] = Path(target)
            (Path(target) / "discarded-before-failure.txt").unlink()
            with kb.connect_closing(board / "kanban.db") as fresh:
                preserved["new_uuid"] = identity.read_board_identity(fresh, required=True)["board_uuid"]
                kb.create_task(fresh, title="new generation")
            raise OSError("synthetic partial deletion")
        return real_rmtree(target, *args, **kwargs)

    monkeypatch.setattr(kb.shutil, "rmtree", fail_delete)
    with pytest.raises(ValueError, match="recover remaining data"):
        kb.remove_board("identity-fixture", archive=False)
    with kb.connect_closing(board / "kanban.db") as fresh:
        assert identity.read_board_identity(fresh, required=True)["board_uuid"] == preserved["new_uuid"]
        assert [task.title for task in kb.list_tasks(fresh)] == ["new generation"]
    with kb.connect_closing(preserved["target"] / "kanban.db") as old:
        assert identity.read_board_identity(old, required=True)["state"] == "retired"
        assert not (preserved["target"] / "discarded-before-failure.txt").exists()


def test_failed_rename_compensation_never_resets_newer_retirement_token(board, monkeypatch):
    def fail_rename(self, target):
        with kb.connect_closing(board / "kanban.db") as owner:
            owner.execute(
                "UPDATE kanban_board_identity SET retirement_token='newer-owner' WHERE singleton=1"
            )
        raise OSError("rename interrupted by later retirement")

    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(OSError):
        kb.remove_board("identity-fixture")
    with kb.connect_closing(board / "kanban.db") as conn:
        current = identity.read_board_identity(conn, required=True)
        assert current["state"] == "retired"
        assert current["retirement_token"] == "newer-owner"


def test_writer_waiting_on_retirement_cannot_commit_into_removed_board(board, monkeypatch):
    path = board / "kanban.db"
    writer_started = threading.Event()
    writer_done = threading.Event()
    outcome = []
    real_retire = identity.retire_board_identity
    threads = []

    def writer():
        try:
            with kb.connect_closing(path) as conn:
                writer_started.set()
                kb.create_task(conn, title="must not race retirement")
            outcome.append("unexpected write")
        except identity.BoardRetiredError:
            outcome.append("retired")
        finally:
            writer_done.set()

    def retire_with_waiting_writer(conn, token):
        result = real_retire(conn, token)
        thread = threading.Thread(target=writer)
        threads.append(thread)
        thread.start()
        assert writer_started.wait(5)
        assert not writer_done.is_set()
        return result

    real_rename = Path.rename

    def wait_before_rename(self, target):
        assert writer_done.wait(5)
        return real_rename(self, target)

    monkeypatch.setattr(identity, "retire_board_identity", retire_with_waiting_writer)
    monkeypatch.setattr(Path, "rename", wait_before_rename)
    try:
        kb.remove_board("identity-fixture")
    finally:
        for thread in threads:
            thread.join(5)
    assert outcome == ["retired"]


@pytest.mark.parametrize("state", ["committed", "rejected"])
def test_native_terminal_operations_and_exactly_drained_held_worker_allow_removal(board, state):
    with kb.connect_closing(board / "kanban.db") as conn:
        conn.execute(
            "INSERT INTO delivery_control_operations "
            "(op_id,task_id,run_id,action,request_sha256,claim_sha256,summary,candidate_head,"
            "peer_uid,owner_instance,state,created_at,updated_at) "
            "VALUES ('fixture','terminal-task',7,'complete','request','claim','summary','head',"
            "1000,'owner',?,1,1)", (state,),
        )
        held = conn.execute(
            "INSERT INTO task_events (task_id,run_id,kind,payload,created_at) "
            "VALUES ('terminal-task',7,'controlled_worker_held',?,1)",
            (json.dumps({"request_id": "held-request"}),),
        ).lastrowid
        conn.execute(
            "INSERT INTO task_events (task_id,run_id,kind,payload,created_at) "
            "VALUES ('terminal-task',7,'controlled_worker_drained',?,2)",
            (json.dumps({"held_event_id": held, "request_id": "held-request"}),),
        )
    result = kb.remove_board("identity-fixture")
    assert result["action"] == "archived"
    assert not board.exists()


def test_late_rename_error_does_not_unretire_new_board_at_original_path(board, monkeypatch):
    real_rename = Path.rename
    preserved = {}

    def rename_then_fail(self, target):
        real_rename(self, target)
        preserved["target"] = Path(target)
        with kb.connect_closing(board / "kanban.db") as fresh:
            preserved["uuid"] = identity.read_board_identity(fresh, required=True)["board_uuid"]
            kb.create_task(fresh, title="new generation")
        raise OSError("synthetic error after rename")

    monkeypatch.setattr(Path, "rename", rename_then_fail)
    with pytest.raises(OSError, match="after rename"):
        kb.remove_board("identity-fixture")
    with kb.connect_closing(board / "kanban.db") as fresh:
        current = identity.read_board_identity(fresh, required=True)
        assert current["board_uuid"] == preserved["uuid"]
        assert current["state"] == "active"
        assert [task.title for task in kb.list_tasks(fresh)] == ["new generation"]
    with kb.connect_closing(preserved["target"] / "kanban.db") as archived:
        assert identity.read_board_identity(archived, required=True)["state"] == "retired"


def test_nested_ordinary_writer_cannot_bypass_same_transaction_retirement(board):
    with kb.connect_closing(board / "kanban.db") as conn:
        with kb.write_txn(conn):
            identity.retire_board_identity(conn, "outer-owner")
            with pytest.raises(identity.BoardRetiredError):
                kb.create_task(conn, title="nested bypass")
        assert kb.list_tasks(conn) == []
        assert identity.read_board_identity(conn, required=True)["state"] == "retired"
