# Stop repeating workers that exit without a task result

Card `t_279a15c7` has a current-source repair for clean worker exits and task-bound lifecycle identity. A successful process exit without a recorded task result now produces one typed block for that exact attempt. It does not start another worker to repeat the work. Real crashes and temporary provider limits keep their existing retry behavior.

The repair starts from native Hermes source `3a170fb835bda574bda5c44afabae61c94f34cd3` in an isolated worktree. The older recovery plan is preserved in card comment 2123. Its archived candidate `0b3c6f86db280d65ab034541b2be500071caaccd` was based on the separate `339a5398` delivery runtime; its bundle was hash-verified and was not applied wholesale.

The database checks the task, open run, process and claim together before reconciling an exit. It records a durable `terminal_reconciled` event and consumes the process exit record after commit. A competing terminal call, a newer run, or a failed transaction cannot be mistaken for a successful reconciliation. This runs before generic stale-claim cleanup. The same identity check protects later crash, expired-claim, stale-heartbeat, timeout and orphan recovery, so a refused record cannot fall through to a different cleanup path on a later tick. Orphan recovery still repairs a missing task claim when neither row records a worker PID and the current open run belongs to that task. A missing PID record does not prove a process never started; the exception does not accept a missing, ended or foreign run, a recorded PID, or a changed task snapshot.

The command-line and model-tool lifecycle calls require a valid current run when invoked by a task-bound worker. They reject invalid identity before calling the completion judge or extending a claim. A worker cannot use a bulk command to change a sibling task. Rejected block/schedule commands leave no misleading task note. Successful operator notes are preserved.

## Validation

Final exact-source test counts and artifact hashes are recorded in the publication receipt under `/home/ab/.local/state/kanban-sequential-20260907/terminal-reconciliation-evidence/`. Tests use temporary SQLite boards and harmless local processes. Repeated-dispatch tests also preserve malformed, ended, missing and foreign run records across three complete cleanup cycles. Existing quota fixtures now attach the worker process to its actual run; their retry expectations are unchanged. The process test runs a real child, lets the real dispatcher reap it, verifies one blocked result, then verifies that another tick neither records a duplicate nor starts another worker.

The coding charter and task protocol already state the exact terminal-call and structured delivery-proof requirements prominently. They require no duplicate instruction edits.

## Delivery boundary

This source slice is not full completion of the card. The canonical interactive source used here does not contain the separate sealed runtime's trusted delivery contract. The installed sealed runtime has `_completion_delivery_policy`, `_normalize_terminal_delivery`, and protected delivery-control machinery; this repair does not replace them with a weaker metadata check. The older candidate also contains delivery-gap reconciliation work that remains separately preserved.

Before runtime adoption, integrate the terminal repair with the reviewed delivery runtime and the existing worktree/isolation candidates, prove structured code-merge evidence and explicit non-code evidence on that combined source, and complete independent review and the controlled installation check. No installed source, service, live board lifecycle, credential, provider routing or deployment is changed by this publication. Local tests do not establish live retry savings or a better 14-day failure rate; count new `terminal_reconciled` events after actual adoption.
