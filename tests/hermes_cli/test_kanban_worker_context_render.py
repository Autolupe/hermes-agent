"""Worker database-context compatibility through real temporary boards.

The rich output fixture was captured from the public builder at
832e6daba3f5ae2a15cca9f6431224f2378708dd before the collector/renderer split.
It protects the exact display bytes for a controlled scenario; the other
tests assert ordering, filtering, fallback, and size relationships explicitly.
No production source text or duplicate renderer is used as an oracle.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import kanban_db as kb


NOW = 1_800_000_000
FIXTURE = Path(__file__).with_name("kanban_worker_context_legacy.txt")


@pytest.fixture
def board(tmp_path):
    with kb.connect_closing(tmp_path / "render-board.db") as conn:
        yield conn


def _task(conn, task_id="task-main", **overrides):
    values = {
        "id": task_id,
        "title": "Render this task",
        "status": "running",
        "created_at": NOW - 86_400,
    }
    values.update(overrides)
    columns = ", ".join(values)
    conn.execute(
        f"INSERT INTO tasks ({columns}) VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )
    return task_id


def _run(conn, task_id="task-main", **overrides):
    values = {
        "task_id": task_id,
        "profile": "writer",
        "status": "done",
        "started_at": NOW - 3_600,
        "ended_at": NOW - 3_000,
        "outcome": "completed",
        "summary": "Finished the earlier work.",
    }
    values.update(overrides)
    columns = ", ".join(values)
    conn.execute(
        f"INSERT INTO task_runs ({columns}) VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


def _comment(conn, body, *, author="writer", created_at=NOW - 600):
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        ("task-main", author, body, created_at),
    )


def _link(conn, parent):
    conn.execute(
        "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (parent, "task-main"),
    )


def _render(conn, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: NOW)
    return kb.build_worker_context(conn, "task-main")


def _seed_rich(conn):
    _task(
        conn,
        title="Keep the task handoff intact",
        body="  First line.\nSecond line with café.  ",
        assignee="writer",
        tenant="example-tenant",
        workspace_kind="worktree",
        workspace_path="/fixture/workspaces/task-main",
        branch_name="task/context-capture",
        max_runtime_seconds=90,
    )
    for name, created, size, content_type in [
        ("later.pdf", NOW - 300, 1025, "application/pdf"),
        ("earlier.txt", NOW - 600, 1, "text/plain"),
        ("empty.bin", NOW - 300, 0, None),
    ]:
        conn.execute(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, size, content_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("task-main", name, "/fixture/attachments/" + name, size, content_type, created),
        )
    _run(
        conn, started_at=NOW - 120, ended_at=None, outcome=None,
        status="running", summary="ACTIVE RUN MUST NOT APPEAR",
    )
    _run(
        conn, started_at=NOW - 7_200, ended_at=NOW - 6_600, outcome="crashed",
        status="crashed", profile=None, summary="  First try.  ",
        error="  Retry after interruption.  ", metadata='{"z": 2, "a": "café"}',
    )
    _run(
        conn, started_at=NOW - 3_600, ended_at=NOW - 3_000,
        outcome=None, status="released", summary="Second try.",
    )
    _task(
        conn, "parent-z", status="done", result="Legacy parent result.",
        completed_at=NOW - 86_400,
    )
    _task(
        conn, "parent-a", status="done", result="Task result is older.",
        completed_at=NOW - 86_400,
    )
    _task(
        conn, "parent-pending", status="todo",
        result="UNFINISHED PARENT MUST NOT APPEAR",
    )
    for parent in ("parent-z", "parent-pending", "parent-a", "parent-missing"):
        _link(conn, parent)
    _run(
        conn, "parent-a", profile="upstream", started_at=NOW - 86_400,
        ended_at=NOW - 7_200, summary="Old completed run.",
    )
    _run(
        conn, "parent-a", profile="upstream", started_at=NOW - 10_800,
        ended_at=NOW - 3_600, summary="Latest completed handoff.",
        metadata='{"z": 9, "a": {"y": true, "b": "résumé"}}',
    )
    _run(
        conn, "parent-a", profile="upstream", started_at=NOW - 1_800,
        ended_at=NOW - 1_200, outcome="crashed", status="crashed",
        summary="NEWER FAILED RUN MUST NOT APPEAR",
    )
    _task(conn, "role-other", title="Earlier writing job", status="done")
    _run(
        conn, "role-other", started_at=NOW - 7_200, ended_at=NOW - 600,
        summary="First summary line.\nSecond summary line.",
    )
    _comment(conn, "Latest comment.", author="`operator`", created_at=NOW - 300)
    _comment(conn, "  Earlier comment.  ", created_at=NOW - 1_800)


def test_database_context_matches_exact_pre_split_output(board, monkeypatch):
    _seed_rich(board)
    monkeypatch.setenv("TERMINAL_TIMEOUT", "10")
    assert _render(board, monkeypatch) == FIXTURE.read_text(encoding="utf-8")


def test_minimal_task_has_no_empty_optional_sections(board, monkeypatch):
    _task(board, body=" \n ")
    text = _render(board, monkeypatch)
    assert "Assignee: (unassigned)\n" in text
    assert "Workspace: scratch @ (unresolved)\n" in text
    assert "## " not in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_unknown_task_raises_before_reading_the_clock(board):
    with mock.patch.object(kb.time, "time", side_effect=AssertionError("clock read")):
        with pytest.raises(ValueError, match="unknown task missing"):
            kb.build_worker_context(board, "missing")


def test_one_clock_read_drives_all_relative_ages(board):
    _task(board, assignee="writer")
    _run(board, started_at=NOW - 3_600)
    _task(board, "parent", status="done", completed_at=NOW - 3_600, result="Parent result.")
    _link(board, "parent")
    _task(board, "role", title="Role history", status="done")
    _run(board, "role", ended_at=NOW - 3_600)
    _comment(board, "Comment body.", created_at=NOW - 3_600)
    with mock.patch.object(kb.time, "time", return_value=NOW) as clock:
        text = kb.build_worker_context(board, "task-main")
    clock.assert_called_once_with()
    assert text.count("1h ago") == 4


@pytest.mark.parametrize("metadata", ["not JSON", "null", "{}", "[]", "0"])
def test_empty_or_invalid_metadata_does_not_hide_run_summary(board, monkeypatch, metadata):
    _task(board)
    _run(board, metadata=metadata, summary="Retain this handoff.")
    text = _render(board, monkeypatch)
    assert "Retain this handoff." in text
    assert "_metadata_:" not in text


def test_metadata_is_sorted_recursively_without_ascii_escaping(board, monkeypatch):
    _task(board)
    _run(board, metadata='{"z": 1, "a": {"z": "café", "b": 2}}')
    text = _render(board, monkeypatch)
    assert '_metadata_: `{"a": {"b": 2, "z": "café"}, "z": 1}`' in text
    assert "\\u00e9" not in text


def test_parent_uses_latest_completed_start_time_not_latest_end_or_failed_run(board, monkeypatch):
    _task(board)
    _task(board, "parent", status="done", result="Old task result.")
    _link(board, "parent")
    _run(
        board, "parent", started_at=NOW - 300, ended_at=NOW - 10,
        summary="Older start but later finish.",
    )
    _run(
        board, "parent", started_at=NOW - 200, ended_at=NOW - 100,
        summary="Selected completed run.",
    )
    _run(
        board, "parent", started_at=NOW - 50, ended_at=NOW - 20,
        outcome="crashed", summary="Latest failed run.",
    )
    text = _render(board, monkeypatch)
    assert "Selected completed run." in text
    assert "Older start but later finish." not in text
    assert "Latest failed run." not in text
    assert "Old task result." not in text


@pytest.mark.parametrize(
    "result, expected",
    [("Fallback task result.", "Fallback task result."), (None, "(no result recorded)")],
)
def test_blank_completed_parent_summary_keeps_metadata_and_result_fallback(
    board, monkeypatch, result, expected,
):
    _task(board)
    _task(board, "parent", status="done", result=result)
    _link(board, "parent")
    _run(board, "parent", summary=" \n ", metadata='{"proof": "kept"}')
    text = _render(board, monkeypatch)
    assert expected in text
    assert '_metadata_: `{"proof": "kept"}`' in text


def test_parent_rows_without_done_results_do_not_create_a_header(board, monkeypatch):
    _task(board)
    _task(board, "pending", status="todo", result="Not completed.")
    _link(board, "pending")
    _link(board, "missing")
    text = _render(board, monkeypatch)
    assert "Parent task results" not in text
    assert "Not completed." not in text


def test_prior_attempt_cap_keeps_latest_closed_runs_in_start_order(board, monkeypatch):
    _task(board)
    count = kb._CTX_MAX_PRIOR_ATTEMPTS + 2
    for index in reversed(range(count)):
        _run(board, started_at=NOW - 1_000 + index, summary=f"Closed attempt [{index}].")
    _run(
        board, started_at=NOW - 10, ended_at=None,
        status="running", outcome=None, summary="Active run excluded.",
    )
    text = _render(board, monkeypatch)
    assert "2 earlier attempts omitted" in text
    assert "Closed attempt [0]." not in text and "Closed attempt [1]." not in text
    assert "Active run excluded." not in text
    positions = [text.index(f"Closed attempt [{index}].") for index in range(2, count)]
    assert positions == sorted(positions)
    assert "### Attempt 3 —" in text
    assert f"### Attempt {count} —" in text


def test_comment_cap_orders_by_time_and_keeps_worker_author_framing(board, monkeypatch):
    _task(board)
    count = kb._CTX_MAX_COMMENTS + 1
    for index in reversed(range(count)):
        _comment(board, f"Comment [{index}].", author="`operator`", created_at=NOW - 1_000 + index)
    text = _render(board, monkeypatch)
    assert "1 earlier comment omitted" in text
    assert "Comment [0]." not in text
    positions = [text.index(f"Comment [{index}].") for index in range(1, count)]
    assert positions == sorted(positions)
    assert text.count("comment from worker `operator` at ") == count - 1
    assert "``operator``" not in text


def test_each_instruction_field_keeps_its_independent_character_cap(board, monkeypatch):
    body_limit = kb._CTX_MAX_BODY_BYTES
    field_limit = kb._CTX_MAX_FIELD_BYTES
    comment_limit = kb._CTX_MAX_COMMENT_BYTES
    _task(board, body="  " + "é" * (body_limit + 3) + "  ")
    _run(
        board, summary="S" * (field_limit + 5), error="E" * (field_limit + 7),
        metadata=json.dumps({"large": "M" * (field_limit + 9)}),
    )
    _task(board, "parent", status="done", result="P" * (field_limit + 11))
    _link(board, "parent")
    _comment(board, "C" * (comment_limit + 13))
    text = _render(board, monkeypatch)
    for char, limit, omitted in [
        ("é", body_limit, 3), ("S", field_limit, 5), ("E", field_limit, 7),
        ("P", field_limit, 11), ("C", comment_limit, 13),
    ]:
        assert char * limit + f"… [truncated, {omitted} chars omitted]" in text
        assert char * (limit + 1) not in text
    metadata_line = next(line for line in text.splitlines() if line.startswith("_metadata_:"))
    serialized = json.dumps({"large": "M" * (field_limit + 9)}, sort_keys=True)
    assert metadata_line == (
        f"_metadata_: `{serialized[:field_limit]}… "
        f"[truncated, {len(serialized) - field_limit} chars omitted]`"
    )


def test_role_history_keeps_latest_five_other_tasks_and_first_summary_line(board, monkeypatch):
    _task(board, assignee="writer")
    for index in range(7):
        task_id = _task(board, f"history-{index}", title=f"History title {index}", status="done")
        _run(
            board, task_id, ended_at=NOW - 700 + index,
            summary=f"History [{index}].\nSecond line must stay hidden.",
        )
    _task(board, "other-profile", status="done")
    _run(
        board, "other-profile", profile="reviewer", ended_at=NOW - 1,
        summary="Different profile excluded.",
    )
    _run(board, ended_at=NOW - 1, summary="Current task appears only in prior attempts.")
    text = _render(board, monkeypatch)
    role = text.split("## Recent work by @writer\n", 1)[1]
    assert "history-0" not in role and "history-1" not in role
    assert "Different profile excluded." not in role
    assert "Current task appears only" not in role
    assert "Second line must stay hidden." not in role
    positions = [role.index(f"History [{index}].") for index in reversed(range(2, 7))]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    "summary, expected",
    [(" \n ", "(no summary)"), ("x" * 230 + "\nsecond", "x" * 200)],
)
def test_role_history_summary_fallback_and_first_line_limit(board, monkeypatch, summary, expected):
    _task(board, assignee="writer")
    _task(board, "history", title="History", status="done")
    _run(board, "history", summary=summary)
    text = _render(board, monkeypatch)
    role = text.split("## Recent work by @writer\n", 1)[1]
    assert role.rstrip().endswith(": " + expected)
    assert "x" * 201 not in role


@pytest.mark.parametrize(
    "runtime, current, expected",
    [
        (None, "600", None), (90, None, "60"), (90, "10", "60"),
        (90, "invalid", "60"), (90, "600", "600"), (10, "0", "1"),
        (0, "80", "80"), (0, None, None),
    ],
)
def test_terminal_timeout_preserves_worker_override_rules(
    board, monkeypatch, runtime, current, expected,
):
    _task(board, max_runtime_seconds=runtime)
    if current is None:
        monkeypatch.delenv("TERMINAL_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("TERMINAL_TIMEOUT", current)
    text = _render(board, monkeypatch)
    if runtime is None:
        assert "Max runtime:" not in text
    else:
        assert f"Max runtime: {runtime}s\n" in text
    if expected is None:
        assert "Terminal timeout:" not in text
    else:
        assert f"Terminal timeout: {expected}s\n" in text
    assert os.environ.get("TERMINAL_TIMEOUT") == current


@pytest.mark.linux_only
def test_local_timezone_changes_absolute_stamps_but_not_relative_ages(board, monkeypatch):
    _task(board)
    _run(board, started_at=NOW - 3_600)
    previous = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "UTC0")
        time.tzset()
        utc = _render(board, monkeypatch)
        monkeypatch.setenv("TZ", "EST5")
        time.tzset()
        eastern = _render(board, monkeypatch)
    finally:
        if previous is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", previous)
        time.tzset()
    assert "2027-01-15 07:00, 1h ago" in utc
    assert "2027-01-15 02:00, 1h ago" in eastern
