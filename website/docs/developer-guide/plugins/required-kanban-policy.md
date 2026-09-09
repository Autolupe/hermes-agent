# Required Kanban workspace requests

This source-only interface makes an administrator-selected workspace policy
mandatory. It does not install a provider or enable autonomous work. The base
provider refuses every request, and controlled worker launch remains unsupported
even when an external provider approves workspace preparation.

Ordinary unenrolled Hermes keeps its existing workspace behavior: best-effort
fetch, the shared 60-second attempt cache, fallback to `HEAD`, existing-worktree
reuse and older worker callback signatures. Callback exceptions are no longer
mistaken for signature-inspection failures: a callback is invoked only once.

## Fixed enrollment, no user override

The POSIX selector uses only
`/etc/hermes-required-kanban-policies.d/uid-N.toml`, with protected root-owned
ancestry, registration and package sources. Keep that registry publicly
traversable when an administrator eventually installs it. It is deliberately
independent of the private legacy `/etc/hermes` tree. This change is to an
uninstalled source contract; it does not migrate or change any live directory.

Only genuine initial absence selects ordinary behavior. Unreadable or unsafe
ancestry, changed enrollment, changed account identity and disappearing selected
sources refuse the operation. No environment variable, user configuration,
command-line flag or optional plugin hook can remove an obligation.

## One request belongs to one claimed run

`kanban_db.required_workspace_request` takes the original open SQLite connection
and explicit task ID, run ID and claim lock. It also binds the board, lane,
process/thread/account identity, selected policy generation and a fresh native
request ID. The claim record is immutable; it does not retain a mutable `Task`
as its authority. Invalid or missing run/claim bindings refuse without attempting
task-ID-only cleanup.

The private request context carries that same connection into direct, public and
internal workspace calls. Enrolled helpers refuse calls without it. The context
does not reopen the database by pathname or serialize an approval record.
Neither a pathname, `PRAGMA database_list`, a Python object identifier nor a copy
of JSON proves the file SQLite actually opened. A future supported capability
adapter must obtain that proof from this exact connection and invalidate it on
close, replacement or backend change. No such adapter is supplied here.

The request freezes task instruction/routing values including title, body,
project, creator, workflow/step, assignee, tenant, workspace kind, skills,
model/provider/reasoning overrides and goal/runtime limits. These values remain
private in-process data. Workspace path and branch advance only through checked
persistence. This is not a complete worker-prompt snapshot: comments, parent
results and live profile configuration are additional dependencies of a future
controlled-launch contract. They cannot be treated as approved by this slice.

Cancellation is native-owned and irreversible. Native state becomes cancelled
before provider cleanup runs. Cleanup failure, a copied context, request B or
recovery of an external runner's negative fence cannot reactivate request A.
Closing the scope ends its authority. A provider close failure is a refusal and
causes exact-claim compensation; an existing primary error is preserved.

## Direct provider interface

`RequiredKanbanPolicy.open_workspace_request(request)` must return a live
`RequiredWorkspaceAdmission`. The base implementation raises
`RequiredPolicyError` with an unsupported result. A dictionary, boolean, marker,
command name or registration generation is not admission.

The live admission implements:

- `checkpoint(boundary, observation)`: return `None` only after the protected
  provider has made the required fresh checks; raise to refuse. Native request,
  account, claim and run checks occur before and after the call.
- `cancel(reason)`: release resources after native cancellation is already set.
- `close()`: finish resource cleanup; failure cannot become successful completion.

The external provider owns positive admission, transport and actual-open source
proof. It is not shipped here. Checkpoint observations are immutable descriptions
of the native operation, not filesystem or authorization proofs. Provider calls
do not run through the optional lifecycle observers, whose errors intentionally
remain non-blocking.

## Covered native paths

Both ready/builder and review dispatch lanes use one shared required-request
helper. The manual claim command uses its actual open connection and cannot use
`--force` to bypass this requirement. Public/internal resolvers, worktree
materialization, existing-worktree reuse, base selection, workspace/branch
setters, PID bookkeeping and default/injected spawn boundaries check the request.

Required fetch does not read or populate the ordinary attempt cache. Nonzero
exit, timeout and unavailable `origin/main` cancel the request; required work does
not fall back to `HEAD`. Existing-worktree reuse still needs fresh approval and
cannot borrow a prior request's permission. Reusing an unrelated branch through
the ordinary legacy fallback is also refused.

Workspace writes compare the exact task, run and claim, including the previous
path/branch, inside the existing write transaction. Checks run after acquiring
the transaction, before commit and after commit. PID bookkeeping likewise
compares both task and run rows; it is not permission to start a process.

On refusal, compensation restores only the exact introduced pointers and event
records. Existing rollback helpers remove only the unchanged directory/worktree
introduced by this request. Changed or uncertain state is preserved. Cleanup
never releases a replacement claim or a run with a recorded worker.

If enrollment appears after an attempt began as ordinary Hermes, the later
boundary refuses and bypasses unbound generic spawn-failure handling. Prepared
artifacts are preserved across that transition rather than assuming the new
policy approved their ownership.

Once an ordinary worker callback has been entered, a later policy refusal cannot
be treated as proof that no worker started. A callback may start a child and then
raise without returning its PID. In that uncertain case the original claim and
workspace are preserved and dispatch reports the uncertainty, not successful
admission. No process is killed by an unproved PID. Compensation also preserves
workspaces when either the task row or its exact run row records a worker.
This immediate preservation is not a durable worker/cancellation receipt and
does not prove safety against later lease expiry or reclaim. The reclaimer is
unchanged. Actual required controlled launch remains unsupported.

## Worker launch remains unsupported

The native launch check always refuses enrolled work. A preparation provider,
custom callback signature or test-like callback cannot enable either default or
injected launch. The check is also present at the default spawn entry and final
process edge. The ordinary command-building and environment behavior is unchanged.

Before/after checks cannot make SQLite claim validation atomic with `Popen`.
The separate [held-worker mechanics](./held-kanban-worker.md) now test an actual
held child and cancellation with temporary boards. They do not replace this
unsupported production gate. A positive contract still needs protected launch
admission, actual-open database identity, a complete frozen worker instruction
bundle, credential transport and trusted task completion. Neither the mechanics
nor a saved journal may be presented as completed activation.

## Verification boundaries

Tests import native Hermes normally and use temporary SQLite databases and real
local Git repositories. Fixture providers exist only in tests. The retained
seven ordinary baseline observations include real 15-second fetch timeouts; the
required tests cover refusal, reuse, direct/manual calls, immutable claims,
sticky cancellation, write/close failures, compensation and unsupported launch.

The shared test fixture replaces the in-process registry with a temporary private
registry. Fresh subprocess behavior probes still honor the production selector;
their results can therefore depend on real enrollment. There is no runtime
test-success override, and an injected test bootstrap would not constitute proof
of the installed enrollment path. No protected board, credential, model call,
live worker, service change or installation is part of these tests.

Run through the canonical test wrapper, for example:

```sh
scripts/run_tests.sh -j 3 \
  tests/hermes_cli/test_kanban_required_policy.py \
  tests/hermes_cli/test_kanban_required_workspace.py \
  tests/hermes_cli/test_kanban_unenrolled_fetch_baseline.py \
  tests/hermes_cli/test_kanban_pause_boundary.py -q
```
