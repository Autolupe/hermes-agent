"""Request-owned database display capture, using real temporary SQLite boards."""

import contextlib
from contextvars import copy_context
from dataclasses import FrozenInstanceError, fields, is_dataclass
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_policy as policy
from tests.hermes_cli.delivery_fixtures import ARTIFACT_CONTRACT


pytestmark = pytest.mark.linux_only
TASK_BODY = "original task body\n\n" + ARTIFACT_CONTRACT


class FixtureProvider(policy.RequiredKanbanPolicy):
    """Only preparation is allowed; the actual worker launch gate is unchanged."""

    def __init__(self):
        self.requests = []
        self.boundaries = []
        self.hook = lambda request, boundary, observation: None

    def open_workspace_request(self, request):
        self.requests.append(request)
        provider = self

        class Admission(policy.RequiredWorkspaceAdmission):
            def checkpoint(self, boundary, observation):
                provider.boundaries.append((boundary, request.connection.in_transaction))
                return provider.hook(request, boundary, observation)

            def cancel(self, reason):
                assert request.cancelled

            def close(self):
                pass

        return Admission()


@pytest.fixture
def native(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    monkeypatch.setattr(kb, "_fire_worker_spawned_hook", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", lambda *a, **kw: None)
    provider = FixtureProvider()
    registration = SimpleNamespace(
        uid=os.getuid(), generation="context-fixture", provider=provider,
        check_integrity=lambda: None,
    )
    monkeypatch.setattr(policy, "select_required_policy", lambda: registration)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / "board.db"
    with contextlib.closing(kb.connect(db_path=path)) as conn:
        yield SimpleNamespace(conn=conn, workspace=workspace, provider=provider,
                              path=path, registration=registration)


def make_task(native, lane="ready", *, claimed=True, skills=None):
    task_id = kb.create_task(
        native.conn, title="captured task", body=TASK_BODY, assignee="default",
        workspace_kind="dir", workspace_path=str(native.workspace), skills=skills,
    )
    if lane == "review":
        native.conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        native.conn.commit()
    if not claimed:
        return kb.get_task(native.conn, task_id)
    return (kb.claim_review_task if lane == "review" else kb.claim_task)(native.conn, task_id)


def request_for(native, task, lane="ready"):
    return kb.required_workspace_request(
        native.conn, task_id=task.id, expected_run_id=task.current_run_id,
        expected_claim_lock=task.claim_lock, lane=lane,
    )


def persist(native, request):
    kb._persist_required_workspace(native.conn, request, str(native.workspace), None)


@contextlib.contextmanager
def refusal(*, cause=()):
    # An assertion or unavailable fixture API wrapped by the native context
    # must not be counted as an expected policy refusal.
    with pytest.raises(policy.RequiredPolicyError) as caught:
        yield caught
    error = caught.value
    while error is not None:
        if not isinstance(error, (policy.RequiredPolicyError, *cause)):
            raise error
        error = error.__cause__


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_capture_uses_original_connection_after_persist_and_freezes_lane_skills(native, lane):
    task = make_task(native, lane, skills=["domain", "sdlc-review"])
    with request_for(native, task, lane) as request:
        original_fields = request.claim.launch_fields
        persist(native, request)
        captured = request.capture_database_context()
        assert request.connection is native.conn
        assert request.database_context is captured
        assert captured.text == kb.render_worker_context(captured.inputs)
        assert captured.inputs.task.workspace_path == str(native.workspace)
        assert captured.effective_skills == ("domain", "sdlc-review")
        assert request.claim.launch_fields == original_fields
        assert kb.get_task(native.conn, task.id).skills == ["domain", "sdlc-review"]
    selected = dict(native.provider.boundaries)
    assert selected["before_context_capture"] is False
    assert selected["context_capture_locked"] is True
    assert selected["before_context_capture_commit"] is True
    assert selected["after_context_capture"] is False
    with pytest.raises(FrozenInstanceError):
        captured.text = "replacement"
    with pytest.raises(FrozenInstanceError):
        captured.inputs.task.body = "replacement"
    with pytest.raises(AttributeError):
        request.database_context = captured


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("skills", [None, ["domain"], ["sdlc-review", "domain"]])
def test_dispatch_both_lanes_capture_existing_workspace_without_starting_worker(native, lane, skills):
    task = make_task(native, lane, claimed=False, skills=skills)
    spawned = []
    result = kb.dispatch_once(native.conn, max_spawn=1,
                              spawn_fn=lambda *args, **kwargs: spawned.append(True))
    assert not spawned and not result.spawned
    assert result.claim_guarded == [(task.id, "required_workspace_denied")]
    request = native.provider.requests[0]
    captured = request.database_context
    assert captured is not None
    assert captured.inputs.task.workspace_path == str(native.workspace)
    expected = tuple(skills or ())
    if lane == "review":
        expected = tuple(dict.fromkeys((*expected, "sdlc-review")))
    assert captured.effective_skills == expected
    assert request.cancelled
    assert native.workspace.is_dir()
    assert kb.get_task(native.conn, task.id).worker_pid is None
    assert kb.get_task(native.conn, task.id).skills == skills
    # Both canonical dispatch lanes reach capture outside a caller transaction.
    assert ("before_context_capture", False) in native.provider.boundaries
    assert ("after_context_capture", False) in native.provider.boundaries


def test_every_database_reader_receives_original_connection_and_no_reopen(native, monkeypatch):
    task = make_task(native)
    observed = []
    for name in ("get_task", "list_runs", "list_attachments", "list_comments"):
        original = getattr(kb, name)

        def read(conn, *args, _original=original, _name=name, **kwargs):
            assert conn is native.conn
            assert conn.in_transaction
            observed.append(_name)
            return _original(conn, *args, **kwargs)

        monkeypatch.setattr(kb, name, read)

    def no_reopen(*args, **kwargs):
        raise AssertionError("capture reopened a database")

    monkeypatch.setattr(kb, "connect", no_reopen)
    with request_for(native, task) as request:
        persist(native, request)
        request.capture_database_context()
    assert set(observed) == {"get_task", "list_runs", "list_attachments", "list_comments"}


def test_pure_render_survives_closed_database_and_changed_environment(native, monkeypatch):
    task = make_task(native)
    native.conn.execute("UPDATE tasks SET max_runtime_seconds = 90 WHERE id = ?", (task.id,))
    native.conn.execute(
        "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (task.id, "fixture", "captured comment", 1_700_000_000),
    )
    native.conn.execute(
        "INSERT INTO task_runs(task_id, profile, status, started_at, ended_at, summary, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task.id, "default", "done", 1_700_000_000, 1_700_000_100, "earlier attempt",
         '{"nested": {"values": [1, 2]}}'),
    )
    native.conn.commit()
    inputs = kb.collect_worker_context(native.conn, task.id)
    expected = kb.render_worker_context(inputs)
    assert "earlier attempt" in expected and "captured comment" in expected

    def immutable(value):
        if is_dataclass(value):
            assert value.__dataclass_params__.frozen
            for item in fields(value):
                immutable(getattr(value, item.name))
        elif isinstance(value, tuple):
            for item in value:
                immutable(item)
        else:
            assert type(value) in (str, int, float, bool, bytes, type(None))

    immutable(inputs)
    native.conn.close()
    monkeypatch.setenv("TERMINAL_TIMEOUT", "99999999")
    monkeypatch.setattr(kb, "_CTX_MAX_FIELD_BYTES", 1)
    monkeypatch.setattr(kb, "_CTX_MAX_COMMENT_BYTES", 1)

    def forbidden(*args, **kwargs):
        raise AssertionError("pure renderer read mutable external state")

    for name in ("time", "localtime", "strftime"):
        monkeypatch.setattr(kb.time, name, forbidden)
    for name in ("get_task", "list_runs", "list_attachments", "list_comments"):
        monkeypatch.setattr(kb, name, forbidden)
    assert kb.render_worker_context(inputs) == expected


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_commit_failure_or_interruption_never_publishes_partial_capture(
    native, monkeypatch, committed, error,
):
    task = make_task(native)
    original = kb._execute_boundary_with_retry
    failure = error("fixture commit failure")

    def boundary(conn, sql):
        if sql == "COMMIT":
            if committed:
                original(conn, sql)
            raise failure
        return original(conn, sql)

    expected = refusal(cause=(OSError,)) if error is OSError else pytest.raises(error)
    with expected:
        with request_for(native, task) as request:
            persist(native, request)
            with monkeypatch.context() as scoped:
                scoped.setattr(kb, "_execute_boundary_with_retry", boundary)
                request.capture_database_context()
    assert request.cancelled and request.database_context is None
    assert not native.conn.in_transaction


@pytest.mark.parametrize("skills", ['["valid", ["mutable"]]', '{"not": "a list"}'])
def test_direct_capture_refuses_non_string_skill_payload(native, skills):
    task = make_task(native)
    native.conn.execute("UPDATE tasks SET skills = ? WHERE id = ?", (skills, task.id))
    native.conn.commit()
    with refusal():
        with request_for(native, task) as request:
            persist(native, request)
            request.capture_database_context()
    assert request.cancelled and request.database_context is None


@pytest.mark.parametrize("boundary", [
    "before_context_capture", "context_capture_locked",
    "before_context_capture_commit", "after_context_capture",
])
def test_denial_at_capture_boundaries_cancels_without_retaining_partial_context(native, boundary):
    task = make_task(native)

    def deny(request, actual, observation):
        if actual == boundary:
            raise policy.RequiredPolicyError("fixture capture denial")

    with refusal():
        with request_for(native, task) as request:
            persist(native, request)
            native.provider.hook = deny
            request.capture_database_context()
    assert request.cancelled
    assert request.database_context is None
    assert not native.conn.in_transaction
    with refusal():
        request.capture_database_context()


@pytest.mark.parametrize("operation", ["collect_worker_context", "render_worker_context", "collect_task_show"])
@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt, SystemExit])
def test_capture_error_or_interrupt_rolls_back_own_transaction_and_cancels(
    native, monkeypatch, operation, error,
):
    task = make_task(native)
    failure = error("fixture interruption")

    def fail(*args, **kwargs):
        raise failure

    expected = refusal(cause=(OSError,)) if error is OSError else pytest.raises(error)
    with expected:
        with request_for(native, task) as request:
            persist(native, request)
            monkeypatch.setattr(kb, operation, fail)
            request.capture_database_context()
    assert request.cancelled
    assert request.database_context is None
    assert not native.conn.in_transaction


