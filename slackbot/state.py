"""SQLite store: turn timers, approval requests, run log."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    player_id TEXT NOT NULL,
    since_ts TEXT NOT NULL,
    started_at REAL NOT NULL,
    pinged INTEGER NOT NULL DEFAULT 0,
    warned INTEGER NOT NULL DEFAULT 0,
    UNIQUE (player_id, since_ts)
);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    turn_id INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    created_at REAL NOT NULL,
    message_ts TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS approvals (
    request_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (request_id, user_id)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    player_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    source TEXT NOT NULL,
    prompt TEXT NOT NULL,
    ok INTEGER NOT NULL,
    summary TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Turn:
    id: int
    player_id: str
    since_ts: str
    started_at: float
    pinged: bool
    warned: bool


@dataclass(frozen=True)
class Request:
    id: str
    turn_id: int
    prompt: str
    created_at: float
    message_ts: str
    status: str


class Store:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    # -- turns --------------------------------------------------------

    def sync_turn(self, player_id: str, since_ts: str) -> Turn:
        """Return the row for this turn, creating it the first time it is seen."""
        self.db.execute(
            "INSERT OR IGNORE INTO turns (player_id, since_ts, started_at) VALUES (?, ?, ?)",
            (player_id, since_ts, float(since_ts)),
        )
        self.db.commit()
        row = self.db.execute(
            "SELECT * FROM turns WHERE player_id = ? AND since_ts = ?", (player_id, since_ts)
        ).fetchone()
        return _turn(row)

    def get_turn(self, turn_id: int) -> Turn | None:
        row = self.db.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return _turn(row) if row else None

    def mark_turn(self, turn_id: int, field: str) -> None:
        if field not in ("pinged", "warned"):
            raise ValueError(field)
        self.db.execute(f"UPDATE turns SET {field} = 1 WHERE id = ?", (turn_id,))
        self.db.commit()

    # -- approval requests ---------------------------------------------

    def create_request(
        self, request_id: str, turn_id: int, prompt: str, created_at: float | None = None
    ) -> Request:
        self.db.execute(
            "INSERT INTO requests (id, turn_id, prompt, created_at) VALUES (?, ?, ?, ?)",
            (request_id, turn_id, prompt, time.time() if created_at is None else created_at),
        )
        self.db.commit()
        return self.get_request(request_id)

    def get_request(self, request_id: str) -> Request | None:
        row = self.db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        return _request(row) if row else None

    def open_request_for_turn(self, turn_id: int) -> Request | None:
        row = self.db.execute(
            "SELECT * FROM requests WHERE turn_id = ? AND status = 'open' "
            "ORDER BY created_at DESC LIMIT 1",
            (turn_id,),
        ).fetchone()
        return _request(row) if row else None

    def set_request_message(self, request_id: str, message_ts: str) -> None:
        self.db.execute("UPDATE requests SET message_ts = ? WHERE id = ?", (message_ts, request_id))
        self.db.commit()

    def set_request_prompt(self, request_id: str, prompt: str) -> None:
        """A new prompt voids every approval given to the old one."""
        self.db.execute("UPDATE requests SET prompt = ? WHERE id = ?", (prompt, request_id))
        self.db.execute("DELETE FROM approvals WHERE request_id = ?", (request_id,))
        self.db.commit()

    def close_request(self, request_id: str, new_status: str) -> bool:
        """Move an open request to new_status. False if it was no longer open."""
        cur = self.db.execute(
            "UPDATE requests SET status = ? WHERE id = ? AND status = 'open'",
            (new_status, request_id),
        )
        self.db.commit()
        return cur.rowcount == 1

    def add_approval(self, request_id: str, user_id: str) -> set[str]:
        self.db.execute(
            "INSERT OR IGNORE INTO approvals (request_id, user_id) VALUES (?, ?)",
            (request_id, user_id),
        )
        self.db.commit()
        return self.approvers(request_id)

    def approvers(self, request_id: str) -> set[str]:
        rows = self.db.execute(
            "SELECT user_id FROM approvals WHERE request_id = ?", (request_id,)
        ).fetchall()
        return {r["user_id"] for r in rows}

    # -- run log ----------------------------------------------------------

    def log_run(
        self, player_id: str, requested_by: str, source: str, prompt: str, ok: bool, summary: str
    ) -> None:
        self.db.execute(
            "INSERT INTO runs (at, player_id, requested_by, source, prompt, ok, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), player_id, requested_by, source, prompt, int(ok), summary[:4000]),
        )
        self.db.commit()


def _turn(row: sqlite3.Row) -> Turn:
    return Turn(
        id=row["id"],
        player_id=row["player_id"],
        since_ts=row["since_ts"],
        started_at=row["started_at"],
        pinged=bool(row["pinged"]),
        warned=bool(row["warned"]),
    )


def _request(row: sqlite3.Row) -> Request:
    return Request(
        id=row["id"],
        turn_id=row["turn_id"],
        prompt=row["prompt"],
        created_at=row["created_at"],
        message_ts=row["message_ts"],
        status=row["status"],
    )
