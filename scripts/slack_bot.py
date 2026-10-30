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

How long a turn has lasted comes from the turn log that scripts/turn_logger.py
keeps (scripts/start.py runs both). Before reading it the bot has the logger
ask the game once, so a turn ended at the PC or by the last run counts
without waiting for the logger's next poll. The bot only reacts to
messages: pinging and reminding players in the game channel is
scripts/turn_announcer.py's job, and the bot calls it once after each run so
the player whose turn it is now hears at once.

While Claude runs, a status message lists the game actions it takes and its
narration, and the bot's console logs the same lines; Claude's final reply
is posted after it. One run at a time: a message that arrives mid-run gets a
busy reply. Tokens come from the repo's .env (see .env.example). The inline
metadata above gives the script its own environment, so it never syncs the
project venv that the running MCP server holds open; the turn logger and the
game-side code it imports from src/ need only the standard library.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import turn_announcer  # scripts/turn_announcer.py, next to this script
import turn_logger  # scripts/turn_logger.py, next to this script
from dotenv import dotenv_values
from players import PLAYERS, SLACK_PLAYERS, Player, player_named
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from turn_logger import TURN_LIMIT_S, WORKDAY, duration

REPO_ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("slack_bot")

REJECTION = "Deleting save, uninstalling Civilization 6, and wiping Windows."
TURN_TIMEOUT_S = 90 * 60
SLACK_MAX_CHARS = 3500

# What the headless run may do without asking: every civ6 game tool, and the
# BBG lookup CLAUDE.md describes (curl the leaders page into the scratchpad,
# then read or grep it). Anything else is denied, and so is reading .env or
# the other players' diaries in ~/.civ6-mcp (get_diary reads this player's).
ALLOWED_TOOLS = [
    "mcp__civ6",
    "Bash(curl:*)",
    "WebFetch(domain:civ6bbg.github.io)",
    "Read",
    "Grep",
    "Glob",
]
DISALLOWED_TOOLS = ["Read(./.env)", "Read(~/.civ6-mcp/**)"]

# Set when this script is started from inside a Claude Code session; the
# child run should not think it is nested.
PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")

SLACK_FORMAT = (
    "Keep it under 15 lines and write Slack mrkdwn: *bold*, _italic_, `code`, "
    '"•" bullets; no headings, tables or [text](url) links.'
)

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

ABSENT_PROMPT = """\
This run was started from Slack {reason}. {name} left no \
note. Nobody is at the terminal and nobody can answer questions before the run \
ends, so decide for yourself and put your reasoning in the diary.

Take {leader}'s turn: follow the stand-in workflow in CLAUDE.md through \
end_turn, with the note "{note}" for begin_standin_turn. Keep to the plan in \
the diary and the map pins and play steadily: nothing {name} would want to \
undo. Act for no other player, and load a save only to open this turn if the \
game is at the main menu. If something stops you (another player's \
turn-change screen, the game not running, a popup that needs ESC in the game), \
stop and say what someone at the PC has to do.

Your final message is sent to {name} in Slack as-is, telling them what \
happened to their turn. {slack_format}
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
# a click after that turn ended plays nothing.
PLAY_ACTION = "play_timed_out_turn"
NO_GAME = (
    "I can't tell whose turn it is: the game isn't loaded and the turn log "
    "is empty. Someone at the PC has to open the hotseat game."
)

# A progress step is (kind, text). "action", "text" and "error" steps are
# listed and logged; "now" only replaces the status line under the list.
Report = Callable[[str, str], None]
STEP_MARKERS = {"action": "▸", "text": "💬", "error": "⚠️"}

CIV6 = "mcp__civ6__"
CIV6_QUERIES = {"hotseat_status", "list_saves"}  # and every get_*
# Unit and city lines in the briefing, get_units and get_cities end in their id.
UNIT_LINE = re.compile(r"^\s*(.+?) \(UNIT_\w+\) at .*\[id:(\d+),", re.M)
CITY_LINE = re.compile(r"^\s*(.+?) \(pop \d+\) at .*\[id:(\d+)\]", re.M)
TYPE_PREFIX = re.compile(
    r"^(UNIT|TECH|CIVIC|BUILDING|DISTRICT|PROJECT|IMPROVEMENT|PROMOTION|POLICY|BELIEF|GOVERNMENT|YIELD)_"
)


@dataclass
class Run:
    user: str
    started: float


class TurnLock:
    """One Claude run at a time; remembers who holds it for the busy reply."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: Run | None = None

    def try_acquire(self, user: str) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        self.current = Run(user, time.monotonic())
        return True

    def release(self) -> None:
        self.current = None
        self._lock.release()


