# Keep failure resets with the exact completed or reclaimed attempt

Completion and manual recovery previously reset `consecutive_failures` and
`last_failure_error` in a separate transaction after committing their task/run
transition. That reset used only the task ID. A successor could start between
the two writes and have its failure erased by the older call. An exception in
the intervening post-commit work could instead leave the old failure budget
attached to a successfully completed or reclaimed task.

Both resets now occur in the existing guarded task UPDATE, inside the same
transaction as the exact run transition and event. A refused or rolled-back
transition preserves the old counters. A successor's changes after commit are
preserved. Event history is unchanged. The standalone compatibility reset
helper remains available; these two lifecycle paths no longer call it.

Six new tests use real temporary SQLite databases. A second connection starts
a successor during the old call's post-commit work and checks that its complete
task row survives. Other cases cover post-commit errors and transaction rollback
for both completion and manual reclaim.

Validation covers 320 distinct passing tests across eight files, with one
Windows-only test skipped. The first seven-file boundary run had 313 passes
and one failure in an existing cooldown fixture. That fixture set the review
exit time before setup, allowing a later second boundary to make the older
builder appear newer. Freezing the setup clock fixes its ordering; all 57 tests
in that file then passed. The six new cases passed separately. No retries were
enabled. Ruff and whitespace checks pass.

Exact commands, file lists and logs are retained under
`/home/ab/.local/state/kanban-sequential-20260907/context-integration-evidence/`
as `failure-reset-*` artifacts. The earlier failure log is preserved.

This is a follow-up to frozen runtime source `c46bc470f5`, not a replacement
for the unimplemented long-running owner and startup design. Paired integration
must retain this fix in addition to that frozen source. No schema, installed
source, board lifecycle, service, provider or messaging state was changed.
The first card remains unfinished.
