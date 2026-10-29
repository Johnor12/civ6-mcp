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

Every POLL_S it has the turn logger ask the game whose turn it is, works out
which message the turn is due for, and reads the channel back to when that
message fell due:

- When the turn begins: "it's your turn", unless someone has @'d the player
  in the channel since. Players ping the next one by hand when they finish,
  so a mention by anyone counts.
- After REMINDER_S of workday time: a reminder from the bot.
- After turn_logger.TURN_LIMIT_S: a last message from the bot, saying any
  other player can now have the turn played through the Slack bot.

Only the latest message due is sent, so a turn found two hours in gets the
two-hour message alone. A reminder counts as sent when the bot has @'d the
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
from players import player_named
from turn_logger import TURN_LIMIT_S, WORKDAY, duration

REPO_ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("turn_announcer")

CHANNEL = "C09AZ6CR7QE"  # #proj-diplomatic-relations
POLL_S = 60
REMINDER_S = 3600  # workday time before the first reminder
LOCK_PATH = REPO_ROOT / "scripts" / "data" / "turn_announcer.lock"

# (workday seconds into the turn, message); the last one due is sent.
STAGES = (
    (0, "{mention} it's your turn: {leader}, turn {turn}. Play it at the PC, or DM me your note and I'll play it for you."),
    (
        REMINDER_S,
        (
            "{mention} reminder: your turn ({leader}, turn {turn}) has been waiting {waited} of workday time "
            "({workday}). After {limit} any other player can have me play it for you."
        ),
    ),
    (
        TURN_LIMIT_S,
        (
            "{mention} your turn ({leader}, turn {turn}) has been waiting {limit} of workday time, so any other "
            "player can now have it played for you through this app. DM me your note first if you want a say in it."
        ),
    ),
)

_sent: set[tuple] = set()  # (turn key, stage) known to be in the channel
_bot_ids: tuple[str, str] | None = None  # the bot's user id and bot id


def announce(client, now: datetime | None = None) -> bool:
    """Ask the game whose turn it is and post the message that turn is due for, unless it is in the channel.

    True when a message was posted. Never raises: a failure is logged and the
    next call tries again.
    """
    try:
        return _announce(client, now or datetime.now())
    except Exception:
        log.exception("announcing the turn failed")
        return False


def _announce(client, now: datetime) -> bool:
    try:
        turn_logger.log_current_turn()
    except Exception:
        log.exception("turn poll failed")
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
        # The turn's first row can be up to a logger poll late, and the
        # player who finished may have pinged the next one in the meantime.
        since, from_bot = rec.at - timedelta(seconds=turn_logger.POLL_S), False
    else:
        since, from_bot = turn_logger.workday_deadline(rec.at, after), True
    posted = False
    with posting_lock():
        if not mentioned(client, player.slack_id, since, from_bot):
            text = template.format(
                mention=f"<@{player.slack_id}>",
                leader=rec.leader_display,
                turn=rec.turn,
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
    logging.getLogger("civ_mcp").setLevel(logging.WARNING)  # a bridge connect per poll
    token = dotenv_values(REPO_ROOT / ".env").get("SLACK_BOT_TOKEN")
    if not token:
        raise SystemExit(f"SLACK_BOT_TOKEN must be set in {REPO_ROOT / '.env'}")
    client = WebClient(token=token)
    log.info(
        "checking every %d min; channel %s; reminders after %s and %s of workday time",
        POLL_S // 60, CHANNEL, duration(REMINDER_S), duration(TURN_LIMIT_S),
    )
    while True:
        announce(client)
        time.sleep(POLL_S - time.time() % POLL_S)


if __name__ == "__main__":
    main()
