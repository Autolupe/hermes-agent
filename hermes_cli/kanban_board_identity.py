"""Durable board identity and retirement over the caller's SQLite connection.

The UUID identifies the database SQLite has open. A path/inode observation is
additional filesystem evidence; it does not prove which inode SQLite opened.
No helper opens or closes a raw descriptor for the database file.
"""

from __future__ import annotations

import sqlite3
import stat
import time
import uuid
from pathlib import Path
from typing import Any


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kanban_board_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    board_uuid TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'retired')),
    retirement_token TEXT,
    created_at INTEGER NOT NULL,
    retired_at INTEGER,
    CHECK ((state = 'active' AND retirement_token IS NULL AND retired_at IS NULL)
        OR (state = 'retired' AND retirement_token IS NOT NULL AND retired_at IS NOT NULL))
);
"""


class BoardIdentityError(ValueError):
    """The board cannot be bound to one durable identity."""


class BoardRetiredError(BoardIdentityError):
    """This database was retired and must not accept ordinary writes."""


def read_board_identity(
    conn: sqlite3.Connection, *, required: bool = False,
) -> dict[str, Any] | None:
    """Read identity without initializing or modifying a legacy database."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_board_identity'"
    ).fetchone()
    if table is None:
        if required:
            raise BoardIdentityError("board has no persisted identity; native initialization is required")
        return None
    rows = conn.execute(
        "SELECT singleton, board_uuid, state, retirement_token, created_at, retired_at "
        "FROM kanban_board_identity"
    ).fetchall()
    if not rows and not required:
        return None
    if len(rows) != 1 or rows[0][0] != 1:
        raise BoardIdentityError("board identity must contain exactly one singleton row")
    _, board_uuid, state, token, created_at, retired_at = rows[0]
    try:
        valid_uuid = str(uuid.UUID(board_uuid)) == board_uuid
    except (ValueError, TypeError, AttributeError):
        valid_uuid = False
    valid_state = (
        state == "active" and token is None and retired_at is None
        or state == "retired" and isinstance(token, str) and bool(token)
        and isinstance(retired_at, int)
    )
    if not valid_uuid or not valid_state or not isinstance(created_at, int):
        raise BoardIdentityError("board identity is malformed")
    return {
        "board_uuid": board_uuid, "state": state, "retirement_token": token,
        "created_at": created_at, "retired_at": retired_at,
    }


def initialize_board_identity(conn: sqlite3.Connection) -> dict[str, Any]:
    """Initialize the singleton once; never reset an existing retirement."""
    conn.execute(
        "INSERT OR IGNORE INTO kanban_board_identity "
        "(singleton, board_uuid, state, created_at) VALUES (1, ?, 'active', ?)",
        (str(uuid.uuid4()), int(time.time())),
    )
    return read_board_identity(conn, required=True)


def assert_board_writable(conn: sqlite3.Connection) -> None:
    """Refuse retired boards, including connections held before retirement."""
    identity = read_board_identity(conn)
    if identity is not None and identity["state"] != "active":
        raise BoardRetiredError("board is retired; ordinary writes are refused")


def board_binding(
    conn: sqlite3.Connection, *, required: bool = True,
) -> dict[str, Any]:
    """Bind SQLite identity and a current path observation for audit receipts."""
    identity = read_board_identity(conn, required=required)
    main = next((row for row in conn.execute("PRAGMA database_list") if row[1] == "main"), None)
    if main is None or not main[2]:
        raise BoardIdentityError("board binding requires a named on-disk database")
    try:
        path = Path(main[2]).resolve(strict=True)
        info = path.stat()
    except OSError as exc:
        raise BoardIdentityError("board database path cannot be observed") from exc
    if not stat.S_ISREG(info.st_mode):
        raise BoardIdentityError("board database path is not a regular file")
    return {
        "board_uuid": identity["board_uuid"] if identity else None,
        "db_path": str(path), "device": info.st_dev, "inode": info.st_ino,
        "state": identity["state"] if identity else None,
    }


def retire_board_identity(conn: sqlite3.Connection, token: str) -> dict[str, Any]:
    """Retire under the caller's IMMEDIATE transaction after its owner checks."""
    if not conn.in_transaction:
        raise BoardIdentityError("board retirement requires an existing write transaction")
    identity = read_board_identity(conn, required=True)
    assert_board_writable(conn)
    changed = conn.execute(
        "UPDATE kanban_board_identity SET state='retired', retirement_token=?, retired_at=? "
        "WHERE singleton=1 AND board_uuid=? AND state='active'",
        (token, int(time.time()), identity["board_uuid"]),
    )
    if changed.rowcount != 1:
        raise BoardIdentityError("board identity changed before retirement")
    return read_board_identity(conn, required=True)


def restore_board_identity(
    conn: sqlite3.Connection, *, board_uuid: str, retirement_token: str,
) -> bool:
    """Compensate only the same failed retirement while owning the write lock.

    The caller must first prove the original directory and database are still
    in place. This is deliberately outside ordinary ``write_txn``: retired
    boards reject that public writer boundary.
    """
    if not conn.in_transaction:
        raise BoardIdentityError("retirement compensation requires a write transaction")
    return conn.execute(
        "UPDATE kanban_board_identity SET state='active', retirement_token=NULL, retired_at=NULL "
        "WHERE singleton=1 AND board_uuid=? AND state='retired' AND retirement_token=?",
        (board_uuid, retirement_token),
    ).rowcount == 1
