#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "slack-sdk>=3.33",
#     "python-dotenv>=1.0",
# ]
# ///
"""Out of office: a vote in #proj-diplomatic-relations that has an absent player's turns played for the rest of the day.

    uv run scripts/out_of_office.py

The Slack bot gives a player who asks about someone else's turn an "out of
office" button. A click calls open_vote(): a message in the game channel
that @'s the player whose turn it is and asks everyone to react :+1: or
:-1:. Every POLL_S this service:

- Closes each vote VOTE_S after it opened. It passes when VOTES_NEEDED
  players reacted :+1: and the player it is about did not react :-1:; then
  that player is out of office until midnight (local time). A vote on a
  turn that ended before it closed changes nothing. The result is added to
  the vote message.
- Plays the turn of a player who is out of office, once per turn, from a
  fixed prompt: the progress and Claude's reply go to their DM, under a
  note with an "I'm back" button (the bot answers it with come_back()),
  and the outcome to the vote's thread.

Whose turn it is comes from the turn log (scripts/data/turn_log.jsonl),
which the turn logger keeps within a second of the game; this service never
asks the game itself. Runs take claude_runs.RunLock, which the bot holds
while it handles a message or plays a turn, so a turn waits for the next poll when the bot is
busy. Votes, who is out and which turns were played live in
scripts/data/out_of_office.json, which both processes read and write under
a lock file. scripts/start.py runs this next to the bot; the bot token
comes from the repo's .env.
"""

from __future__ import annotations

import json
import logging
import msvcrt
import os
import shutil
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import claude_runs  # scripts/claude_runs.py, next to this script
import turn_announcer  # scripts/turn_announcer.py, next to this script
import turn_logger  # scripts/turn_logger.py, next to this script
from claude_runs import ABSENT_PROMPT, SLACK_FORMAT, RunLock, open_dm
from players import PLAYERS, Player, player_named, same_name
from turn_logger import TurnRecord

REPO_ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("out_of_office")

CHANNEL = turn_announcer.CHANNEL  # #proj-diplomatic-relations
POLL_S = 60
VOTE_S = 600  # how long a vote stays open
VOTES_NEEDED = 3  # players' :+1: for a vote to pass
KEEP = 50  # closed votes and played turns the state file remembers
STATE_PATH = REPO_ROOT / "scripts" / "data" / "out_of_office.json"

VOTE_ACTION = "out_of_office_vote"  # the bot's button that opens a vote
BACK_ACTION = "back_in_office"  # the button in an absent player's DM

UP = {"+1", "thumbsup"}
DOWN = {"-1", "thumbsdown"}

# The vote @'s the player as the bot, so the turn announcer counts it as a
# reminder already sent if it falls after one is due.
VOTE_TEXT = (
    "<@{owner}> {requester} says you're out of office and asked me to play your turn "
    "({leader}, turn {turn}). React :+1: if {name} is out, :-1: if not. At {closes:%H:%M}, if "
    "{needed} players have voted :+1: and {name} hasn't voted :-1:, I'll play this turn and "
    "the rest of {name}'s turns today."
)
AWAY_REASON = (
    "because {name} is out of office: the other players voted so in Slack, and "
    "{name}'s turns are played for them for the rest of the day. This is turn {turn}"
)
AWAY_NOTE = "Out of office (the players voted in Slack); played without a note from the player."


def is_away(name: str, now: datetime | None = None) -> bool:
    """Whether a vote put this player out of office today."""
    with state() as s:
        return _away(s, name, now or datetime.now())


def vote_on(key: tuple) -> dict | None:
    """The open vote on this turn, if any."""
    with state() as s:
        return _open_vote(s, key)


def closes_at(vote: dict) -> datetime:
    return datetime.fromisoformat(vote["opened"]) + timedelta(seconds=VOTE_S)


