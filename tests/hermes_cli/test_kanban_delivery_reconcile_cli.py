"""Operator-only delivery repair uses explicit manifests and existing boards."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from agent import delegation_context
from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_delivery_reconcile as repair
from hermes_cli import delivery_verifier
from hermes_cli import sqlite_safe_read as safe_sqlite
from tests.hermes_cli.delivery_fixtures import ARTIFACT_CONTRACT


def _run(arguments):
    parser = argparse.ArgumentParser(prog="hermes-fixture")
    cli.build_parser(parser.add_subparsers(dest="command"))
    try:
        args = parser.parse_args(["kanban", *arguments])
    except SystemExit as exc:
        return int(exc.code)
    return cli.kanban_command(args)


def _dump(path):
    with closing(safe_sqlite.connect_tracked(path.as_uri() + "?mode=ro", uri=True)) as conn:
        return tuple(conn.iterdump())


def _tree(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


@pytest.fixture
def boards(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key in (
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TERMINAL_SANDBOX",
        "HERMES_KANBAN_DELIVERY_CONTROL", "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    kb.create_board("secondary")
    fixture = SimpleNamespace(root=tmp_path, home=home, paths={}, tasks={}, manifests={}, forbidden=[])
    for board in ("default", "secondary"):
        with closing(kb.connect(board=board)) as conn:
            tid = kb.create_task(conn, title=f"Historical {board}", body=ARTIFACT_CONTRACT)
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=100, result='original result' WHERE id=?",
                (tid,),
            )
            conn.commit()
            fixture.tasks[board] = tid
            fixture.paths[board] = kb.kanban_db_path(board=board)
            fixture.manifests[board] = repair.preview_delivery_repair(conn, board=board)

    def forbidden(name):
        def reject(*_args, **_kwargs):
            fixture.forbidden.append(name)
            raise AssertionError(f"reconciliation used forbidden implicit path: {name}")
        return reject

    # All fixture initialization is complete before these guards are installed.
    monkeypatch.setattr(kb, "connect", forbidden("kb.connect"))
    monkeypatch.setattr(kb, "init_db", forbidden("kb.init_db"))
    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", forbidden("notification"))
    monkeypatch.setattr(kb, "notify_task_updated", forbidden("task notification"))
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", forbidden("worker signal"))
    monkeypatch.setattr(kb, "_cleanup_workspace", forbidden("workspace cleanup"))
    monkeypatch.setattr(delivery_verifier, "verify_terminal", forbidden("terminal provider verification"))
    monkeypatch.setattr(delivery_verifier, "verify_submission", forbidden("submission provider verification"))
    fixture.tripwire = forbidden
    yield fixture
    assert fixture.forbidden == []


def _manifest_file(boards, *, board="default", name="reviewed.json"):
    path = boards.root / name
    path.write_text(json.dumps(boards.manifests[board]), encoding="utf-8")
    return path


def _deny_board_open(monkeypatch):
    calls = []

    def denied(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("manifest or identity rejection must happen before opening a board")

    monkeypatch.setattr(cli, "_open_delivery_repair_board", denied)
    return calls


def test_default_preview_prints_real_manifest_without_files_or_database_changes(boards, capsys):
    before = _dump(boards.paths["default"])
    files = _tree(boards.root)
    assert _run(["reconcile-delivery"]) == 0
    captured = capsys.readouterr()
    manifest = json.loads(captured.out)
    assert captured.err == ""
    assert manifest["schema"] == repair.MANIFEST_SCHEMA
    assert manifest["board"] == "default"
    assert manifest["database"] == boards.manifests["default"]["database"]
    assert [row["task_id"] for row in manifest["audit"]["entries"]] == [boards.tasks["default"]]
    assert _dump(boards.paths["default"]) == before
    assert _tree(boards.root) == files


def test_manifest_out_is_exclusive_and_private_even_with_permissive_umask(boards, capsys):
    path = boards.root / "new-manifest.json"
    before = _dump(boards.paths["default"])
    previous_umask = os.umask(0)
    try:
        assert _run(["reconcile-delivery", "--manifest-out", str(path)]) == 0
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["schema"] == repair.MANIFEST_SCHEMA
    repair._validate_manifest(manifest, board="default")
    assert _dump(boards.paths["default"]) == before
    capsys.readouterr()


@pytest.mark.parametrize("kind", ["regular", "symlink"])
def test_manifest_out_never_overwrites_an_existing_destination(boards, capsys, kind):
    original = boards.root / "original.json"
    original.write_text("Preserve this exact file.\n", encoding="utf-8")
    original.chmod(0o640)
    path = original
    if kind == "symlink":
        path = boards.root / "output-link.json"
        path.symlink_to(original)
    before = _dump(boards.paths["default"])
    assert _run(["reconcile-delivery", "--manifest-out", str(path)]) != 0
    assert original.read_text(encoding="utf-8") == "Preserve this exact file.\n"
    assert stat.S_IMODE(original.stat().st_mode) == 0o640
    assert _dump(boards.paths["default"]) == before
    assert capsys.readouterr().err


@pytest.mark.parametrize("kind", ["malformed", "array", "tampered", "oversized", "symlink", "directory", "fifo"])
def test_bad_manifest_is_refused_before_any_board_open(boards, monkeypatch, capsys, kind):
    path = boards.root / "input.json"
    if kind == "malformed":
        path.write_text("{broken", encoding="utf-8")
    elif kind == "array":
        path.write_text("[]", encoding="utf-8")
    elif kind == "tampered":
        value = dict(boards.manifests["default"], digest="0" * 64)
        path.write_text(json.dumps(value), encoding="utf-8")
    elif kind == "oversized":
        with path.open("wb") as stream:
            stream.truncate(repair.MAX_MANIFEST_BYTES + 1)
    elif kind == "symlink":
        path.symlink_to(_manifest_file(boards))
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
        native_open = os.open

        def bounded_open(candidate, flags, *args, **kwargs):
            if Path(candidate) == path:
                assert flags & os.O_NONBLOCK, "opening a FIFO must never wait for a writer"
            return native_open(candidate, flags, *args, **kwargs)

        monkeypatch.setattr(cli.os, "open", bounded_open)
    before = _dump(boards.paths["default"])
    calls = _deny_board_open(monkeypatch)
    assert _run(["reconcile-delivery", "--apply-manifest", str(path)]) != 0
    assert calls == []
    assert _dump(boards.paths["default"]) == before
    assert capsys.readouterr().err


@pytest.mark.parametrize("marker", [
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_TERMINAL_SANDBOX", "HERMES_KANBAN_DELIVERY_CONTROL",
])
@pytest.mark.parametrize("operation", ["preview", "apply", "manifest_out"])
def test_worker_denial_precedes_manifest_access_and_database_open(boards, monkeypatch, capsys, marker, operation):
    path = boards.root / "must-not-be-read-or-created.json"
    calls = _deny_board_open(monkeypatch)
    accesses = []

    def forbidden_manifest(*args, **kwargs):
        accesses.append((args, kwargs))
        raise AssertionError("worker touched a repair manifest")

    monkeypatch.setattr(cli, "_read_delivery_repair_manifest", forbidden_manifest)
    monkeypatch.setattr(cli, "_write_delivery_repair_manifest", forbidden_manifest)
    monkeypatch.setattr(repair, "_validate_manifest", forbidden_manifest)
    monkeypatch.setenv(marker, "fixture-worker")
    arguments = ["reconcile-delivery"]
    if operation == "apply":
        arguments += ["--apply-manifest", str(path)]
    elif operation == "manifest_out":
        arguments += ["--manifest-out", str(path)]
    assert _run(arguments) != 0
    assert calls == [] and accesses == []
    assert not path.exists()
    assert capsys.readouterr().err


def test_delegated_denial_precedes_manifest_access_and_database_open(boards, monkeypatch, capsys):
    path = boards.root / "unread.json"
    calls = _deny_board_open(monkeypatch)
    accesses = []
    monkeypatch.setattr(cli, "_read_delivery_repair_manifest", lambda *_a, **_k: accesses.append(True))
    with delegation_context.delegated_child_context():
        assert _run(["reconcile-delivery", "--apply-manifest", str(path)]) != 0
    assert calls == [] and accesses == []
    assert not path.exists()
    assert capsys.readouterr().err


@pytest.mark.parametrize("current,explicit,expected", [
    ("default", None, "default"), ("secondary", None, "secondary"),
    ("secondary", "default", "default"), ("default", "secondary", "secondary"),
])
def test_preview_selects_current_or_explicit_board(boards, monkeypatch, capsys, current, explicit, expected):
    monkeypatch.setenv("HERMES_KANBAN_BOARD", current)
    arguments = [] if explicit is None else ["--board", explicit]
    assert _run([*arguments, "reconcile-delivery"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["board"] == expected
    assert manifest["database"]["db_path"] == str(boards.paths[expected].resolve())
    assert [entry["task_id"] for entry in manifest["audit"]["entries"]] == [boards.tasks[expected]]


@pytest.mark.parametrize("value", ["0", "1001", "-1", "not-an-integer"])
def test_invalid_preview_limit_never_opens_board(boards, monkeypatch, capsys, value):
    calls = _deny_board_open(monkeypatch)
    assert _run(["reconcile-delivery", "--limit", value]) != 0
    assert calls == []
    assert capsys.readouterr().err


def test_apply_rejects_explicit_limit_before_opening_or_parsing_manifest(boards, monkeypatch, capsys):
    path = _manifest_file(boards)
    calls = _deny_board_open(monkeypatch)
    reads = []
    monkeypatch.setattr(cli, "_read_delivery_repair_manifest", lambda *_a, **_k: reads.append(True))
    assert _run(["reconcile-delivery", "--apply-manifest", str(path), "--limit", "1000"]) != 0
    assert calls == [] and reads == []
    assert capsys.readouterr().err


def test_output_and_apply_options_are_mutually_exclusive(boards, monkeypatch, capsys):
    calls = _deny_board_open(monkeypatch)
    path = boards.root / "manifest.json"
    assert _run(["reconcile-delivery", "--manifest-out", str(path), "--apply-manifest", str(path)]) != 0
    assert calls == [] and not path.exists()
    assert capsys.readouterr().err


@pytest.mark.parametrize("operation", ["preview", "apply"])
def test_missing_database_is_not_created(boards, monkeypatch, capsys, operation):
    manifest = _manifest_file(boards)
    missing = boards.root / "missing" / "kanban.db"
    monkeypatch.setattr(kb, "kanban_db_path", lambda *args, **kwargs: missing)
    files = _tree(boards.root)
    arguments = [] if operation == "preview" else ["--apply-manifest", str(manifest)]
    assert _run(["reconcile-delivery", *arguments]) != 0
    assert not missing.exists() and not missing.parent.exists()
    assert _tree(boards.root) == files
    assert capsys.readouterr().err


def test_missing_board_identity_requires_explicit_schema_update(boards, capsys):
    path = boards.paths["default"]
    with closing(safe_sqlite.connect_tracked(path.as_uri() + "?mode=rw", uri=True)) as conn:
        conn.execute("DROP TABLE kanban_board_identity")
        conn.commit()
    before = _dump(path)
    assert _run(["reconcile-delivery"]) != 0
    error = capsys.readouterr().err.lower()
    assert "schema" in error and "updat" in error
    assert _dump(path) == before


@pytest.mark.parametrize("operation", ["preview", "apply"])
def test_connections_are_tracked_and_use_existing_readonly_or_readwrite_board(boards, monkeypatch, capsys, operation):
    path = boards.paths["default"]
    assert safe_sqlite.has_live_connection(path) is False
    manifest_path = _manifest_file(boards)
    observed = []
    native_connect = safe_sqlite.connect_tracked

    def connect(*args, **kwargs):
        observed.append((str(args[0]), kwargs))
        return native_connect(*args, **kwargs)

    monkeypatch.setattr(safe_sqlite, "connect_tracked", connect)
    if getattr(cli, "connect_tracked", None) is native_connect:
        monkeypatch.setattr(cli, "connect_tracked", connect)

    def preview(conn, *, board, limit, **_kwargs):
        assert safe_sqlite.has_live_connection(path) is True
        assert board == "default" and limit == 7
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            conn.execute("UPDATE tasks SET title = title")
        return boards.manifests["default"]

    def apply(conn, manifest, *, board):
        assert safe_sqlite.has_live_connection(path) is True
        assert board == "default" and manifest == boards.manifests["default"]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE tasks SET title = title WHERE 0")
        conn.rollback()
        return {"repaired_count": 0, "results": []}

    monkeypatch.setattr(repair, "preview_delivery_repair", preview)
    monkeypatch.setattr(repair, "apply_delivery_repair", apply)
    arguments = ["--limit", "7"] if operation == "preview" else ["--apply-manifest", str(manifest_path)]
    assert _run(["reconcile-delivery", *arguments]) == 0
    assert len(observed) == 1
    assert f"mode={'ro' if operation == 'preview' else 'rw'}" in observed[0][0]
    assert observed[0][1].get("uri") is True
    assert safe_sqlite.has_live_connection(path) is False
    assert json.loads(capsys.readouterr().out)


def test_real_apply_validates_before_open_and_repairs_only_reviewed_board(boards, monkeypatch, capsys):
    path = _manifest_file(boards)
    unchanged_secondary = _dump(boards.paths["secondary"])
    native_validate = repair._validate_manifest
    native_open = cli._open_delivery_repair_board
    sequence = []

    def validate(*args, **kwargs):
        sequence.append("validate")
        return native_validate(*args, **kwargs)

    def open_board(*args, **kwargs):
        sequence.append("open")
        return native_open(*args, **kwargs)

    monkeypatch.setattr(repair, "_validate_manifest", validate)
    monkeypatch.setattr(cli, "_open_delivery_repair_board", open_board)
    assert _run(["reconcile-delivery", "--apply-manifest", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["repaired_count"] == 1
    assert sequence.index("validate") < sequence.index("open")
    with closing(safe_sqlite.connect_tracked(boards.paths["default"].as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        task = kb.get_task(conn, boards.tasks["default"])
        assert task.status == "blocked" and task.block_kind == "capability"
        assert task.result == "original result"
        assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?", (task.id,)).fetchone()[0] == 1
    assert _dump(boards.paths["secondary"]) == unchanged_secondary


def test_wrong_board_manifest_is_refused_before_database_open(boards, monkeypatch, capsys):
    path = _manifest_file(boards, board="secondary")
    calls = _deny_board_open(monkeypatch)
    assert _run(["reconcile-delivery", "--apply-manifest", str(path)]) != 0
    assert calls == []
    assert capsys.readouterr().err


def test_manifest_path_cannot_raw_read_a_live_tracked_database(boards, monkeypatch, capsys):
    path = boards.paths["default"]
    board_opens = _deny_board_open(monkeypatch)
    raw_opens = []
    native_open = os.open

    def reject_database_open(candidate, flags, *args, **kwargs):
        if Path(candidate).resolve() == path.resolve():
            raw_opens.append(str(candidate))
            raise AssertionError("manifest reader tried to open a live database as a raw file")
        return native_open(candidate, flags, *args, **kwargs)

    with closing(safe_sqlite.connect_tracked(path.as_uri() + "?mode=rw", uri=True)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        before = tuple(conn.iterdump())
        monkeypatch.setattr(cli.os, "open", reject_database_open)
        assert _run(["reconcile-delivery", "--apply-manifest", str(path)]) != 0
        assert raw_opens == [] and board_opens == []
        assert conn.in_transaction is True
        assert safe_sqlite.has_live_connection(path) is True
        assert tuple(conn.iterdump()) == before
        conn.rollback()
    assert safe_sqlite.has_live_connection(path) is False
    assert capsys.readouterr().err
