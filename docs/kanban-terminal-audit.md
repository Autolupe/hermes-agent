# Measure terminal outcomes after installation

Use the installed Python environment and source tree. Capture a baseline only
after the terminal-reconciliation fix has actually been installed:

```sh
python -m hermes_cli.kanban_terminal_audit --database /path/to/kanban.db
```

Keep the returned `database` and `through_event_id` with the installation
receipt. To inspect later activity on that same board, supply that event ID:

```sh
python -m hermes_cli.kanban_terminal_audit --database /path/to/kanban.db --since-event-id 123
```

Replace `123` with the actual saved ID. The default command captures a baseline
without presenting historical counts as post-fix evidence. The counter reads
one consistent SQLite snapshot, opens only an existing database, and performs
no migration, dispatch, model call or notification. It never prints event
payloads or task bodies. An unavailable board or a baseline ahead of the current
history fails instead of returning a healthy-looking zero.

`terminal_reconciled` counts clean worker exits that needed reconciliation.
The report separately counts completion, block, crash, timeout and rate-limit
events. It also flags reconciliation records without a matching task/run and
repeated reconciliations of the same attempt, including repetitions of an event
before the baseline. These are event counters, not independently verified
delivery results or proof of a lower failure rate. A restored or different
board requires checking its history and recording a new appropriate baseline.