class Progress:
    """A Slack message edited in place with the run's latest steps.

    Edits are batched every few seconds (chat.update allows about 50 a
    minute) and repeated every half minute even without news, so the
    elapsed time keeps moving while Claude thinks or end_turn waits.
    """

    TAIL_LINES = 15
    LINE_MAX_CHARS = 300
    BATCH_S = 3
    REFRESH_S = 30

    def __init__(self, client, channel: str, ts: str) -> None:
        self.client, self.channel, self.ts = client, channel, ts
        self.started = time.monotonic()
        self.state = "Running"
        self.actions = 0
        self.now = "starting Claude Code"
        self._lines: deque[str] = deque(maxlen=self.TAIL_LINES)
        self._dirty = True
        self._last_push = 0.0
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._ticker = threading.Thread(target=self._tick, daemon=True)
        self._ticker.start()

    def add(self, kind: str, text: str) -> None:
        line = " ".join(text.split())
        if len(line) > self.LINE_MAX_CHARS:
            line = line[: self.LINE_MAX_CHARS - 1] + "…"
        with self._lock:
            self._dirty = True
            if kind == "now":
                self.now = line
                return
            line = f"{STEP_MARKERS[kind]} {line}"
            self.actions += kind == "action"
            self._lines.append(line)
        log.info("  %s", line)

    def finish(self, state: str) -> None:
        self.state = state
        self._done.set()
        self._ticker.join()
        self._push()

    def _tick(self) -> None:
        while not self._done.wait(self.BATCH_S):
            if self._dirty or time.monotonic() - self._last_push >= self.REFRESH_S:
                self._push()

    def _push(self) -> None:
        with self._lock:
            lines = [slack_escape(line) for line in self._lines]
            self._dirty = False
        actions = f"{self.actions} game action{'' if self.actions == 1 else 's'}"
        lines.insert(0, f"*{self.state}* · {minutes(time.monotonic() - self.started)} · {actions}")
        if not self._done.is_set():
            lines.append(f"_⏳ {slack_escape(self.now)}…_")
        self._last_push = time.monotonic()
        try:
            self.client.chat_update(channel=self.channel, ts=self.ts, text="\n".join(lines))
        except Exception as exc:  # rate limit, network: the next edit catches up
            log.warning("progress update failed: %s", exc)


