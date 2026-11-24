"""Headless runs: which session a run is in, resuming one, and the session kept for a turn.

Claude Code never runs: Popen is a fake that plays back stream-json events.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import claude_runs  # noqa: E402

KEY = (10, "LEADER_NZINGA_MBANDE", "Aidan")


def init(session: str) -> dict:
    return {"type": "system", "subtype": "init", "session_id": session, "mcp_servers": [{"name": "civ6", "status": "connected"}]}


def result(session: str, text: str = "Done.", **more) -> dict:
    return {"type": "result", "subtype": "success", "session_id": session, "result": text, **more}


class FakeClaude:
    """Stands in for subprocess.Popen: records each command and prompt, prints the next run's events."""

    def __init__(self, monkeypatch) -> None:
        self.commands: list[list[str]] = []
        self.prompts: list[str] = []
        self.outputs: list[list[dict]] = []
        monkeypatch.setattr(claude_runs.subprocess, "Popen", self._popen)
        monkeypatch.setattr(claude_runs.screen, "clear_screenshots", lambda: None)
        monkeypatch.setattr(claude_runs.screen, "set_delivery", lambda on: None)

    def _popen(self, cmd, **kwargs):
        fake = self
        self.commands.append(cmd)

        class Stdin(io.StringIO):
            def close(self) -> None:
                fake.prompts.append(self.getvalue())
                super().close()

        class Proc:
            pid = 0
            returncode = 0
            stdin = Stdin()
            stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in self.outputs.pop(0)))
            stderr = io.StringIO("")

            def wait(self) -> int:
                return self.returncode

        return Proc()

    def run(self, prompt: str, resume: str | None = None) -> claude_runs.Outcome:
        return claude_runs.run_claude(prompt, "claude", lambda kind, text: None, resume=resume)


@pytest.fixture
def claude(monkeypatch) -> FakeClaude:
    return FakeClaude(monkeypatch)


def test_a_run_reports_its_session(claude):
    claude.outputs = [[init("S1"), result("S1", "Moved the scout.")]]
    outcome = claude.run("play my turn")
    assert outcome.session == "S1"
    assert outcome.reply.startswith("Moved the scout.") and "session `S1`" in outcome.reply
    assert "--resume" not in claude.commands[0]
    assert claude.prompts == ["play my turn"]


def test_a_resumed_run_stays_in_its_session(claude):
    claude.outputs = [[init("S1"), result("S1", "The river spot.")]]
    outcome = claude.run("and the second city?", resume="S1")
    command = claude.commands[0]
    assert command[command.index("--resume") + 1] == "S1"
    assert outcome.session == "S1" and outcome.reply.startswith("The river spot.")


def test_resuming_a_session_that_is_gone_raises(claude):
    # What Claude Code 2.1 prints for an unknown id: an error result and nothing else.
    gone = result("S1", "", subtype="error_during_execution", is_error=True, errors=["No conversation found with session ID: S1"])
    claude.outputs = [[gone]]
    with pytest.raises(claude_runs.SessionLost):
        claude.run("and the second city?", resume="S1")


def test_a_failed_run_keeps_the_session_it_began(claude):
    claude.outputs = [[init("S1")]]  # killed or crashed before its result
    outcome = claude.run("play my turn")
    assert outcome.session == "S1" and "no result" in outcome.reply


def test_a_run_that_never_began_has_no_session(claude):
    claude.outputs = [[]]
    outcome = claude.run("play my turn")
    assert outcome.session is None and "no result" in outcome.reply


def test_the_session_is_kept_for_its_turn_only(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_runs, "TURN_SESSION_PATH", tmp_path / "data" / "turn_session.json")
    assert claude_runs.turn_session(KEY) is None
    claude_runs.remember_turn_session(KEY, "S1")
    assert claude_runs.turn_session(KEY) == "S1"
    assert claude_runs.turn_session((11, *KEY[1:])) is None
    assert claude_runs.turn_session((10, "LEADER_VICTORIA", "Jake DuRegger")) is None
    claude_runs.remember_turn_session((11, *KEY[1:]), "S2")
    assert claude_runs.turn_session(KEY) is None and claude_runs.turn_session((11, *KEY[1:])) == "S2"


def test_an_unreadable_session_file_is_no_session(monkeypatch, tmp_path):
    path = tmp_path / "turn_session.json"
    monkeypatch.setattr(claude_runs, "TURN_SESSION_PATH", path)
    path.write_text("not json", encoding="utf-8")
    assert claude_runs.turn_session(KEY) is None
