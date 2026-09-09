# Held Kanban worker mechanics

This source-only helper can hold a real child before it executes a command,
record its ownership, and cancel its descendant tree. It does not enable native
required dispatch. The ordinary dispatcher is unchanged, and
`require_supported_worker_launch` still refuses both required dispatch lanes,
including default and injected worker callbacks.

The internal `kanban_held_worker.HeldWorker` is preparation for the existing
required-policy interface. There is no user-facing command, environment flag,
configuration override, successful base provider or saved approval input.
Tests supply a preparation provider and harmless commands; no model, credential
or remote delivery operation is part of the implementation or its tests.

## One original request and one held child

The helper retains the actual `WorkspaceRequest` and its original SQLite
connection. That request binds the task, run, claim lock, board, dispatch lane,
process/thread/account, policy generation and instruction fields. The helper
also captures the workspace directory's device/inode and a command digest.
These are local consistency checks, not proof of the database file SQLite
opened or a complete immutable worker prompt.

The sequence is:

1. Write and sync a private journal. Commit a negative `controlled_worker_held`
   event for the exact run **before creating any child**. An interrupted PID
   transaction therefore cannot falsely imply that no child exists.
2. Hold the original SQLite write transaction while a fresh supervisor creates
   its child. The child waits on a private pipe and cannot execute the command.
   The supervisor reports the actual child PID, start ticks, boot, account and
   held workspace identity. Record the supervisor PID in both task and run rows,
   plus the original child identity in an event; commit and sync the journal.
3. For release, hold the original connection's write transaction again. Recheck
   the live request, exact claim/run/workspace, both recorded PID values and the
   original still-owned process identities. Record `release_uncertain` durably,
   recheck, and exchange a one-use release message and acknowledgement while
   the write transaction remains held. A cooperating SQLite reclaimer cannot
   replace the claim across this release edge.
4. Continue checking the original request while waiting. Cancellation is sticky
   and is set before sending cleanup. A later request, stale message, changed
   enrollment, expired claim or lost acknowledgement cannot revive the handle.
5. Treat process exit and descendant cleanup separately. Even exit code zero
   leaves the board task running. Only this live handle's validated drained
   response permits its exact PID records and negative fence to be cleared.
   This operation does not complete, release or authorize the task.

The private journal is synced and replaced atomically. It records preparation,
held, release-uncertain, released, drained or uncertain states. It contains no
command arguments, prompt, environment or credentials. It is audit/negative
state only: the helper never reads it back as permission or a cleanup receipt.
Closing a request does not make an uncertain result successful. Cancellation and
close attempt both the held-worker owner and the preparation provider even if
one cleanup fails; an existing primary error is retained.
The native write-transaction helper also rolls back on interrupts, including
an acquired transaction, inner savepoint or commit, without replaying the write body.
Cleanup errors do not replace the original refusal or interrupt. A rollback
which itself cannot complete is still uncertainty, not proof of cleanup.

## Descendant ownership and its limits

The isolated, single-threaded Linux supervisor uses the documented
`prctl(PR_SET_CHILD_SUBREAPER)` interface. Children that double-fork or change
session are adopted when their parents exit. Cleanup signals only currently
owned direct children, keeps them unreaped until after identity checks and
signals, and repeatedly drains newly adopted children. A final `ECHILD` result
from the kernel establishes that this supervisor owns no remaining descendants.
It never signals a PID recovered from a journal or a process-group scan.

The original child gets no control socket or release descriptor after `exec`.
The supervisor watches the private parent channel and a bounded deadline.
Normal caller-channel loss and termination signals cause cleanup. The parent
does not report drained merely because the supervisor disappeared. If the
supervisor is forcibly killed, inspection fails, the result is lost, or cleanup
exceeds its bound, the original claim and workspace remain fenced. This is not
containment against privileged interference, an arbitrary same-account process,
kernel failure, or a task that obtains an external service to work for it.

The helper currently supports Linux only, at most 64 arguments of 4096
characters each, a 256 KiB protocol frame, a 300-second held/running process
budget plus a five-second cleanup budget, and at most 4096 direct children per
inspection. Limits refuse; they do not
silently discard children or report partial cleanup as complete. Time spent in
SQLite locking or durable filesystem writes is not a guaranteed wall-clock
deadline. A future protected runner must impose its own outer deadline.

## The ordinary reclaimer cannot clear uncertainty

The negative board event blocks fresh ready/review claims, TTL/manual/crash/
maximum-runtime/stale-heartbeat/orphan reclaim, required workspace compensation
and ordinary completion while unresolved. Common run-ending and workspace cleanup paths also refuse or
preserve it. This applies even if the recorded supervisor PID is absent or now
belongs to another process. An unchanged ordinary task has no such event and
retains its existing behavior.
The stale-heartbeat and orphan passes skip unresolved workers and continue
processing ordinary tasks; uncertainty in one task does not stop the board pass.

There is deliberately no crash-recovery operation that closes a pending fence
from saved JSON or a PID-only liveness check. The current helper's final live
response and exact native bookkeeping comparison are the only clearing path.
Future protected recovery must prove descendant ownership/quiescence before
releasing any interrupted request. Arbitrary direct database writes are not a
security boundary; deployed access control and protected admission are still
required.

## Remaining production dependencies

This is not an atomic protected-admission-to-execution guarantee. Production
still needs the actual-open SQLite capability, protected positive launch
provider, complete instruction/configuration snapshot, continuing request and
lease coordination, protected recovery for supervisor loss, and trusted
completion bound to the original run and delivery evidence. Existing ordinary
`complete_task` behavior after a drained fence is not that completion interface.
No Authority service, account migration, registry installation or live change is
included. The fixed launch gates must remain unsupported until those pieces are
integrated and proved together.

Negative fixtures explicitly reject an assertion failure or an unavailable
test API wrapped by the native request's error handling. Such an exception is
not an expected policy refusal. The held-child death control uses libc's actual
process-handle APIs because this Python build omits their `os`/`signal` wrappers;
those calls exist only in tests, never as a production success override.

Run the actual-process fixtures through the canonical wrapper:

```sh
scripts/run_tests.sh -j 2 --file-retries 0 \
  tests/hermes_cli/test_kanban_held_worker.py \
  tests/hermes_cli/test_kanban_required_workspace.py -q
```
