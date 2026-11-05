#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "slack-sdk>=3.33",
#     "python-dotenv>=1.0",
# ]
# ///
"""Turn report: posts the turn times of the previous workday and of the whole game in #proj-diplomatic-relations every workday morning.

    uv run scripts/turn_report.py            # post at POST_AT every workday, for as long as it runs
    uv run scripts/turn_report.py --now      # post once, now, and exit
    uv run scripts/turn_report.py --print    # print the message instead of posting, and exit

The figures come from scripts/turn_times.py: the turn log plus the hotseat
saves. A turn counts towards the workday it ended in, and a workday lasts
until the next one begins at 9:00 (turn_times.workday_of), so a turn
finished overnight or over the weekend is the previous workday's. The post
has two sections, the previous workday and every turn since the log began,
and each opens with its figures, one to a line: the turns (the previous
workday's against the record for most turns on one workday), the average
turn time (minutes of workday time, turn_logger.WORKDAY, what the bot's
turn limit counts), the average and total play time, and the longest and
shortest play time with whose turn it was (in the second section, the
records, with the workday they count towards). Play time runs from Start
Turn to the turn's end at any hour, over the turns that have one (the
taken log began on 4 Nov 2026), and leaves out the turns Claude took.
Two rankings follow, slowest first: each player's
average turn time with their number of turns, then each player's average
play time, with its own count when it covers fewer of their turns, and a
line for the turns Claude took. Nothing is written anywhere; the turn log
is the history. A workday on which the process is not up at POST_AT gets
no report. scripts/start.py runs this next to the turn logger, the
announcer and the bot. The bot token comes from the repo's .env and the
bot must be a member of the channel.
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


def _figures(
    done: list[tuple[turn_times.PlayerTurn, datetime]], record: str = "", dated: bool = False
) -> list[str]:
    """A section's figures, one to a line: its turns (with `record` after), average turn time, then play time:
    average, total, longest and shortest.

    '3 turns (record: 5, Tue 27 Oct)', '23m 20s average turn time',
    '12m 05s average play time', '24m 10s total play time',
    '20m 00s longest play time (Mike, turn 21)',
    '0m 17s shortest play time (Cameron, turn 20)'. `dated` adds the workday
    each of the last two counts towards ('(Mike, turn 21, Wed 04 Nov)'),
    for a section that spans several. The play time leaves out the turns
    Claude took (own_takes); its lines are missing while no other turn has
    one, the longest and shortest while only one does, and only the turns
    show when there are none.
    """
    lines = [_turns(len(done)) + record]
    if not done:
        return lines
    lines.append(f"{turn_times.hms(turn_times.mean_minutes(done))} average turn time")
    own = turn_times.own_takes(done)
    mean = turn_times.mean_take_minutes(own)
    if mean is not None:
        lines.append(f"{turn_times.hms(mean)} average play time")
        lines.append(f"{turn_times.hms(turn_times.total_take_minutes(own))} total play time")
    timed = turn_times.timed_takes(own)
    if len(timed) > 1:
        for label, pick in (("longest", max), ("shortest", min)):
            pt, end, mins = pick(timed, key=lambda t: t[2])
            when = f", {turn_times.workday_of(end):%a %d %b}" if dated else ""
            lines.append(f"{turn_times.hms(mins)} {label} play time ({pt.player}, turn {pt.turn}{when})")
    return lines


def _turn_ranking(averages: list[turn_times.PlayerAverage]) -> list[str]:
    """'1. Cy: 30m 00s (3 turns)': each player's average turn time and number of turns, slowest first."""
    return ["_Average turn time, slowest first:_"] + [
        f"{rank}. {a.player}: {turn_times.hms(a.mean_minutes)} ({_turns(a.turns)})"
        for rank, a in enumerate(averages, 1)
    ]