def test_capture_without_persistence_refuses(native):
    task = make_task(native)
    with refusal():
        with request_for(native, task) as request:
            request.capture_database_context()
    assert request.cancelled and request.database_context is None


def test_caller_transaction_refusal_preserves_uncommitted_changes_and_rollback(native):
    task = make_task(native)
    with refusal(cause=(RuntimeError,)):
        with request_for(native, task) as request:
            persist(native, request)
            native.conn.execute("BEGIN IMMEDIATE")
            native.conn.execute(
                "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (task.id, "fixture", "caller pending", 1),
            )
            with refusal(cause=(RuntimeError,)):
                request.capture_database_context()
            assert request.cancelled and request.database_context is None
            assert native.conn.in_transaction
            assert kb.list_comments(native.conn, task.id)[0].body == "caller pending"
    assert native.conn.in_transaction
    native.conn.rollback()
    assert kb.list_comments(native.conn, task.id) == []


def test_ordinary_collector_does_not_commit_caller_transaction(native):
    task = make_task(native)
    native.conn.execute("BEGIN IMMEDIATE")
    native.conn.execute("UPDATE tasks SET body = 'uncommitted display' WHERE id = ?", (task.id,))
    inputs = kb.collect_worker_context(native.conn, task.id)
    assert "uncommitted display" in kb.render_worker_context(inputs)
    assert native.conn.in_transaction
    native.conn.rollback()
    assert kb.get_task(native.conn, task.id).body == TASK_BODY
    assert inputs.task.body == "uncommitted display"


