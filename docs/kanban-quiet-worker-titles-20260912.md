# Skip automatic chat titles for board workers

Board workers enter Hermes through the CLI, but the dispatcher records their
session source as `kanban`. The turn prologue previously checked only the CLI
platform. It could therefore start an auxiliary model call to name a worker
session that is hidden from chat lists and already has a named board task.

The prologue now checks the existing session source bridge before entering the
title generator. Kanban, cron and delegated-worker sources skip title settings,
the title thread and title writes. Session-local bindings override the process
environment, including an explicitly empty binding, so other chats sharing a
gateway retain their normal title behavior. No setting or model tool was added.

Validation: 72 tests passed across these four files, with no retries or skips:

- `tests/agent/test_turn_context.py`
- `tests/agent/test_title_generator.py`
- `tests/run_agent/test_session_source.py`
- `tests/hermes_cli/test_kanban_worker_session_source.py`

The new worker checks use the real title entry point and a temporary SQLite
session database. They verify no title configuration read, no title thread and
an unchanged session row. Companion checks cover bound worker sessions without
a process marker, and normal bound chats with a worker marker in the process.
Existing dispatcher tests verify that worker launch supplies the source marker.
Ruff and `git diff --check` pass.

This is a source change, not installed behavior or measured token savings.
No provider call, Telegram message, service change or board lifecycle transition
was performed. The first selected card, `t_279a15c7`, remains unfinished:
protected startup, the original worker's trusted completion connection and
verified installation still need completion. This small efficiency fix does not
grant worker launch authority or satisfy those missing requirements.