class Steps:
    """Turns Claude Code's stream-json events into progress steps people can follow.

    Game actions get one line each once their result is in. Game queries,
    file reads and doc lookups only change the "now" line, and their errors
    are left to Claude. Claude's narration is held back until its next tool
    call, so the final message, which is posted as the reply, is not listed.
    """

    def __init__(self) -> None:
        self.calls: dict[str, tuple[str, dict]] = {}
        self.names: dict[str, str] = {}  # unit and city ids -> names, from results
        self.narration: str | None = None

    def feed(self, event: dict) -> Iterator[tuple[str, str]]:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            civ6 = next((s.get("status") for s in event.get("mcp_servers", []) if s.get("name") == "civ6"), "missing")
            log.info("session %s · %s · civ6 %s", event.get("session_id"), event.get("model"), civ6)
            if civ6 != "connected":
                yield "error", f"The civ6 game server is {civ6}."
        elif kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    yield from self._narrate()
                    self.narration = block["text"]
                elif block.get("type") == "thinking":
                    yield "now", "thinking"
                elif block.get("type") == "tool_use":
                    yield from self._narrate()
                    name = block.get("name", "")
                    self.calls[block.get("id")] = (name, block.get("input") or {})
                    yield "now", activity(name)
        elif kind == "user":
            content = event.get("message", {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    yield from self._result(block)

    def _narrate(self) -> Iterator[tuple[str, str]]:
        if self.narration:
            yield "text", self.narration
            self.narration = None

    def _result(self, block: dict) -> Iterator[tuple[str, str]]:
        name, args = self.calls.pop(block.get("tool_use_id"), ("", {}))
        if not name.startswith(CIV6):
            return
        tool = name.removeprefix(CIV6)
        text = tool_result_text(block.get("content"))
        for pattern in (UNIT_LINE, CITY_LINE):
            self.names.update((id_, label) for label, id_ in pattern.findall(text))
        if tool.startswith("get_") or tool in CIV6_QUERIES:
            return
        line = self._action(tool, args, text)
        if block.get("is_error") or text.startswith("Error"):
            yield "error", f"{line}: {first_sentences(text, 1)}"
        else:
            yield "action", line

    def _action(self, tool: str, args: dict, result: str) -> str:
        unit = self.names.get(str(args.get("unit_id")), f"unit {args.get('unit_id')}")
        city = self.names.get(str(args.get("city_id")), f"city {args.get('city_id')}")
        target = f"({args['target_x']},{args['target_y']})" if args.get("target_x") is not None else ""
        match tool:
            case "begin_standin_turn":
                return first_sentences(result, 1)
            case "end_turn":
                return first_sentences(result, 2)
            case "unit_action":
                what = args.get("action", "?").replace("_", " ")
                if args.get("improvement"):
                    what += f" {pretty(args['improvement'])}"
                return f"{unit}: {what}" + (f" → {target}" if target else "")
            case "set_research":
                kind = "Civic" if args.get("category") == "civic" else "Research"
                return f"{kind}: {pretty(args.get('tech_or_civic'))}"
            case "set_city_production":
                return f"{city}: produce {pretty(args.get('item_name'))}" + (f" at {target}" if target else "")
            case "purchase_item":
                return f"{city}: buy {pretty(args.get('item_name'))}"
            case "skip_remaining_units":
                return "Skipped the remaining units"
            case _:
                return tool_call(tool, args)


def slack_unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def minutes(seconds: float) -> str:
    return f"{max(1, round(seconds / 60))} min"


def pretty(type_name) -> str:
    """UNIT_WARRIOR -> Warrior, CIVIC_CODE_OF_LAWS -> Code of Laws."""
    words = TYPE_PREFIX.sub("", str(type_name)).lower().split("_")
    return " ".join(w if w in ("of", "the", "and") else w.capitalize() for w in words)


def first_sentences(text: str, n: int) -> str:
    return " ".join(re.split(r"(?<=[.!?])\s+", text.strip().split("\n")[0])[:n])


def activity(tool: str) -> str:
    """What the status line says while a tool runs."""
    if not tool.startswith(CIV6):
        return "looking things up"
    tool = tool.removeprefix(CIV6)
    if tool.startswith("get_"):
        return "checking " + tool.removeprefix("get_").replace("_", " ")
    return tool.replace("_", " ")


def tool_call(name: str, args: dict) -> str:
    """`promote_unit(unit_id=65537, promotion_type=PROMOTION_BATTLECRY)`, values cut short."""
    parts = []
    for key, value in args.items():
        value = " ".join((value if isinstance(value, str) else json.dumps(value)).split())
        parts.append(f"{key}={value if len(value) <= 40 else value[:39] + '…'}")
    return f"{name}({', '.join(parts)})"


def tool_result_text(content) -> str:
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    text = str(content or "")
    if text.startswith('{"result"'):  # FastMCP's structured output for a str return
        try:
            text = str(json.loads(text)["result"])
        except (json.JSONDecodeError, KeyError):
            pass
    return text


def run_claude(prompt: str, claude: str, report: Report) -> str:
    """Run one headless Claude Code session in the repo root, reporting each step; return the Slack reply."""
    cmd = [
        claude, "-p", "--output-format", "stream-json", "--verbose",
        # Only the repo's civ6 server, not the account's claude.ai connectors.
        "--strict-mcp-config", "--mcp-config", str(REPO_ROOT / ".mcp.json"),
        "--allowedTools", *ALLOWED_TOOLS,
        "--disallowedTools", *DISALLOWED_TOOLS,
    ]
    env = {k: v for k, v in os.environ.items() if k not in PARENT_SESSION_VARS}
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd, cwd=REPO_ROOT, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    stderr: list[str] = []
    drain = threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True)
    drain.start()
    timed_out = threading.Event()

    def kill() -> None:
        timed_out.set()
        # /T takes the MCP server (uv, civ-mcp) down with it.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)

    watchdog = threading.Timer(TURN_TIMEOUT_S, kill)
    watchdog.start()
    steps = Steps()
    result: dict = {}
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                result = event
                continue
            for kind, text in steps.feed(event):
                report(kind, text)
        proc.wait()
    finally:
        watchdog.cancel()
    drain.join(timeout=5)
    elapsed = time.monotonic() - started
    log.info("claude exited %s after %s", proc.returncode, minutes(elapsed))

    if timed_out.is_set():
        return (
            f"Gave up after {minutes(TURN_TIMEOUT_S)} and stopped Claude Code. The turn "
            "may be half played; someone should look at the game before the next run."
        )
    if not result:
        tail = ("".join(stderr).strip() or "(no output)")[-1500:]
        return f"Claude Code exited with code {proc.returncode} and no result:\n```{tail}```"

    reply = (result.get("result") or "").strip() or "(Claude finished without a reply.)"
    if result.get("is_error"):
        reply = f"Claude Code stopped with an error ({result.get('subtype')}):\n{reply}"
    session = result.get("session_id", "?")
    if len(reply) > SLACK_MAX_CHARS:
        reply = reply[:SLACK_MAX_CHARS].rstrip() + "\n… (cut short; the full reply is in the session)"
    return f"{reply}\n\n_{minutes(elapsed)} · session `{session}`_"


def turn_status(rec: turn_logger.TurnRecord, worked: float, whose: str) -> str:
    """'It's your turn (Poundmaker, turn 64) since Mon 26 Oct 10:05: 1h 10m of workday time so far.'"""
    return (
        f"It's {whose} turn ({rec.leader_display}, turn {rec.turn}) since "
        f"{rec.at:%a %d %b %H:%M}: {duration(worked)} of workday time so far."
    )


def whose_turn() -> tuple[turn_logger.TurnRecord, float] | None:
    """The current turn and its workday seconds so far, from the turn log after one fresh poll."""
    try:
        turn_logger.log_current_turn()
    except Exception:
        log.exception("turn poll failed")
    return turn_logger.duration_of_current_turn()


def open_dm(client, player: Player | None) -> str | None:
    if player is None:
        return None
    try:
        return client.conversations_open(users=player.slack_id)["channel"]["id"]
    except Exception as exc:
        log.warning("could not open a DM with %s: %s", player.name, exc)
        return None


def log_step(kind: str, text: str) -> None:
    if kind != "now":
        log.info("  %s %s", STEP_MARKERS[kind], " ".join(text.split()))


def run_with_progress(client, channel: str, thread_ts: str | None, prompt: str, claude: str) -> None:
    """Run Claude with a live progress message in a conversation, then post its reply there."""
    status = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="On it.")
    progress = Progress(client, status["channel"], status["ts"])
    state = "Failed"
    try:
        result = run_claude(prompt, claude, progress.add)
        state = "Finished"
    finally:
        progress.finish(state)
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=result)


