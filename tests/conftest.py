"""Shared fixtures. Every test gets a Lua.log of its own, so nothing reads the running game's (civ_mcp.turn_watch)."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from civ_mcp import turn_watch  # noqa: E402


class GameLog:
    """A Lua.log for the test to write the bridge mod's turn lines and the game's own lines into."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

    def write(self, *lines: str) -> None:
        with open(self.path, "ab") as f:
            f.write(b"".join(line.encode("utf-8") + b"\r\n" for line in lines))

    def turn_line(self, at: datetime, turn: int, leader: str, player: str, slot: int = 0, why: str = "begin") -> None:
        """The line the bridge mod prints when a hotseat turn begins (or a save is loaded into one)."""
        self.write(f"MCPBridge_InGame: MCPB|0|T|{int(at.timestamp())}|{turn}|{slot}|{leader}|{player}|{why}")

    def screen_line(self, turn: int) -> None:
        """The line the game's PlayerChange.lua prints when it builds a turn-change screen."""
        self.write(f"PlayerChange: BuildTurnControls: CurrentGameTurn={turn}")


@pytest.fixture(autouse=True)
def game_log(tmp_path, monkeypatch) -> GameLog:
    log = GameLog(tmp_path / "Logs" / "Lua.log")
    monkeypatch.setattr(turn_watch, "_default", turn_watch.TurnWatch(log.path))
    return log
