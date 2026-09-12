# Retain the delivery request's database connection

The delivery broker previously opened the board again between authorization,
operation creation, remote checks, and the final board update. That discarded
the original connection at each boundary. Its connector also bypassed the
shared SQLite connection registry and could create an empty file if the checked
database disappeared just before opening.

`DeliveryControl.handle` now opens one existing database and retains that exact
connection for the entire request. It passes the connection explicitly through
operation acquisition, every delivery guard, publication, acceptance rejection,
completion and request-local quarantine. The connection closes on success,
replay, refusal, exception and interruption. Separate requests still receive
separate connections; no write transaction spans remote backend work.

The connector uses `connect_tracked` and an escaped SQLite `mode=rw` URI. It
retains the existing schema and durability checks. The shared registry now
protects the connection from unsafe raw file inspections until it closes.
Recovery scanning still opens its own connection; each recovered operation is
handled through the same request entry point.

## Verification

457 tests passed across 10 files, with no retries, failures or skips. Commands:

```sh
scripts/run_tests.sh -j 2 --file-retries 0 tests/hermes_cli/test_delivery_control.py
scripts/run_tests.sh -j 2 --file-retries 0 \
  tests/hermes_cli/test_delivery_terminal_base.py \
  tests/hermes_cli/test_delivery_verifier.py \
  tests/hermes_cli/test_trusted_delivery_runtime.py \
  tests/hermes_cli/test_kanban_delivery_gate.py \
  tests/hermes_cli/test_kanban_delivery_modern_fences.py \
  tests/hermes_cli/test_kanban_delivery_writer_fences.py \
  tests/hermes_cli/test_kanban_board_identity.py \
  tests/tools/test_kanban_delivery_interfaces.py \
  tests/test_sqlite_lock_safe_inspection.py
```

New checks exercise real temporary SQLite databases and the real broker/native
transition functions, using the existing fake remote backend. They verify one
opened connection per request, identical connection objects at authorization and
native terminal calls, registration through delivery, closure after completion
and replay, interrupted-call cleanup with the same durable operation on retry,
and no database creation after pre-open removal. Existing checks cover stale
run/claim/PID changes, acceptance rejection, recovery, quarantine, held workers,
board retirement and the worker-facing tool path. Ruff and whitespace checks
also pass.

Logs and hashes are retained under the sequential runner's
`context-integration-evidence/retained-delivery-*` files.

## Remaining work

This preserves the broker's connection after its existing open. It is not
executed-runtime attestation, protected worker bootstrap, or proof that a socket
request originated from the original `WorkspaceRequest`. The protected launch
gate remains unsupported. Those connections and verified installation still
must be completed before card `t_279a15c7` can be Done. No live database mutation,
installation, service action, provider call or Telegram message was performed.
