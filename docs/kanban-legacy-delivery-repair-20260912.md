# Historical delivery repair and dashboard safeguards

This candidate adds a local, explicit way to correct old Done records with missing or conflicting delivery evidence. It also brings dashboard edits under the same worker and delivery safeguards as native commands. It does not install or activate the candidate.

## What changed

- A read-only audit classifies missing records, inconsistent records, and records that still need independent historical verification. It never calls matching database copies verified proof. Old uncontracted scratch work remains outside the repair scope.
- Preview produces a versioned manifest with the selected board identity and exact task, run, event, comment, attachment, operation and dependency snapshots. Reports contain digests and reason codes, not raw claim values or task bodies.
- Explicit apply changes only listed, unchanged missing/inconsistent records. It preserves original results and run history, adds a durable comment and typed block, and invalidates inactive descendants while preserving the review queue. Replays do not emit duplicate records.
- Every graph with an active, held, shipping or uncertain worker remains unchanged. The existing owner must finish or drain that attempt before a new preview can repair it. This command never signals a process. Human input gates are preserved.
- Dashboard priority, text, status, dependency and recovery paths respect native safeguards. Run-specific termination requests cannot reclaim a replacement attempt. Shipping tasks now appear in their actual board column.
- Board databases gain a persistent identity. Removal refuses unfinished workers and unresolved delivery operations, records retirement before moving the directory, and prevents existing native writer connections from changing the retired board. Failed deletion leaves recoverable retired data.

## Operator commands

On an installed version whose normal native initialization has created the board identity:

```sh
hermes kanban --board default reconcile-delivery --limit 100
hermes kanban --board default reconcile-delivery --manifest-out /path/to/new-preview.json
hermes kanban --board default reconcile-delivery --apply-manifest /path/to/new-preview.json
```

The default command is a read-only preview. Saving requires a new file. Apply requires that explicit saved manifest and the original board. Task workers and delegated children are refused before manifest files are opened. These commands open an existing database without initializing or migrating it. A missing board identity is an error, not permission to silently modify an older installation.

## Read-only board audit

A fresh read-only scan on 12 September examined all 102 Done cards. It found 26 with missing local records, five with inconsistent records, one with internally consistent records still needing independent verification, and 70 outside the contract/workspace classification scope. One affected graph has an ownership fence. These are record-quality findings, not a claim that 31 deliveries failed. No cards were changed.

## Verification and limits

Focused disposable SQLite tests cover full preview/apply/replay, dependency queue restoration, changed contracts/comments/links/proof, ownership holds, database statement and commit failures, manifest tampering, board binding, and human gates. Separate tests exercise native/dashboard edits and retirement races. The publication receipt records the final exact test commands, counts and source identity.

Internally consistent historical records are left unchanged and reported as requiring independent verification. The current terminal verifier requires an active review attempt and current deployment evidence; it is not used to manufacture historical proof. This candidate does not add a historical deployment verifier or another worker termination controller.

No live board repair, schema migration, service restart, provider call or Telegram message was performed while preparing this candidate. The existing controlled-install and disposable live task checks still govern completion of card `t_279a15c7`.