def _play_ranking(averages: list[turn_times.PlayerAverage]) -> list[str]:
    """'1. Cy: 12m 05s (2 turns)': each player's average play time, slowest first; nothing when no one has one.

    The count shows when the play time covers fewer of the player's turns
    than the turn time does. Players without a play time are left out.
    """
    timed = sorted(
        (a for a in averages if a.take_mean_minutes is not None), key=lambda a: (-a.take_mean_minutes, a.seat)
    )
    if not timed:
        return []
    return ["_Average play time, slowest first:_"] + [
        f"{rank}. {a.player}: {turn_times.hms(a.take_mean_minutes)}" + (f" ({_turns(a.taken)})" if a.taken != a.turns else "")
        for rank, a in enumerate(timed, 1)
    ]


def _section(
    heading: str,
    done: list[tuple[turn_times.PlayerTurn, datetime]],
    record: str = "",
    idle: list[str] | None = None,
    dated: bool = False,
) -> list[str]:
    """'*heading:*', the figures (_figures, `record` and `dated` go to it), then the turn time ranking,
    with the `idle` players after it, and the play time ranking, with the turns Claude took after it."""
    lines = [f"*{heading}:*", *_figures(done, record, dated)]
    if not done:
        return lines
    averages = turn_times.player_averages(done)
    lines += ["", *_turn_ranking(averages)]
    if idle:
        lines.append("No turn: " + ", ".join(idle))
    play = _play_ranking(averages) + _claude(done)
    if play:
        lines += ["", *play]
    return lines


def _claude(done: list[tuple[turn_times.PlayerTurn, datetime]]) -> list[str]:
    """'Claude took 2 turns (Di, Bob), play time: 4m 10s', or nothing when it took none."""
    claude = turn_times.claude_takes(done)
    if not claude:
        return []
    whose = ", ".join(dict.fromkeys(pt.player for pt, _ in claude))
    mean = turn_times.mean_take_minutes(claude)
    return [f"Claude took {_turns(len(claude))} ({whose})" + (f", play time: {turn_times.hms(mean)}" if mean is not None else "")]


def _record(seq: list[turn_times.PlayerTurn], day: date, count: int) -> str:
    """' (record: 36, Fri 30 Oct)': `count` turns on workday `day` against the most on any earlier one; '' without one.

    ' (new record! Previous: 30, Thu 29 Oct)', ' (ties the record, Thu 29 Oct)'
    or ' (record: 36, Fri 30 Oct)'. Days are workdays as turns_per_day
    counts them; the record's is the first that reached it.
    """
    earlier = {d: n for d, n in turn_times.turns_per_day(seq).items() if d < day}
    if not earlier:
        return ""
    best = max(earlier.values())
    when = f"{min(d for d, n in earlier.items() if n == best):%a %d %b}"
    if count > best:
        return f" (new record! Previous: {best}, {when})"
    if count == best:
        return f" (ties the record, {when})"
    return f" (record: {best}, {when})"


def message(seq: list[turn_times.PlayerTurn], today: date | None = None) -> str:
    """The post: the previous workday's section, then the same for every turn since the log began (_section).

    The previous workday's turns are the ones that count towards it
    (turn_times.ended_on), held against the record (_record), and the
    players with no turn that day follow its turn time ranking. Empty
    while no turn has finished.
    """
    done = turn_times.finished(seq)
    if not done:
        return ""
    today = today or date.today()
    day = previous_workday(today)
    label = f"Yesterday ({day:%a %d %b})" if day == today - timedelta(days=1) else f"{day:%A %d %b}"
    yesterday = turn_times.ended_on(seq, day)
    played = {pt.player for pt, _ in yesterday}
    seats = {pt.player: pt.seat for pt in seq}
    idle = sorted((p for p in seats if p not in played), key=seats.get)

    lines = _section(label, yesterday, _record(seq, day, len(yesterday)), idle)
    lines += ["", *_section(f"All turns since {seq[0].start:%a %d %b}", done, dated=True)]
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
