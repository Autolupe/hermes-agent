"""Task-bound CLI calls cannot mutate a different or superseded run."""
import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def worker(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID', raising=False)
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title='CLI identity', assignee='worker')
        assert kb.claim_task(conn, tid)
        run = kb.latest_run(conn, tid)
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run.id))
    return tid, run.id


def invoke(words):
    wrap = argparse.ArgumentParser()
    parser = cli.build_parser(wrap.add_subparsers())
    return cli.kanban_command(parser.parse_args(words))


def snapshot():
    with kb.connect_closing() as conn:
        return {table: [tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY id')]
                for table in ('tasks', 'task_runs', 'task_events', 'task_comments')}


def command(action, tid):
    return {
        'complete': ['complete', tid, '--result', 'finished'],
        'block': ['block', tid, 'cannot continue', '--kind', 'capability'],
        'schedule': ['schedule', tid, 'wait'],
        'heartbeat': ['heartbeat', tid],
        'request-review': ['request-review', tid, '--summary', 'ready'],
        'request-changes': ['request-changes', tid, 'fix this'],
    }[action]


@pytest.mark.parametrize('action', ['complete', 'block', 'schedule', 'heartbeat', 'request-review', 'request-changes'])
@pytest.mark.parametrize('identity', [None, '', 'broken', '0', '-1', 'stale'])
def test_invalid_run_has_no_effect(worker, monkeypatch, action, identity):
    tid, run_id = worker
    if identity is None:
        monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
    else:
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run_id + 99) if identity == 'stale' else identity)
    before = snapshot()
    monkeypatch.setattr(cli, '_goal_mode_handoff_rejection', lambda *a: pytest.fail('invalid attempt reached judge'))
    assert invoke(command(action, tid)) == 1
    assert snapshot() == before


@pytest.mark.parametrize('action', ['complete', 'block', 'schedule'])
def test_bulk_worker_call_rejects_before_first_write(worker, action):
    tid, _ = worker
    with kb.connect_closing() as conn:
        sibling = kb.create_task(conn, title='sibling')
    words = ['complete', tid, sibling] if action == 'complete' else [action, tid, 'reason', '--ids', sibling]
    before = snapshot()
    assert invoke(words) == 1
    assert snapshot() == before


@pytest.mark.parametrize('action', ['complete', 'block', 'schedule', 'heartbeat', 'request-review', 'request-changes'])
def test_foreign_task_call_has_no_effect(worker, action):
    with kb.connect_closing() as conn:
        sibling = kb.create_task(conn, title='sibling')
    before = snapshot()
    assert invoke(command(action, sibling)) == 1
    assert snapshot() == before


@pytest.mark.parametrize('action, expected', [('complete', 'done'), ('block', 'blocked'), ('schedule', 'scheduled'), ('heartbeat', 'running')])
def test_valid_worker_call(worker, action, expected):
    tid, _ = worker
    assert invoke(command(action, tid)) == 0
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == expected


def test_stale_block_does_not_leave_comment(worker, monkeypatch):
    tid, _ = worker
    monkeypatch.setattr(kb, 'block_task', lambda *a, **k: False)
    before = snapshot()
    assert invoke(command('block', tid)) == 1
    assert snapshot() == before


@pytest.mark.parametrize('words', [
    ['claim', '{task}'], ['archive', '{task}'], ['unblock', '{task}'],
    ['gc'], ['dispatch', '--dry-run'], ['boards', 'show'],
])
def test_worker_cannot_use_operator_lifecycle_routes(worker, monkeypatch, words):
    tid, _ = worker
    before = snapshot()
    monkeypatch.setattr(kb, 'init_db', lambda *a, **k: pytest.fail('operator command reached database init'))
    assert invoke([word.format(task=tid) for word in words]) == 1
    assert snapshot() == before


def test_worker_cannot_switch_board(worker, monkeypatch):
    tid, _ = worker
    before = snapshot()
    monkeypatch.setattr(kb, 'board_exists', lambda *a: pytest.fail('foreign board inspected'))
    assert invoke(['--board', 'foreign', 'heartbeat', tid]) == 1
    assert snapshot() == before


def test_operator_can_complete_without_worker_identity(worker, monkeypatch):
    tid, _ = worker
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
    assert invoke(command('complete', tid)) == 0


def test_run_replaced_during_judge_is_not_completed(worker, monkeypatch):
    tid, _ = worker
    def replace_run(*args):
        with kb.connect_closing() as conn:
            assert kb.block_task(conn, tid, reason='replace old attempt', kind='capability')
            assert kb.unblock_task(conn, tid)
            assert kb.claim_task(conn, tid)
        return None
    monkeypatch.setattr(cli, '_goal_mode_handoff_rejection', replace_run)
    assert invoke(command('complete', tid)) == 1
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == 'running'
        assert kb.latest_run(conn, tid).ended_at is None


@pytest.mark.parametrize('action, tag', [('block', 'BLOCKED'), ('schedule', 'SCHEDULED')])
def test_successful_operator_retains_authored_note(worker, monkeypatch, action, tag):
    tid, _ = worker
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
    assert invoke(command(action, tid)) == 0
    with kb.connect_closing() as conn:
        comments = kb.list_comments(conn, tid)
        assert len(comments) == 1
        assert comments[0].body.startswith(tag + ':')


def test_malformed_board_returns_normal_cli_error(worker):
    tid, _ = worker
    before = snapshot()
    assert invoke(['--board', '../bad', 'heartbeat', tid]) == 1
    assert snapshot() == before
