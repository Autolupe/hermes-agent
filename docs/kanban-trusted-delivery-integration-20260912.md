# Trusted delivery integration on the current Kanban source

This candidate connects the protected delivery controller and verifier to the newer native board. It prevents code tasks from completing on a worker's claim that a pull request merged. The controller verifies the submitted commit, review, acceptance checks, merge and any required deployment, and records the result against the exact task attempt.

The integration starts at `f8de5ba38760072bc68772b6304854981751dd3f`, preserving its terminal reconciliation, required-worker ownership and workspace-base repairs. It selectively ports protected source `339a5398dedc7d60b8da1a3783d132227297072a`; it does not merge that older branch wholesale. The archived `0b3c6f86` contributes only the explicit non-code completion classification. Its historical Done repair is not imported: matching JSON fields do not establish trusted delivery, and clearing a descendant's ownership before confirmed cleanup is unsafe.

## Resulting behavior

- Persistent workspace tasks require a valid canonical acceptance contract before dispatch. Code work requires a task worktree. A valid non-code contract needs an explicit `no_merge_expected` completion record.
- Builders submit an exact pull request, commit and branch to a distinct reviewer. Review rejection binds the submitted commit and review round. Retries and delays preserve the review lane, parent dependencies and the dispatch stop.
- Submission, rejection and completion check the current task, open run, claim, worker process, contract, workspace and recorded base again after verification. Changed evidence cannot complete a replacement attempt.
- Delivery operations hold the task in `shipping`. Ordinary lifecycle changes cannot take over that operation. Exact-attempt heartbeats remain available while delivery is pending.
- Controller authorization preserves held-worker cleanup and open-run checks. Its fingerprint and schema preflight include the recorded workspace base. Acceptance uses the independently proved merge parent rather than a moving pull-request base.
- Delayed spawn responses cannot write a process ID onto a replacement, closed, held or shipping attempt. A returned process ID alone is not treated as authority to kill a process.
- Worker tools use the fixed delivery controller only after validating current ownership. Terminal subprocesses cannot impersonate that controller's worker peer. Delegated children retain their operator restrictions.
- Code delivery requires the protected worker path. Repository eligibility alone cannot enable ordinary worker launch. The current required launcher still refuses production launch until its adapter is implemented and verified.

## Validation

The canonical test runner uses temporary homes, SQLite boards, local Git repositories and fixture verifiers. Controller tests use local socket pairs and fake delivery backends. No model probes, live provider checks, notification sends, controller installation or service changes are part of these tests.

The focused validation includes the previous 40-file native acceptance set plus delivery parser, controller, verifier, sandbox support, CLI, tool, race and launch-boundary tests. Existing workspace fixtures now declare their actual non-code purpose; they continue to exercise real contract validation and the original ownership, pause and base checks. Final commands, file hashes, counts and review evidence are retained in the card's `delivery-integration-evidence` directory alongside its publication receipt.

## Remaining card work

This is a source candidate, not native Done or runtime adoption. Remaining work includes safe legacy-Done reconciliation, integration of the separately owned current worker-context release, and controlled installation/disposable-task evidence. The protected controller's historical repository, workflow, key and runtime pins remain unchanged and require current admission evidence before use. Existing review, merge and deployment requirements remain in force.
