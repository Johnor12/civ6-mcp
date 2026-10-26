"""place_map_pin: create in one call, name in a later one, verify it held.

A pin named in the call that creates it comes back nameless (seen live in
hotseat), so GameState.place_map_pin splits the work and reads the pin back.
"""

import asyncio

from civ_mcp.game_state import GameState


class _StubConnection:
    """Answers execute() from a queue of responses, in call order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    async def execute(self, lua, timeout=None):
        self.calls.append(lua)
        return self._responses.pop(0)


def _gs(responses) -> tuple[GameState, _StubConnection]:
    conn = _StubConnection(responses)
    gs = GameState(conn)  # type: ignore[arg-type]
    gs.PIN_SETTLE_S = 0
    return gs, conn


def test_new_pin_is_created_then_named_privately_and_verified():
    gs, conn = _gs([
        ["PIN_CREATED|7"],
        ["OK:PIN_SET|(46,9) Iron: mine later [ICON_IMPROVEMENT_MINE]"],
        ["PIN|46,9|Iron: mine later|ICON_IMPROVEMENT_MINE|you"],
    ])
    result = asyncio.run(gs.place_map_pin(46, 9, "Iron: mine later", "IMPROVEMENT_MINE"))
    assert result == "PIN_PLACED|(46,9) Iron: mine later [ICON_IMPROVEMENT_MINE]"
    assert "pin:SetVisibility(me)" in conn.calls[1]


def test_existing_pin_keeps_its_visibility():
    gs, conn = _gs([
        ["PIN_EXISTS|3"],
        ["OK:PIN_SET|(47,8) Hunter's House [ICON_MAP_PIN_CIRCLE]"],
        ["PIN|47,8|Hunter's House|ICON_MAP_PIN_CIRCLE|you"],
    ])
    result = asyncio.run(gs.place_map_pin(47, 8, "Hunter's House", "ICON_MAP_PIN_CIRCLE"))
    assert result.startswith("PIN_UPDATED|(47,8)")
    assert "SetVisibility" not in conn.calls[1]


def test_a_reset_name_is_retried_then_reported():
    reset = ["PIN|46,9||ICON_MAP_PIN_TRIANGLE|you"]
    ok = ["OK:PIN_SET|(46,9) x [ICON_MAP_PIN_CIRCLE]"]
    gs, _ = _gs([["PIN_CREATED|7"], ok, reset, ok, reset, ok, reset])
    result = asyncio.run(gs.place_map_pin(46, 9, "x", "ICON_MAP_PIN_CIRCLE"))
    assert result.startswith("Error: PIN_NOT_SAVED")


def test_off_map_stops_after_the_create_call():
    gs, conn = _gs([["ERR:OFF_MAP|(999,9) is not on the map"]])
    assert asyncio.run(gs.place_map_pin(999, 9, "x", "ICON_MAP_PIN_CIRCLE")).startswith("Error: OFF_MAP")
    assert len(conn.calls) == 1
