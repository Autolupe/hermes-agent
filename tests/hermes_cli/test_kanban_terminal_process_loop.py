"""A real child exit is reconciled once by a real dispatcher tick."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb


@pytest.mark.linux_only
def test_real_child_exit_is_blocked_once_without_respawn(tmp_path, monkeypatch):
    home = tmp_path / 'hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(kb, '_recent_worker_exits', {})
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title='Harmless process proof', assignee='default')
        assert kb.claim_task(conn, tid)
        run_id = kb.latest_run(conn, tid).id
        child = subprocess.Popen([sys.executable, '-I', '-c', 'pass'])
        try:
            kb._set_worker_pid(conn, tid, child.pid)
            # Observe exit without consuming wait status; the dispatcher owns it.
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
            first = kb.dispatch_once(conn, max_spawn=0)
            assert first.terminal_reconciled == [tid]
            assert first.crashed == [] and first.spawned == []
            task = kb.get_task(conn, tid)
            assert task.status == 'blocked' and task.block_kind == 'capability'
            run = kb.latest_run(conn, tid)
            assert run.id == run_id and run.outcome == 'blocked'
            assert run.ended_at is not None
            assert child.pid not in kb._recent_worker_exits
            second = kb.dispatch_once(conn, max_spawn=0)
            assert second.terminal_reconciled == [] and second.spawned == []
            events = [e for e in kb.list_events(conn, tid) if e.kind == 'terminal_reconciled']
            assert len(events) == 1 and events[0].run_id == run_id
        finally:
            child.wait(timeout=5)
