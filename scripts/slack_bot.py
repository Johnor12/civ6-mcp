#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "slack-bolt>=1.21",
#     "python-dotenv>=1.0",
# ]
# ///
"""Slack trigger for the stand-in: players message @civ6 to have Claude Code play a hotseat turn.

    uv run scripts/slack_bot.py

Connects over Socket Mode (an outbound websocket, so no public URL). Only
the players in PLAYERS (scripts/players.py) may use it; anyone else gets a
joke and nothing runs.
Every message is checked against whose turn the game is on. The bot only
decides who may do what; what a message means is left to Claude.

- From the player whose turn it is, by DM: the message goes as-is to
  `claude -p` in the repo root, which plays the turn with it as the note or
  just answers it, whichever the player asked for. In a channel they are
  asked to DM instead, because the other players can read the channel.
- From any other player, by DM or channel mention: the reply says whose turn
  it is and how long it has lasted. Once that is TURN_LIMIT_S of workday
  time, the reply carries a button; clicking it has Claude play the turn
  from a fixed prompt (nothing the player wrote reaches Claude). The absent
  player gets the progress and Claude's reply by DM; the player who clicked
  only hears whose turn it is now.
- The admin (Player.admin in scripts/players.py: John) gets that button on
  any other player's turn without waiting for the timer. A click is checked
  the same way: before the limit only the admin's counts, so a button the
  admin got in a channel thread plays nothing for anyone else.
- That reply also carries an "out of office" button, at any time. Clicking
  it posts a vote in the game channel (scripts/out_of_office.py); when it
  passes, that service plays the player's turns for the rest of the day.
  The player's DM then has an "I'm back" button, which the bot answers by
  ending their day out of office.
- The app's Home tab is a status page: whose turn it is, its timer and what
  the viewer may do about it. It is published each time the tab is opened
  and when its Refresh button is pressed; the button to play a turn stays
  in DMs.

How long a turn has lasted comes from the turn log that scripts/turn_logger.py
keeps (scripts/start.py runs both). Before reading it the bot has the logger
ask the game once, so a turn ended at the PC or by the last run counts
without waiting for the logger's next poll. The bot only reacts to
messages: pinging and reminding players in the game channel is
scripts/turn_announcer.py's job, and the bot calls it once after each run so
the player whose turn it is now hears at once; closing out-of-office votes
and playing those turns is scripts/out_of_office.py's.

While Claude runs, a status message lists the game actions it takes and its
narration, and the bot's console logs the same lines; Claude's final reply
is posted after it (scripts/claude_runs.py). One run at a time, shared with
the out-of-office service: a message that arrives mid-run gets a busy
reply. Tokens come from the repo's .env (see .env.example). The inline
metadata above gives the script its own environment, so it never syncs the
project venv that the running MCP server holds open; the turn logger and the
game-side code it imports from src/ need only the standard library.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import claude_runs  # scripts/claude_runs.py, next to this script
import out_of_office  # scripts/out_of_office.py, next to this script
import turn_announcer  # scripts/turn_announcer.py, next to this script
import turn_logger  # scripts/turn_logger.py, next to this script
from claude_runs import ABSENT_PROMPT, SLACK_FORMAT, Run, RunLock, minutes, open_dm
from dotenv import dotenv_values
from players import PLAYERS, SLACK_PLAYERS, Player, player_named, same_name
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from turn_logger import TURN_LIMIT_S, WORKDAY, duration

REPO_ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("slack_bot")

REJECTION = "Deleting save, uninstalling Civilization 6, and wiping Windows."

OWN_TURN_PROMPT = """\
This run was started from Slack by {name}, who plays {leader} in the hotseat \
game. {status} After {limit} of workday time ({workday}) any other player can \
have it played for them. Nobody is at the terminal and nobody can answer \
questions before the run ends.

Do what {name}'s message below asks. It may ask you to play their turn, or it \
may only be a question. To play the turn, follow the stand-in workflow in \
CLAUDE.md through end_turn with the message as the note for \
begin_standin_turn; decide for yourself and put your reasoning in the diary. \
begin_standin_turn presses Start Turn in the game for them, so call it only \
when they want the turn played or ask about something only their game shows; \
if you take the seat without playing the turn, leave it unended and say so.