def test_other_connection_cannot_mutate_during_capture_and_later_changes_do_not_rewrite_it(native, monkeypatch):
    task = make_task(native)
    original = kb.list_attachments
    attempts = []
    with contextlib.closing(sqlite3.connect(native.path, timeout=0)) as other:
        def concurrent(conn, task_id):
            assert conn is native.conn and conn.in_transaction
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE tasks SET body = 'concurrent mutation' WHERE id = ?", (task.id,))
            other.rollback()
            attempts.append(True)
            return original(conn, task_id)

        monkeypatch.setattr(kb, "list_attachments", concurrent)
        with request_for(native, task) as request:
            persist(native, request)
            captured = request.capture_database_context()
            other.execute(
                "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (task.id, "fixture", "later steering input", 2),
            )
            other.commit()
            assert kb.list_comments(native.conn, task.id)[0].body == "later steering input"
            assert captured.inputs.comments == ()
            assert "later steering input" not in captured.text
            assert "original task body" in captured.text
    assert attempts == [True]


@pytest.mark.parametrize("boundary", ["before_context_capture", "after_context_capture"])
def test_claim_change_at_unlocked_edges_refuses(native, boundary):
    task = make_task(native)
    with contextlib.closing(sqlite3.connect(native.path, timeout=0)) as other:
        def mutate(request, actual, observation):
            if actual == boundary:
                other.execute("UPDATE tasks SET body = 'replacement task' WHERE id = ?", (task.id,))
                other.commit()

        with refusal():
            with request_for(native, task) as request:
                persist(native, request)
                native.provider.hook = mutate
                request.capture_database_context()
    assert request.cancelled and request.database_context is None


