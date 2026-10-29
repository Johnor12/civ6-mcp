#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Turn logger: records whose turn the hotseat game is on, and saves a turn nobody saved.

    uv run scripts/turn_logger.py

Every POLL_S it asks the game whose turn it is (civ_mcp's
hotseat.whose_turn, over the same bridge the MCP server uses) and appends
a row to scripts/data/turn_log.jsonl when the turn or the player differs
from the last row: the history behind the Slack turn timer. The Slack bot
(scripts/slack_bot.py) and the turn announcer (scripts/turn_announcer.py)
import this module, have it log the current turn and read the log with
duration_of_current_turn().

The same poll stands in for the Save Game button players are meant to
press on every turn-change screen and sometimes forget. When the hotseat
save folder holds no save of the current turn (none written since the
turn can have begun, autosaves included, and none named after its
turn-change screen, "<LEADER> <turn> <year>"), it saves the game the way
end_turn does, as "<LEADER> <turn> <year> MCP", or "... MCP MIDTURN" when
the player had already started playing. Only this script's loop saves;
log_current_turn, which the bot and the announcer call, never does.

The inline metadata above gives the script its own environment, so it
never syncs the project venv that the running MCP server holds open;
civ_mcp is imported from src/ and needs only the standard library.
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
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
from civ_mcp import hotseat  # noqa: E402
from civ_mcp.bridge import hotseat_saves_dir  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterable

    from civ_mcp.bridge import BridgeConnection

log = logging.getLogger("turn_logger")

TURN_LOG_PATH = REPO_ROOT / "scripts" / "data" / "turn_log.jsonl"
POLL_S = 5 * 60
MIDTURN_SAVE_TAG = f"{hotseat.SAVE_TAG} MIDTURN"  # a save taken after the player pressed Start Turn

# Workday hours, local time, Monday to Friday.
WORKDAY_START_HOUR = 9
WORKDAY_END_HOUR = 17
WORKDAY = f"{WORKDAY_START_HOUR}:00–{WORKDAY_END_HOUR}:00, Mon–Fri"
TURN_LIMIT_S = 2 * 3600  # workday time after which another player may have a turn played


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


async def poll(
    path: Path | None = None, save: bool = False, saves_dir: Path | None = None
) -> hotseat.HotseatStatus | None:
    """Ask the game whose turn it is, add a row if that changed and, with `save`, see that the turn has a save.

    None when no hotseat turn answers: Civ 6 closed, at the main menu,
    loading, or between two players. A save that fails is logged, not raised.
    """
    conn = await hotseat.open_connection()
    if conn is None:
        return None
    st = await hotseat.whose_turn(conn)
    if st is None:
        return None
    if record(st.turn, st.leader, st.player_name, path=path):
        log.info("turn %d: %s (%s)", st.turn, st.player_name, st.leader)
    if save:
        try:
            await ensure_saved(conn, st, path, saves_dir)
        except Exception:
            log.exception("could not see that turn %d has a save", st.turn)
    return st


def log_current_turn(path: Path | None = None) -> bool:
    """Ask the game whose turn it is and add a row if that changed; False when no hotseat turn answers."""
    return asyncio.run(poll(path)) is not None


async def ensure_saved(
    conn: BridgeConnection,
    st: hotseat.HotseatStatus,
    path: Path | None = None,
    saves_dir: Path | None = None,
) -> str | None:
    """Save the game unless the hotseat save folder already has a save of the current turn.

    The name is the one the game offers on this turn's Save Game screen
    plus the tag end_turn's saves carry, or MIDTURN_SAVE_TAG once the
    player is past the turn-change screen, so that such a save is not
    mistaken for one of the turn's start. Returns the name written, or None.
    """
    current = current_turn(path)
    if current is None:
        return None
    first, previous = current
    default_name = await hotseat.default_save_name(conn)
    files = [f for f, _ in hotseat.hotseat_save_files(saves_dir)]
    found = save_of_turn(first, previous, default_name, files)
    if found is not None:
        log.debug("turn %d has a save: %s", st.turn, found.name)
        return None
    tag = hotseat.SAVE_TAG if st.turn_controls_visible else MIDTURN_SAVE_TAG
    name = hotseat.tagged_save_name(default_name, tag)
    await hotseat.save_hotseat(conn, name)
    log.info("no save of %s's turn %d (%s) yet: saved '%s'", st.player_name, st.turn, st.leader_display, name)
    return name


