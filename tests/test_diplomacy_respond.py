"""Leader dialogues: answer like the choice button, wait for the reply, leave cleanly.

A forced DiplomacyManager.CloseSession while the leader screen was still
presenting the reply froze the game at PLEASE WAIT, so the response path
waits for the engine to either close the session or deliver the next
statement before it does anything else.
"""

from __future__ import annotations

import asyncio

from civ_mcp.game_state import GameState


def _session_line(pid: int, text: str) -> str:
    return f"SESSION|1|{pid}|Greece|Gorgo|{text}|||0"


class ScriptedConn:
    """A diplomacy session with one leader whose reply may arrive after a few polls."""

    def __init__(self, reply_after_polls: int, reply_text: str | None):
        self.open = True
        self.polls = 0
        self.reply_after_polls = reply_after_polls
        self.reply_text = reply_text
        self.text = "I am Gorgo. Who are you?"
        self.calls: list[str] = []

    async def execute(self, lua: str, timeout: float | None = None) -> list[str]:
        if 'if "EXIT" == "EXIT"' in lua:  # build_diplomacy_respond(EXIT)
            self.calls.append("close")
            self.open = False
            return ["OK:SESSION_CLOSED"]
        if "DiplomacyManager.AddResponse" in lua:
            self.calls.append("add_response")
            return ["OK:RESPONSE_SENT|POSITIVE"]
        if 'print("SESSION_OPEN|"' in lua:
            self.polls += 1
            if self.open and self.reply_text and self.polls >= self.reply_after_polls:
                self.text = self.reply_text
            return ["SESSION_OPEN|1"] if self.open else ["SESSION_CLOSED"]
        if "FindOpenSessionID(me, i)" in lua:  # session listing
            return [_session_line(3, self.text)] if self.open else ["NONE"]
        return []


def _gs(conn: ScriptedConn) -> GameState:
    gs = GameState(conn)  # type: ignore[arg-type]
    gs.RESPONSE_SETTLE_S = 0.001
    return gs


def test_waits_for_the_leaders_reply_instead_of_forcing_a_close():
    conn = ScriptedConn(reply_after_polls=3, reply_text="We have a city nearby. Visit us?")
    result = asyncio.run(_gs(conn).diplomacy_respond(3, "POSITIVE"))
    assert "SESSION_CONTINUES" in result
    assert "We have a city nearby" in result
    assert conn.calls == ["add_response"]  # no close of any kind
    assert conn.open


def test_goodbye_phase_closes_the_session():
    conn = ScriptedConn(reply_after_polls=99, reply_text=None)  # nothing more to say
    result = asyncio.run(_gs(conn).diplomacy_respond(3, "POSITIVE"))
    assert "SESSION_CLOSED" in result
    assert conn.calls == ["add_response", "close"]
    assert not conn.open


def test_engine_closing_the_session_is_reported():
    conn = ScriptedConn(reply_after_polls=99, reply_text=None)

    async def run():
        gs = _gs(conn)
        orig = conn.execute

        async def execute(lua, timeout=None):
            out = await orig(lua, timeout)
            if "DiplomacyManager.AddResponse" in lua:
                conn.open = False  # the engine closes it right after our answer
            return out

        conn.execute = execute  # type: ignore[method-assign]
        return await gs.diplomacy_respond(3, "NEGATIVE")

    assert asyncio.run(run()) == "OK:RESPONDED|NEGATIVE|SESSION_CLOSED"
    assert conn.calls == ["add_response"]


def test_exit_closes_an_open_session_and_refuses_a_missing_one():
    conn = ScriptedConn(reply_after_polls=99, reply_text=None)
    assert asyncio.run(_gs(conn).diplomacy_respond(3, "EXIT")).startswith(
        "OK:RESPONDED|EXIT|SESSION_CLOSED"
    )
    assert conn.calls == ["close"] and not conn.open
    assert asyncio.run(_gs(conn).diplomacy_respond(3, "EXIT")) == "Error: ERR:NO_SESSION"
