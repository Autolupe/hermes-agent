# Kanban dispatch and worktree repair

This source candidate prevents two boards from admitting workers against the same free slot, refuses conflicting checkouts, and records the exact commit used to create a task branch. It addresses prerequisite card `t_11d4a9d6`. Publication does not activate workers or complete the board's delivery checks.

## Source and scope

| Component | Immutable source base | Candidate branch |
| --- | --- | --- |
| Native Hermes | `3a170fb835bda574bda5c44afabae61c94f34cd3` | `hermes/runtime/t_11d4a9d6` in `Autolupe/hermes-agent` |
| Existing ClausEye Git wrapper companion | `e280a3b6ae0f1f5ca8178705414e659aa9645a53` | `hermes/clauseye/t_11d4a9d6-git` in `clauseye-com/clauseye-contra-rope` |

The card originally described Node changes in one application worktree. Source inspection located dispatch and checkout ownership in native Python Hermes. One isolated native worktree was created from the exact base above. The separate application worktree only changes its existing Git wrapper, tests, and compatibility document to accept token-free remote-name discovery, an exact ancestry check, and the new fixed fetch mapping. No dispatcher is added to the application.

Live service metadata and the home virtual environment's editable-package mapping selected the home Hermes source for gateway and Serve. This identifies their selected package path; it does not inspect module bytes already loaded into process memory. The separate installed Python 3.13 worker modules match `339a5398dedc7d60b8da1a3783d132227297072a`. That implementation already requires protected controller receipts and task base refs. Neither source version may replace the other wholesale. This candidate's `worktree_base_sha` is creation metadata, not a trusted controller receipt or independent delivery approval.

## Behavior

- Cooperating dispatchers sharing one Hermes home acquire a common lock before counting workers and admitting new ones. Existing board locks, retry limits, pause checks, and memory limits remain in use. Unavailable locks or unreadable board counts refuse admission. Separate Hermes homes and unrelated processes are outside this lock's scope.
- New worktrees use an explicit full commit ID, freshly fetched main, or local main when the repository has no origin. Remote refresh failure cannot fall back to stale main or the current feature branch. Successful refreshes cache the selected commit for up to 60 seconds; failed refreshes cool down for 60 seconds.
- The fetch maps `+refs/heads/main:refs/remotes/origin/main` explicitly. The application companion admits exactly that spelling through the existing private staging and credential-revocation path. It also permits the native caller's exact read-only `git -C <absolute-directory> remote` name listing and `merge-base --is-ancestor <full-commit-id> refs/heads/hermes/...` check without credentials. Other mappings and remote mutations remain rejected. Native refresh still has its existing 15-second timeout; interrupted wrapper cleanup is a separate unresolved lifecycle boundary.
- `kanban create --base-sha` accepts a nonzero lowercase 40-character commit ID. The actual commit must exist before checkout creation. Selected bases are saved before dispatcher spawn. A retry must retain the same base in the task branch's history. Existing unpinned legacy branches keep an unknown historical base.
- Existing linked worktrees must belong to the requested branch. Conflicts retain useful path, branch, and owner details in local run/event evidence. A stale task pointer cannot authorize removal of another branch's checkout. Pause compensation restores the previous path, branch, and base together.

## September 12 integration

The terminal-reconciliation branch now combines this published repair (`e20108f2e0e1aa7b9f0ffbf69c5481fe7501f386`) with the worker-control and terminal source in `486e90001924ed920218800a08cec09b4143811c`. Both parent histories are retained. This includes host admission serialization, immutable worktree bases, checkout ownership checks and the earlier exact-run recovery/held-worker fences.

Required workspace requests bind the creation base alongside their existing path/branch pair. They save it with the exact task/run/claim comparison and restore all three values during compensation. A changed base cancels the request without overwriting that change. Required manual claims and direct setters use the same guard. Explicit available bases avoid fetch while still receiving the policy's base checkpoint. Required refreshes use the explicit mapping, return an immutable commit and remain independent of ordinary success/failure caches. No production launch support was enabled.

