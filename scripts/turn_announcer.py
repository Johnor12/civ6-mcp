#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "slack-sdk>=3.33",
#     "python-dotenv>=1.0",
# ]
# ///
"""Turn announcer: @'s the player whose turn it is in #proj-diplomatic-relations, then reminds them.

    uv run scripts/turn_announcer.py

Every second (POLL_S) it reads whose turn it is, and since when, from the
turn log (scripts/data/turn_log.jsonl), which the turn logger keeps within a
second of the game from the turn lines in Lua.log; it never asks the game
itself. So a new turn is announced within about two seconds of its line in
Lua.log, before the player who just finished usually gets to ping by hand.
It works out which message the turn is due for and, only when that message
is one it has not dealt with yet, reads the channel back to when it fell
due. Slack is called a few times per message, never once a second; after a
failure it waits RETRY_S before trying again.

Every message carries today's turns, how many player-turns have ended
today, counted as the daily report counts a day's turns against the record
(turn_times.turns_per_day), so the channel can see how close the day is to
a new one.

- When the turn begins: "it's your turn", unless someone has @'d the player
  in the channel since. Players ping the next one by hand when they finish,
  so a mention by anyone counts. The message ends with every turn finished
  since the bot last posted in the channel and how long each took, in
  workday time, from scripts/turn_times.py (the daily report's figures), so
  the turns that got a hand ping are accounted for too. A turn with a play
  time (from Start Turn to its end, at any hour) has it in brackets, marked when
  Claude took the turn. Without a post of
  its own in the last LOOKBACK_S, the turns of that stretch are listed.
- After REMINDER_S of workday time: a reminder from the bot.
- After turn_logger.TURN_LIMIT_S: a last message from the bot, saying any
  other player can now have the turn played through the Slack bot.

Only the latest message due is sent, so a turn found an hour in gets the
one-hour message alone. A reminder counts as sent when the bot has @'d the
player in the channel after it fell due, so a restart sends nothing twice.
Thread replies are not read.

The Slack bot (scripts/slack_bot.py) calls announce() right after each of
its runs, so the next player hears about a turn the bot ended at once
instead of at the next poll; a lock file keeps the two processes from
posting the same message. scripts/start.py runs this next to the turn logger
and the bot. The bot token comes from the repo's .env; the bot must be a
member of the channel.
"""

from __future__ import annotations

import logging
import msvcrt
import os
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import turn_logger  # scripts/turn_logger.py, next to this script
import turn_times  # scripts/turn_times.py, next to this script
from players import player_named
from turn_logger import TURN_LIMIT_S, WORKDAY, duration

REPO_ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("turn_announcer")

CHANNEL = "C09AZ6CR7QE"  # #proj-diplomatic-relations
POLL_S = 1  # how often the turn log is read; Slack is called only when a message falls due
RETRY_S = 60  # after a failure (Slack down, rate-limited), how long before the next try
REMINDER_S = 1800  # workday time before the first reminder
LOOKBACK_S = 24 * 3600  # how far back the turn message looks for the bot's last post
HAND_PING_S = 60  # a mention this long before the turn began counts as the hand ping
LOCK_PATH = REPO_ROOT / "scripts" / "data" / "turn_announcer.lock"
SAVES_DIR: Path | None = None  # hotseat save folder for turn_times; None is the game's

# (workday seconds into the turn, message); the last one due is sent.
# {today} is today's turn count (see todays_turns); {recent} is the finished
# turns since the bot's last post (see recent_turns).
STAGES = (
    (0, "{mention} it's your turn{today}.{recent}"),
    (
        REMINDER_S,
        (
            "{mention} reminder: your turn{today} has been waiting {waited} of workday time "
            "({workday}). After {limit} any other player can have me play it for you."
        ),
    ),
    (
        TURN_LIMIT_S,
        (
            "{mention} your turn{today} has been waiting {limit} of workday time, so any other "
            "player can now have it played for you through this app. DM me your note first if you want a say in it."
        ),
    ),
)

_sent: set[tuple] = set()  # (turn key, stage) known to be in the channel
_bot_ids: tuple[str, str] | None = None  # the bot's user id and bot id
_retry_at: datetime | None = None  # after a failure, no call before this


def announce(client, now: datetime | None = None) -> bool:
    """Read whose turn it is from the turn log and post the message that turn is due for, unless it is in the channel.

    True when a message was posted. Never raises: a failure is logged and the
    first call RETRY_S or more later tries again, so a Slack outage is not
    hit (or logged) once a second.
    """
    global _retry_at
    now = now or datetime.now()
    if _retry_at is not None and now < _retry_at:
        return False
    try:
        posted = _announce(client, now)
    except Exception:
        log.exception("announcing the turn failed; trying again in %s", duration(RETRY_S))
        _retry_at = now + timedelta(seconds=RETRY_S)
        return False
    _retry_at = None
    return posted


