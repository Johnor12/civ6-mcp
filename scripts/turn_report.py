#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "slack-sdk>=3.33",
#     "python-dotenv>=1.0",
# ]
# ///
"""Turn report: posts everyone's average turn time in #proj-diplomatic-relations every workday morning.

    uv run scripts/turn_report.py            # post at POST_AT every workday, for as long as it runs
    uv run scripts/turn_report.py --now      # post once, now, and exit
    uv run scripts/turn_report.py --print    # print the message instead of posting, and exit

The figures come from scripts/turn_times.py: the turn log plus the hotseat
saves, each turn's minutes of workday time (turn_logger.WORKDAY, what the
bot's turn limit counts). The post ranks players slowest first twice: over
the previous workday's turns (how many were taken, against the record for
most turns on one workday, the average turn, and each player's average
and number of turns that day) and over every turn
since the log began (again with the turn count, the average turn and each
player's average and number of turns). Beside each average is the time to
take the turn, from Start Turn to its end, over the turns that have one
(the taken log began on 4 Nov 2026), with its own count when that covers
fewer turns; turns Claude took get a line of their own instead. Nothing
is written anywhere; the turn log is the history. A workday on which the
process is not up at POST_AT gets no report. scripts/start.py runs this
next to the turn logger, the announcer and the bot. The bot token comes
from the repo's .env and the bot must be a member of the channel.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path

import turn_times  # scripts/turn_times.py, next to this script
from turn_announcer import CHANNEL
from turn_logger import WORKDAY

REPO_ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("turn_report")

POST_AT = clock(9, 0)  # local time, Monday to Friday
SLEEP_S = 300  # how long to sleep at a stretch while waiting for POST_AT


def previous_workday(day: date) -> date:
    """The weekday before `day`: Friday for a Monday (or a weekend day)."""
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _turns(n: int) -> str:
    return f"{n} turn" + ("s" if n != 1 else "")


def _play_time(mean: float | None, n: int, of: int) -> str:
    """', play time: 12m 05s', with '(n turns)' after it when that covers fewer than `of` turns; '' for none."""
    if mean is None:
        return ""
    return f", play time: {turn_times.hms(mean)}" + (f" ({_turns(n)})" if n != of else "")


def _ranking(averages: list[turn_times.PlayerAverage]) -> list[str]:
    """'1. Cy: 30m 00s (3 turns), play time: 12m 05s': each player's average turn, its turns and play time."""
    return [
        f"{rank}. {a.player}: {turn_times.hms(a.mean_minutes)} ({_turns(a.turns)})"
        + _play_time(a.take_mean_minutes, a.taken, a.turns)
        for rank, a in enumerate(averages, 1)
    ]


def _count(done: list[tuple[turn_times.PlayerTurn, datetime]]) -> str:
    """'3 turns, 23m 20s average, play time: 12m 05s'; '0 turns' when there are none."""
    turns = _turns(len(done))
    if not done:
        return turns
    own = turn_times.own_takes(done)
    return (
        f"{turns}, {turn_times.hms(turn_times.mean_minutes(done))} average"
        + _play_time(turn_times.mean_take_minutes(own), len(own), len(done))
    )


def _claude(done: list[tuple[turn_times.PlayerTurn, datetime]]) -> list[str]:
    """'Claude took 2 turns (Di, Bob), play time: 4m 10s', or nothing when it took none."""
    claude = turn_times.claude_takes(done)
    if not claude:
        return []
    whose = ", ".join(dict.fromkeys(pt.player for pt, _ in claude))
    mean = turn_times.mean_take_minutes(claude)
    return [f"Claude took {_turns(len(claude))} ({whose})" + (f", play time: {turn_times.hms(mean)}" if mean is not None else "")]


