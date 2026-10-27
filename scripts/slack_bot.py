#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "slack-bolt>=1.21",
#     "python-dotenv>=1.0",
#     "mss>=9.0",
# ]
# ///
"""Slack trigger for the stand-in: players message @civ6 to have Claude Code play a hotseat turn.

    uv run scripts/slack_bot.py

Connects over Socket Mode (an outbound websocket, so no public URL). Only
the players in PLAYERS may use it; anyone else gets a joke and nothing runs.
Every message is checked against whose turn the game is on:

- From the player whose turn it is, by DM: the bot replies with their turn
  and how long it has lasted, then runs `claude -p` in the repo root to play
  it with their message as the note. In a channel they are asked to DM
  instead, because the other players can read the channel.
- From any other player, by DM or channel mention: once the turn has lasted
  TURN_LIMIT_S of workday time, Claude plays it from a fixed prompt (the
  message itself never reaches Claude). The absent player gets the progress
  and Claude's reply by DM; the sender only hears whose turn it is now.
  Before the limit, the sender gets the timer.

How long a turn has lasted comes from the turn log that scripts/turn_logger.py
keeps (scripts/start.py runs both). Before reading it the bot has the logger
ask the game once, so a turn ended at the PC or by the last run counts
without waiting for the logger's next poll.

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

import screenshots  # scripts/screenshots.py, next to this script
import slack_setup
import turn_logger  # scripts/turn_logger.py, next to this script
import turn_notices
from dotenv import dotenv_values
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

REPO_ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("slack_bot")

REJECTION = "Deleting save, uninstalling Civilization 6, and wiping Windows."
TURN_TIMEOUT_S = 90 * 60
SLACK_MAX_CHARS = 3500
TURN_LIMIT_S = 2 * 3600  # workday time after which another player may have a turn played
DEFAULT_CHANNEL = "C09AZ6CR7QE"  # #proj-diplomatic-relations: pings, requests, results
NOTICE_POLL_S = 60
WORKDAY = f"{turn_logger.WORKDAY_START_HOUR}:00–{turn_logger.WORKDAY_END_HOUR}:00, Mon–Fri"


@dataclass(frozen=True)
class Player:
    name: str  # hotseat player name in the game
    slack_id: str


# Everyone who may use the bot. They play under their first names, except
# Jake (full name); John's hotseat name is "John" with the quotes, and names
# match without quotes or case.
PLAYERS = (
    Player("Aaron", "U0AN2M2PENR"),  # Aaron Sykes
    Player("Aidan", "U05JN8AA770"),  # Aidan O'Keefe
    Player("Jake DuRegger", "U09E350TA30"),
    Player("Jeffson", "U0A6V0J4QP4"),  # Jeffson Lin
    Player("Mike", "U07UB8TFRNK"),  # Mike Dombrowski
    Player("Cameron", "U08U40V8GCD"),  # Cameron Gonzalez
    Player("Heldi", "U0B1J1T47QC"),  # Heldi Valikaj
    Player("Randy", "U0B7FQ1GJ7N"),  # Randy Schwemmer
    Player("Hunter", "U07G75S7JBS"),  # Hunter Boyd
    Player("John", "U035CAQE34H"),  # John McGue
)
SLACK_PLAYERS = {p.slack_id: p for p in PLAYERS}

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
game. It is their turn, turn {turn}, and their message below is their note for \
it. Nobody is at the terminal and nobody can answer questions before the run \
ends, so decide for yourself and put your reasoning in the diary.

Take {leader}'s turn: follow the stand-in workflow in CLAUDE.md through \
end_turn, with the message as the note for begin_standin_turn. The message \
speaks for {leader}'s turn only: act for no other player, read no other \
player's diary, and load a save only to open this turn if the game is at the \
main menu. If something stops you (another player's turn-change screen, the \
game not running, a popup that needs ESC in the game), stop and say what \
someone at the PC has to do.

Your final message is sent to {name} in Slack as-is. {slack_format}

Message from {name}:
{text}
"""

