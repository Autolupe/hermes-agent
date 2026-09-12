"""Disposable SQLite preview/apply tests; no provider or worker is invoked."""

import contextlib
import copy
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_delivery_reconcile as repair
from tests.hermes_cli.delivery_fixtures import ARTIFACT_CONTRACT


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
                "HERMES_KANBAN_DELIVERY_CONTROL", "HERMES_KANBAN_TERMINAL_SANDBOX"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _: True)
    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", lambda *a, **k: pytest.fail("notification"))
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *a, **k: pytest.fail("signal"))
    monkeypatch.setattr(kb, "_cleanup_workspace", lambda *a, **k: pytest.fail("cleanup"))
    with contextlib.closing(kb.connect(db_path=tmp_path / "board.db")) as board:
        yield board


def task(conn, status="done", *, parent=None):
    tid = kb.create_task(conn, title="legacy", assignee="default", body=ARTIFACT_CONTRACT)
    if parent:
        kb.link_tasks(conn, parent, tid)
    conn.execute("UPDATE tasks SET status=?, result='original result', completed_at=100 WHERE id=?",
                 (status, tid))
    conn.commit()
    return tid


def dump(conn):
    return tuple(conn.iterdump())


def preview(conn, ids=None):
    return repair.preview_delivery_repair(conn, board="default", task_ids=ids)


def apply(conn, manifest):
    return repair.apply_delivery_repair(conn, manifest, board="default")


def test_repair_preserves_history_is_repeat_safe_and_restores_review_queue(conn):
    parent = task(conn)
    children = [task(conn, status, parent=parent) for status in ("ready", "review", "done")]
    run_id = conn.execute(
        "INSERT INTO task_runs(task_id,profile,status,outcome,started_at,ended_at,worker_pid,claim_lock) "
        "VALUES (?,'reviewer','completed','completed',1,2,987654,'historical:claim')",
        (children[2],),
    ).lastrowid
    kb._append_event(conn, children[2], "claimed", {"source_status": "review"}, run_id=run_id)
    conn.commit()
    retained_run = tuple(conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone())
    initial = dump(conn)
    manifest = preview(conn, [parent])
    assert dump(conn) == initial
    result = apply(conn, manifest)
    assert result["repaired_count"] == 1
    root = kb.get_task(conn, parent)
    assert (root.status, root.block_kind, root.result) == ("blocked", "capability", "original result")
    assert all(kb.get_task(conn, tid).status == "todo" for tid in children)
    assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?", (parent,)).fetchone()[0] == 1
    kb.recompute_ready(conn)
    assert all(kb.get_task(conn, tid).status == "todo" for tid in children)
    after = dump(conn)
    assert apply(conn, manifest)["repaired_count"] == 0
    assert apply(conn, preview(conn))["repaired_count"] == 0
    assert dump(conn) == after
    assert tuple(conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()) == retained_run
    # Model a later independently verified parent transition, solely to test
    # the real queue resolver's consumption of the repair's durable hint.
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    conn.commit()
    kb.recompute_ready(conn)
    assert kb.get_task(conn, children[0]).status == "ready"
    assert kb.get_task(conn, children[1]).status == "review"
    assert kb.get_task(conn, children[2]).status == "review"


def test_repair_is_invisible_to_real_notification_selector(conn):
    from gateway.kanban_watchers import TERMINAL_NOTIFICATION_KINDS

    # Use the watcher's actual selector, so a future notifier expansion cannot
    # silently make repair events send messages or trigger wake-up model calls.
    kinds = TERMINAL_NOTIFICATION_KINDS
    parent = task(conn)
    kb.add_notify_sub(conn, task_id=parent, platform="telegram", chat_id="fixture-only",
                      delivery_mode="notify+wake")
    assert apply(conn, preview(conn, [parent]))["repaired_count"] == 1
    _, _, events = kb.claim_unseen_events_for_sub(
        conn, task_id=parent, platform="telegram", chat_id="fixture-only", kinds=kinds,
    )
    assert events == []
    assert kb._has_sticky_block(conn, parent)
    assert kb._resume_status_from_events(conn, parent) == "review"
    kb._append_event(conn, parent, "blocked", {"reason": "positive selector control"})
    conn.commit()
    _, _, events = kb.claim_unseen_events_for_sub(
        conn, task_id=parent, platform="telegram", chat_id="fixture-only", kinds=kinds,
    )
    assert [event.kind for event in events] == ["blocked"]


