"""Read-only terminal counters measured from an explicit event watermark.

Run with no watermark to capture a baseline after installing the fix. Later
reports count only events after that baseline. No initialization, migration,
worker dispatch, provider access or notification is performed here.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
import sqlite3
import sys

from hermes_cli.sqlite_safe_read import connect_tracked


TERMINAL_KINDS = (
    "terminal_reconciled", "completed", "blocked", "crashed", "timed_out",
    "rate_limited",
)


def terminal_audit(conn: sqlite3.Connection, *, since_event_id: int | None = None) -> dict:
    """Read a coherent baseline or event counts without owning a caller's txn."""
    if since_event_id is not None and (type(since_event_id) is not int or since_event_id < 0):
        raise ValueError("since-event-id must be a non-negative integer")
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN")
    try:
        through = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
        if since_event_id is not None and since_event_id > through:
            raise ValueError("baseline is newer than this database; check the board or restored history")
        report = {
            "schema": "hermes-kanban-terminal-audit/v1",
            "mode": "baseline" if since_event_id is None else "counts",
            "through_event_id": through,
        }
        if since_event_id is None:
            return report
        report["since_event_id"] = since_event_id
        marks = ",".join("?" for _ in TERMINAL_KINDS)
        counts = dict(conn.execute(
            f"SELECT kind, COUNT(*) FROM task_events WHERE id > ? AND id <= ? "
            f"AND kind IN ({marks}) GROUP BY kind",
            (since_event_id, through, *TERMINAL_KINDS),
        ).fetchall())
        report["event_counts"] = {kind: counts.get(kind, 0) for kind in TERMINAL_KINDS}
        report["unbound_reconciliation_events"] = conn.execute(
            "SELECT COUNT(*) FROM task_events e LEFT JOIN task_runs r ON r.id=e.run_id "
            "WHERE e.id > ? AND e.id <= ? AND e.kind='terminal_reconciled' "
            "AND (r.id IS NULL OR r.task_id != e.task_id)",
            (since_event_id, through),
        ).fetchone()[0]
        # Count repeat events after the baseline, including repetitions of a
        # reconciliation already recorded before it. The task/run pair is the
        # attempt identity; the numeric run ID alone is not enough.
        report["repeated_reconciliation_events"] = conn.execute(
            "SELECT COUNT(*) FROM task_events e WHERE e.id > ? AND e.id <= ? "
            "AND e.kind='terminal_reconciled' AND e.run_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM task_events earlier WHERE earlier.id < e.id "
            "AND earlier.task_id=e.task_id AND earlier.run_id=e.run_id "
            "AND earlier.kind='terminal_reconciled')",
            (since_event_id, through),
        ).fetchone()[0]
        return report
    finally:
        if owned and conn.in_transaction:
            conn.rollback()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="Existing Kanban SQLite database")
    parser.add_argument("--since-event-id", type=int, default=None,
                        help="Baseline through_event_id captured after installing the fix")
    args = parser.parse_args(argv)
    if args.since_event_id is not None and args.since_event_id < 0:
        parser.error("--since-event-id must be non-negative")
    path = args.database.expanduser().resolve()
    try:
        with contextlib.closing(connect_tracked(
            path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=5,
        )) as conn:
            conn.execute("PRAGMA query_only=ON")
            report = terminal_audit(conn, since_event_id=args.since_event_id)
        report["database"] = str(path)
        print(json.dumps(report, sort_keys=True))
        return 0
    except ValueError as exc:
        print(f"terminal audit: {exc}", file=sys.stderr)
    except (OSError, sqlite3.Error):
        print("terminal audit: existing board could not be read; no database was initialized", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
