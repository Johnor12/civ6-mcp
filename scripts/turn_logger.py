#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Turn logger: records whose turn the hotseat game is on, the history behind the Slack turn timer.

    uv run scripts/turn_logger.py

Every POLL_S it asks the game whose turn it is (civ_mcp's
hotseat.whose_turn, over the same bridge the MCP server uses) and appends
a row to scripts/data/turn_log.jsonl when the turn or the player differs
from the last row. The Slack bot (scripts/slack_bot.py) imports this module
and reads the log with duration_of_current_turn(). The inline metadata
above gives the script its own environment, so it never syncs the project
venv that the running MCP server holds open; civ_mcp is imported from src/
and needs only the standard library.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as clock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
from civ_mcp import hotseat  # noqa: E402

log = logging.getLogger("turn_logger")

TURN_LOG_PATH = REPO_ROOT / "scripts" / "data" / "turn_log.jsonl"
POLL_S = 5 * 60

# Workday hours, local time, Monday to Friday.
WORKDAY_START_HOUR = 9
WORKDAY_END_HOUR = 17


@dataclass(frozen=True)
class TurnRecord:
    at: datetime  # local time, naive
    turn: int
    leader: str  # e.g. LEADER_POUNDMAKER
    player: str  # hotseat player name shown on the turn-change screen

    @property
    def key(self) -> tuple[int, str, str]:
        return self.turn, self.leader, self.player

    @property
    def leader_display(self) -> str:
        return self.leader.replace("LEADER_", "").replace("_", " ").title()


def read_records(path: Path | None = None) -> list[TurnRecord]:
    path = path or TURN_LOG_PATH
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            at = datetime.fromisoformat(row["at"]).astimezone().replace(tzinfo=None)
            records.append(TurnRecord(at, int(row["turn"]), row["leader"], row["player"]))
        except (ValueError, KeyError, TypeError):
            log.warning("skipping a bad row in %s: %r", path, line)
    return records


def record(
    turn: int,
    leader: str,
    player: str,
    now: datetime | None = None,
    path: Path | None = None,
) -> bool:
    """Append a row when the game is on another turn or player than the last row says.

    Returns True when a row was written.
    """
    path = path or TURN_LOG_PATH
    records = read_records(path)
    if records and records[-1].key == (turn, leader, player):
        return False
    at = (now or datetime.now()).astimezone()
    row = {"at": at.isoformat(timespec="seconds"), "turn": turn, "leader": leader, "player": player}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return True


def log_current_turn(path: Path | None = None) -> bool:
    """Ask the game whose turn it is and add a row if that changed.

    False when no hotseat turn answers: Civ 6 closed, at the main menu,
    loading, or between two players.
    """
    st = asyncio.run(hotseat.whose_turn())
    if st is None:
        return False
    if record(st.turn, st.leader, st.player_name, path=path):
        log.info("turn %d: %s (%s)", st.turn, st.player_name, st.leader)
    return True


def duration_of_current_turn(
    now: datetime | None = None, path: Path | None = None
) -> tuple[TurnRecord, float] | None:
    """Whose turn the log says it is, and how many seconds of workday time it has lasted.

    The record's ``at`` is when the turn was first recorded: loading another
    save and coming back records the same turn again, and its clock keeps
    running from the first row instead of starting over. None while the log
    is empty.
    """
    records = read_records(path)
    if not records:
        return None
    last = records[-1]
    first = next(r for r in records if r.key == last.key)
    return first, workday_seconds(first.at, now or datetime.now())


def is_workday(dt: datetime) -> bool:
    return dt.weekday() < 5 and WORKDAY_START_HOUR <= dt.hour < WORKDAY_END_HOUR


def workday_seconds(start: datetime, end: datetime) -> float:
    """Seconds between two naive local times that fall in workday hours."""
    total = 0.0
    day = start.date()
    while day <= end.date():
        if day.weekday() < 5:
            opens = datetime.combine(day, clock(WORKDAY_START_HOUR))
            closes = datetime.combine(day, clock(WORKDAY_END_HOUR))
            total += max(0.0, (min(end, closes) - max(start, opens)).total_seconds())
        day += timedelta(days=1)
    return total


def main() -> None:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("civ_mcp").setLevel(logging.WARNING)  # a bridge connect per poll
    current = duration_of_current_turn()
    last = f"{current[0].player}'s turn {current[0].turn} since {current[0].at:%a %d %b %H:%M}" if current else "none"
    log.info("asking the game every %d min; log %s, last turn: %s", POLL_S // 60, TURN_LOG_PATH, last)
    while True:
        try:
            log_current_turn()
        except Exception:
            log.exception("turn poll failed")
        time.sleep(POLL_S - time.time() % POLL_S)


if __name__ == "__main__":
    main()
