"""set_city_production silent-failure detection.

CityManager.RequestOperation is fire-and-forget; even when CanStartOperation
returned true it can silently no-op if the queue is in a degenerate state.
The OK-path verification round-trips to confirm.
"""

import asyncio

from civ_mcp.game_state import GameState


class _StubConnection:
    """Answers execute() from a queue of responses, in call order."""

    def __init__(self, responses):
        self._responses = list(responses)

    async def execute(self, lua, timeout=None):
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _gs(responses) -> GameState:
    return GameState(_StubConnection(responses))  # type: ignore[arg-type]


class TestSetCityProductionVerification:
    def test_ok_path_verified(self):
        gs = _gs([["OK:PRODUCING|BUILDING_MONUMENT|6 turns"], ["CONFIRMED|6 turns"]])
        result = asyncio.run(gs.set_city_production(65536, "BUILDING", "BUILDING_MONUMENT"))
        assert result == "PRODUCING|BUILDING_MONUMENT|6 turns"

    def test_ok_path_silent_failure(self):
        gs = _gs([["OK:PRODUCING|UNIT_TRADER|1 turns"], ["NOT_SET|current=nil|expected=UNIT_TRADER"]])
        result = asyncio.run(gs.set_city_production(262145, "UNIT", "UNIT_TRADER"))
        assert "SILENT_FAILURE" in result
        assert "UNIT_TRADER" in result
        assert "purchase_item" in result

    def test_cannot_produce_bypasses_verify(self):
        gs = _gs(
            [
                [
                    "ERR:CANNOT_PRODUCE|BUILDING_UNIVERSITY cannot be produced "
                    "(requires DISTRICT_CAMPUS district)"
                ]
            ]
        )
        result = asyncio.run(gs.set_city_production(65536, "BUILDING", "BUILDING_UNIVERSITY"))
        assert "CANNOT_PRODUCE" in result

    def test_verify_failure_falls_through(self):
        """If the verify read itself throws, return the original OK optimistically."""
        gs = _gs([["OK:PRODUCING|UNIT_WARRIOR|2 turns"], RuntimeError("connection dropped")])
        result = asyncio.run(gs.set_city_production(65536, "UNIT", "UNIT_WARRIOR"))
        assert result == "PRODUCING|UNIT_WARRIOR|2 turns"
