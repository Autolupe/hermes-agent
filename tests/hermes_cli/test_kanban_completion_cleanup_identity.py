"""Committed cleanup cannot follow a task ID into another attempt's files."""

import contextlib
import shutil
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.delivery_fixtures import ARTIFACT_CONTRACT, ARTIFACT_DELIVERY


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(kb, "_cleanup_worker_tmux", lambda *_a: None)
    kb.init_db()
    with contextlib.closing(kb.connect()) as conn:
        yield conn


def scratch(conn):
    tid = kb.create_task(conn, title="Temporary cleanup audit", body=ARTIFACT_CONTRACT,
                         assignee="backend")
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    kb.set_workspace_path(conn, tid, ws)
    (ws / "keep.txt").write_text("original")
    run = kb.claim_task(conn, tid, claimer="original-claim")
    assert run is not None
    return tid, run.current_run_id, ws


def finish(conn, tid, rid):
    return kb.complete_task(conn, tid, expected_run_id=rid, summary="Evidence recorded",
                            delivery=ARTIFACT_DELIVERY, fire_lifecycle_hook=False)


@pytest.mark.parametrize("action", ["complete", "archive"])
@pytest.mark.parametrize("successor_state", ["running", "done", "new_workspace"])
def test_old_cleanup_preserves_successor_workspace(board, monkeypatch, action, successor_state):
    conn = board
    tid, rid, ws = scratch(conn)
    invoked = []
    kept = []

    def successor(*_args, **_kwargs):
        if invoked:
            return []
        invoked.append(True)
        assert not conn.in_transaction
        with contextlib.closing(kb.connect()) as other:
            with kb.write_txn(other):
                other.execute("UPDATE tasks SET status='ready', completed_at=NULL WHERE id=?", (tid,))
            new = kb.claim_task(other, tid, claimer="successor-claim")
            assert new is not None and new.current_run_id != rid
            if successor_state == "done":
                assert finish(other, tid, new.current_run_id)
                ws.mkdir(exist_ok=True)
            target = ws
            if successor_state == "new_workspace":
                target = ws.parent / (tid + "-replacement")
                target.mkdir()
                with kb.write_txn(other):
                    other.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(target), tid))
            (target / "keep.txt").write_text("successor")
            kept.append((target, tuple(other.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())))
        return []

    hook = "_scan_prose_for_phantom_ids" if action == "complete" else "_recompute_ready_after_committed_mutation"
    monkeypatch.setattr(kb, hook, successor)
    assert finish(conn, tid, rid) if action == "complete" else kb.archive_task(conn, tid)
    assert len(kept) == 1
    target, row = kept[0]
    assert (target / "keep.txt").read_text() == "successor"
    assert tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == row


@pytest.mark.parametrize("action", ["complete", "archive"])
def test_cleanup_preserves_replacement_directory_at_same_path(board, monkeypatch, action):
    conn = board
    tid, rid, ws = scratch(conn)
    original = ws.with_name(ws.name + "-original")

    def replace_directory(*_args, **_kwargs):
        assert not conn.in_transaction
        ws.rename(original)
        ws.mkdir()
        (ws / "keep.txt").write_text("replacement")
        return []

    hook = "_scan_prose_for_phantom_ids" if action == "complete" else "_recompute_ready_after_committed_mutation"
    monkeypatch.setattr(kb, hook, replace_directory)
    assert finish(conn, tid, rid) if action == "complete" else kb.archive_task(conn, tid)
    assert (ws / "keep.txt").read_text() == "replacement"
    assert (original / "keep.txt").read_text() == "original"


@pytest.mark.parametrize("action", ["complete", "archive"])
def test_cleanup_serializes_removal_with_board_writers(board, monkeypatch, action):
    conn = board
    tid, rid, ws = scratch(conn)
    removed = []
    original = shutil.rmtree
    with contextlib.closing(kb.connect()) as other:
        other.execute("PRAGMA busy_timeout=0")

        def remove(path, *args, **kwargs):
            if path == ws:
                assert conn.in_transaction
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("BEGIN IMMEDIATE")
                removed.append(True)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", remove)
        assert finish(conn, tid, rid) if action == "complete" else kb.archive_task(conn, tid)
        assert removed == [True]
        assert not ws.exists()
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
    assert not conn.in_transaction


@pytest.mark.parametrize("parent_state", ["ready", "running", "held", "replaced"])
def test_child_cleanup_preserves_reopened_or_held_parent(board, monkeypatch, tmp_path, parent_state):
    conn = board
    parent, parent_run, parent_ws = scratch(conn)
    child = kb.create_task(conn, title="Temporary child", body=ARTIFACT_CONTRACT,
                           workspace_kind="dir", workspace_path=str(tmp_path))
    kb.link_tasks(conn, parent, child)
    assert finish(conn, parent, parent_run)
    assert parent_ws.exists()

    def change_parent(*_args):
        if parent_state == "replaced":
            parent_ws.rename(parent_ws.with_name(parent_ws.name + "-original"))
            parent_ws.mkdir()
            (parent_ws / "keep.txt").write_text("replacement")
            return []
        with contextlib.closing(kb.connect()) as other, kb.write_txn(other):
            if parent_state == "held":
                kb._append_event(other, parent, "controlled_worker_held", {"request_id": "held-parent"},
                                 run_id=parent_run)
            else:
                other.execute("UPDATE tasks SET status='ready', completed_at=NULL WHERE id=?", (parent,))
        if parent_state == "running":
            with contextlib.closing(kb.connect()) as other:
                assert kb.claim_task(other, parent, claimer="parent-successor") is not None
        return []

    monkeypatch.setattr(kb, "_scan_prose_for_phantom_ids", change_parent)
    assert kb.complete_task(conn, child, summary="Child evidence", delivery=ARTIFACT_DELIVERY,
                            fire_lifecycle_hook=False)
    expected = "replacement" if parent_state == "replaced" else "original"
    assert (parent_ws / "keep.txt").read_text() == expected
    if parent_state == "replaced":
        assert (parent_ws.with_name(parent_ws.name + "-original") / "keep.txt").read_text() == "original"


def test_cleanup_refuses_caller_transaction_without_rolling_it_back(board, monkeypatch):
    conn = board
    tid, rid, ws = scratch(conn)
    with monkeypatch.context() as scope:
        scope.setattr(kb, "_cleanup_workspace", lambda *_a, **_k: None)
        assert finish(conn, tid, rid)
    expected = kb._workspace_cleanup_snapshot(conn, tid)
    assert expected is not None
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE tasks SET title='uncommitted caller edit' WHERE id=?", (tid,))
    kb._cleanup_workspace(conn, tid, expected_snapshot=expected)
    assert conn.in_transaction
    assert kb.get_task(conn, tid).title == "uncommitted caller edit"
    assert ws.exists()
    conn.rollback()
    assert kb.get_task(conn, tid).title == "Temporary cleanup audit"