def _record(seq: list[turn_times.PlayerTurn], day: date, count: int) -> list[str]:
    """`count` turns taken on `day` against the most taken on any earlier workday; nothing without one.

    '36 turns taken, a new record! Previous record: 30 (Thu 29 Oct)',
    '30 turns taken, tying the record (Thu 29 Oct)' or
    '26 turns taken. Record: 36 (Fri 30 Oct)'. The record's day is the
    first that reached it. Weekend days are left out: the report never
    covers one, so it could never announce their record.
    """
    earlier = {d: n for d, n in turn_times.turns_per_day(seq).items() if d < day and d.weekday() < 5}
    if not earlier:
        return []
    best = max(earlier.values())
    when = f"{min(d for d, n in earlier.items() if n == best):%a %d %b}"
    taken = f"{_turns(count)} taken"
    if count > best:
        return [f"{taken}, a new record! Previous record: {best} ({when})"]
    if count == best:
        return [f"{taken}, tying the record ({when})"]
    return [f"{taken}. Record: {best} ({when})"]


def message(seq: list[turn_times.PlayerTurn], today: date | None = None) -> str:
    """The post: the previous workday's turns and everyone's averages, then the same since the log began.

    Each heading gives the turn count, the average turn and the average
    play time; each ranking line gives that player's average, how many
    turns it covers and their play time. Under the previous workday's
    heading, its turn count against the record (_record); a line for the
    turns Claude took follows each ranking. Empty while no turn has
    finished.
    """
    done = turn_times.finished(seq)
    if not done:
        return ""
    today = today or date.today()
    day = previous_workday(today)
    label = f"Yesterday ({day:%a %d %b})" if day == today - timedelta(days=1) else f"{day:%A %d %b}"
    yesterday = turn_times.ended_on(seq, day)

    head = f"*Turn times* in workday hours ({WORKDAY}), slowest first."
    if any(pt.taken is not None for pt, _ in done):
        head += " Play time: from pressing Start Turn to the end of the turn."
    lines = [head, "", f"*{label}: {_count(yesterday)}*", *_record(seq, day, len(yesterday))]
    if yesterday:
        lines += _ranking(turn_times.player_averages(yesterday)) + _claude(yesterday)
        played = {pt.player for pt, _ in yesterday}
        seats = {pt.player: pt.seat for pt in seq}
        idle = sorted((p for p in seats if p not in played), key=seats.get)
        if idle:
            lines.append("No turn: " + ", ".join(idle))
    lines += ["", f"*Since {seq[0].start:%a %d %b}: {_count(done)}*", *_ranking(turn_times.player_averages(done))]
    lines += _claude(done)
    return "\n".join(lines)


def build(today: date | None = None) -> str:
    """Read the turn log and the hotseat saves and write the message."""
    seq, skipped = turn_times.compute()
    for s in skipped:
        log.warning("a save fits no turn in the log: %s", s)
    return message(seq, today)


def post(client) -> bool:
    """Post the report in the channel. True when something was posted. Never raises."""
    try:
        text = build()
        if not text:
            log.info("no finished turn in the log yet; nothing to post")
            return False
        client.chat_postMessage(channel=CHANNEL, text=text)
    except Exception:
        log.exception("posting the turn report failed")
        return False
    log.info("posted to %s:\n%s", CHANNEL, text)
    return True


def next_post(now: datetime) -> datetime:
    """The next POST_AT after `now` that falls on a weekday."""
    due = datetime.combine(now.date(), POST_AT)
    if due <= now:
        due += timedelta(days=1)
    while due.weekday() >= 5:
        due += timedelta(days=1)
    return due


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--now", action="store_true", help="post once, now, and exit")
    parser.add_argument("--print", action="store_true", help="print the message instead of posting, and exit")
    args = parser.parse_args()
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.print:
        print(build() or "No finished turn in the log yet.")
        return

    # Imported here so the tests, in the project venv, can import this module without them.
    from dotenv import dotenv_values
    from slack_sdk import WebClient

    token = dotenv_values(REPO_ROOT / ".env").get("SLACK_BOT_TOKEN")
    if not token:
        raise SystemExit(f"SLACK_BOT_TOKEN must be set in {REPO_ROOT / '.env'}")
    client = WebClient(token=token)
    if args.now:
        sys.exit(0 if post(client) else 1)

    while True:
        due = next_post(datetime.now())
        log.info("next report at %s in %s", due.strftime("%a %d %b %H:%M"), CHANNEL)
        while (left := (due - datetime.now()).total_seconds()) > 0:
            time.sleep(min(left, SLEEP_S))
        post(client)


if __name__ == "__main__":
    main()