The message speaks for {leader}'s turn only: act for no other player, read no \
other player's diary, and load a save only to open this turn if the game is \
at the main menu. If something stops you (another player's turn-change \
screen, the game not running, a popup that needs ESC in the game), stop and \
say what someone at the PC has to do.

Your final message is sent to {name} in Slack as-is. {slack_format}

Message from {name}:
{text}
"""

# The first sentence of ABSENT_PROMPT and the begin_standin_turn note: for a
# turn whose timer ran out, and for one the admin had played before that.
TIMED_OUT_REASON = (
    "because a turn timer ran out: it has been {name}'s turn ({leader}, turn "
    "{turn}) for {worked} of workday time, past the {limit} limit, and another "
    "player asked for it to be played"
)
ADMIN_REASON = (
    "by {admin}, the game's admin, who asked for {name}'s turn ({leader}, turn "
    "{turn}) to be played after {worked} of workday time, before the {limit} limit"
)
TIMED_OUT_NOTE = "Turn timer ran out; played without a note from the player."
ADMIN_NOTE = "Played at the game admin's request, without a note from the player."

# The button under a timed-out turn's status; its value is the turn's key, so
# a click after that turn ended plays nothing. The out-of-office button
# (out_of_office.VOTE_ACTION) carries the key the same way.
PLAY_ACTION = "play_timed_out_turn"
HOME_REFRESH_ACTION = "refresh_home"
NO_GAME = (
    "I can't tell whose turn it is: the game isn't loaded and the turn log "
    "is empty. Someone at the PC has to open the hotseat game."
)


def slack_unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def turn_status(rec: turn_logger.TurnRecord, worked: float, whose: str) -> str:
    """'It's your turn (Poundmaker, turn 64) since Mon 26 Oct 10:05: 1h 10m of workday time so far.'"""
    return (
        f"It's {whose} turn ({rec.leader_display}, turn {rec.turn}) since "
        f"{rec.at:%a %d %b %H:%M}: {duration(worked)} of workday time so far."
    )


def busy(run: Run | None) -> str:
    return f"Busy with {run.label} (started {minutes(time.time() - run.started)} ago)." if run else "Busy."


def home_view(
    viewer: Player | None,
    current: tuple[turn_logger.TurnRecord, float] | None,
    run: Run | None,
    now: datetime | None = None,
) -> dict:
    """The App Home tab: whose turn it is, its timer, and what the viewer can do about it.

    The same answers a message would get, without the button: a Home tab
    click has no conversation to reply in, so the button stays in DMs.
    """
    if viewer is None:
        lines = [REJECTION]
    elif current is None:
        lines = [NO_GAME]
    else:
        rec, worked = current
        owner = player_named(rec.player)
        name = owner.name if owner else rec.player
        limit = duration(TURN_LIMIT_S)
        if owner == viewer:
            lines = [
                turn_status(rec, worked, "your"),
                "DM me what you want done with it: a note to play the turn by, or a question "
                "about your game. Not in a channel: the other players can read it.",
            ]
        elif worked >= TURN_LIMIT_S:
            lines = [
                turn_status(rec, worked, f"{name}'s"),
                f"That's past the {limit} limit: DM me for a button to have me play it for {name}.",
            ]
        elif viewer.admin:
            lines = [
                turn_status(rec, worked, f"{name}'s"),
                f"The {limit} limit is {duration(TURN_LIMIT_S - worked)} away, but as the admin "
                f"you can DM me for a button to have me play it for {name} now.",
            ]
        else:
            lines = [
                turn_status(rec, worked, f"{name}'s"),
                f"Another player can have it played after {limit} of workday time ({WORKDAY}), "
                f"so in {duration(TURN_LIMIT_S - worked)}. DM me then for the button.",
            ]
    if run is not None:
        lines.append(busy(run))
    refresh = {
        "type": "button",
        "action_id": HOME_REFRESH_ACTION,
        "text": {"type": "plain_text", "text": "Refresh"},
    }
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Civ 6 hotseat"}},
        *({"type": "section", "text": {"type": "mrkdwn", "text": line}} for line in lines),
        {"type": "actions", "elements": [refresh]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"As of {(now or datetime.now()):%a %d %b %H:%M}."}]},
    ]
    return {"type": "home", "blocks": blocks}


