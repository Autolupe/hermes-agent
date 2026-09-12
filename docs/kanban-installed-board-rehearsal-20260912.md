# Database upgrade and rollback compatibility rehearsal

The native candidate at `c46bc470f50121213182734f0d1a8ea9f3772e58` initialized
a private SQLite backup of the installed board successfully. The currently
installed source at `3a170fb835bda574bda5c44afabae61c94f34cd3` then reopened a
copy of that upgraded database successfully. Neither operation changed the
live board.

## Results

- All 32,421 existing rows across 10 tables retained their original values.
- All 1,174 task rows were preserved: 985 non-archived and 189 archived.
- All 1,730 runs, 25,107 events, 2,291 comments, 1,947 links and six delivery
  operation rows were preserved. Other existing tables were compared too.
- The candidate added `tasks.worktree_base_sha` and the single-row
  `kanban_board_identity` table.
- A second candidate initialization made no further changes.
- Installed-source initialization preserved the upgraded database, including
  its added column and identity row. Repeating it made no changes.
- Both databases passed `PRAGMA integrity_check`; both had zero foreign-key
  violations.

## Method and retained evidence

The live source was opened using a SQLite `mode=ro` URI. SQLite's backup API
created the private copy; no raw database-file read or live initializer was
used. The rehearsal directory is mode 0700 and database copies are mode 0600.

The one-off harness ran each source version with a clean environment, private
`HOME`, `HERMES_HOME` and `HERMES_KANBAN_HOME`, an explicit source `PYTHONPATH`,
and bytecode writes disabled. It called the real `kanban_db.init_db`, compared
sorted row hashes over every original column, and repeated initialization.
A Python audit hook refused subprocess/network execution and SQLite access
outside the private rehearsal directory. The reports contain row counts and
hashes, not task text or claim contents.

Evidence directory:
`/home/ab/.local/state/kanban-sequential-20260907/installed-board-rehearsal/`

`validation.json` binds both source commits, results, harness and log hashes.
`result.json` records upgrade results; `rollback/result.json` records installed
source compatibility. `run_rehearsal.py` retains the exact local harness.
Both processes exited zero. No provider call or Telegram message was sent.

## Limits

This closes the database compatibility rehearsal for the observed snapshot.
It does not prove service rollback, compatibility after future lifecycle
changes, runtime admission, credential handling or worker execution. In
particular, older source does not acquire the candidate's newer enforcement
rules merely because it can read the new schema.

Protected bootstrap, original-request trusted completion and supported live
installation remain unfinished. Card `t_279a15c7` stays open. The frozen source
handoff remains `c46bc470f5`; this report introduces no runtime code change.
