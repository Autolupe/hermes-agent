"""Standalone, non-live Hermes Kanban dispatch-boundary capability probe.

Invoke this module directly with the Hermes Python interpreter::

    python3 -m hermes_cli.dispatch_boundary_probe

It deliberately does not pass through ``hermes_cli.main`` or the normal
Kanban argument parser. That structural separation keeps dotenv loading,
configuration repair, file logging, profile selection, plugin discovery, and
board initialization outside the process path that promises no live writes.
"""

from __future__ import annotations

import json
import sys


_CHECK_NAMES = (
    "absent_brakes_allow",
    "dispatch_pause_regular_blocks",
    "dispatch_pause_broken_symlink_blocks",
    "halt_regular_blocks",
    "halt_broken_symlink_blocks",
    "profile_shared_root_halt_blocks",
    "lookup_errors_fail_closed",
    "halt_blocks_final_spawn_edge",
    "estop_blocks_final_spawn_edge",
    "halt_blocks_gateway_auto_decompose_edge",
)


def _failed_payload() -> dict[str, object]:
    """Return the fixed v1 failure shape without importing runtime modules."""
    return {
        "schema_version": 1,
        "contract": "hermes-kanban-dispatch-boundary",
        "state": "failed",
        "probe_scope": "temporary_shared_root",
        "shared_halt_path": "state/halt.json",
        "live_writes_performed": False,
        "checks": {name: False for name in _CHECK_NAMES},
    }


def run_probe() -> int:
    """Run the isolated behavior probe and emit exactly one JSON line."""
    try:
        from hermes_cli import kanban_db as kb

        payload, verified = kb.run_dispatch_boundary_self_test()
    except BaseException:
        payload = _failed_payload()
        verified = False
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 0 if verified else 1


def main(argv: list[str] | None = None) -> int:
    """Reject every argument; this module has one exact machine interface."""
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        return 2
    return run_probe()


if __name__ == "__main__":
    raise SystemExit(main())
