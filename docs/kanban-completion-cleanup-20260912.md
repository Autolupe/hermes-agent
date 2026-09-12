# Preserve newer task work during completion cleanup

An old completion or archive call could reread a task's current workspace after
committing its result and delete files belonging to a newer attempt. Deferred
parent cleanup had the same problem. This patch preserves those files when the
task, attempt, terminal event, workspace assignment, or directory identity has
changed.

The terminal transaction captures the task and linked parent cleanup state.
Cleanup compares that state under a new SQLite write transaction before
unlocking or removing the workspace. Cooperating board writers cannot replace
the assignment during removal. Parent cleanup also requires the captured parent
to remain terminal, with no active child, held worker, or delivery operation.
Caller-owned transactions are refused without rolling back the caller's work.
Cleanup failure does not undo the committed completion or archive result.

## Validation

Run from the Hermes candidate checkout:

```sh
scripts/run_tests.sh -j 2 --file-retries 0 \
  tests/hermes_cli/test_kanban_completion_cleanup_identity.py \
  tests/hermes_cli/test_kanban_db.py \
  tests/hermes_cli/test_kanban_worktree_teardown.py

scripts/run_tests.sh -j 2 --file-retries 0 \
  tests/hermes_cli/test_kanban_failure_reset_identity.py \
  tests/hermes_cli/test_delivery_control.py \
  tests/hermes_cli/test_kanban_delivery_gate.py \
  tests/hermes_cli/test_kanban_delivery_modern_fences.py \
  tests/hermes_cli/test_kanban_board_identity.py \
  tests/hermes_cli/test_kanban_native_edit_fences.py \
  tests/hermes_cli/test_kanban_delivery_writer_fences.py
```

Results: **369 tests passed across 10 files; one Windows-only test skipped on
Linux; no failed tests or retries.** The 15 new cases use disposable databases
and real temporary directories. They cover successor runs and assignments,
successors already completed, directory replacement at the same path, reopened
and held parents, parent directory replacement, actual competing SQLite writers,
normal removal, and preservation of caller-owned transactions. Ruff and
`git diff --check` passed for the scoped changes.

## Limits

The write lock remains held during cleanup and can delay other board writes.
Existing Git and tmux command timeouts remain in effect; recursive filesystem
removal has no new total deadline. Directory identity is compared before removal;
this is not protection against an unrelated process replacing filesystem paths
after the comparison. Existing managed-path, dirty-worktree, and unpublished
commit guards remain in place.

This is source validation for `t_279a15c7`, following `4fb8f76919` and the frozen
`c46bc470` source handoff. It does not install a runtime, authorize a worker,
release dispatch, change a live card, or establish live completion evidence.
No model calls or Telegram messages were used. The card remains open until the
matching supported installation and disposable live-task proof are complete.
