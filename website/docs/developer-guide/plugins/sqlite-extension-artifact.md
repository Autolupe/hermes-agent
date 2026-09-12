# SQLite extension artifact capture

This source-only preparation can verify one SQLite extension file and retain its
actual compiled connection method. It does **not** establish trust in the whole
running program, install a provider, or permit a worker to start. Public
`bootstrap_required_policy()` always refuses before selecting or executing a
provider. Complete runtime provenance remains unsupported.

## Enrollment compatibility

Initial absence and schema 1 keep their existing behavior. Schema 2 adds exactly
one `runtime` table to the existing required-policy enrollment. All existing
provider, package-file and scope checks still apply. For example, the additional
table has this shape (paths and digest below are illustrative, not installed):

```toml
schema_version = 2
# Existing uid, provider, files and scope fields are also required.

[runtime]
capability = "sqlite-opened-identity-v1"
inventory = "/opt/hermes-runtime/sqlite-extension-artifact.json"
inventory_sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
```

Ordinary startup refuses schema 2 before provider execution. There is no command
line, environment, enrollment or configuration option that enables the missing
protected bootstrap. The private registry constructor dependency is a library
test seam, not a supported deployment entry point or an approval flag.

The inventory is deliberately named **extension artifact**, not protected runtime.
It is a closed JSON document with these exact fields:

```json
{
  "schema_version": 1,
  "kind": "sqlite-extension-artifact",
  "python": {
    "implementation": "cpython",
    "version": [3, 11, 15],
    "extension_suffix": ".cpython-311-x86_64-linux-gnu.so"
  },
  "sqlite_version": "3.53.1",
  "artifact": {
    "path": "/opt/hermes-runtime/_sqlite3.cpython-311-x86_64-linux-gnu.so",
    "size": 1305528,
    "sha256": "bc27399bacb08b450346e770f0c4eb0db40c4b2c5edde14fc533deb9b11bcd94"
  }
}
```

The example describes the retained local build used by the compiled tests; it
does not authorize that build for installation. Unknown fields, duplicate JSON
keys, nonfinite numbers, wrong types, invalid paths and mismatched bytes refuse.
The inventory is at most 64 KiB and the single extension at most 4 MiB. Both use
the existing descriptor-relative `_ProtectedFiles` checks: protected ancestry,
single-link regular files, expected owner, no symlinks or group/other writes,
bounded reads and stable file metadata. The provider's separate Python-file
allowlist and its size limits are not widened to permit native files.

## What capture does

The private capture operation checks the running CPython version and extension
ABI (its binary interface), then refuses preloaded SQLite or native Kanban
database modules. It copies the checked bytes into a Linux memory-backed file,
seals it against writing, shrinking, growing and removal of seals, and loads
`_sqlite3` from that retained file descriptor. It never reopens the source path to
execute it. Missing memory-file or sealing support refuses; there is no unsealed
or disk-file fallback. The supported fixture Python lacks the convenience
wrappers, so this uses libc's `memfd_create` and documented Linux UAPI
`F_ADD_SEALS`/`F_GET_SEALS` commands, not architecture-specific syscall numbers.

Capture occurs before standard `sqlite3`, native database or provider imports.
Only after the extension reports the declared SQLite version and exposes the
actual compiled method does the existing registry import the checked provider.
The existing `_RegistrationContext` privately carries the retained capture;
scope data never substitutes for it.

The capture retains the original base type and its `_opened_identity` method.
Calling that saved method directly avoids instance, subclass or public module
alias substitutions. A binding retains the exact original connection object and
its initial six-integer identity tuple. Every observation calls the method again
on that same object. A different connection is refused even if its tuple is
equal. A closed or reinitialized connection is refused, not reopened by path.

Checks also retain process, thread and account identity, module origin and loader,
sealed-file identity, enrollment and artifact/package snapshots. Detected drift
invalidates the capture or binding permanently; restoring old bytes does not
restore a failed registration. A failed registration closes its retained file
descriptor. A successful capture owns that descriptor until explicit close or
process exit. Closing a request ends that request's authority, not the lifetime
of a still-selected process-wide extension.

## What is still missing

An extension hash and matching ABI do not authenticate the interpreter, standard
library, native dependency closure, build headers or bootstrap that loaded them.
Private fixture ownership is not root-protected installation. No completeness
boolean, saved JSON, pathname or caller-supplied method makes that trust chain
complete. A future supported bootstrap must protect and validate those inputs
before importing this code or handing the retained capture to the existing
registry; this change does not provide that bootstrap or its installer.

This handoff also does not approve database access, move native SQL claim checks
behind an identity check, freeze all worker instructions, provide credentials,
or implement trusted completion. Both required worker-launch gates remain
unsupported, including with the actual compiled descriptor fixture. The
[held-worker mechanics](./held-kanban-worker.md) remain separate preparation,
not positive launch authority.

## Tests

```sh
umask 022
scripts/run_tests.sh -j 3 --file-retries 0 \
  tests/hermes_cli/test_kanban_runtime_artifact.py \
  tests/hermes_cli/test_kanban_required_policy.py \
  tests/hermes_cli/test_kanban_required_workspace.py -q
```

Data-only fixtures test the closed inventory and unchanged legacy selection.
Fresh isolated processes test the actual retained compiled extension, kernel
seals, provider-import ordering, original native connection checkpoints, drift,
preloads and unsupported launch. They use temporary boards and a private test
registry, not the host registry. The compiled cases explicitly skip if the
retained external fixture or matching CPython ABI is absent; a skipped case is
not proof of compiled behavior. Tests do not download, build or install an
extension, access a protected board, call a model or start a live worker.