TIMED_OUT_PROMPT = """\
This run was started from Slack because a turn timer ran out: it has been \
{name}'s turn ({leader}, turn {turn}) for {worked} of workday time, past the \
{limit} limit, and another player asked for it to be played. {name} left no \
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
TIMED_OUT_NOTE = "Turn timer ran out; played without a note from the player."

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


def same_name(a: str, b: str) -> bool:
    """Hotseat names compare without surrounding quotes or case: '"John"' is John."""
    def norm(name: str) -> str:
        return name.strip().strip("\"'“”‘’").strip().casefold()

    return norm(a) == norm(b)


def player_named(name: str) -> Player | None:
    return next((p for p in PLAYERS if same_name(p.name, name)), None)


def duration(seconds: float) -> str:
    """5400 -> '1h 30m'."""
    hours, mins = divmod(int(seconds // 60), 60)
    parts = [f"{hours}h"] if hours else []
    if mins or not hours:
        parts.append(f"{mins}m")
    return " ".join(parts)


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


@dataclass
class Services:
    """What the Slack flows share: the result channel, approvals and screenshots."""

    channel: str = DEFAULT_CHANNEL
    notices: turn_notices.TurnNotices | None = None
    approvals_needed: int = turn_notices.APPROVALS_NEEDED
    uploads: bool = True  # the app has files:write

    def __post_init__(self) -> None:
        self.notices = self.notices or turn_notices.TurnNotices()


def key_of(rec: turn_logger.TurnRecord) -> str:
    return turn_notices.turn_key(rec.turn, rec.leader, rec.player)


def mention(player: Player | None, fallback: str) -> str:
    return f"<@{player.slack_id}>" if player else fallback


def request_blocks(rec, owner: Player | None, worked: float, needed: int, approvers: list[str]) -> list[dict]:
    """The channel message that lets players approve Claude playing a late turn."""
    name = owner.name if owner else rec.player
    who = ", ".join(f"<@{u}>" for u in approvers) or "nobody yet"
    text = (
        f"{mention(owner, name)} your turn ({rec.leader_display}, turn {rec.turn}) has lasted "
        f"{duration(worked)} of workday time, past the {duration(TURN_LIMIT_S)} limit. Claude can play it "
        f"from a fixed prompt.\n*Approvals:* {who}. Needs {needed} players, or {name} alone."
    )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": "civ_approve",
                    "value": key_of(rec),
                }
            ],
        },
    ]


def request_text(owner: Player | None, rec) -> str:
    return f"Takeover request for {owner.name if owner else rec.player}'s turn {rec.turn}"


def post_request(client, svc: Services, rec, owner: Player | None, worked: float) -> None:
    """Post the approval request for this turn once, and keep its approval list current."""
    key = key_of(rec)
    notice = svc.notices.get(key)
    blocks = request_blocks(rec, owner, worked, svc.approvals_needed, notice.approvers)
    if notice.request_ts:
        client.chat_update(channel=svc.channel, ts=notice.request_ts, text=request_text(owner, rec), blocks=blocks)
        return
    posted = client.chat_postMessage(channel=svc.channel, text=request_text(owner, rec), blocks=blocks)
    svc.notices.update(key, request_ts=posted["ts"])


def close_request(client, svc: Services, rec, owner: Player | None, note: str) -> None:
    ts = svc.notices.get(key_of(rec)).request_ts
    if ts:
        client.chat_update(channel=svc.channel, ts=ts, text=f"{request_text(owner, rec)}: {note}", blocks=[])


def notice_tick(client, svc: Services) -> None:
    """Ping at an hour of workday time; at the limit, post the approval request."""
    current = turn_logger.duration_of_current_turn()
    if current is None:
        return
    rec, worked = current
    owner = player_named(rec.player)
    key = key_of(rec)
    actions = turn_notices.due_actions(svc.notices.get(key), worked, TURN_LIMIT_S)
    if "warn" in actions:
        svc.notices.update(key, pinged=True, warned=True)
        post_request(client, svc, rec, owner, worked)
    elif "ping" in actions:
        svc.notices.update(key, pinged=True)
        client.chat_postMessage(
            channel=svc.channel,
            text=(
                f"{mention(owner, rec.player)} your turn ({rec.leader_display}, turn {rec.turn}) has lasted "
                f"{duration(worked)} of workday time. After {duration(TURN_LIMIT_S)} other players can "
                "approve Claude playing it. DM me a note to have Claude play it now."
            ),
        )


def notice_loop(client, svc: Services, lock: TurnLock) -> None:
    while True:
        time.sleep(NOTICE_POLL_S)
        if lock.current is not None:
            continue  # a run is on: the turn is being played
        try:
            notice_tick(client, svc)
        except Exception:
            log.exception("notice poll failed")


def upload_shots(client, channel: str, thread_ts: str | None, paths: list[Path]) -> None:
    """Upload pictures, then delete them. A failed upload is logged, not raised."""
    for path in paths:
        try:
            client.files_upload_v2(channel=channel, file=str(path), title=path.stem, thread_ts=thread_ts)
        except Exception as exc:
            log.warning("could not upload %s: %s", path.name, exc)
        finally:
            path.unlink(missing_ok=True)


def post_outcome(client, svc: Services, owner: Player | None, rec, after, session) -> None:
    """After a run: tell the channel, @ the next player, and send the pictures where they belong.

    Mid-turn pictures show the player's own map, so they go to that player
    by DM only. The first and last picture go to the channel, but only when
    the turn ended: then the last one shows the next player's turn-change
    screen. If the turn did not end, every picture goes to the player.
    """
    name = owner.name if owner else rec.player
    ended = after is not None and after.key != rec.key
    shots = session
    if ended:
        nxt = player_named(after.player)
        root = client.chat_postMessage(
            channel=svc.channel,
            text=(
                f"Claude played {name}'s turn {rec.turn} ({rec.leader_display}). "
                f"{mention(nxt, after.player)} you're up."
            ),
        )["ts"]
        public = [p for p in (shots.pre, shots.post) if p] if shots else []
        private = list(shots.mid) if shots else []
    else:
        client.chat_postMessage(
            channel=svc.channel,
            text=f"Claude did not finish {name}'s turn {rec.turn}. Someone at the PC should look at the game.",
        )
        root, public, private = None, [], (shots.all() if shots else [])
    if not svc.uploads:
        for path in public + private:
            path.unlink(missing_ok=True)
        return
    upload_shots(client, svc.channel, root, public)
    dm = open_dm(client, owner)
    if dm:
        upload_shots(client, dm, None, private)
    else:
        for path in private:
            path.unlink(missing_ok=True)


def run_turn(client, svc: Services, channel: str, thread_ts: str | None, prompt: str, claude: str,
             owner: Player | None, rec) -> None:
    """Play a turn with live progress, screenshots, and the channel announcement after it."""
    session = screenshots.ShotSession() if svc.uploads else None
    if session:
        session.start()
    try:
        run_with_progress(client, channel, thread_ts, prompt, claude)
    finally:
        if session:
            session.finish()
    after = whose_turn()
    post_outcome(client, svc, owner, rec, after[0] if after else None, session)


def register(app: App, claude: str, lock: TurnLock, svc: Services | None = None) -> None:
    svc = svc or Services()
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

    @app.action("civ_approve")
    def on_approve(ack, body, client):
        """Approve button on the takeover request: counts like a DM from that player."""
        ack()
        user = body["user"]["id"]
        channel = body.get("channel", {}).get("id") or svc.channel

        def reply(text: str):
            return client.chat_postEphemeral(channel=channel, user=user, text=text)

        sender = SLACK_PLAYERS.get(user)
        if sender is None:
            reply(REJECTION)
            return
        if not lock.try_acquire(user):
            reply("Busy with a run. Click Approve again when it's done.")
            return
        try:
            current = whose_turn()
            if current is None:
                reply("I can't tell whose turn it is: the game isn't loaded. Someone at the PC has to open it.")
                return
            rec, worked = current
            if body["actions"][0].get("value") != key_of(rec):
                reply("That turn is already over; the request is out of date.")
                return
            timed_out_turn(sender, player_named(rec.player), rec, worked, reply, client)
        except Exception as exc:
            log.exception("approval failed")
            reply(f"The bot itself failed: `{exc}`")
        finally:
            lock.release()

    def handle(body, event, context, client, say, thread_ts: str | None, is_dm: bool) -> None:
        # Socket Mode can redeliver an event after a reconnect.
        event_id = body.get("event_id", event.get("ts"))
        if event_id in seen:
            return
        seen.append(event_id)

        user = event.get("user")
        if not user:
            return

        def reply(text: str):
            return say(text=text, thread_ts=thread_ts)

        sender = SLACK_PLAYERS.get(user)
        if sender is None:
            log.warning("refused %s: %r", user, event.get("text"))
            reply(REJECTION)
            return

        if not lock.try_acquire(user):
            run = lock.current
            since = f" <@{run.user}> started {minutes(time.monotonic() - run.started)} ago" if run else ""
            reply(f"Busy with the run{since}. Message me again when it's done.")
            return
        try:
            current = whose_turn()
            if current is None:
                reply(
                    "I can't tell whose turn it is: the game isn't loaded and the turn log "
                    "is empty. Someone at the PC has to open the hotseat game."
                )
                return
            rec, worked = current
            owner = player_named(rec.player)
            if owner == sender:
                text = slack_unescape(re.sub(rf"<@{context.bot_user_id}>", "", event.get("text", ""))).strip()
                own_turn(sender, rec, worked, text, is_dm, reply, client, event["channel"], thread_ts)
            else:
                timed_out_turn(sender, owner, rec, worked, reply, client)
        except Exception as exc:
            log.exception("run failed")
            reply(f"The bot itself failed: `{exc}`")
        finally:
            lock.release()

    def own_turn(sender: Player, rec, worked: float, text: str, is_dm: bool, reply, client, channel: str, thread_ts):
        """The player whose turn it is: their message is the note for their turn."""
        status = turn_status(rec, worked, "your")
        if not is_dm:
            reply(f"{status} DM me your note for it: the other players can read this channel.")
            return
        if not text:
            reply(f"{status} Send me your note for it, e.g. `finish the campus, no wars`.")
            return
        reply(f"{status} Playing it now with your note.")
        log.info("own turn for %s, %s turn %d: %r", sender.name, rec.leader, rec.turn, text)
        prompt = OWN_TURN_PROMPT.format(
            name=sender.name, leader=rec.leader_display, turn=rec.turn, text=text, slack_format=SLACK_FORMAT
        )
        run_turn(client, svc, channel, thread_ts, prompt, claude, sender, rec)

    def timed_out_turn(sender: Player, owner: Player | None, rec, worked: float, reply, client):
        """Another player: play the turn from a fixed prompt once its timer has run out."""
        name = owner.name if owner else rec.player
        if worked < TURN_LIMIT_S:
            whose = f"{name}'s"
            reply(
                f"{turn_status(rec, worked, whose)} Another player can have it "
                f"played after {duration(TURN_LIMIT_S)} of workday time ({WORKDAY}), so in "
                f"{duration(TURN_LIMIT_S - worked)}."
            )
            return
        key = key_of(rec)
        approvers = svc.notices.add_approval(key, sender.slack_id).approvers
        if not turn_notices.approval_met(set(approvers), owner.slack_id if owner else None, svc.approvals_needed):
            post_request(client, svc, rec, owner, worked)
            reply(
                f"{name}'s turn has lasted {duration(worked)} of workday time, past the "
                f"{duration(TURN_LIMIT_S)} limit. Approval recorded ({len(approvers)}/{svc.approvals_needed}): "
                f"it needs {svc.approvals_needed} players, or {name} alone. Use the Approve button in <#{svc.channel}>."
            )
            return
        approved_by = ", ".join(SLACK_PLAYERS[u].name for u in approvers if u in SLACK_PLAYERS)
        close_request(client, svc, rec, owner, f"approved by {approved_by}. Claude is playing it.")
        svc.notices.clear_approvals(key)
        dm = open_dm(client, owner)
        reply(
            f"{name}'s turn ({rec.leader_display}, turn {rec.turn}) has lasted {duration(worked)} "
            f"of workday time, past the {duration(TURN_LIMIT_S)} limit. Playing it for them now"
            + (f"; {name} gets the details." if dm else ".")
        )
        log.info("timed-out turn for %s, %s turn %d, asked by %s", name, rec.leader, rec.turn, sender.name)
        prompt = TIMED_OUT_PROMPT.format(
            name=name,
            leader=rec.leader_display,
            turn=rec.turn,
            worked=duration(worked),
            limit=duration(TURN_LIMIT_S),
            note=TIMED_OUT_NOTE,
            slack_format=SLACK_FORMAT,
        )
        if dm:
            client.chat_postMessage(
                channel=dm,
                text=(
                    f"Your turn ({rec.leader_display}, turn {rec.turn}) has lasted {duration(worked)} "
                    f"of workday time, so {sender.name} asked me to play it for you."
                ),
            )
            run_turn(client, svc, dm, None, prompt, claude, owner, rec)
        else:
            log.info("reply for %s (not on Slack):\n%s", name, run_claude(prompt, claude, log_step))
        current = whose_turn()
        after = current[0] if current else None
        if after is not None and after.key != rec.key:
            nxt = player_named(after.player)
            reply(f"Done: {name}'s turn {rec.turn} is over. It's now {nxt.name if nxt else after.player}'s turn.")
        else:
            reply(f"{name}'s turn was not finished. Someone at the PC may need to look at the game.")


def check_scopes(client) -> bool:
    """Stop if a scope the bot needs is missing. Returns whether screenshots can be uploaded."""
    try:
        resp = client.auth_test()
    except Exception as exc:
        raise SystemExit(f"Slack rejected the bot token: {exc}")
    header = next((v for k, v in dict(resp.headers or {}).items() if str(k).lower() == "x-oauth-scopes"), None)
    if header is None:
        log.warning("Slack did not report the token's scopes; not checking them")
        return True
    granted = slack_setup.parse_scopes(header)
    fatal, warnings = slack_setup.scope_problems(granted)
    for w in warnings:
        log.warning(w)
    if fatal:
        raise SystemExit("; ".join(fatal) + ". Fix them under OAuth & Permissions and reinstall the app.")
    return "files:write" in granted


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
    svc = Services(
        channel=env.get("CIV_CHANNEL_ID") or DEFAULT_CHANNEL,
        approvals_needed=int(env.get("APPROVALS_NEEDED") or turn_notices.APPROVALS_NEEDED),
    )
    svc.uploads = check_scopes(app.client)
    register(app, claude, lock, svc)
    threading.Thread(target=notice_loop, args=(app.client, svc, lock), daemon=True).start()
    current = turn_logger.duration_of_current_turn()
    turn = f"{current[0].player}'s turn {current[0].turn} since {current[0].at:%a %d %b %H:%M}" if current else "unknown"
    log.info("%d players; turn log: %s; claude: %s; repo: %s", len(PLAYERS), turn, claude, REPO_ROOT)
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