def play_button(rec: turn_logger.TurnRecord, name: str) -> dict:
    return {
        "type": "button",
        "action_id": PLAY_ACTION,
        "value": json.dumps(list(rec.key)),
        "text": {"type": "plain_text", "text": f"Play {name}'s turn"},
        "confirm": {
            "title": {"type": "plain_text", "text": "Play the turn?"},
            "text": {
                "type": "plain_text",
                "text": f"Claude plays {rec.leader_display}'s turn {rec.turn} by {name}'s diary and ends it. {name} gets the details.",
            },
            "confirm": {"type": "plain_text", "text": "Play it"},
            "deny": {"type": "plain_text", "text": "Cancel"},
        },
    }


def away_button(rec: turn_logger.TurnRecord, name: str) -> dict:
    return {
        "type": "button",
        "action_id": out_of_office.VOTE_ACTION,
        "value": json.dumps(list(rec.key)),
        "text": {"type": "plain_text", "text": f"{name} is out of office"},
        "confirm": {
            "title": {"type": "plain_text", "text": "Start a vote?"},
            "text": {
                "type": "plain_text",
                "emoji": True,
                "text": (
                    f"I @ {name} in #proj-diplomatic-relations and everyone votes. If "
                    f"{out_of_office.VOTES_NEEDED} players vote :+1: within {out_of_office.VOTE_S // 60} "
                    f"minutes and {name} doesn't vote :-1:, I play {name}'s turns for the rest of today."
                ),
            },
            "confirm": {"type": "plain_text", "text": "Start the vote"},
            "deny": {"type": "plain_text", "text": "Cancel"},
        },
    }


def without_action(blocks: list[dict], action_id: str) -> list[dict]:
    """A message's blocks minus one button; an actions block left empty goes too."""
    kept = []
    for block in blocks:
        if block.get("type") == "actions":
            elements = [e for e in block.get("elements", []) if e.get("action_id") != action_id]
            if not elements:
                continue
            block = {**block, "elements": elements}
        kept.append(block)
    return kept


def whose_turn() -> tuple[turn_logger.TurnRecord, float] | None:
    """The current turn and its workday seconds so far, from the turn log after one fresh poll."""
    try:
        turn_logger.log_current_turn()
    except Exception:
        log.exception("turn poll failed")
    return turn_logger.duration_of_current_turn()