def test_recapture_refuses_without_replacing_first_snapshot(native):
    task = make_task(native)
    with refusal():
        with request_for(native, task) as request:
            persist(native, request)
            first = request.capture_database_context()
            request.capture_database_context()
    assert request.cancelled
    assert request.database_context is first


@pytest.mark.parametrize("boundary", [
    "before_context_capture", "context_capture_locked",
    "before_context_capture_commit", "after_context_capture",
])
def test_provider_callback_cannot_recursively_publish_a_second_capture(native, boundary):
    task = make_task(native)
    recursive_attempts = []

    def reenter(request, actual, observation):
        if actual == boundary:
            native.provider.hook = lambda *args: None
            with refusal():
                request.capture_database_context()
            recursive_attempts.append(True)
            assert request.cancelled
            assert request.database_context is None

    with refusal():
        with request_for(native, task) as request:
            persist(native, request)
            native.provider.hook = reenter
            request.capture_database_context()
    assert recursive_attempts == [True]
    assert request.cancelled and request.database_context is None
    assert not native.conn.in_transaction


def test_later_request_or_copied_context_cannot_revive_cancelled_capture(native):
    first_task = make_task(native)
    second_task = make_task(native)
    with refusal():
        with request_for(native, first_task) as first:
            persist(native, first)
            old_context = copy_context()
            first.cancel("fixture cancellation")
            first.capture_database_context()
    with request_for(native, second_task) as second:
        persist(native, second)
        with refusal():
            old_context.run(first.capture_database_context)
        assert first.database_context is None
        assert first.cancelled and not second.cancelled
        captured = second.capture_database_context()
        assert captured.inputs.task.id == second_task.id


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_ordinary_dispatch_skill_and_callback_behavior_unchanged(native, monkeypatch, lane):
    monkeypatch.setattr(policy, "select_required_policy", lambda: None)
    task = make_task(native, lane, claimed=False, skills=["domain"])
    seen = []

    def old_two_argument_callback(claimed, workspace):
        seen.append((claimed.id, tuple(claimed.skills), workspace))

    result = kb.dispatch_once(native.conn, max_spawn=1, spawn_fn=old_two_argument_callback)
    skills = ("domain", "sdlc-review") if lane == "review" else ("domain",)
    assert seen == [(task.id, skills, str(native.workspace))]
    assert result.spawned == [(task.id, "default", str(native.workspace))]
    assert native.provider.requests == []
    assert kb.get_task(native.conn, task.id).skills == ["domain"]


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_initial_show_and_comment_watermark_share_original_request_snapshot(native, monkeypatch, lane):
    task = make_task(native, lane)
    other = make_task(native)
    for comment_id, task_id, timestamp in [(5, task.id, 300), (7, task.id, 100),
                                            (9, task.id, 200), (500, other.id, 400)]:
        native.conn.execute(
            "INSERT INTO task_comments(id, task_id, author, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (comment_id, task_id, "fixture", f"comment {comment_id}", timestamp),
        )
    native.conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (task.id, other.id))
    native.conn.execute(
        "INSERT INTO task_events(task_id, kind, payload, created_at, run_id) VALUES (?, ?, ?, ?, ?)",
        (task.id, "fixture", '{"nested": {"values": [1, 2]}}', 1_700_000_000, task.current_run_id),
    )
    native.conn.commit()
    with request_for(native, task, lane) as request:
        persist(native, request)
        captured = request.capture_database_context()
        show = captured.initial_show
        data = json.loads(show.response_json)
        assert data["worker_context"] == captured.text
        assert data["task"]["id"] == task.id
        assert data["task"]["current_run_id"] == request.claim.run_id
        assert data["children"] == [other.id]
        assert show.child_ids == (other.id,)
        assert show.parent_ids == ()
        assert show.comment_ids == (7, 9, 5)
        assert show.comment_watermark == 9
        assert [item["body"] for item in data["comments"]] == ["comment 7", "comment 9", "comment 5"]
        assert show.run_ids == (request.claim.run_id,)
        assert show.event_ids == tuple(event.id for event in kb.list_events(native.conn, task.id)[-50:])
        # The explicit marker sees same-second/newer-ID notes even when their
        # display timestamps precede comments in the initial view.
        with monkeypatch.context() as clock:
            clock.setattr(kb.time, "time", lambda: 200)
            added = kb.add_comment(native.conn, task.id, author="fixture", body="next note")
        assert [comment.id for comment in kb.list_comments_after(
            native.conn, task.id, after_id=show.comment_watermark,
        )] == [added]
        assert kb.list_comments(native.conn, task.id)[-1].id == 5
        assert captured.initial_show is show
        assert "next note" not in show.response_json
        data["events"][0]["payload"]["nested"]["values"].append("mutated decoded copy")
        assert "mutated decoded copy" not in show.response_json
    with pytest.raises(FrozenInstanceError):
        show.comment_watermark = 500


