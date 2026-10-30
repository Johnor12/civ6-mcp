#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Turn times: how long each player's turns take, from the turn log and the hotseat saves.

    uv run scripts/turn_times.py            # every player's average, slowest first
    uv run scripts/turn_times.py --turns    # plus every turn and what its timing rests on

The turn log (scripts/turn_logger.py) has a row for the first poll that saw
each player's turn-change screen, so a row says that turn had begun by then
and no more than one poll earlier (the interval in force at the time: see
POLL_CHANGES). A poll can miss a player who finished
within the interval, so the sequence of player-turns is rebuilt from the
seat order (SEATS) and the saves in the hotseat folder fill in what the log
missed: a save named "<LEADER> <turn> <year>..." (the game's default name,
which the players keep and which the tooling's "MCP" saves start with) says
that player's turn was under way when it was written, and the game's
autosave at the start of each game turn says exactly when the first seat's
turn began. Each turn's start is placed in the middle of the window those
facts leave, and players with no evidence of their own are spaced evenly
between their neighbours, which averages the time over the turns taken.

Times are minutes of workday time (turn_logger.WORKDAY, what the bot's turn
limit counts), so a turn held overnight costs nothing. scripts/turn_report.py
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

# How often the turn logger has polled, by the moment each interval took
# effect; the first entry is the interval the log began with. The last entry
# must be turn_logger.POLL_S: when the logger's interval changes, add the
# moment it was restarted with the new one here, or every later row gets the
# wrong window and the error compounds.
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
    """The logger's polling interval, in seconds, when a row was written at `when`."""
    return next(seconds for since, seconds in reversed(POLL_CHANGES) if since <= when)


@dataclass
class PlayerTurn:
    turn: int
    seat: int
    leader: str
    player: str
    rows: list[datetime] = field(default_factory=list)  # log rows of this turn
    saves: list[tuple[datetime, str]] = field(default_factory=list)  # (written at, save name)
    autosave: datetime | None = None  # the game's autosave that opened this game turn
    start: datetime | None = None
    earliest: datetime | None = None  # the window the start was placed in
    latest: datetime | None = None
    basis: str = ""  # autosave | row | save | row+save | interpolated

    @property
    def evidence(self) -> list[datetime]:
        """Every moment this turn is known to have been under way."""
        times = [*self.rows, *(at for at, _ in self.saves)]
        if self.autosave is not None:
            times.append(self.autosave)
        return times


@dataclass(frozen=True)
class PlayerAverage:
    """One player's finished turns, in minutes of workday time."""

    player: str
    leader: str
    seat: int  # 1-based
    turns: int
    guessed: int  # turns with no row or save of their own
    mean_minutes: float
    median_minutes: float
    max_minutes: float


def player_turns(records: list[turn_logger.TurnRecord]) -> list[PlayerTurn]:
    """Every player-turn from the first row to the last, in seat order, with the rows that saw it.

    Rows that go backwards (a reload of an earlier save) are skipped with
    a warning.
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
        cur.rows.append(r.at)
    seq.append(cur)
    return seq


def _rows_before(seq: list[PlayerTurn], k: int) -> datetime | None:
    times = [t for pt in seq[:k] for t in pt.rows]
    return max(times) if times else None


def _rows_after(seq: list[PlayerTurn], k: int) -> datetime | None:
    times = [t for pt in seq[k + 1:] for t in pt.rows]
    return min(times) if times else None


def attach_saves(
    seq: list[PlayerTurn], saves_dir: Path | None, poll_at: Callable[[datetime], int] = poll_s_at
) -> list[str]:
    """Give each player-turn the saves written during it. Returns the saves that fit nowhere."""
    index = {(pt.turn, pt.leader): k for k, pt in enumerate(seq)}
    leader_of = {name.casefold(): leader for leader, name in SEATS}
    first_row = min(seq[0].rows)
    begun = first_row - timedelta(seconds=poll_at(first_row))
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


def estimate_starts(seq: list[PlayerTurn], poll_at: Callable[[datetime], int] = poll_s_at) -> None:
    """Place every player-turn's start: exactly, in the middle of its window, or spaced between neighbours.

    `poll_at` gives the logger's interval when a row was written: the turn
    had begun no more than that long before its first row.
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
                first_row = min(pt.rows)
                bounds.append(first_row - timedelta(seconds=poll_at(first_row)))
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
    Both lists are empty while the log is. ValueError for a leader not in SEATS.
    """
    if POLL_CHANGES[-1][1] != turn_logger.POLL_S:
        log.warning(
            "the turn logger polls every %d s but POLL_CHANGES ends at %d s: rows since the change "
            "get the wrong window until the moment it took effect is added",
            turn_logger.POLL_S, POLL_CHANGES[-1][1],
        )
    records = turn_logger.read_records(log_path)
    if not records:
        return [], []
    seq = player_turns(records)
    skipped = attach_saves(seq, saves_dir, poll_at)
    estimate_starts(seq, poll_at)
    return seq, skipped


def finished(seq: list[PlayerTurn]) -> list[tuple[PlayerTurn, datetime]]:
    """Every player-turn that has ended, with its end (the next turn's start)."""
    return [(pt, nxt.start) for pt, nxt in zip(seq, seq[1:])]


def ended_on(seq: list[PlayerTurn], day: date) -> list[tuple[PlayerTurn, datetime]]:
    """The finished player-turns whose end fell on `day`."""
    return [(pt, end) for pt, end in finished(seq) if end.date() == day]


def ended_after(seq: list[PlayerTurn], when: datetime) -> list[tuple[PlayerTurn, datetime]]:
    """The finished player-turns that ended after `when`."""
    return [(pt, end) for pt, end in finished(seq) if end > when]


def minutes(pt: PlayerTurn, end: datetime) -> float:
    """A finished turn's minutes of workday time."""
    return turn_logger.workday_seconds(pt.start, end) / 60


def mean_minutes(done: list[tuple[PlayerTurn, datetime]]) -> float:
    """The average turn among `done` (see finished, ended_on), in minutes of workday time."""
    return statistics.mean(minutes(pt, end) for pt, end in done)


def player_averages(done: list[tuple[PlayerTurn, datetime]]) -> list[PlayerAverage]:
    """One entry per player with a turn among `done` (see finished, ended_on), slowest on average first."""
    by_player: dict[str, list[tuple[PlayerTurn, datetime]]] = {}
    for pt, end in done:
        by_player.setdefault(pt.player, []).append((pt, end))
    out = []
    for player, turns in by_player.items():
        mins = [minutes(pt, end) for pt, end in turns]
        first = turns[0][0]
        out.append(PlayerAverage(
            player=player,
            leader=first.leader,
            seat=first.seat + 1,
            turns=len(turns),
            guessed=sum(pt.basis == "interpolated" for pt, _ in turns),
            mean_minutes=statistics.mean(mins),
            median_minutes=statistics.median(mins),
            max_minutes=max(mins),
        ))
    out.sort(key=lambda a: (-a.mean_minutes, a.seat))
    return out


def hm(minutes: float) -> str:
    """95.4 -> '1h 35m'."""
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def report(seq: list[PlayerTurn], skipped: list[str], turns: bool = False) -> str:
    done = finished(seq)
    if not done:
        return "The log has only the turn in progress; nothing to measure yet."
    lines = []
    if turns:
        for pt, end in done:
            lines.append(
                f"T{pt.turn:<3}{pt.player:<14}{pt.start:%a %d %H:%M:%S} -> {end:%a %H:%M:%S}  "
                f"{hm(minutes(pt, end)):>8}  {pt.basis:<13}"
                f"window {pt.earliest:%H:%M:%S}-{pt.latest:%H:%M:%S}  "
                + "; ".join(
                    [f"log {t:%H:%M:%S}" for t in sorted(pt.rows)]
                    + ([f"autosave {pt.autosave:%H:%M:%S}"] if pt.autosave else [])
                    + [f"'{name}' {t:%H:%M:%S}" for t, name in sorted(pt.saves)]
                )
            )
        lines.append("")
    lines.append(f"{'player':<14}{'turns':>6}{'guessed':>8}{'average':>9}{'median':>8}{'longest':>9}")
    for a in player_averages(done):
        lines.append(
            f"{a.player:<14}{a.turns:>6}{a.guessed:>8}{hm(a.mean_minutes):>9}{hm(a.median_minutes):>8}"
            f"{hm(a.max_minutes):>9}"
        )
    median = statistics.median(minutes(pt, end) for pt, end in done)
    lines.append(f"{'ALL':<14}{len(done):>6}{'':>8}{hm(mean_minutes(done)):>9}{hm(median):>8}")
    first, last = seq[0].start, seq[-1].start
    lines.append(
        f"\nMinutes of workday time ({turn_logger.WORKDAY}). "
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