def register(app: App, claude: str, lock: RunLock) -> None:
    seen: deque[str] = deque(maxlen=200)

    @app.event("app_mention")
    def on_mention(body, event, context, client, say):
        # Replies go in a thread under the mention.
        thread_ts = event.get("thread_ts") or event["ts"]
        handle(body, event, context, client, say, thread_ts=thread_ts, is_dm=False)

    @app.event("message")
    def on_message(body, event, context, client, say):
        # Direct messages only: a channel message that mentions the bot also
        # arrives as app_mention. Edits, deletions and bot posts are skipped.
        if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id"):
            return
        handle(body, event, context, client, say, thread_ts=event.get("thread_ts"), is_dm=True)

    @app.event("app_home_opened")
    def on_home_opened(event, client):
        # Fires for the Messages tab too; that one needs nothing.
        if event.get("tab", "home") == "home":
            publish_home(client, event["user"])

    @app.action(HOME_REFRESH_ACTION)
    def on_home_refresh(ack, body, client):
        ack()
        publish_home(client, body["user"]["id"])

    def publish_home(client, user: str) -> None:
        """Put whose turn it is, and what the viewer may do about it, on their Home tab."""
        viewer = SLACK_PLAYERS.get(user)
        current = whose_turn() if viewer else None
        try:
            client.views_publish(user_id=user, view=home_view(viewer, current, lock.current))
        except Exception as exc:
            log.warning("home view for %s failed: %s", user, exc)

    def handle(body, event, context, client, say, thread_ts: str | None, is_dm: bool) -> None:
        # Socket Mode can redeliver an event after a reconnect.
        event_id = body.get("event_id", event.get("ts"))
        if event_id in seen:
            return
        seen.append(event_id)

        user = event.get("user")
        if not user:
            return

        def reply(text: str, **kwargs):
            return say(text=text, thread_ts=thread_ts, **kwargs)

        sender = SLACK_PLAYERS.get(user)
        if sender is None:
            log.warning("refused %s: %r", user, event.get("text"))
            reply(REJECTION)
            return

        def work() -> None:
            current = whose_turn()
            if current is None:
                reply(NO_GAME)
                return
            rec, worked = current
            owner = player_named(rec.player)
            text = slack_unescape(re.sub(rf"<@{context.bot_user_id}>", "", event.get("text", ""))).strip()
            log.info("%s on %s's turn %d (%s): %r", sender.name, rec.player, rec.turn, "DM" if is_dm else "mention", text)
            if owner == sender:
                own_turn(sender, rec, worked, text, is_dm, reply, client, event["channel"], thread_ts)
            else:
                other_turn(sender, owner, rec, worked, reply)

        locked(user, reply, work)

    @app.action(PLAY_ACTION)
    def on_play(ack, body, action, client, say):
        ack()
        user = body["user"]["id"]
        message = body.get("message") or {}
        thread_ts = message.get("thread_ts")

        def reply(text: str):
            return say(text=text, thread_ts=thread_ts)

        sender = SLACK_PLAYERS.get(user)
        if sender is None:
            log.warning("refused a click from %s", user)
            reply(REJECTION)
            return

        def work() -> None:
            current = whose_turn()
            if current is None:
                reply(NO_GAME)
                return
            rec, worked = current
            owner = player_named(rec.player)
            if list(rec.key) != json.loads(action["value"]):
                nxt = owner.name if owner else rec.player
                reply(f"That turn is over: it's {nxt}'s turn {rec.turn} now. Message me for its timer.")
                return
            if owner == sender:
                reply("It's your turn: DM me what you want done with it.")
                return
            if worked < TURN_LIMIT_S and not sender.admin:
                # The admin's button in a channel thread is visible to everyone.
                name = owner.name if owner else rec.player
                status = turn_status(rec, worked, f"{name}'s")
                reply(
                    f"{status} Only the admin can have it played before the "
                    f"{duration(TURN_LIMIT_S)} limit; you can in {duration(TURN_LIMIT_S - worked)}."
                )
                return
            # Take the buttons away so nobody clicks them again.
            client.chat_update(
                channel=body["channel"]["id"],
                ts=message["ts"],
                text=f"{message.get('text', '')}\n_{sender.name} asked me to play it._",
                blocks=[],
            )
            play_requested_turn(sender, owner, rec, worked, reply, client)

        locked(user, reply, work)

    @app.action(out_of_office.VOTE_ACTION)
    def on_out_of_office(ack, body, action, client, say):
        """The out-of-office button: a vote in the game channel on the player whose turn it is."""
        ack()
        user = body["user"]["id"]
        message = body.get("message") or {}
        thread_ts = message.get("thread_ts")

        def reply(text: str):
            return say(text=text, thread_ts=thread_ts)

        sender = SLACK_PLAYERS.get(user)
        if sender is None:
            log.warning("refused a click from %s", user)
            reply(REJECTION)
            return

        def work() -> None:
            current = whose_turn()
            if current is None:
                reply(NO_GAME)
                return
            rec, _ = current
            owner = player_named(rec.player)
            name = owner.name if owner else rec.player
            channel = f"<#{out_of_office.CHANNEL}>"
            if list(rec.key) != json.loads(action["value"]):
                reply(f"That turn is over: it's {name}'s turn {rec.turn} now. Message me for its timer.")
                return
            if owner == sender:
                reply("It's your turn: DM me what you want done with it.")
                return
            if owner is None:
                reply(f"{name} isn't on Slack, so there's nobody to ask.")
                return
            if out_of_office.is_away(owner.name):
                reply(f"{name} is already out of office today: I'm playing their turns.")
                return
            vote, new = out_of_office.open_vote(client, sender, owner, rec)
            closes = f"{out_of_office.closes_at(vote):%H:%M}"
            if not new:
                reply(f"A vote on {name} being out of office is already open in {channel} until {closes}.")
                return
            client.chat_update(
                channel=body["channel"]["id"],
                ts=message["ts"],
                text=f"{message.get('text', '')}\n_{sender.name} started an out-of-office vote._",
                blocks=without_action(message.get("blocks") or [], out_of_office.VOTE_ACTION),
            )
            reply(f"Vote started in {channel}. It closes at {closes}.")

        locked(user, reply, work)

    @app.action(out_of_office.BACK_ACTION)
    def on_back(ack, body, action, client, say):
        """The "I'm back" button in an absent player's DM. Not behind the run lock: it must work mid-run."""
        ack()
        user = body["user"]["id"]
        message = body.get("message") or {}
        sender = SLACK_PLAYERS.get(user)
        if sender is None:
            log.warning("refused a click from %s", user)
            say(text=REJECTION)
            return
        if not same_name(sender.name, action["value"]):
            say(text="That button is for the player it was sent to.")
            return
        was_away = out_of_office.come_back(sender.name)
        try:
            client.chat_update(
                channel=body["channel"]["id"],
                ts=message["ts"],
                text=message.get("text", ""),
                blocks=without_action(message.get("blocks") or [], out_of_office.BACK_ACTION),
            )
        except Exception as exc:
            log.warning("could not take the I'm back button away: %s", exc)
        say(text="Welcome back: I won't start any more of your turns." if was_away else "You weren't out of office: your turns are yours.")

    def locked(user: str, reply, work: Callable[[], None]) -> None:
        """Do one message's work holding the run lock, or say who holds it."""
        if not lock.try_acquire(f"a run for <@{user}>"):
            reply(f"{busy(lock.current)} Message me again when it's done.")
            return
        try:
            work()
        except Exception as exc:
            log.exception("run failed")
            reply(f"The bot itself failed: `{exc}`")
        finally:
            lock.release()

    def own_turn(sender: Player, rec, worked: float, text: str, is_dm: bool, reply, client, channel: str, thread_ts):
        """The player whose turn it is: their DM goes to Claude as-is, to play the turn or answer."""
        if not is_dm:
            reply(f"{turn_status(rec, worked, 'your')} DM me about it: the other players can read this channel.")
            return
        log.info("own turn for %s, %s turn %d: %r", sender.name, rec.leader, rec.turn, text)
        prompt = OWN_TURN_PROMPT.format(
            name=sender.name,
            leader=rec.leader_display,
            status=turn_status(rec, worked, f"{sender.name}'s"),
            limit=duration(TURN_LIMIT_S),
            workday=WORKDAY,
            text=text,
            slack_format=SLACK_FORMAT,
        )
        claude_runs.run_with_progress(client, channel, thread_ts, prompt, claude)
        turn_announcer.announce(client)

    def other_turn(sender: Player, owner: Player | None, rec, worked: float, reply) -> None:
        """Another player: the turn's timer, and once it has run out a button to have the turn played.

        The admin gets the button before the timer runs out as well. Anyone
        gets the out-of-office button, unless a vote on this turn is open or
        the player is out already.
        """
        name = owner.name if owner else rec.player
        status = turn_status(rec, worked, f"{name}'s")
        buttons = []
        if worked >= TURN_LIMIT_S:
            text = f"{status} That's past the {duration(TURN_LIMIT_S)} limit, so you can have me play it for {name}."
            buttons.append(play_button(rec, name))
        elif sender.admin:
            text = (
                f"{status} The {duration(TURN_LIMIT_S)} limit is {duration(TURN_LIMIT_S - worked)} away, "
                f"but as the admin you can have me play it for {name} now."
            )
            buttons.append(play_button(rec, name))
        else:
            text = (
                f"{status} Another player can have it "
                f"played after {duration(TURN_LIMIT_S)} of workday time ({WORKDAY}), so in "
                f"{duration(TURN_LIMIT_S - worked)}."
            )
        if owner is not None:
            if out_of_office.is_away(owner.name):
                text += f" {name} is out of office today, so I'm playing their turns."
            elif (vote := out_of_office.vote_on(rec.key)) is not None:
                text += (
                    f" A vote on {name} being out of office is open in "
                    f"<#{out_of_office.CHANNEL}> until {out_of_office.closes_at(vote):%H:%M}."
                )
            else:
                text += f" If {name} is out of office, you can start a vote to have me play their turns today."
                buttons.append(away_button(rec, name))
        if not buttons:
            reply(text)
            return
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}, {"type": "actions", "elements": buttons}]
        reply(text, blocks=blocks)

    def play_requested_turn(sender: Player, owner: Player | None, rec, worked: float, reply, client):
        """Play a turn from a fixed prompt for whoever clicked the button: its timer ran out, or the admin asked before that."""
        name = owner.name if owner else rec.player
        timed_out = worked >= TURN_LIMIT_S
        dm = open_dm(client, owner)
        lasted = f"{name}'s turn ({rec.leader_display}, turn {rec.turn}) has lasted {duration(worked)} of workday time"
        details = f"; {name} gets the details." if dm else "."
        if timed_out:
            reply(f"{lasted}, past the {duration(TURN_LIMIT_S)} limit. Playing it for them now{details}")
        else:
            reply(f"{lasted}, under the {duration(TURN_LIMIT_S)} limit. Playing it for them now as you, the admin, asked{details}")
        log.info(
            "%s turn for %s, %s turn %d, asked by %s",
            "timed-out" if timed_out else "admin-requested", name, rec.leader, rec.turn, sender.name,
        )
        fields = dict(
            name=name,
            leader=rec.leader_display,
            turn=rec.turn,
            worked=duration(worked),
            limit=duration(TURN_LIMIT_S),
            admin=sender.name,
        )
        prompt = ABSENT_PROMPT.format(
            reason=(TIMED_OUT_REASON if timed_out else ADMIN_REASON).format(**fields),
            note=TIMED_OUT_NOTE if timed_out else ADMIN_NOTE,
            slack_format=SLACK_FORMAT,
            **fields,
        )
        yours = f"Your turn ({rec.leader_display}, turn {rec.turn}) has lasted {duration(worked)} of workday time"
        if timed_out:
            intro = f"{yours}, so {sender.name} asked me to play it for you."
        else:
            intro = f"{yours}; {sender.name}, the game's admin, asked me to play it for you."
        claude_runs.play_absent_turn(client, claude, dm, name, prompt, intro)
        turn_announcer.announce(client)
        current = whose_turn()
        after = current[0] if current else None
        if after is not None and after.key != rec.key:
            nxt = player_named(after.player)
            reply(f"Done: {name}'s turn {rec.turn} is over. It's now {nxt.name if nxt else after.player}'s turn.")
        else:
            reply(f"{name}'s turn was not finished. Someone at the PC may need to look at the game.")


def main() -> None:
    # Progress lines carry arrows and emoji; a redirected stderr would be cp1252.
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("civ_mcp").setLevel(logging.WARNING)  # a bridge connect per poll
    env = dotenv_values(REPO_ROOT / ".env")
    bot_token, app_token = env.get("SLACK_BOT_TOKEN"), env.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        raise SystemExit(f"SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set in {REPO_ROOT / '.env'}")
    claude = shutil.which("claude")
    if not claude:
        raise SystemExit("claude is not on PATH")

    lock = RunLock()
    app = App(token=bot_token)
    register(app, claude, lock)
    current = turn_logger.duration_of_current_turn()
    turn = f"{current[0].player}'s turn {current[0].turn} since {current[0].at:%a %d %b %H:%M}" if current else "unknown"
    log.info("%d players; turn log: %s; claude: %s; repo: %s", len(PLAYERS), turn, claude, REPO_ROOT)
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