def test_empty_comment_watermark_is_zero(native):
    task = make_task(native)
    with request_for(native, task) as request:
        persist(native, request)
        captured = request.capture_database_context()
        assert captured.initial_show.comment_ids == ()
        assert captured.initial_show.comment_watermark == 0


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_initial_show_retains_parent_and_exact_displayed_event_tail_ids(native, lane):
    task = make_task(native, lane)
    parent = make_task(native)
    native.conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (parent.id, task.id))
    for index in range(64):
        native.conn.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at, run_id) VALUES (?, ?, ?, ?, ?)",
            (task.id, f"fixture_{index}", "{}", 1_700_000_000 + 64 - index, task.current_run_id),
        )
    native.conn.commit()
    with request_for(native, task, lane) as request:
        persist(native, request)
        captured = request.capture_database_context()
        show = captured.initial_show
        data = json.loads(show.response_json)
        events = kb.list_events(native.conn, task.id)
        assert len(events) > 50
        assert len(show.event_ids) == len(data["events"]) == 50
        assert show.event_ids == tuple(event.id for event in events[-50:])
        assert show.event_ids != tuple(sorted(show.event_ids))
        assert [event["kind"] for event in data["events"]] == [event.kind for event in events[-50:]]
        assert show.parent_ids == (parent.id,)
        assert data["parents"] == [parent.id]


def test_initial_show_reuses_captured_text_without_another_clock_or_renderer(native, monkeypatch):
    task = make_task(native)

    def forbidden(*args, **kwargs):
        raise AssertionError("initial show rebuilt the captured display")

    with request_for(native, task) as request:
        persist(native, request)
        monkeypatch.setattr(kb, "build_worker_context", forbidden)
        captured = request.capture_database_context()
        assert json.loads(captured.initial_show.response_json)["worker_context"] == captured.text


def test_initial_show_readers_hold_original_transaction_and_block_other_writer(native, monkeypatch):
    task = make_task(native)
    seen = []
    with contextlib.closing(sqlite3.connect(native.path, timeout=0)) as other:
        for name in ("list_events", "parent_ids", "child_ids"):
            original = getattr(kb, name)

            def read(conn, task_id, _original=original, _name=name):
                assert conn is native.conn and conn.in_transaction
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute(
                        "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                        (task.id, "fixture", "must not slip between reads", 1),
                    )
                other.rollback()
                seen.append(_name)
                return _original(conn, task_id)

            monkeypatch.setattr(kb, name, read)
        with request_for(native, task) as request:
            persist(native, request)
            captured = request.capture_database_context()
            assert captured.initial_show.comment_ids == ()
    assert seen == ["list_events", "parent_ids", "child_ids"]


def test_missing_initial_view_cancels_without_publishing_context(native, monkeypatch):
    task = make_task(native)
    with refusal():
        with request_for(native, task) as request:
            persist(native, request)
            monkeypatch.setattr(kb, "collect_task_show", lambda *args, **kwargs: None)
            request.capture_database_context()
    assert request.cancelled and request.database_context is None


def test_initial_show_collection_cannot_reenter_capture(native, monkeypatch):
    task = make_task(native)
    with refusal():
        with request_for(native, task) as request:
            persist(native, request)
            monkeypatch.setattr(kb, "collect_task_show", lambda *args, **kwargs: request.capture_database_context())
            request.capture_database_context()
    assert request.cancelled and request.database_context is None
    assert not native.conn.in_transaction


def test_data_only_initial_show_preserves_caller_rollback_and_retains_no_connection(native):
    task = make_task(native)
    native.conn.execute("BEGIN IMMEDIATE")
    native.conn.execute("UPDATE tasks SET result = 'pending result' WHERE id = ?", (task.id,))
    captured = kb.collect_task_show(native.conn, task.id)
    assert native.conn.in_transaction
    assert json.loads(captured.response_json)["task"]["result"] == "pending result"
    native.conn.rollback()
    assert kb.get_task(native.conn, task.id).result is None
    native.conn.close()
    assert json.loads(captured.response_json)["task"]["result"] == "pending result"
    for item in fields(captured):
        value = getattr(captured, item.name)
        assert type(value) in (str, int, tuple)
        if type(value) is tuple:
            assert all(type(entry) in (str, int) for entry in value)