def save_of_turn(
    first: TurnRecord, previous: TurnRecord | None, default_name: str, files: Iterable[Path]
) -> Path | None:
    """A save of the current turn among `files`, or None.

    The turn began after the row before its first row (the game was still
    on the previous player then) and at most POLL_S before that first row,
    so any save written since then is one of this turn, autosaves included.
    A save named the way the game offers it on this turn's Save Game screen
    (`default_name`, "<LEADER> <turn> <year>", whatever follows it) counts
    from the previous row on, however late the turn was first logged.
    """
    after_previous = previous.at.timestamp() if previous else float("-inf")
    begun = max(after_previous, (first.at - timedelta(seconds=POLL_S)).timestamp())
    prefix = default_name.casefold()
    for f in files:
        mtime = f.stat().st_mtime
        if mtime >= begun or (prefix and mtime >= after_previous and f.stem.casefold().startswith(prefix)):
            return f
    return None


def current_turn(path: Path | None = None) -> tuple[TurnRecord, TurnRecord | None] | None:
    """The first row of the turn the log says it is on, and the row just before it.

    The turn is the last row's; its first row is the earliest with the same
    key, so loading another save and coming back does not start it over.
    The row before is None when the log begins with this turn. None while
    the log is empty.
    """
    records = read_records(path)
    if not records:
        return None
    i = next(i for i, r in enumerate(records) if r.key == records[-1].key)
    return records[i], (records[i - 1] if i else None)


def duration_of_current_turn(
    now: datetime | None = None, path: Path | None = None
) -> tuple[TurnRecord, float] | None:
    """Whose turn the log says it is, and how many seconds of workday time it has lasted.

    The record's ``at`` is when the turn was first recorded (see
    current_turn), so its clock keeps running across a reload. None while
    the log is empty.
    """
    current = current_turn(path)
    if current is None:
        return None
    first, _ = current
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


def workday_deadline(start: datetime, seconds: float) -> datetime:
    """The naive local time at which `seconds` of workday time since `start` are up."""
    day = start.date()
    while True:
        if day.weekday() < 5:
            opens = max(start, datetime.combine(day, clock(WORKDAY_START_HOUR)))
            open_s = (datetime.combine(day, clock(WORKDAY_END_HOUR)) - opens).total_seconds()
            if seconds <= open_s:
                return opens + timedelta(seconds=seconds)
            seconds -= max(0.0, open_s)
        day += timedelta(days=1)


def duration(seconds: float) -> str:
    """5400 -> '1h 30m'."""
    hours, mins = divmod(int(seconds // 60), 60)
    parts = [f"{hours}h"] if hours else []
    if mins or not hours:
        parts.append(f"{mins}m")
    return " ".join(parts)


def main() -> None:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("civ_mcp").setLevel(logging.WARNING)  # a bridge connect per poll
    current = duration_of_current_turn()
    last = f"{current[0].player}'s turn {current[0].turn} since {current[0].at:%a %d %b %H:%M}" if current else "none"
    log.info(
        "asking the game every %d min; log %s, last turn: %s; a turn without a save in %s gets one",
        POLL_S // 60, TURN_LOG_PATH, last, hotseat_saves_dir(),
    )
    while True:
        try:
            asyncio.run(poll(save=True))
        except Exception:
            log.exception("turn poll failed")
        time.sleep(POLL_S - time.time() % POLL_S)


if __name__ == "__main__":
    main()