Current integration validation covers 40 files: 1,047 passed, no remaining failures, and three Windows-only skips. The first 38-file run returned 1,023 passes and four failures because its required-fetch failure simulation matched the old fetch spelling. Updating that simulation to recognize and assert the explicit mapping restored all 77 tests in that file. The other 37 files remained unchanged; ten required-base cases and ten updated ordinary-fetch cases also passed. The ordinary tests include real local timeout children, immutable cached commits, both dispatch lanes and refusal without checkout creation. These are separate runs, not one all-pass aggregate. Independent source review and exact commands/hashes are recorded under `/home/ab/.local/state/kanban-sequential-20260907/worktree-integration-evidence/`.

The separate trusted delivery contract and activation-owned context-capture work still require integration and proof before the full terminal card can close. Existing application wrapper source remains the published companion identified above. No live database schema or service changed.

## Validation evidence

The canonical native runner was used with two workers and no automatic retries:

```sh
HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh -j 2 \
  tests/hermes_cli/test_kanban_worktree_contract.py \
  tests/hermes_cli/test_kanban_cli_base_sha.py \
  tests/hermes_cli/test_kanban_workspace_failure_details.py \
  tests/hermes_cli/test_kanban_claim_guard.py \
  tests/hermes_cli/test_kanban_worktree_isolation.py \
  tests/hermes_cli/test_kanban_worktree_teardown.py \
  tests/hermes_cli/test_kanban_dispatch_lock.py \
  tests/hermes_cli/test_kanban_host_cap.py \
  tests/hermes_cli/test_kanban_host_dispatch_serialization.py \
  tests/hermes_cli/test_kanban_per_profile_cap.py \
  tests/hermes_cli/test_kanban_memory_guard.py \
  tests/hermes_cli/test_kanban_pause_boundary.py \
  tests/hermes_cli/test_kanban_db.py \
  tests/hermes_cli/test_kanban_core_functionality.py \
  tests/hermes_cli/test_kanban_cli.py \
  tests/hermes_cli/test_kanban_cli_claim_guard.py
```

The final aggregate initially returned 455 passes, one outdated expectation that unreadable board enumeration should admit work, and three Windows-only skips. That expectation was updated to require refusal and recovery; all 11 tests in its affected file then passed. Together the two runs cover 456 unique passing tests and three Windows-only skips. The publication record preserves the earlier aggregate failure rather than labeling that run as all-pass. Earlier real-Git regression controls reproduced wrong-branch reuse, stale fetch selection, and movement of a cached tracking ref before their fixes.

The application companion passed 107 focused wrapper/lifecycle tests and all 149 repository script tests, with no failures or skips. Its new mapping controls failed against the original helper. Tests use temporary local repositories and fixture credentials. The full script command required by the card was run:

```sh
node --test ./scripts/tests/*.test.mjs
```

Full commands, outputs, reviewed file hashes, formatting checks, candidate commit IDs, and remote branch verification are recorded in `/home/ab/.local/state/kanban-sequential-20260907/hermes-worktree-evidence/publication.json` and its referenced logs. Native test execution used the repository's canonical runner. An earlier mistaken `--help` invocation launched excess runner processes; those exact processes were stopped, and that invocation is excluded from validation. Gateway, Serve, and dashboard remained active in the subsequent service check.

## Remaining delivery work

Adopt the paired changes through the intended native review and integration path. Preserve the separate protected worker/controller contract, verify hosted checks on the exact integrated source, and prove a bounded worker canary before wider dispatch. The dependent provenance card `t_20b5bd72` must use the recorded candidate pair when reconciling its application changes.

No installed runtime, live database schema, services, dispatch pause, GitHub settings, provider configuration, or Telegram route was changed for this candidate. No native claim, review receipt, terminal result, merge, deployment, or end-to-end delivery was fabricated. The board records source evidence while retaining its existing lifecycle state.
