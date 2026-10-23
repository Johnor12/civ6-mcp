"""Launch Claude Code in the repo to play the turn, and read what happened."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from slackbot.config import Settings

log = logging.getLogger(__name__)

DEFAULT_NOTE = "Play a steady, sensible turn. No wars or peace deals. Leave a clear diary entry."


@dataclass
class RunResult:
    ok: bool
    text: str  # Claude's final answer, or the error
    pre_turn_save: str = ""
    finished_save: str = ""
    next_player: str = ""  # hotseat player name the game was left on


def build_prompt(game_leader: str, note: str) -> str:
    """Matches the operator phrasing that CLAUDE.md expects."""
    note = note.strip() or DEFAULT_NOTE
    return f"take {game_leader}'s turn — they said: {note}"


def _read_marker() -> Any:
    from civ_mcp import standin

    return standin.load_config()


class AgentRunner:
    def __init__(
        self,
        settings: Settings,
        spawn: Callable[..., Any] = asyncio.create_subprocess_exec,
        read_marker: Callable[[], Any] = _read_marker,
    ) -> None:
        self.settings = settings
        self._spawn = spawn
        self._read_marker = read_marker

    def command(self) -> list[str]:
        s = self.settings
        exe = shutil.which(s.claude_bin) or s.claude_bin
        # The prompt goes through stdin: no shell quoting problems on Windows.
        return [
            exe,
            "-p",
            "--output-format",
            "json",
            "--allowedTools",
            "mcp__civ6",
            "--max-turns",
            str(s.max_agent_turns),
        ]

    async def run(self, game_leader: str, note: str) -> RunResult:
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        prompt = build_prompt(game_leader, note)
        try:
            proc = await self._spawn(
                *self.command(),
                cwd=str(self.settings.repo_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return RunResult(False, f"Could not start Claude Code: {e}")
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), self.settings.run_timeout_s
            )
        except asyncio.TimeoutError:
            await _kill_tree(proc)
            return self._with_marker(
                RunResult(False, f"Timed out after {int(self.settings.run_timeout_s // 60)} minutes."),
                started,
            )
        text, is_error = _parse_output(out.decode("utf-8", "replace"))
        if proc.returncode not in (0, None) or is_error:
            tail = err.decode("utf-8", "replace").strip()[-500:]
            text = f"{text}\n{tail}".strip() or f"Claude Code exited with code {proc.returncode}."
            return self._with_marker(RunResult(False, text), started)
        return self._with_marker(RunResult(True, text or "(no output)"), started)

    def _with_marker(self, result: RunResult, started_iso: str) -> RunResult:
        """The marker file is the proof: it says whether end_turn really ran."""
        cfg = self._read_marker()
        if cfg is None or not cfg.started_at or cfg.started_at < started_iso:
            if result.ok:
                result.ok = False
                result.text = "Claude finished, but the game shows no stand-in turn.\n" + result.text
            return result
        result.pre_turn_save = cfg.pre_turn_save
        result.finished_save = cfg.finished_save
        result.next_player = cfg.next_player
        if result.ok and cfg.finished_turn is None:
            result.ok = False
            result.text = "Claude finished, but the turn was not ended.\n" + result.text
        return result


def _parse_output(raw: str) -> tuple[str, bool]:
    """(final text, is_error) from `claude -p --output-format json`."""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.strip(), False
    if isinstance(data, dict):
        return str(data.get("result") or "").strip(), bool(data.get("is_error"))
    return raw.strip(), False


async def _kill_tree(proc: Any) -> None:
    """claude.cmd on Windows spawns children; kill the whole tree."""
    try:
        if sys.platform == "win32" and getattr(proc, "pid", None):
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid)
            )
            await killer.wait()
        else:
            proc.kill()
        await proc.wait()
    except Exception as e:  # noqa: BLE001 - best effort
        log.warning("could not kill agent process: %s", e)


def split_for_slack(text: str, limit: int = 3500) -> list[str]:
    """Cut long text into Slack-sized pieces, preferring line breaks."""
    chunks: list[str] = []
    text = text.strip() or "(empty)"
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    chunks.append(text)
    return chunks
