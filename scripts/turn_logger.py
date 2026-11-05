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

When each turn was taken, as opposed to when its turn-change screen came
up, goes into scripts/data/taken_log.jsonl (beside the turn log): one row
per turn and taker, the first time the player pressed Start Turn ("by":
"player") and the first time Claude took the seat through the MCP tooling's
begin_standin_turn ("by": "claude"; Claude's own press of Start Turn comes
right after and counts as Claude's). poll_s is how late the row can be: 0
from the line the bridge mod or the tooling printed, which carries its
time; a second or two from the game's own line when the game runs a copy of
the mod from before those lines (civ_mcp.turn_watch). scripts/turn_times.py
measures a turn's play time from there to the turn's end.

Every save loaded into the game gets a row in scripts/data/reload_log.jsonl
(beside the turn log), from the turn line that follows the load: the
turn the save was on, whether Civ 6 had been closed and launched again
first (it empties Lua.log when it starts), whether the save was picked
in the Load Game menu or loaded by the MCP tooling, whether it put the
player mid-turn or back on the turn-change screen, whether they had
already pressed Start Turn on that turn, and when the turn log says the
turn began. A player who replays a turn from its start (save-scumming)
shows up as a row with had_started true and mid_turn false.

What the game looked like before the load is in the row's snapshot: the
bridge mod saves the game into Documents/My Games/Sid Meier's
Civilization VI/Snapshots (beside Saves, where the Load Game list does
not look) whenever the pause menu opens, which every way out of a game
goes through. The row names the newest snapshot taken before the load
since the turn then in progress began, or null. Snapshots no row names
are deleted after SNAPSHOT_KEEP_S.

The poll stands in for the Save Game button players are meant to press on
every turn-change screen and sometimes forget. When the hotseat save
folder holds no save of the current turn (none written since the turn can
have begun and none named after its turn-change screen, "<LEADER> <turn>
<year>"; the game's own autosaves do not count, not even for the first
seat, whose turn the autosave opens), it saves the game the way end_turn
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
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
from civ_mcp import hotseat, turn_watch  # noqa: E402
from civ_mcp.bridge import default_lua_log, hotseat_saves_dir, snapshots_dir  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterable

    from civ_mcp.bridge import BridgeConnection
    from civ_mcp.turn_watch import TurnStart, TurnTaken, TurnWatch

log = logging.getLogger("turn_logger")

TURN_LOG_PATH = REPO_ROOT / "scripts" / "data" / "turn_log.jsonl"
RELOAD_LOG_NAME = "reload_log.jsonl"  # beside the turn log
TAKEN_LOG_NAME = "taken_log.jsonl"  # beside the turn log
SNAPSHOTS_DIR = snapshots_dir()  # the bridge mod's saves from each opening of the pause menu
SNAPSHOT_KEEP_S = 7 * 86400  # how long a snapshot no reload log row names is kept (~2 MB each)
WATCH_S = 1  # how often the loop reads what Lua.log gained
POLL_S = 900  # how often it asks the game anyway, in case a turn left no line in Lua.log
SCREEN_POLL_S = 2  # how late a row is when a turn-change screen line set off its poll: one WATCH_S and the poll
SAVE_LOOKBACK_S = 60  # how late a row from before poll_s can be, for the saves that count as its turn's
MIDTURN_SAVE_TAG = f"{hotseat.SAVE_TAG} MIDTURN"  # a save taken after the player pressed Start Turn

# Workday hours, local time, Monday to Friday. The grace period (lunch) inside
# them counts as outside work hours: turn clocks, deadlines and totals skip it.
WORKDAY_START_HOUR = 9
WORKDAY_END_HOUR = 17
GRACE_START = clock(12, 15)
GRACE_END = clock(12, 45)
WORKDAY_SPANS = ((clock(WORKDAY_START_HOUR), GRACE_START), (GRACE_END, clock(WORKDAY_END_HOUR)))
WORKDAY = ", ".join(f"{a.hour}:{a.minute:02d}–{b.hour}:{b.minute:02d}" for a, b in WORKDAY_SPANS) + ", Mon–Fri"
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


def reload_log_path(path: Path | None = None) -> Path:
    """The reload log beside the turn log at `path`."""
    return (path or TURN_LOG_PATH).with_name(RELOAD_LOG_NAME)


def taken_log_path(path: Path | None = None) -> Path:
    """The taken log beside the turn log at `path`."""
    return (path or TURN_LOG_PATH).with_name(TAKEN_LOG_NAME)


@dataclass(frozen=True)
class TakenRecord:
    """A row of the taken log: when a turn was taken, and by whom."""

    at: datetime  # local time, naive
    turn: int
    leader: str
    player: str  # whose turn it is, whoever took it
    by: str  # "player" (pressed Start Turn) or "claude" (took the seat as stand-in)
    poll_s: int  # how late the row can be: 0 from a line with its own time

    @property
    def key(self) -> tuple[int, str, str]:
        return self.turn, self.leader, self.player


def read_taken(path: Path | None = None) -> list[TakenRecord]:
    """The taken log's rows; `path` is the turn log beside it."""
    taken_path = taken_log_path(path)
    if not taken_path.exists():
        return []
    rows = []
    for line in taken_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            rows.append(TakenRecord(
                _local(row["at"]), int(row["turn"]), row["leader"], row["player"], row["by"], int(row["poll_s"])
            ))
        except (ValueError, KeyError, TypeError):
            log.warning("skipping a bad row in %s: %r", taken_path, line)
    return rows


def log_taken(items: Iterable[TurnTaken], path: Path | None = None) -> int:
    """Add a taken log row for each turn and taker it has none for yet; the number added.

    `path` is the turn log; the taken log is beside it. Only the first
    time counts: the press after a reload of the turn, or the whole of
    Lua.log offered again after a restart, adds nothing.
    """
    done = {(r.key, r.by) for r in read_taken(path)}
    added = 0
    for t in items:
        if (t.key, t.by) in done:
            continue
        row = {
            "at": _stamp(t.at),
            "turn": t.turn,
            "leader": t.leader,
            "player": t.player,
            "by": t.by,
            "poll_s": t.late_s,
        }
        taken_path = taken_log_path(path)
        taken_path.parent.mkdir(parents=True, exist_ok=True)
        with open(taken_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        done.add((t.key, t.by))
        added += 1
        log.info(
            "turn %d: %s at %s%s",
            t.turn,
            f"{t.player} pressed Start Turn" if t.by == turn_watch.PLAYER else f"Claude took {t.player}'s seat",
            f"{t.at:%H:%M:%S}",
            f" (read up to {t.late_s} s after)" if t.late_s else "",
        )
    return added


def _local(at: str) -> datetime:
    return datetime.fromisoformat(at).astimezone().replace(tzinfo=None)


def _stamp(at: datetime) -> str:
    return at.astimezone().isoformat(timespec="seconds")


def read_reloads(path: Path | None = None) -> list[dict]:
    """The reload log's rows; `path` is the turn log beside it."""
    reload_path = reload_log_path(path)
    if not reload_path.exists():
        return []
    rows = []
    for line in reload_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            _local(row["at"])
            rows.append(row)
        except (ValueError, KeyError, TypeError):
            log.warning("skipping a bad row in %s: %r", reload_path, line)
    return rows


def snapshot_before(at: datetime, since: datetime | None, folder: Path | None = None) -> Path | None:
    """The newest snapshot saved before `at` and not before `since`, or None."""
    folder = folder or SNAPSHOTS_DIR
    best, best_mtime = None, None
    try:
        files = list(folder.glob("*.Civ6Save"))
    except OSError:
        return None
    for f in files:
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
        except OSError:
            continue
        if mtime < at and (since is None or mtime >= since) and (best_mtime is None or mtime > best_mtime):
            best, best_mtime = f, mtime
    return best


def prune_snapshots(path: Path | None = None, folder: Path | None = None, now: float | None = None) -> int:
    """Delete snapshots older than SNAPSHOT_KEEP_S that no reload log row names; the number deleted."""
    folder = folder or SNAPSHOTS_DIR
    named = {row.get("snapshot") for row in read_reloads(path)}
    cutoff = (time.time() if now is None else now) - SNAPSHOT_KEEP_S
    deleted = 0
    try:
        files = list(folder.glob("*.Civ6Save"))
    except OSError:
        return 0
    for f in files:
        try:
            if f.name not in named and f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError:
            log.warning("could not delete the old snapshot %s", f)
    if deleted:
        log.info("deleted %d snapshot(s) older than %d days", deleted, SNAPSHOT_KEEP_S // 86400)
    return deleted


def _turn_in_progress(records: list[TurnRecord], at: datetime) -> TurnRecord | None:
    """The first row of the turn the log was on just before `at` (see current_turn), or None."""
    before = [r for r in records if r.at < at]
    if not before:
        return None
    return next(r for r in before if r.key == before[-1].key)


def log_reloads(starts: Iterable[TurnStart], path: Path | None = None, snapshots: Path | None = None) -> int:
    """Add a reload log row for each turn line that came right after a save was loaded; the number added.

    `path` is the turn log; the reload log is beside it. A load the
    reload log already has (same time, turn and player) is passed over,
    so the whole of Lua.log can be offered again after a restart.
    turn_began is the first turn log row of that turn from before the
    load, None when the turn log had none. snapshot is the newest file in
    `snapshots` (SNAPSHOTS_DIR) saved before the load since the turn in
    progress then began (its first row, less that row's poll_s).
    """
    done = {(_local(row["at"]), int(row["turn"]), row["leader"], row["player"]) for row in read_reloads(path)}
    records = read_records(path)
    added = 0
    for start in starts:
        if start.reload is None or (start.at, *start.key) in done:
            continue
        began = next((r.at for r in records if r.key == start.key and r.at < start.at), None)
        current = _turn_in_progress(records, start.at)
        since = current.at - timedelta(seconds=current.poll_s or 0) if current else None
        snapshot = snapshot_before(start.at, since, snapshots)
        reload_path = reload_log_path(path)
        row = {
            "at": _stamp(start.at),
            "turn": start.turn,
            "leader": start.leader,
            "player": start.player,
            "relaunched": start.reload.relaunched,
            "menu": start.reload.menu,
            "mid_turn": start.loaded,
            "had_started": start.reload.had_started,
            "turn_began": _stamp(began) if began else None,
            "snapshot": snapshot.name if snapshot else None,
        }
        reload_path.parent.mkdir(parents=True, exist_ok=True)
        with open(reload_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        done.add((start.at, *start.key))
        added += 1
        log.info(
            "save loaded at %s: %s's turn %d%s; %s, %s, %s; %s; state before it: %s",
            f"{start.at:%a %d %b %H:%M:%S}",
            start.player,
            start.turn,
            f" (began {began:%a %H:%M:%S})" if began else " (not in the turn log before)",
            "Civ 6 relaunched first" if start.reload.relaunched else "no relaunch",
            "picked in the Load Game menu" if start.reload.menu else "loaded by the MCP tooling",
            "mid-turn" if start.loaded else "on the turn-change screen",
            {True: "had pressed Start Turn before", False: "had not pressed Start Turn", None: "Start Turn before: unknown"}[
                start.reload.had_started
            ],
            snapshot.name if snapshot else "no snapshot",
        )
    return added


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
    once, which sees that the new turn has a save. Loads the watch saw,
    in this pass or the poll's read, go into the reload log, turns being
    taken into the taken log, and a pass that polled prunes old snapshots.
    """
    now = time.monotonic() if now is None else now
    polled = True
    try:
        screens = watch.screens
        started = log_turn_lines(watch.read(), path)
        if started or watch.screens != screens:
            asyncio.run(poll(path, save=True, saves_dir=saves_dir, poll_s=SCREEN_POLL_S))
        elif now < next_poll:
            polled = False
        else:
            asyncio.run(poll(path, save=True, saves_dir=saves_dir))
    except Exception:
        log.exception("turn watch failed")
    try:
        if watch.reloads:
            log_reloads(watch.reloads, path)
            watch.reloads.clear()
        if polled:
            prune_snapshots(path)
    except Exception:
        log.exception("could not log a save load")
    try:
        if watch.taken:
            log_taken(watch.taken, path)
            watch.taken.clear()
    except Exception:
        log.exception("could not log a turn being taken")
    return now + POLL_S if polled else next_poll


def run(path: Path | None = None, saves_dir: Path | None = None) -> None:
    """The logger's loop. Never returns.

    The watch's first read is all of Lua.log since the game was launched:
    the turns in it that the log has no row for (this script was not
    running) are added, so are the loads the reload log has no row for
    (with had_started unknown when the turn began before the launch), and
    the game is asked at once.
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
    so any save written since then is one of this turn. A save named the
    way the game offers it on this turn's Save Game screen (`default_name`,
    "<LEADER> <turn> <year>", whatever follows it) counts from the previous
    row on, however late the turn was first logged.

    The game's own autosaves (in an "auto" folder) never count. The one
    that opens each game turn is written in the same second as the first
    seat's turn line, but before the game hands that seat over, so the
    Load Game list shows it as the last seat's save; the game keeps only
    the last ten, and a load can write it again. The first seat gets a
    save under its own name like every other seat.
    """
    after_previous = previous.at.timestamp() if previous else float("-inf")
    window = SAVE_LOOKBACK_S if first.poll_s is None else first.poll_s
    begun = max(after_previous, (first.at - timedelta(seconds=window)).timestamp())
    prefix = default_name.casefold()
    for f in files:
        if f.parent.name.casefold() == "auto":
            continue
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


def _workday_spans(day: date) -> list[tuple[datetime, datetime]]:
    """`day`'s stretches of workday time (WORKDAY_SPANS), in order; none at the weekend."""
    if day.weekday() >= 5:
        return []
    return [(datetime.combine(day, a), datetime.combine(day, b)) for a, b in WORKDAY_SPANS]


def is_workday(dt: datetime) -> bool:
    return any(opens <= dt < closes for opens, closes in _workday_spans(dt.date()))


def workday_seconds(start: datetime, end: datetime) -> float:
    """Seconds between two naive local times that fall in workday hours (not the grace period)."""
    total = 0.0
    day = start.date()
    while day <= end.date():
        for opens, closes in _workday_spans(day):
            total += max(0.0, (min(end, closes) - max(start, opens)).total_seconds())
        day += timedelta(days=1)
    return total


def workday_deadline(start: datetime, seconds: float) -> datetime:
    """The naive local time at which `seconds` of workday time since `start` are up."""
    day = start.date()
    while True:
        for opens, closes in _workday_spans(day):
            opens = max(start, opens)
            open_s = (closes - opens).total_seconds()
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
        "reading %s every %d s for turn starts and loads, asking the game on each and every %d min; log %s, "
        "loads in %s, last turn: %s; a turn without a save in %s gets one",
        default_lua_log(), WATCH_S, POLL_S // 60, TURN_LOG_PATH, reload_log_path().name, last, hotseat_saves_dir(),
    )
    run()


if __name__ == "__main__":
    main()