def _announce(client, now: datetime) -> bool:
    current = turn_logger.duration_of_current_turn(now)
    if current is None:
        return False
    rec, worked = current
    stage = max(i for i, (after, _) in enumerate(STAGES) if worked >= after)
    if (rec.key, stage) in _sent:
        return False
    player = player_named(rec.player)
    if player is None:
        log.warning("%s plays turn %d but is not in PLAYERS; nobody to ping", rec.player, rec.turn)
        _sent.add((rec.key, stage))
        return False

    after, template = STAGES[stage]
    if stage == 0:
        # The player who finished often pings the next one just before
        # ending the turn, so before this turn began.
        since, from_bot = rec.at - timedelta(seconds=HAND_PING_S), False
    else:
        since, from_bot = turn_logger.workday_deadline(rec.at, after), True
    posted = False
    with posting_lock():
        if not mentioned(client, player.slack_id, since, from_bot):
            seq = player_turns()
            text = template.format(
                mention=f"<@{player.slack_id}>",
                today=todays_turns(seq, now),
                recent=recent_turns(client, seq, now) if stage == 0 else "",
                waited=duration(REMINDER_S),
                limit=duration(TURN_LIMIT_S),
                workday=WORKDAY,
            )
            client.chat_postMessage(channel=CHANNEL, text=text)
            log.info("pinged %s for turn %d (%s in): %s", player.name, rec.turn, duration(worked), text)
            posted = True
    _sent.add((rec.key, stage))
    return posted


def mentioned(client, slack_id: str, since: datetime, from_bot: bool) -> bool:
    """Whether a channel message since `since` @'s the player; with from_bot, only the bot's own count."""
    tag = re.compile(rf"<@{slack_id}[>|]")
    bot_ids = own_ids(client) if from_bot else ()
    cursor = None
    while True:
        page = client.conversations_history(
            channel=CHANNEL, oldest=f"{since.timestamp():.6f}", inclusive=True, limit=200, cursor=cursor
        )
        for msg in page["messages"]:
            if tag.search(msg.get("text", "")) and (not from_bot or msg.get("user") in bot_ids or msg.get("bot_id") in bot_ids):
                return True
        cursor = (page.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return False


def _recent(pt: turn_times.PlayerTurn, end: datetime) -> str:
    """'Heldi 30m 00s (play time: 10m 00s)', 'Randy 1h 05m 00s (Claude, play time: 4m 10s)' or just 'Cy 5m 00s'."""
    text = f"{pt.player} {turn_times.hms(turn_times.minutes(pt, end))}"
    take = turn_times.take_minutes(pt, end)
    if take is not None:
        text += f" ({'Claude, ' if pt.claude else ''}play time: {turn_times.hms(take)})"
    return text


def player_turns() -> list[turn_times.PlayerTurn] | None:
    """The player-turns from turn_times, the daily report's figures, so the two agree; None when they cannot be worked out.

    Never raises: a failure (a leader not in turn_times.SEATS) is logged and
    the message goes out without today's turns and the list.
    """
    try:
        seq, _ = turn_times.compute(saves_dir=SAVES_DIR)
    except Exception:
        log.exception("could not work out the finished turns; the message goes out without them")
        return None
    return seq


def todays_turns(seq: list[turn_times.PlayerTurn] | None, now: datetime) -> str:
    """' (today's turns: 12)': the player-turns that ended on `now`'s day, as the daily report counts them; '' without seq."""
    if seq is None:
        return ""
    return f" (today's turns: {turn_times.turns_per_day(seq).get(now.date(), 0)})"


def recent_turns(client, seq: list[turn_times.PlayerTurn] | None, now: datetime) -> str:
    """' Since my last post: Heldi 30m 00s (play time: 10m 00s), Randy 10m 12s.' for the turns finished since; '' for none.

    The turns and their minutes of workday time (each listed as _recent
    gives it) come from `seq` (see player_turns); '' without it. The bot's
    last post is looked for LOOKBACK_S back; without one, the turns of that
    stretch are listed. Never raises: a failure is logged and the message
    goes out without the list.
    """
    if seq is None:
        return ""
    try:
        floor = now - timedelta(seconds=LOOKBACK_S)
        since = last_own_post(client, floor) or floor
        done = turn_times.ended_after(seq, since)
    except Exception:
        log.exception("could not work out the turns since the last post")
        return ""
    if not done:
        return ""
    return " Since my last post: " + ", ".join(_recent(pt, end) for pt, end in done) + "."


def last_own_post(client, floor: datetime) -> datetime | None:
    """When the bot last posted in the channel, or None if not since `floor`."""
    bot_ids = own_ids(client)
    cursor = None
    while True:
        page = client.conversations_history(
            channel=CHANNEL, oldest=f"{floor.timestamp():.6f}", inclusive=True, limit=200, cursor=cursor
        )
        for msg in page["messages"]:  # newest first
            if msg.get("user") in bot_ids or msg.get("bot_id") in bot_ids:
                return datetime.fromtimestamp(float(msg["ts"]))
        cursor = (page.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return None


def own_ids(client) -> tuple[str, str]:
    global _bot_ids
    if _bot_ids is None:
        auth = client.auth_test()
        _bot_ids = (auth["user_id"], auth["bot_id"])
    return _bot_ids


@contextmanager
def posting_lock() -> Iterator[None]:
    """Check and post one message at a time, against the Slack bot's process too."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT)
    try:
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # retries for 10 s, then OSError
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def main() -> None:
    # Imported here so the tests, in the project venv, can import this module without them.
    from dotenv import dotenv_values
    from slack_sdk import WebClient

    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = dotenv_values(REPO_ROOT / ".env").get("SLACK_BOT_TOKEN")
    if not token:
        raise SystemExit(f"SLACK_BOT_TOKEN must be set in {REPO_ROOT / '.env'}")
    client = WebClient(token=token)
    log.info(
        "reading the turn log every %d s; channel %s; reminders after %s and %s of workday time",
        POLL_S, CHANNEL, duration(REMINDER_S), duration(TURN_LIMIT_S),
    )
    while True:
        announce(client)
        time.sleep(POLL_S - time.time() % POLL_S)


if __name__ == "__main__":
    main()