def open_vote(client, requester: Player, owner: Player, rec: TurnRecord, now: datetime | None = None) -> tuple[dict, bool]:
    """Post a vote on the owner being out of office; (the vote, True), or (the open one, False) if there is one."""
    now = now or datetime.now()
    with state() as s:
        if (vote := _open_vote(s, rec.key)) is not None:
            return vote, False
        text = VOTE_TEXT.format(
            owner=owner.slack_id,
            requester=requester.name,
            name=owner.name,
            leader=rec.leader_display,
            turn=rec.turn,
            closes=now + timedelta(seconds=VOTE_S),
            needed=VOTES_NEEDED,
        )
        posted = client.chat_postMessage(channel=CHANNEL, text=text)
        vote = {
            "ts": posted["ts"],
            "player": owner.name,
            "key": list(rec.key),
            "by": requester.name,
            "opened": now.isoformat(timespec="seconds"),
            "result": None,
        }
        s["votes"].append(vote)
    log.info("%s opened a vote on %s being out of office (turn %d)", requester.name, owner.name, rec.turn)
    return vote, True


def come_back(name: str) -> bool:
    """End a player's day out of office; whether they were out."""
    with state() as s:
        away = s["away"]
        gone = [n for n in away if same_name(n, name)]
        for n in gone:
            del away[n]
    if gone:
        log.info("%s is back", name)
    return bool(gone)


def check(client, claude: str, lock: RunLock, now: datetime | None = None) -> None:
    """Close the votes that are due, then play the current turn if its player is out. Never raises."""
    now = now or datetime.now()
    try:
        with state() as s:
            due = [v for v in s["votes"] if v["result"] is None and closes_at(v) <= now]
        for vote in due:
            close_vote(client, vote, now)
        play_if_away(client, claude, lock, now)
    except Exception:
        log.exception("out-of-office check failed")


def close_vote(client, vote: dict, now: datetime) -> str:
    """Count a vote, record and post its result; 'passed', 'vetoed', 'failed' or 'lapsed'."""
    name = vote["player"]
    owner = player_named(name)
    message = vote_message(client, vote["ts"])
    reacted_up = reacted(message, UP)
    up = [p.name for p in PLAYERS if p.slack_id in reacted_up]
    vetoed = owner is not None and owner.slack_id in reacted(message, DOWN)
    current = whose_turn()
    if current is None or list(current[0].key) != vote["key"]:
        result, line = "lapsed", f"*Vote closed:* {name}'s turn {vote['key'][0]} ended first, so nothing changes."
    elif vetoed:
        result, line = "vetoed", f"*Vote failed:* {name} voted :-1:."
    elif len(up) < VOTES_NEEDED:
        result, line = "failed", f"*Vote failed:* {len(up)} of the {VOTES_NEEDED} :+1: needed."
    else:
        result = "passed"
        line = f"*Vote passed* ({', '.join(up)}): {name} is out of office for the rest of today, so I'm playing their turns."
    with state() as s:
        for v in s["votes"]:
            if v["ts"] == vote["ts"]:
                v["result"] = result
        if result == "passed":
            s["away"][name] = {"day": now.date().isoformat(), "vote": vote["ts"]}
    log.info("vote on %s: %s (up: %s)", name, result, ", ".join(up) or "nobody")
    if message is None:
        log.warning("the vote message %s is gone; not posting: %s", vote["ts"], line)
    else:
        client.chat_update(channel=CHANNEL, ts=vote["ts"], text=f"{message.get('text', '')}\n{line}")
    return result


def vote_message(client, ts: str) -> dict | None:
    page = client.conversations_history(channel=CHANNEL, latest=ts, oldest=ts, inclusive=True, limit=1)
    return next((m for m in page.get("messages", []) if m.get("ts") == ts), None)


def reacted(message: dict | None, names: set[str]) -> set[str]:
    """Who reacted to the message with one of these emoji, skin tones included."""
    users: set[str] = set()
    for reaction in (message or {}).get("reactions", []):
        if reaction.get("name", "").split("::")[0] in names:
            users.update(reaction.get("users", []))
    return users


def play_if_away(client, claude: str, lock: RunLock, now: datetime) -> bool:
    """Play the current turn if its player is out of office and it was not tried yet; whether it was played."""
    current = turn_logger.duration_of_current_turn(now)
    if current is None or not _due(current[0], now):
        return False
    rec = current[0]
    owner = player_named(rec.player)
    if not lock.try_acquire(f"{owner.name if owner else rec.player}'s turn while they're out of office"):
        log.info("%s is out of office, but a run is under way; trying again next poll", rec.player)
        return False
    try:
        current = whose_turn()  # once more, now that no run can end it
        if current is None or not _due(current[0], now):
            return False
        rec = current[0]
        with state() as s:
            s["played"].append(list(rec.key))  # one try per turn, played or not
            vote_ts = next((e["vote"] for n, e in s["away"].items() if same_name(n, rec.player)), None)
        play(client, claude, rec, vote_ts)
        return True
    finally:
        lock.release()


