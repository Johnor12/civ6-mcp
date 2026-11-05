#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Turn times: how long each player's turns take, from the turn log and the hotseat saves.

    uv run scripts/turn_times.py            # every player's average, slowest first
    uv run scripts/turn_times.py --turns    # plus every turn and what its timing rests on

The turn log (scripts/turn_logger.py) has a row for each player-turn it saw
begin. Since 2026-11-03 nearly every row comes from the bridge mod's turn
line in Lua.log and has the time the turn began (poll_s 0). The older rows
came from polls: such a row says the turn had begun by then and no more
than one poll earlier (the interval the row carries as poll_s, or for rows
from before 2026-11-03, the one POLL_CHANGES gives for their time). A poll
could miss a player who finished within the interval, so the sequence of
player-turns is rebuilt from the
seat order (SEATS) and the saves in the hotseat folder fill in what the log
missed: a save named "<LEADER> <turn> <year>..." (the game's default name,
which the players keep and which the tooling's "MCP" saves start with) says
that player's turn was under way when it was written, and the game's
autosave at the start of each game turn says exactly when the first seat's
turn began. Each turn's start is placed in the middle of the window those
facts leave, and players with no evidence of their own are spaced evenly
between their neighbours, which averages the time over the turns taken.

Beside each turn's workday time, its play time: from when it was taken
(turn_logger's taken log: the player pressed Start Turn, or Claude took the
seat as stand-in) to its end. A turn Claude took counts as Claude's, not
the player's: player_averages leaves it out of the player's play time
and claude_takes lists it, while its turn time still counts for the player.
Turns from before the taken log began have a workday time only.

A turn's workday time (minutes) is minutes of workday time
(turn_logger.WORKDAY, what the bot's turn limit counts), so a turn held
overnight costs nothing. Play time is minutes at any hour: a turn taken at
8:30 and finished at 8:45 has 15 minutes of play time though it has no
workday time. Its turn time (overall_minutes) joins the two, wait and
play: the workday time plus the play time outside the workday, so a turn
waited on until 17:00 and then played until 17:30 has the half hour after
17:00 too. The averages, medians, longest, slowest and fastest turns go by
turn time. A turn with no workday time at all, begun and finished outside
it, still counts as a turn, but the averages, medians and the fastest turn
leave it out (workday_turns): a turn played overnight says nothing about
how long a player takes. scripts/turn_report.py
posts player_averages() in the game channel every workday morning, and
scripts/turn_announcer.py lists the turns finished since its last post when
it pings the next player. Standard library only; turn_logger and civ_mcp
come from the repo.
"""

from __future__ import annotations

import argparse
import logging
import re
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
import turn_logger  # noqa: E402  scripts/turn_logger.py, next to this script
from civ_mcp.bridge import hotseat_saves_dir  # noqa: E402
from civ_mcp.hotseat import hotseat_save_files  # noqa: E402

log = logging.getLogger("turn_times")

# How often the turn logger polled before its rows carried the interval
# (poll_s, since 2026-11-03), by the moment each interval took effect; the
# first entry is the interval the log began with. Only rows without poll_s
# use it, so a later change of turn_logger.POLL_S needs no entry here.
POLL_CHANGES: tuple[tuple[datetime, int], ...] = (
    (datetime.min, 300),
    (datetime(2026, 10, 29, 16, 21), 60),  # commit 9158f04, "shorten polling interval to 1 minute"
)

# The game's seats in turn order: the leader type the turn log records and
# the leader name the game puts first in its default save name for that
# player ("<LEADER> <turn> <year>"). A leader in the log that is missing
# here stops the script with a message saying so.
SEATS: tuple[tuple[str, str], ...] = (
    ("LEADER_CLEOPATRA", "CLEOPATRA (EGYPTIAN)"),
    ("LEADER_NZINGA_MBANDE", "NZINGA MBANDE"),
    ("LEADER_VICTORIA", "VICTORIA (AGE OF EMPIRE)"),
    ("LEADER_SUK_AL_HASAN", "AL-HASAN IBN SULAIMAN"),
    ("LEADER_JFD_STANISLAW", "STANISLAW II"),
    ("LEADER_JULIUS_CAESAR", "JULIUS CAESAR"),
    ("LEADER_GITARJA", "GITARJA"),
    ("LEADER_GENGHIS_KHAN", "GENGHIS KHAN"),
    ("LEADER_AMANITORE", "AMANITORE"),
    ("LEADER_LIME_THULE_DAVE", "KIVIUQ"),
)

# "<LEADER> <turn> <year BC|AD>" and whatever tag follows ("MCP", "MCP MIDTURN").
SAVE_NAME = re.compile(r"^(?P<leader>.+?) (?P<turn>\d+) \d+ (?:BC|AD)(?: .*)?$")


def leader_display(leader: str) -> str:
    return leader.replace("LEADER_", "").replace("_", " ").title()


def poll_s_at(when: datetime) -> int:
    """The logger's polling interval, in seconds, when a row without its own poll_s was written at `when`."""
    return next(seconds for since, seconds in reversed(POLL_CHANGES) if since <= when)


@dataclass
class PlayerTurn:
    turn: int
    seat: int
    leader: str
    player: str
    rows: list[datetime] = field(default_factory=list)  # log rows of this turn
    poll_s: int = 0  # the logger's interval when the first of them was written
    saves: list[tuple[datetime, str]] = field(default_factory=list)  # (written at, save name)
    autosave: datetime | None = None  # the game's autosave that opened this game turn
    start: datetime | None = None
    earliest: datetime | None = None  # the window the start was placed in
    latest: datetime | None = None
    basis: str = ""  # autosave | row | save | row+save | interpolated
    taken: datetime | None = None  # first taken log row, the middle of its window; None: none
    claude: bool = False  # Claude took the seat on this turn

    @property
    def evidence(self) -> list[datetime]:
        """Every moment this turn is known to have been under way."""
        times = [*self.rows, *(at for at, _ in self.saves)]
        if self.autosave is not None:
            times.append(self.autosave)
        return times


@dataclass(frozen=True)
class PlayerAverage:
    """One player's finished turns: turn times (overall_minutes) and play times, in minutes.

    The mean and median turn time leave out the turns with no workday time
    (off_hours); they are None when every turn is one. The longest covers
    every turn.
    """

    player: str
    leader: str
    seat: int  # 1-based
    turns: int
    guessed: int  # turns with no row or save of their own
    off_hours: int  # turns begun and finished outside the workday
    mean_minutes: float | None
    median_minutes: float | None
    max_minutes: float
    taken: int = 0  # turns the player took themselves with a play time
    take_mean_minutes: float | None = None  # their average play time; None without one


def player_turns(
    records: list[turn_logger.TurnRecord], poll_at: Callable[[datetime], int] = poll_s_at
) -> list[PlayerTurn]:
    """Every player-turn from the first row to the last, in seat order, with the rows that saw it.

    Each takes the logger's interval from its first row, or from `poll_at`
    when that row predates poll_s. Rows that go backwards (a reload of an
    earlier save) are skipped with a warning.
    """
    seat_of = {leader: i for i, (leader, _) in enumerate(SEATS)}
    names: dict[str, str] = {}
    for r in records:
        if r.leader not in seat_of:
            raise ValueError(f"{r.leader} is not in SEATS; add it, with the name the game gives its saves")
        names.setdefault(r.leader, r.player)

    def new(turn: int, seat: int) -> PlayerTurn:
        leader = SEATS[seat][0]
        return PlayerTurn(turn, seat, leader, names.get(leader) or leader_display(leader))

    seq: list[PlayerTurn] = []
    cur = new(records[0].turn, seat_of[records[0].leader])
    for r in records:
        key = (r.turn, seat_of[r.leader])
        if key < (cur.turn, cur.seat):
            log.warning(
                "%s: turn %d %s comes after turn %d %s in the log; skipped",
                r.at.strftime("%d %b %H:%M"), r.turn, r.leader, cur.turn, cur.leader,
            )
            continue
        while (cur.turn, cur.seat) != key:
            seq.append(cur)
            seat = (cur.seat + 1) % len(SEATS)
            cur = new(cur.turn + (seat == 0), seat)
        if not cur.rows:
            cur.poll_s = r.poll_s if r.poll_s is not None else poll_at(r.at)
        cur.rows.append(r.at)
    seq.append(cur)
    return seq


def _rows_before(seq: list[PlayerTurn], k: int) -> datetime | None:
    times = [t for pt in seq[:k] for t in pt.rows]
    return max(times) if times else None


def _rows_after(seq: list[PlayerTurn], k: int) -> datetime | None:
    times = [t for pt in seq[k + 1:] for t in pt.rows]
    return min(times) if times else None


def attach_saves(seq: list[PlayerTurn], saves_dir: Path | None) -> list[str]:
    """Give each player-turn the saves written during it. Returns the saves that fit nowhere."""
    index = {(pt.turn, pt.leader): k for k, pt in enumerate(seq)}
    leader_of = {name.casefold(): leader for leader, name in SEATS}
    begun = min(seq[0].rows) - timedelta(seconds=seq[0].poll_s)
    skipped: list[str] = []
    autosaves: list[tuple[datetime, str]] = []
    for f, tag in hotseat_save_files(saves_dir):
        at = datetime.fromtimestamp(f.stat().st_mtime)
        if at < begun:
            continue
        if tag == " [autosave]":
            autosaves.append((at, f.stem))
            continue
        if tag:  # a quicksave says nothing about whose turn it was
            continue
        m = SAVE_NAME.match(f.stem)
        if not m:
            continue
        leader = leader_of.get(m["leader"].casefold())
        if leader is None:
            skipped.append(f"{f.stem}: no seat is named {m['leader']!r}")
            continue
        k = index.get((int(m["turn"]), leader))
        if k is None:
            continue  # a turn the log does not cover
        before, after = _rows_before(seq, k), _rows_after(seq, k)
        if (before is not None and at < before) or (after is not None and at > after):
            skipped.append(f"{f.stem}, written {at:%d %b %H:%M}: the log puts another turn there")
            continue
        seq[k].saves.append((at, f.stem))

    for at, stem in autosaves:
        k = _autosave_turn(seq, at)
        if k is None:
            skipped.append(f"{stem}, written {at:%d %b %H:%M}: no game turn the log covers began then")
            continue
        pt = seq[k]
        pt.autosave = at if pt.autosave is None else min(pt.autosave, at)
    return skipped


def _autosave_turn(seq: list[PlayerTurn], at: datetime) -> int | None:
    """The first-seat player-turn that an autosave written at `at` opened, if any."""
    for k, pt in enumerate(seq):
        if pt.seat != 0:
            continue
        before = [t for p in seq[:k] for t in p.evidence]
        after = [t for p in seq[k:] for t in p.evidence]
        if (not before or max(before) <= at) and (not after or at <= min(after)):
            return k
    return None


def estimate_starts(seq: list[PlayerTurn]) -> None:
    """Place every player-turn's start: exactly, in the middle of its window, or spaced between neighbours.

    A turn with rows had begun no more than its poll_s before the first.
    """
    last_known: datetime | None = None  # the latest moment an earlier turn was known to be under way
    for pt in seq:
        evidence = pt.evidence
        if pt.autosave is not None:
            pt.start = pt.earliest = pt.latest = pt.autosave
            pt.basis = "autosave"
        elif evidence:
            hi = min(evidence)
            bounds = [last_known] if last_known is not None else []
            if pt.rows:
                bounds.append(min(pt.rows) - timedelta(seconds=pt.poll_s))
            lo = min(max(bounds), hi) if bounds else hi
            pt.earliest, pt.latest = lo, hi
            pt.start = lo + (hi - lo) / 2
            pt.basis = "+".join(name for name, ok in (("row", pt.rows), ("save", pt.saves)) if ok)
        else:
            pt.basis = "interpolated"
        if evidence:
            latest = max(evidence)
            last_known = latest if last_known is None else max(last_known, latest)

    k = 0
    while k < len(seq):
        if seq[k].start is not None:
            k += 1
            continue
        j = k
        while seq[j].start is None:
            j += 1  # the last player-turn has a row, so this ends
        prev, nxt = seq[k - 1], seq[j]  # the first player-turn has a row, so k > 0
        lo = max([prev.start, *prev.evidence])
        hi = max(nxt.start, lo)
        n = j - k
        for i in range(n):
            pt = seq[k + i]
            pt.start = lo + (hi - lo) * (i + 1) / (n + 1)
            pt.earliest, pt.latest = lo, hi
        k = j

    for a, b in zip(seq, seq[1:]):
        if b.start < a.start:
            b.start = a.start


def compute(
    log_path: Path | None = None,
    saves_dir: Path | None = None,
    poll_at: Callable[[datetime], int] = poll_s_at,
) -> tuple[list[PlayerTurn], list[str]]:
    """The player-turns the log covers, with their starts placed, and the saves that fit nowhere.

    The last player-turn is the one in progress; it has a start but no end.
    `poll_at` gives the interval of rows without poll_s. Both lists are
    empty while the log is. ValueError for a leader not in SEATS.
    """
    records = turn_logger.read_records(log_path)
    if not records:
        return [], []
    seq = player_turns(records, poll_at)
    skipped = attach_saves(seq, saves_dir)
    estimate_starts(seq)
    attach_taken(seq, turn_logger.read_taken(log_path))
    return seq, skipped


def attach_taken(seq: list[PlayerTurn], rows: list[turn_logger.TakenRecord]) -> None:
    """Give each player-turn when it was taken (its first taken log row) and whether Claude took it.

    A row's time is placed in the middle of its window (poll_s), and never
    before the turn's own start. Rows of turns the log does not cover are
    passed over.
    """
    index = {(pt.turn, pt.leader): pt for pt in seq}
    for r in rows:
        pt = index.get((r.turn, r.leader))
        if pt is None:
            continue
        at = max(r.at - timedelta(seconds=r.poll_s / 2), pt.start)
        pt.taken = at if pt.taken is None else min(pt.taken, at)
        pt.claude = pt.claude or r.by == "claude"


def finished(seq: list[PlayerTurn]) -> list[tuple[PlayerTurn, datetime]]:
    """Every player-turn that has ended, with its end (the next turn's start)."""
    return [(pt, nxt.start) for pt, nxt in zip(seq, seq[1:])]


def workday_of(when: datetime) -> date:
    """The workday a moment counts towards: the last weekday whose workday had begun by then.

    Overnight, until the next workday begins (turn_logger.WORKDAY_START_HOUR),
    and over the weekend, it is still the workday before: a turn that ends
    at 8:46 on Thursday is Wednesday's, one that ends on Saturday or before
    9:00 on Monday is Friday's.
    """
    day = when.date()
    if when.hour < turn_logger.WORKDAY_START_HOUR:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def ended_on(seq: list[PlayerTurn], day: date) -> list[tuple[PlayerTurn, datetime]]:
    """The finished player-turns that count towards workday `day`: those whose end has it as workday_of."""
    return [(pt, end) for pt, end in finished(seq) if workday_of(end) == day]


def turns_per_day(seq: list[PlayerTurn]) -> dict[date, int]:
    """How many player-turns each workday that had one counts (as ended_on counts them), by workday_of their end."""
    counts: dict[date, int] = {}
    for _, end in finished(seq):
        day = workday_of(end)
        counts[day] = counts.get(day, 0) + 1
    return counts


def ended_after(seq: list[PlayerTurn], when: datetime) -> list[tuple[PlayerTurn, datetime]]:
    """The finished player-turns that ended after `when`."""
    return [(pt, end) for pt, end in finished(seq) if end > when]


def minutes(pt: PlayerTurn, end: datetime) -> float:
    """A finished turn's minutes of workday time (what the bot's turn limit counts)."""
    return turn_logger.workday_seconds(pt.start, end) / 60


def overall_minutes(pt: PlayerTurn, end: datetime) -> float:
    """A finished turn's turn time: its workday time (minutes) and its play time joined, each minute once.

    The workday time leaves out play outside the workday, the play time
    (take_minutes) the wait before Start Turn; this keeps both. A turn
    begun at 16:00 and played from 17:00 to 17:30 has 1h 30m; one begun at
    16:30, taken at 8:30 the next morning and finished at 9:15 has 1h 15m
    (16:30 to 17:00, then 8:30 to 9:15). Without a play time, the workday time.
    """
    total = turn_logger.workday_seconds(pt.start, end)
    if pt.taken is None:
        return total / 60
    taken = min(max(pt.taken, pt.start), end)
    off_hours_play = (end - taken).total_seconds() - turn_logger.workday_seconds(taken, end)
    return (total + off_hours_play) / 60


def workday_turns(done: list[tuple[PlayerTurn, datetime]]) -> list[tuple[PlayerTurn, datetime]]:
    """The turns among `done` with some workday time: all but those begun and finished outside the workday.

    The average and median turn time and the fastest turn go by these: a
    turn played overnight says nothing about how long a player takes.
    """
    return [(pt, end) for pt, end in done if minutes(pt, end) > 0]


def timed_turns(done: list[tuple[PlayerTurn, datetime]]) -> list[tuple[PlayerTurn, datetime, float]]:
    """Every turn among `done` with its end and its turn time (overall_minutes), in the order of `done`.

    max() over all of `done` gives the slowest turn, min() over its
    workday_turns the fastest; ties go to the first turn that reached them.
    """
    return [(pt, end, overall_minutes(pt, end)) for pt, end in done]


def mean_minutes(done: list[tuple[PlayerTurn, datetime]]) -> float | None:
    """The average turn time among `done` (see finished, ended_on), over its workday_turns; None for none."""
    mins = [m for _, _, m in timed_turns(workday_turns(done))]
    return statistics.mean(mins) if mins else None


def median_minutes(done: list[tuple[PlayerTurn, datetime]]) -> float | None:
    """The median turn time among `done`, over its workday_turns; None for none."""
    mins = [m for _, _, m in timed_turns(workday_turns(done))]
    return statistics.median(mins) if mins else None


def take_minutes(pt: PlayerTurn, end: datetime) -> float | None:
    """A finished turn's play time, from when it was taken to its end, in minutes at any hour; None if unknown.

    Unlike the total (minutes), it is not limited to the workday: playing
    before 9:00 or after 17:00 is still playing.
    """
    if pt.taken is None:
        return None
    return (end - min(pt.taken, end)).total_seconds() / 60


def own_takes(done: list[tuple[PlayerTurn, datetime]]) -> list[tuple[PlayerTurn, datetime]]:
    """The turns among `done` that their player took (not Claude) with a known play time."""
    return [(pt, end) for pt, end in done if pt.taken is not None and not pt.claude]


def claude_takes(done: list[tuple[PlayerTurn, datetime]]) -> list[tuple[PlayerTurn, datetime]]:
    """The turns among `done` that Claude took as stand-in."""
    return [(pt, end) for pt, end in done if pt.claude]


def timed_takes(done: list[tuple[PlayerTurn, datetime]]) -> list[tuple[PlayerTurn, datetime, float]]:
    """The turns among `done` with a known play time, each with its end and that play time, in the order of `done`.

    Pass own_takes or claude_takes. max() and min() over the play time
    give the first turn that reached a tie, as `done` is in turn order.
    """
    return [(pt, end, m) for pt, end in done if (m := take_minutes(pt, end)) is not None]


def mean_take_minutes(done: list[tuple[PlayerTurn, datetime]]) -> float | None:
    """The average play time among `done` (pass own_takes or claude_takes); None for none."""
    mins = [m for _, _, m in timed_takes(done)]
    return statistics.mean(mins) if mins else None


def total_take_minutes(done: list[tuple[PlayerTurn, datetime]]) -> float | None:
    """The play time summed over `done` (pass own_takes or claude_takes); None for none."""
    mins = [m for _, _, m in timed_takes(done)]
    return sum(mins) if mins else None


def player_averages(done: list[tuple[PlayerTurn, datetime]]) -> list[PlayerAverage]:
    """One entry per player with a turn among `done` (see finished, ended_on), slowest on average first.

    The turn count and longest turn cover every turn, the mean and median
    only the workday_turns: players without one come last, by seat. The
    play time covers only the turns the player took themselves
    (own_takes): Claude's are in claude_takes.
    """
    by_player: dict[str, list[tuple[PlayerTurn, datetime]]] = {}
    for pt, end in done:
        by_player.setdefault(pt.player, []).append((pt, end))
    out = []
    for player, turns in by_player.items():
        first = turns[0][0]
        own = own_takes(turns)
        out.append(PlayerAverage(
            player=player,
            leader=first.leader,
            seat=first.seat + 1,
            turns=len(turns),
            guessed=sum(pt.basis == "interpolated" for pt, _ in turns),
            off_hours=len(turns) - len(workday_turns(turns)),
            mean_minutes=mean_minutes(turns),
            median_minutes=median_minutes(turns),
            max_minutes=max(m for _, _, m in timed_turns(turns)),
            taken=len(own),
            take_mean_minutes=mean_take_minutes(own),
        ))
    out.sort(key=lambda a: (a.mean_minutes is None, -(a.mean_minutes or 0.0), a.seat))
    return out


def hms(minutes: float) -> str:
    """4.37 -> '4m 22s', 95.4 -> '1h 35m 24s'."""
    h, rest = divmod(round(minutes * 60), 3600)
    m, s = divmod(rest, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def report(seq: list[PlayerTurn], skipped: list[str], turns: bool = False) -> str:
    done = finished(seq)
    if not done:
        return "The log has only the turn in progress; nothing to measure yet."
    lines = []
    if turns:
        for pt, end in done:
            take = take_minutes(pt, end)
            taken = (
                f"taken {pt.taken:%H:%M:%S}{' by Claude' if pt.claude else ''}, play time: {hms(take)}; "
                if take is not None else ""
            )
            lines.append(
                f"T{pt.turn:<3}{pt.player:<14}{pt.start:%a %d %H:%M:%S} -> {end:%a %H:%M:%S}  "
                f"{hms(overall_minutes(pt, end)):>11}  {pt.basis:<13}workday time: {hms(minutes(pt, end))}; {taken}"
                f"window {pt.earliest:%H:%M:%S}-{pt.latest:%H:%M:%S}  "
                + "; ".join(
                    [f"log {t:%H:%M:%S}" for t in sorted(pt.rows)]
                    + ([f"autosave {pt.autosave:%H:%M:%S}"] if pt.autosave else [])
                    + [f"'{name}' {t:%H:%M:%S}" for t, name in sorted(pt.saves)]
                )
            )
        lines.append("")
    def take(mean: float | None, n: int) -> str:
        return f"{hms(mean)} ({n})" if mean is not None else "-"

    def avg(mins: float | None) -> str:
        return hms(mins) if mins is not None else "-"

    lines.append(
        f"{'player':<14}{'turns':>6}{'guessed':>8}{'off hrs':>8}{'average':>12}{'median':>12}{'longest':>12}"
        f"{'play time (turns)':>20}"
    )
    for a in player_averages(done):
        lines.append(
            f"{a.player:<14}{a.turns:>6}{a.guessed:>8}{a.off_hours:>8}{avg(a.mean_minutes):>12}"
            f"{avg(a.median_minutes):>12}{hms(a.max_minutes):>12}{take(a.take_mean_minutes, a.taken):>20}"
        )
    own = own_takes(done)
    lines.append(
        f"{'ALL':<14}{len(done):>6}{'':>8}{len(done) - len(workday_turns(done)):>8}{avg(mean_minutes(done)):>12}"
        f"{avg(median_minutes(done)):>12}{'':>12}{take(mean_take_minutes(own), len(own)):>20}"
    )
    claude = claude_takes(done)
    if claude:
        lines.append(f"{'Claude':<14}{'':>58}{take(mean_take_minutes(claude), len(claude)):>20}")
    first, last = seq[0].start, seq[-1].start
    lines.append(
        f"\nTurn time: workday time ({turn_logger.WORKDAY}) from the turn's start to its end, plus play time "
        "outside it. The average and median leave out the turns with no workday time (off hrs: begun and "
        "finished outside it). "
        "Play time: from Start Turn (or Claude taking the seat) to the turn's end, at any hour; Claude's turns "
        "are in its own row, not the player's. "
        f"{len(done)} player-turns from {first:%a %d %b %H:%M} to {last:%a %d %b %H:%M}; "
        f"{sum(bool(pt.saves) for pt in seq)} have a save of their own, "
        f"{sum(pt.autosave is not None for pt in seq)} an autosave. "
        f"In progress, not counted: turn {seq[-1].turn} {seq[-1].player} since {last:%a %d %b %H:%M}."
    )
    if skipped:
        lines.append("\nSaves that fit no turn in the log:")
        lines.extend(f"  {s}" for s in skipped)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--log", type=Path, default=turn_logger.TURN_LOG_PATH, help="the turn log (jsonl)")
    parser.add_argument("--saves", type=Path, default=None, help=f"hotseat save folder (default: {hotseat_saves_dir()})")
    parser.add_argument("--turns", action="store_true", help="also list every turn and what its timing rests on")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        seq, skipped = compute(args.log, args.saves)
    except ValueError as e:
        raise SystemExit(str(e)) from None
    print(report(seq, skipped, turns=args.turns))


if __name__ == "__main__":
    main()
