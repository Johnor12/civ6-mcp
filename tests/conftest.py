"""Shared fixtures. Every test gets a Lua.log of its own, so nothing reads the running game's (civ_mcp.turn_watch)."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from civ_mcp import screen, turn_watch  # noqa: E402


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


class FakeGameWindow:
    """Stands in for the game window: records the keys pressed and the pictures taken, and can fail like no game."""

    def __init__(self) -> None:
        self.keys: list[int] = []
        self.captures: list[Path] = []
        self.error: Exception | None = None

    def find(self) -> screen.GameWindow:
        if self.error:
            raise self.error
        return screen.GameWindow(hwnd=1, width=3840, height=2160)

    def press_key(self, vk: int = screen.VK_ESCAPE, window=None) -> None:
        self.find()
        self.keys.append(vk)

    def capture(self, path: Path, window=None, quality: int = screen.JPEG_QUALITY) -> screen.GameWindow:
        found = self.find()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpg")
        self.captures.append(path)
        return found


@pytest.fixture(autouse=True)
def game_window(monkeypatch) -> FakeGameWindow:
    """No test presses a key in, or takes a picture of, the Civ 6 that may be running on this PC."""
    window = FakeGameWindow()
    monkeypatch.setattr(screen, "find_game_window", window.find)
    monkeypatch.setattr(screen, "press_key", window.press_key)
    monkeypatch.setattr(screen, "capture", window.capture)
    return window


@pytest.fixture(autouse=True)
def screenshots(tmp_path, monkeypatch) -> Path:
    """Every test gets a screenshots folder of its own."""
    folder = tmp_path / "screenshots"
    folder.mkdir()
    monkeypatch.setattr(screen, "screenshots_dir", lambda: folder)
    return folder