def play(client, claude: str, rec: TurnRecord, vote_ts: str | None) -> None:
    """Have Claude play an absent player's turn: progress in their DM, the outcome in the vote's thread."""
    owner = player_named(rec.player)
    name = owner.name if owner else rec.player
    log.info("playing %s's turn %d (%s): out of office", name, rec.turn, rec.leader)
    prompt = ABSENT_PROMPT.format(
        reason=AWAY_REASON.format(name=name, turn=rec.turn),
        note=AWAY_NOTE,
        name=name,
        leader=rec.leader_display,
        slack_format=SLACK_FORMAT,
    )
    intro = (
        f"The players voted that you're out of office today, so I'm playing your turn "
        f"({rec.leader_display}, turn {rec.turn}), and any more of yours today."
    )
    back = {
        "type": "button",
        "action_id": BACK_ACTION,
        "value": name,
        "text": {"type": "plain_text", "text": "I'm back"},
    }
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": intro}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "Back before the day is over? Then I'll leave your next turns to you."}]},
        {"type": "actions", "elements": [back]},
    ]
    claude_runs.play_absent_turn(client, claude, open_dm(client, owner), name, prompt, intro, blocks)
    turn_announcer.announce(client)
    after = whose_turn()
    if after is not None and after[0].key != rec.key:
        nxt = player_named(after[0].player)
        text = f"Done: {name}'s turn {rec.turn} is over. It's now {nxt.name if nxt else after[0].player}'s turn."
    else:
        text = f"{name}'s turn {rec.turn} was not finished. Someone at the PC may need to look at the game."
    client.chat_postMessage(channel=CHANNEL, thread_ts=vote_ts, text=text)


def whose_turn() -> tuple[TurnRecord, float] | None:
    """The current turn and its workday seconds so far, from the turn log."""
    return turn_logger.duration_of_current_turn()


def _away(s: dict, name: str, now: datetime) -> bool:
    day = now.date().isoformat()
    return any(same_name(n, name) and e["day"] == day for n, e in s["away"].items())


def _due(rec: TurnRecord, now: datetime) -> bool:
    with state() as s:
        return _away(s, rec.player, now) and list(rec.key) not in s["played"]


def _open_vote(s: dict, key: tuple) -> dict | None:
    return next((v for v in s["votes"] if v["result"] is None and v["key"] == list(key)), None)


@contextmanager
def state() -> Iterator[dict]:
    """The state file's contents, written back when the block ends without an error; one process at a time."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(STATE_PATH.with_suffix(".lock"), os.O_RDWR | os.O_CREAT)
    try:
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # retries for 10 s, then OSError
        try:
            try:
                data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data.setdefault("votes", [])
            data.setdefault("away", {})
            data.setdefault("played", [])
            before = json.dumps(data, sort_keys=True)
            yield data
            _prune(data)
            if json.dumps(data, sort_keys=True) != before:
                tmp = STATE_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
                os.replace(tmp, STATE_PATH)
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def _prune(data: dict) -> None:
    closed = [v for v in data["votes"] if v["result"] is not None][-KEEP:]
    data["votes"] = [v for v in data["votes"] if v["result"] is None or v in closed]
    data["played"] = data["played"][-KEEP:]


def main() -> None:
    # Imported here so the tests, in the project venv, can import this module without them.
    from dotenv import dotenv_values
    from slack_sdk import WebClient

    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = dotenv_values(REPO_ROOT / ".env").get("SLACK_BOT_TOKEN")
    if not token:
        raise SystemExit(f"SLACK_BOT_TOKEN must be set in {REPO_ROOT / '.env'}")
    claude = shutil.which("claude")
    if not claude:
        raise SystemExit("claude is not on PATH")
    client = WebClient(token=token)
    lock = RunLock()
    log.info(
        "checking every %d s; votes in %s close after %d min and pass with %d players' :+1:",
        POLL_S, CHANNEL, VOTE_S // 60, VOTES_NEEDED,
    )
    while True:
        check(client, claude, lock)
        time.sleep(POLL_S - time.time() % POLL_S)


if __name__ == "__main__":
    main()
