"""Headless Claude Code runs for the Slack bot and the out-of-office service.

Imported by scripts/slack_bot.py and scripts/out_of_office.py; standard
library only. A run is `claude -p` in the repo root with only the repo's
civ6 MCP server and tight tool permissions. Its progress goes to a Slack
message edited in place and to the console; Claude's final message is the
reply. RunLock keeps runs one at a time across both processes, since they
drive the same game.
"""

from __future__ import annotations

import json
import logging
import msvcrt
import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from players import Player

REPO_ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("claude_runs")

TURN_TIMEOUT_S = 90 * 60
SLACK_MAX_CHARS = 3500
RUN_LOCK_PATH = REPO_ROOT / "scripts" / "data" / "claude_run.lock"

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

# For a turn played without its player: `reason` ends the first sentence,
# `note` goes to begin_standin_turn.
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
    label: str  # what the run is, for the busy reply: "a run for <@U123>"
    started: float  # time.time()


class RunLock:
    """One Claude run at a time, across the bot's threads and the out-of-office service.

    A byte lock on a file, which Windows drops when the process holding it
    dies. What holds it is written next to it, for the busy reply.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or RUN_LOCK_PATH
        self.info = self.path.with_suffix(".json")
        self._thread = threading.Lock()
        self._fd: int | None = None

    def try_acquire(self, label: str) -> bool:
        if not self._thread.acquire(blocking=False):
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                os.close(fd)
                self._thread.release()
                return False
        except BaseException:
            self._thread.release()
            raise
        self._fd = fd
        self.info.write_text(json.dumps({"label": label, "started": time.time()}), encoding="utf-8")
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        try:
            self.info.unlink(missing_ok=True)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
            self._thread.release()

    @property
    def current(self) -> Run | None:
        """The run holding the lock in either process, or None."""
        try:
            info = json.loads(self.info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if self._fd is None and not self._held_elsewhere():
            return None  # left behind by a process that died holding the lock
        return Run(info.get("label", "a run"), float(info.get("started", time.time())))

    def _held_elsewhere(self) -> bool:
        try:
            fd = os.open(self.path, os.O_RDWR)
        except OSError:
            return False
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return False
        finally:
            os.close(fd)


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


def play_absent_turn(client, claude: str, dm: str | None, name: str, prompt: str, intro: str, blocks: list | None = None) -> None:
    """Have Claude play the turn of a player who isn't here, from a fixed prompt.

    `intro` (with `blocks`, if any), the run's progress and Claude's reply go
    to the player's DM; for a player with no DM the reply goes to the log.
    """
    if dm:
        client.chat_postMessage(channel=dm, text=intro, **({"blocks": blocks} if blocks else {}))
        run_with_progress(client, dm, None, prompt, claude)
    else:
        log.info("reply for %s (not on Slack):\n%s", name, run_claude(prompt, claude, log_step))
