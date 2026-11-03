"""set_research / set_civic always switch, never queue.

The game's ResearchChooser.lua and CivicsChooser.lua send the item's Hash
with PARAM_INSERT_MODE = VALUE_EXCLUSIVE. Without the insert mode the game
appends the request, so switching away from an in-progress tech (Astrology
to Archery, 2026-11-02) did nothing.
"""

import asyncio

from civ_mcp import lua as lq
from civ_mcp.game_state import GameState


class _StubConnection:
    """Answers execute() from a queue of responses, in call order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent: list[str] = []

    async def execute(self, lua, timeout=None):
        self.sent.append(lua)
        return self._responses.pop(0)


class TestSwitchRequest:
    def test_research_is_exclusive_by_hash(self):
        lua = lq.build_set_research("TECH_ARCHERY")
        assert "PlayerOperations.RESEARCH" in lua
        assert "params[PlayerOperations.PARAM_TECH_TYPE] = hash" in lua
        assert (
            "params[PlayerOperations.PARAM_INSERT_MODE] = PlayerOperations.VALUE_EXCLUSIVE"
            in lua
        )
        assert "VALUE_APPEND" not in lua

    def test_civic_is_exclusive_by_hash(self):
        lua = lq.build_set_civic("CIVIC_FOREIGN_TRADE")
        assert "PlayerOperations.PROGRESS_CIVIC" in lua
        assert "params[PlayerOperations.PARAM_CIVIC_TYPE] = hash" in lua
        assert (
            "params[PlayerOperations.PARAM_INSERT_MODE] = PlayerOperations.VALUE_EXCLUSIVE"
            in lua
        )
        assert "VALUE_APPEND" not in lua


class TestVerification:
    def test_research_switch_confirmed(self):
        conn = _StubConnection([["OK:RESEARCHING|TECH_ARCHERY"], ["MATCH"]])
        result = asyncio.run(GameState(conn).set_research("TECH_ARCHERY"))  # type: ignore[arg-type]
        assert "SILENT_FAILURE" not in result
        assert "TECH_ARCHERY" in result

    def test_research_mismatch_reported(self):
        conn = _StubConnection([["OK:RESEARCHING|TECH_ARCHERY"], ["MISMATCH:12~=3"]])
        result = asyncio.run(GameState(conn).set_research("TECH_ARCHERY"))  # type: ignore[arg-type]
        assert "SILENT_FAILURE" in result
        assert "get_tech_civics" in result

    def test_civic_mismatch_reported(self):
        conn = _StubConnection([["OK:PROGRESSING|CIVIC_FOREIGN_TRADE"], ["MISMATCH:4~=2"]])
        result = asyncio.run(GameState(conn).set_civic("CIVIC_FOREIGN_TRADE"))  # type: ignore[arg-type]
        assert "SILENT_FAILURE" in result
