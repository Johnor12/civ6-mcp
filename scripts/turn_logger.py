#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Turn logger: records whose turn the hotseat game is on, and saves a turn nobody saved.

    uv run scripts/turn_logger.py

Every WATCH_S it reads what the game's Lua.log gained (civ_mcp.turn_watch)
and appends a row to scripts/data/turn_log.jsonl for each turn line the
bridge mod printed there whose turn or player differs from the last row:
the history behind the Slack bot's turn timer and the turn times
(scripts/turn_times.py). Such a row carries the turn line's own time,
which is the game's clock when the turn began, and poll_s 0.

The logger also asks the game whose turn it is (civ_mcp's
hotseat.whose_turn, over the same bridge the MCP server uses) when a turn
line or the game's own turn-change screen line comes in, and every POLL_S
in case a turn left no line at all. A row from such a poll carries how
late it can be as poll_s: SCREEN_POLL_S after a screen line, POLL_S
otherwise. The turn announcer (scripts/turn_announcer.py) and the
out-of-office service (scripts/out_of_office.py) read the log as it is,
with duration_of_current_turn(); the Slack bot (scripts/slack_bot.py)
first has a poll log the turn. The log, not Lua.log, is the record: the
game empties Lua.log every time it is launched.

The poll stands in for the Save Game button players are meant to press on
every turn-change screen and sometimes forget. When the hotseat save
folder holds no save of the current turn (none written since the turn can
have begun, autosaves included, and none named after its turn-change
screen, "<LEADER> <turn> <year>"), it saves the game the way end_turn
does, as "<LEADER> <turn> <year> MCP", or "... MCP MIDTURN" when the
player had already started playing. Only this script's loop saves;
log_current_turn, which the bot calls, never does.

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
from civ_mcp import hotseat, turn_watch  # noqa: E402
from civ_mcp.bridge import default_lua_log, hotseat_saves_dir  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterable

    from civ_mcp.bridge import BridgeConnection
    from civ_mcp.turn_watch import TurnStart, TurnWatch

log = logging.getLogger("turn_logger")

TURN_LOG_PATH = REPO_ROOT / "scripts" / "data" / "turn_log.jsonl"
WATCH_S = 1  # how often the loop reads what Lua.log gained
POLL_S = 900  # how often it asks the game anyway, in case a turn left no line in Lua.log
SCREEN_POLL_S = 2  # how late a row is when a turn-change screen line set off its poll: one WATCH_S and the poll
SAVE_LOOKBACK_S = 60  # how long before a turn's first row the game's autosave of it can be written
MIDTURN_SAVE_TAG = f"{hotseat.SAVE_TAG} MIDTURN"  # a save taken after the player pressed Start Turn

# Workday hours, local time, Monday to Friday.
WORKDAY_START_HOUR = 9
WORKDAY_END_HOUR = 17
WORKDAY = f"{WORKDAY_START_HOUR}:00–{WORKDAY_END_HOUR}:00, Mon–Fri"
TURN_LIMIT_S = 3600  # workday time after which another player may have a turn played


@dataclass(frozen=True)
class TurnRecord:
    at: datetime  # local time, naive
    turn: int
    leader: str  # e.g. LEADER_POUNDMAKER
    player: str  # hotseat player name shown on the turn-change screen
    poll_s: int | None = None  # how late the row can be: 0 from a turn line; None in rows from before 2026-11-03

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
            poll_s = int(row["poll_s"]) if row.get("poll_s") is not None else None
            records.append(TurnRecord(at, int(row["turn"]), row["leader"], row["player"], poll_s))
        except (ValueError, KeyError, TypeError):
            log.warning("skipping a bad row in %s: %r", path, line)
    return records