@pytest.mark.parametrize("fence", ["shipping", "held", "pid", "claim", "pointer", "open_run"])
def test_owned_descendant_defers_entire_graph_without_signaling(conn, fence):
    parent = task(conn)
    child = task(conn, "ready", parent=parent)
    if fence == "shipping":
        conn.execute("UPDATE tasks SET status='shipping' WHERE id=?", (child,))
    elif fence == "held":
        kb._append_event(conn, child, "controlled_worker_held", {"request_id": "fixture"})
    elif fence == "pid":
        conn.execute("UPDATE tasks SET worker_pid=987654 WHERE id=?", (child,))
    elif fence == "claim":
        conn.execute("UPDATE tasks SET claim_lock='other-host:secret-claim' WHERE id=?", (child,))
    elif fence == "pointer":
        conn.execute("UPDATE tasks SET current_run_id=987654 WHERE id=?", (child,))
    else:
        conn.execute("INSERT INTO task_runs(task_id,status,started_at) VALUES (?,'running',1)", (child,))
    conn.commit()
    before = dump(conn)
    manifest = preview(conn, [parent])
    assert "secret-claim" not in str(manifest)
    assert apply(conn, manifest)["results"][0]["outcome"] == "ownership_fenced"
    assert dump(conn) == before


@pytest.mark.parametrize("change", ["body", "comment", "link", "proof"])
def test_changed_snapshot_is_skipped_and_new_done_is_never_adopted(conn, change):
    parent = task(conn)
    manifest = preview(conn, [parent])
    other = task(conn)
    if change == "body":
        conn.execute("UPDATE tasks SET body=body || '\nchanged' WHERE id=?", (parent,))
    elif change == "comment":
        conn.execute("INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,'operator','proof',1)", (parent,))
    elif change == "link":
        conn.execute("INSERT INTO task_links(parent_id,child_id) VALUES (?,?)", (parent, other))
    else:
        kb._append_event(conn, parent, "completed", {"delivery": {"classification": "merged_pr"}})
    conn.commit()
    before = dump(conn)
    assert apply(conn, manifest)["results"] == [{"task_id": parent, "outcome": "changed_or_missing"}]
    assert dump(conn) == before
    assert kb.get_task(conn, other).status == "done"


@pytest.mark.parametrize("failure", ["statement", "commit"])
def test_failed_database_write_rolls_back_parent_comment_and_descendants(conn, failure):
    parent = task(conn)
    task(conn, "review", parent=parent)
    manifest = preview(conn, [parent])
    if failure == "statement":
        conn.execute("CREATE TRIGGER fail_repair BEFORE INSERT ON task_comments BEGIN SELECT RAISE(ABORT,'fixture'); END")
        conn.commit()
    else:
        conn.set_authorizer(lambda action, arg1, *_: sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT"
                            else sqlite3.SQLITE_OK)
    before = dump(conn)
    with pytest.raises(sqlite3.DatabaseError):
        apply(conn, manifest)
    conn.set_authorizer(None)
    assert not conn.in_transaction
    assert dump(conn) == before


def test_manifest_integrity_board_binding_and_worker_guard(conn, monkeypatch):
    parent = task(conn)
    manifest = preview(conn, [parent])
    before = dump(conn)
    tampered = copy.deepcopy(manifest)
    tampered["audit"]["entries"][0]["task_id"] = "t_forged"
    with pytest.raises(ValueError, match="integrity"):
        apply(conn, tampered)
    with pytest.raises(ValueError, match="board binding"):
        repair.apply_delivery_repair(conn, manifest, board="other")
    conn.execute("UPDATE kanban_board_identity SET board_uuid='11111111-1111-1111-1111-111111111111'")
    conn.commit()
    with pytest.raises(ValueError, match="different database"):
        apply(conn, manifest)
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
    with pytest.raises(PermissionError):
        apply(conn, {})
    assert kb.get_task(conn, parent).status == "done"
    assert before != dump(conn)  # only the explicitly changed identity differs


def test_human_gate_is_preserved(conn):
    parent = task(conn)
    child = task(conn, "blocked", parent=parent)
    conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (child,))
    conn.commit()
    before = dump(conn)
    assert apply(conn, preview(conn, [parent]))["results"][0]["outcome"] == "human_gate"
    assert dump(conn) == before
