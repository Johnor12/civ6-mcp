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

    def taken_line(self, at: datetime, turn: int, leader: str, player: str, slot: int = 0, why: str = "play") -> None:
        """The bridge mod's line for Start Turn pressed ("play"), or the MCP tooling's for Claude taking the seat
        ("standin"), which comes wrapped in the output line of the bridge command that printed it."""
        line = f"MCPB|0|T|{int(at.timestamp())}|{turn}|{slot}|{leader}|{player}|{why}"
        self.write(f"MCPBridge_InGame: MCPB|1793740441034|O|{line}" if why == "standin" else f"MCPBridge_InGame: {line}")

    def screen_line(self, turn: int) -> None:
        """The line the game's PlayerChange.lua prints when it builds a turn-change screen."""
        self.write(f"PlayerChange: BuildTurnControls: CurrentGameTurn={turn}")

    def start_turn(self, slot: int) -> None:
        """The line the game's UnitPanel.lua prints when that slot presses Start Turn."""
        self.write(f"UnitPanel: OnPlayerChangeClose: {slot}, GameConfiguration.IsPaused()=true")

    def relaunch(self) -> None:
        """Civ 6 started again: it empties Lua.log and writes its first lines."""
        self.path.write_bytes(b"")
        self.write("DebugHotloadCache: GameDebug initialized!", "Test: Test.LUA loaded!")

    def load(self, menu: bool = True) -> None:
        """The game's lines for a save being loaded: picked in the Load Game menu, or (menu=False) by the tooling."""
        if menu:
            self.write("LoadGameMenu: LoadGameMenu::OnLoadYes() leaving the network session.")
        self.write("StagingRoom: Building Game Setup", "LoadScreen: true", "LoadScreen: OnLoadGameViewStateDone")


@pytest.fixture(autouse=True)
def game_log(tmp_path, monkeypatch) -> GameLog:
    log = GameLog(tmp_path / "Logs" / "Lua.log")
    monkeypatch.setattr(turn_watch, "_default", turn_watch.TurnWatch(log.path))
    return log