def record(
    turn: int,
    leader: str,
    player: str,
    now: datetime | None = None,
    path: Path | None = None,
    poll_s: int | None = None,
) -> bool:
    """Append a row when the game is on another turn or player than the last row says.

    `poll_s` is how long before `now` the turn can have begun, POLL_S
    unless given. Returns True when a row was written.
    """
    path = path or TURN_LOG_PATH
    records = read_records(path)
    if records and records[-1].key == (turn, leader, player):
        return False
    at = (now or datetime.now()).astimezone()
    row = {
        "at": at.isoformat(timespec="seconds"),
        "turn": turn,
        "leader": leader,
        "player": player,
        "poll_s": POLL_S if poll_s is None else poll_s,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return True


def log_turn_lines(starts: Iterable[TurnStart], path: Path | None = None) -> bool:
    """Add a row for each turn line the log has no row for yet; True when one was added.

    A row from a turn line has the line's time and poll_s 0. A line older
    than the log's last row is history the log already covers (a watch's
    first read takes all of Lua.log), so it is passed over.
    """
    wrote = False
    for start in starts:
        records = read_records(path)
        if records and start.at < records[-1].at:
            continue
        if record(start.turn, start.leader, start.player, now=start.at, path=path, poll_s=0):
            log.info("turn %d: %s (%s)", start.turn, start.player, start.leader)
            wrote = True
    return wrote


async def poll(
    path: Path | None = None,
    save: bool = False,
    saves_dir: Path | None = None,
    poll_s: int | None = None,
) -> hotseat.HotseatStatus | None:
    """Ask the game whose turn it is, add a row if that changed and, with `save`, see that the turn has a save.

    The row takes the time of the turn's line in Lua.log when the latest
    line is this turn's; otherwise it is the time of the poll, with
    `poll_s` (POLL_S unless given) as how late it can be. None when no
    hotseat turn answers: Civ 6 closed, at the main menu, loading, or
    between two players. A save that fails is logged, not raised.
    """
    conn = await hotseat.open_connection()
    if conn is None:
        return None
    st = await hotseat.whose_turn(conn)
    if st is None:
        return None
    watch = turn_watch.default()
    watch.read()
    key = (st.turn, st.leader, st.player_name)
    line = watch.latest if watch.latest is not None and watch.latest.key == key else None
    from_line = line is not None and log_turn_lines([line], path)
    if not from_line and record(st.turn, st.leader, st.player_name, path=path, poll_s=poll_s):
        log.info("turn %d: %s (%s), no turn line in Lua.log", st.turn, st.player_name, st.leader)
    if save:
        try:
            await ensure_saved(conn, st, path, saves_dir)
        except Exception:
            log.exception("could not see that turn %d has a save", st.turn)
    return st


def log_current_turn(path: Path | None = None) -> bool:
    """Ask the game whose turn it is and add a row if that changed; False when no hotseat turn answers."""
    return asyncio.run(poll(path)) is not None


def tick(
    watch: TurnWatch,
    next_poll: float,
    path: Path | None = None,
    saves_dir: Path | None = None,
    now: float | None = None,
) -> float:
    """One pass of the loop: log new turn lines, and ask the game if one came in or a poll is due.

    `next_poll` and `now` are time.monotonic() values; returns the next
    poll's. A turn line or a turn-change screen line sets off a poll at
    once, which sees that the new turn has a save.
    """
    now = time.monotonic() if now is None else now
    try:
        screens = watch.screens
        started = log_turn_lines(watch.read(), path)
        if started or watch.screens != screens:
            asyncio.run(poll(path, save=True, saves_dir=saves_dir, poll_s=SCREEN_POLL_S))
        elif now < next_poll:
            return next_poll
        else:
            asyncio.run(poll(path, save=True, saves_dir=saves_dir))
    except Exception:
        log.exception("turn watch failed")
    return now + POLL_S


def run(path: Path | None = None, saves_dir: Path | None = None) -> None:
    """The logger's loop. Never returns.

    The watch's first read is all of Lua.log since the game was launched:
    the turns in it that the log has no row for (this script was not
    running) are added, and the game is asked at once.
    """
    watch = turn_watch.default()
    log_turn_lines(watch.read(), path)
    next_poll = 0.0
    while True:
        next_poll = tick(watch, next_poll, path, saves_dir)
        time.sleep(WATCH_S)


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
    on the previous player then) and at most the first row's poll_s before
    it (rows from before poll_s: SAVE_LOOKBACK_S, their polls' interval),
    so any save written since then is one of this turn. An autosave (in an
    "auto" folder) counts from SAVE_LOOKBACK_S before the first row: the
    game writes it while a game turn opens, a little before the first
    seat's turn-change screen shows. A save named the way the game offers
    it on this turn's Save Game screen (`default_name`, "<LEADER> <turn>
    <year>", whatever follows it) counts from the previous row on, however
    late the turn was first logged.
    """
    after_previous = previous.at.timestamp() if previous else float("-inf")
    window = SAVE_LOOKBACK_S if first.poll_s is None else first.poll_s
    begun = max(after_previous, (first.at - timedelta(seconds=window)).timestamp())
    autosaves_from = max(after_previous, (first.at - timedelta(seconds=max(window, SAVE_LOOKBACK_S))).timestamp())
    prefix = default_name.casefold()
    for f in files:
        mtime = f.stat().st_mtime
        since = autosaves_from if f.parent.name.casefold() == "auto" else begun
        if mtime >= since or (prefix and mtime >= after_previous and f.stem.casefold().startswith(prefix)):
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
        "reading %s every %d s for turn starts, asking the game on each and every %d min; log %s, "
        "last turn: %s; a turn without a save in %s gets one",
        default_lua_log(), WATCH_S, POLL_S // 60, TURN_LOG_PATH, last, hotseat_saves_dir(),
    )
    run()


if __name__ == "__main__":
    main()
