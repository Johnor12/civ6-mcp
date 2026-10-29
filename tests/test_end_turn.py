"""Ending a stand-in turn: seat checks, preflight refusals, the happy path."""

from __future__ import annotations

import asyncio

from civ_mcp import end_turn
from civ_mcp.game_state import GameState


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _hs(leader="LEADER_POUNDMAKER", name="Ray", slot=2, turn=63, active=True, controls=False):
    b = lambda v: "true" if v else "false"  # noqa: E731
    return f"HS|{turn}|{slot}|{leader}|{name}|{b(active)}|true|true|{b(controls)}|{b(controls)}|false|false"


class _Conn:
    """Scripted bridge: answers by substring of the Lua, in registration order."""

    def __init__(self):
        self.replies: list[tuple[str, object]] = []
        self.sent: list[str] = []

    def on(self, key: str, lines):
        self.replies.append((key, lines))

    async def execute_in_context(self, context, code, timeout=None):
        return await self.execute(code, timeout)

    async def execute(self, code, timeout=None):
        self.sent.append(code)
        for key, lines in self.replies:
            if key in code:
                return lines() if callable(lines) else lines
        return []


def _quiet_turn(conn: _Conn, idle: str = "NONE") -> None:
    """No diplomacy, no deals, no popups, no blockers."""
    conn.on("FindOpenSessionID(me, i)", ["NONE"])
    conn.on("GetPendingDeal", ["NONE"])  # pending deals query
    conn.on("DISMISSED|", [])  # dismiss_popup
    conn.on("GetFirstEndTurnBlocking", ["BLOCK|NONE"])
    conn.on("EndTurnBlocking", ["NONE"])  # blocking notifications query
    conn.on("IsReadyToMove", [idle])


def test_refuses_when_another_player_is_seated():
    conn = _Conn()
    conn.on("HS|", [_hs(leader="LEADER_ALEXANDER")])
    res = _run(end_turn.execute_hotseat_end_turn(GameState(conn), "LEADER_POUNDMAKER"))
    assert not res.ended and "LEADER_ALEXANDER" in res.text and "Nothing was done" in res.text
    assert not any("ACTION_ENDTURN" in c for c in conn.sent)


def test_refuses_while_units_still_have_moves():
    conn = _Conn()
    conn.on("HS|", [_hs()])
    _quiet_turn(conn, idle="IDLE|UNIT_WARRIOR@10,24 (idx:3)")
    res = _run(end_turn.execute_hotseat_end_turn(GameState(conn), "LEADER_POUNDMAKER"))
    assert not res.ended
    assert "still have moves" in res.text and "UNIT_WARRIOR@10,24" in res.text
    assert not any("ACTION_ENDTURN" in c for c in conn.sent)


def test_refuses_on_open_diplomacy():
    conn = _Conn()
    conn.on("HS|", [_hs()])
    conn.on("FindOpenSessionID(me, i)", ["SESSION|1|3|Greece|Gorgo|Hello|||0"])
    res = _run(end_turn.execute_hotseat_end_turn(GameState(conn), "LEADER_POUNDMAKER"))
    assert not res.ended and "diplomacy encounter pending" in res.text and "Greece" in res.text


def test_ends_turn_waits_for_next_player_and_saves():
    conn = _Conn()
    calls = {"n": 0}

    def status():
        calls["n"] += 1
        if calls["n"] <= 2:
            return [_hs()]  # the seat check, then one poll still on Poundmaker
        return [_hs(leader="LEADER_BARBAROSSA", name="Aidan", slot=3, controls=True)]

    conn.on("HS|", status)
    _quiet_turn(conn)
    conn.on("ACTION_ENDTURN", ["SENT|2"])
    conn.on("NAME|", ["NAME|FREDERICK BARBAROSSA 63 150 AD"])
    conn.on("Network.SaveGame", ["SAVED|3"])
    res = _run(end_turn.execute_hotseat_end_turn(GameState(conn), "LEADER_POUNDMAKER"))
    assert res.ended and res.ended_turn == 63
    assert res.next_leader == "LEADER_BARBAROSSA" and res.next_name == "Aidan"
    assert res.save_name == "FREDERICK BARBAROSSA 63 150 AD MCP"
    assert res.text.startswith("TURN 63 ENDED for Poundmaker")
    assert "Barbarossa" in res.text and "locked" in res.text
    # The preflight ran before ACTION_ENDTURN, and the save came after it.
    order = [i for i, c in enumerate(conn.sent) if "IsReadyToMove" in c or "ACTION_ENDTURN" in c or "Network.SaveGame" in c]
    assert order == sorted(order) and len(order) == 3


def test_save_failure_still_counts_as_ended():
    conn = _Conn()
    conn.on("HS|", lambda: [_hs(leader="LEADER_BARBAROSSA", slot=3, controls=True)] if any("ACTION_ENDTURN" in c for c in conn.sent) else [_hs()])
    _quiet_turn(conn)
    conn.on("ACTION_ENDTURN", ["SENT|2"])
    conn.on("NAME|", ["NAME|X 63 Y"])
    conn.on("Network.SaveGame", ["nope"])
    res = _run(end_turn.execute_hotseat_end_turn(GameState(conn), "LEADER_POUNDMAKER"))
    assert res.ended and res.save_name == ""
    assert "save FAILED" in res.text and "Save Game button" in res.text


def test_interrupted_end_turn_retakes_the_seat_and_reports():
    """The engine handed the turn back (automated scout found a natural wonder)."""
    conn = _Conn()
    calls = {"n": 0}

    def status():
        calls["n"] += 1
        if calls["n"] <= 2:  # seat check, pre-check
            return [_hs()]
        if calls["n"] == 3:  # turn ended, please wait
            return [_hs(active=False)]
        return [_hs(controls=True)]  # Start Turn again for Poundmaker

    conn.on("HS|", status)
    _quiet_turn(conn)
    conn.on("ACTION_ENDTURN", ["SENT|2"])
    conn.on("ACCEPTED", ["ACCEPTED"])
    conn.on("NOTIF|", ["NOTIF|NOTIFICATION_DISCOVER_NATURAL_WONDER|Natural Wonder Discovered|63|29,6"])
    res = _run(end_turn.execute_hotseat_end_turn(GameState(conn), "LEADER_POUNDMAKER"))
    assert not res.ended
    assert res.text.startswith("Turn NOT ended") and "interrupted" in res.text
    assert "call end_turn again" in res.text
    assert "Natural Wonder Discovered" in res.text
    # The screen was dismissed again, after End Turn, and nothing was saved.
    codes = conn.sent
    end = next(i for i, c in enumerate(codes) if "ACTION_ENDTURN" in c)
    accept = [i for i, c in enumerate(codes) if "PlayerChange_Close" in c]
    assert accept and accept[-1] > end
    assert not any("Network.SaveGame" in c for c in codes)