def register(app: App, claude: str, lock: TurnLock) -> None:
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
    def on_home_opened():
        pass

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
            # Take the button away so nobody clicks it again.
            client.chat_update(
                channel=body["channel"]["id"],
                ts=message["ts"],
                text=f"{message.get('text', '')}\n_{sender.name} asked me to play it._",
                blocks=[],
            )
            play_absent_turn(sender, owner, rec, worked, reply, client)

        locked(user, reply, work)

    def locked(user: str, reply, work: Callable[[], None]) -> None:
        """Do one message's work holding the run lock, or say who holds it."""
        if not lock.try_acquire(user):
            run = lock.current
            since = f" <@{run.user}> started {minutes(time.monotonic() - run.started)} ago" if run else ""
            reply(f"Busy with the run{since}. Message me again when it's done.")
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
        run_with_progress(client, channel, thread_ts, prompt, claude)
        turn_announcer.announce(client)

    def other_turn(sender: Player, owner: Player | None, rec, worked: float, reply) -> None:
        """Another player: the turn's timer, and once it has run out a button to have the turn played.

        The admin gets the button before the timer runs out as well.
        """
        name = owner.name if owner else rec.player
        status = turn_status(rec, worked, f"{name}'s")
        if worked >= TURN_LIMIT_S:
            text = f"{status} That's past the {duration(TURN_LIMIT_S)} limit, so you can have me play it for {name}."
        elif sender.admin:
            text = (
                f"{status} The {duration(TURN_LIMIT_S)} limit is {duration(TURN_LIMIT_S - worked)} away, "
                f"but as the admin you can have me play it for {name} now."
            )
        else:
            reply(
                f"{status} Another player can have it "
                f"played after {duration(TURN_LIMIT_S)} of workday time ({WORKDAY}), so in "
                f"{duration(TURN_LIMIT_S - worked)}."
            )
            return
        button = {
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
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}, {"type": "actions", "elements": [button]}]
        reply(text, blocks=blocks)

    def play_absent_turn(sender: Player, owner: Player | None, rec, worked: float, reply, client):
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
        if dm:
            yours = f"Your turn ({rec.leader_display}, turn {rec.turn}) has lasted {duration(worked)} of workday time"
            if timed_out:
                text = f"{yours}, so {sender.name} asked me to play it for you."
            else:
                text = f"{yours}; {sender.name}, the game's admin, asked me to play it for you."
            client.chat_postMessage(channel=dm, text=text)
            run_with_progress(client, dm, None, prompt, claude)
        else:
            log.info("reply for %s (not on Slack):\n%s", name, run_claude(prompt, claude, log_step))
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

    lock = TurnLock()
    app = App(token=bot_token)
    register(app, claude, lock)
    current = turn_logger.duration_of_current_turn()
    turn = f"{current[0].player}'s turn {current[0].turn} since {current[0].at:%a %d %b %H:%M}" if current else "unknown"
    log.info("%d players; turn log: %s; claude: %s; repo: %s", len(PLAYERS), turn, claude, REPO_ROOT)
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
