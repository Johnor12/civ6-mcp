"""Tests for the hotseat flow helpers (parsing and control logic, no game)."""

from __future__ import annotations

import asyncio

import pytest

from civ_mcp import hotseat
from civ_mcp.bridge import CONTEXT_FRONTEND, CONTEXT_INGAME


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeBridge:
    """Answers execute_in_context from a script of (substring -> lines)."""

    def __init__(self, where="ingame"):
        self._where = where
        self.replies: list[tuple[str, list[str]]] = []
        self.sent: list[tuple[str | None, str]] = []

    async def where(self, timeout=2.0):
        return self._where

    async def execute_in_context(self, context, code, timeout=None):
        self.sent.append((context, code))
        for key, lines in self.replies:
            if key in code:
                return lines() if callable(lines) else lines
        return []

    async def execute(self, code, timeout=None):
        return await self.execute_in_context(CONTEXT_INGAME, code, timeout)


def _hs_line(
    turn=63,
    slot=2,
    leader="LEADER_POUNDMAKER",
    name="Ray",
    active=True,
    hotseat=True,
    anymp=True,
    pc_visible=True,
    controls=True,
    paused=True,
    pw=False,
):
    b = lambda v: "true" if v else "false"  # noqa: E731
    return (
        f"HS|{turn}|{slot}|{leader}|{name}|{b(active)}|{b(hotseat)}|{b(anymp)}|"
        f"{b(pc_visible)}|{b(controls)}|{b(paused)}|{b(pw)}"
    )


class TestParsing:
    def test_normalize_leader(self):
        assert hotseat.normalize_leader("Poundmaker") == "LEADER_POUNDMAKER"
        assert hotseat.normalize_leader("LEADER_POUNDMAKER") == "LEADER_POUNDMAKER"
        assert hotseat.normalize_leader(" mansa musa ") == "LEADER_MANSA_MUSA"
        assert hotseat.normalize_leader("basil") == "LEADER_BASIL"

    def test_ingame_status_parse(self):
        conn = _FakeBridge()
        conn.replies.append(("HS|", [_hs_line()]))
        st = _run(hotseat.ingame_status(conn))
        assert st.screen == "ingame" and st.turn == 63 and st.local_player == 2
        assert st.leader == "LEADER_POUNDMAKER" and st.player_name == "Ray"
        assert st.turn_active and st.is_hotseat and st.turn_controls_visible
        assert st.paused and not st.has_password
        assert st.leader_display == "Poundmaker"
        text = st.describe()
        assert "hotseat" in text and "Ray" in text and "Save Game / Start Turn" in text

    def test_ingame_status_no_answer(self):
        conn = _FakeBridge()
        st = _run(hotseat.ingame_status(conn))
        assert st.screen == "none"
        assert "did not answer" in st.describe()

    def test_frontend_status(self):
        conn = _FakeBridge(where="frontend")
        conn.replies.append(("FE|", ["FE|true|true"]))
        st = _run(hotseat.status(conn))
        assert st.screen == "frontend" and st.in_session and st.is_hotseat
        assert "staging room" in st.describe()

    def test_lua_str_quotes(self):
        assert hotseat._lua_str('a"b\\c') == '"a\\"b\\\\c"'
        assert "POUNDMAKER 63 150 AD" in hotseat._load_lua("POUNDMAKER 63 150 AD")


class TestWhoseTurn:
    def test_turn_active_or_turn_change_screen(self):
        conn = _FakeBridge()
        conn.replies.append(("HS|", [_hs_line(controls=False)]))
        assert _run(hotseat.whose_turn(conn)).player_name == "Ray"
        conn.replies[0] = ("HS|", [_hs_line(active=False, controls=True)])
        assert _run(hotseat.whose_turn(conn)).leader == "LEADER_POUNDMAKER"

    def test_no_turn_up(self):
        conn = _FakeBridge()
        conn.replies.append(("HS|", [_hs_line(active=False, controls=False)]))  # between players
        assert _run(hotseat.whose_turn(conn)) is None
        conn.replies[0] = ("HS|", [_hs_line(hotseat=False)])
        assert _run(hotseat.whose_turn(conn)) is None
        assert _run(hotseat.whose_turn(_FakeBridge(where="frontend"))) is None
        assert _run(hotseat.whose_turn(_FakeBridge(where=None))) is None

    def test_no_game_does_not_raise(self, monkeypatch):
        async def refuse(self):
            raise ConnectionError("Is Civ 6 running?")

        monkeypatch.setattr(hotseat.BridgeConnection, "connect", refuse)
        assert _run(hotseat.whose_turn()) is None


class TestAcceptTurn:
    def test_accept_turn_dismisses_and_reports(self):
        conn = _FakeBridge()
        conn.replies.append(("ACCEPTED", ["ACCEPTED"]))
        conn.replies.append(
            ("HS|", [_hs_line(pc_visible=False, controls=False, paused=False)])
        )
        st = _run(hotseat.accept_turn(conn))
        assert st.turn_active and not st.player_change_visible and not st.paused
        assert any("PlayerChange_Close" in code for _, code in conn.sent)


class TestEndTurn:
    def test_waits_for_next_player_screen(self):
        conn = _FakeBridge()
        calls = {"n": 0}

        def status_lines():
            calls["n"] += 1
            if calls["n"] < 3:
                return [_hs_line()]  # still Poundmaker
            return [_hs_line(slot=3, leader="LEADER_BARBAROSSA", name="Aidan")]

        conn.replies.append(("SENT|", ["SENT|2"]))
        conn.replies.append(("HS|", status_lines))
        st = _run(hotseat.end_turn_and_wait(conn, 2, 63, timeout=20))
        assert st.leader == "LEADER_BARBAROSSA" and st.player_name == "Aidan"
        assert any("ACTION_ENDTURN" in code for _, code in conn.sent)

    def test_turn_advance_counts_as_change(self):
        conn = _FakeBridge()
        conn.replies.append(("SENT|", ["SENT|2"]))
        conn.replies.append(("HS|", [_hs_line(turn=64)]))
        st = _run(hotseat.end_turn_and_wait(conn, 2, 63, timeout=20))
        assert st.turn == 64

    def test_reports_engine_blocker(self, monkeypatch):
        monkeypatch.setattr(hotseat, "BLOCKER_CHECK_AFTER", 1.0)
        conn = _FakeBridge()
        conn.replies.append(("SENT|", ["SENT|2"]))
        # Mid-turn: the turn-change screen is down and the seat never moves.
        conn.replies.append(("HS|", [_hs_line(pc_visible=False, controls=False, paused=False)]))
        conn.replies.append(("BLOCK|", ["BLOCK|ENDTURN_BLOCKING_TYPE_PRODUCTION"]))
        with pytest.raises(RuntimeError, match="PRODUCTION"):
            _run(hotseat.end_turn_and_wait(conn, 2, 63, timeout=6))

    def test_no_endturn_blocking_is_not_a_blocker(self, monkeypatch):
        """NO_ENDTURN_BLOCKING is the engine's 'nothing blocks' value: press
        End Turn again, then give up with a popup hint, never 'blocker NO_...'."""
        monkeypatch.setattr(hotseat, "BLOCKER_CHECK_AFTER", 1.0)
        monkeypatch.setattr(hotseat, "GIVE_UP_AFTER", 2.5)
        conn = _FakeBridge()
        conn.replies.append(("SENT|", ["SENT|2"]))
        conn.replies.append(("HS|", [_hs_line(pc_visible=False, controls=False, paused=False)]))
        conn.replies.append(("BLOCK|", ["BLOCK|NO_ENDTURN_BLOCKING"]))
        with pytest.raises(RuntimeError, match="reports no blocker") as ei:
            _run(hotseat.end_turn_and_wait(conn, 2, 63, timeout=8))
        assert "NO_ENDTURN_BLOCKING" not in str(ei.value)
        assert sum("ACTION_ENDTURN" in code for _, code in conn.sent) == 2

    def test_same_player_screen_returning_is_an_interruption(self):
        """An automated scout found a natural wonder while the turn ended:
        the engine handed the turn straight back and re-showed Start Turn."""
        conn = _FakeBridge()
        calls = {"n": 0}

        def status_lines():
            calls["n"] += 1
            if calls["n"] == 1:  # the pre-check: mid-turn, screen down
                return [_hs_line(pc_visible=False, controls=False, paused=False)]
            if calls["n"] == 2:  # turn ended, 'please wait'
                return [_hs_line(active=False, controls=False, paused=False)]
            if calls["n"] == 3:  # active again, controls not built yet
                return [_hs_line(controls=False, paused=False)]
            return [_hs_line()]  # Start Turn screen for the same player

        conn.replies.append(("SENT|", ["SENT|2"]))
        conn.replies.append(("HS|", status_lines))
        with pytest.raises(hotseat.TurnInterrupted) as ei:
            _run(hotseat.end_turn_and_wait(conn, 2, 63, timeout=20))
        assert ei.value.status.leader == "LEADER_POUNDMAKER"
        assert not any("BLOCK|" in code for _, code in conn.sent)

    def test_accepts_a_visible_screen_before_pressing_end_turn(self):
        conn = _FakeBridge()
        calls = {"n": 0}

        def status_lines():
            calls["n"] += 1
            if calls["n"] <= 2:  # pre-check and accept_turn's report: screen up
                return [_hs_line()]
            return [_hs_line(slot=3, leader="LEADER_BARBAROSSA", name="Aidan")]

        conn.replies.append(("SENT|", ["SENT|2"]))
        conn.replies.append(("ACCEPTED", ["ACCEPTED"]))
        conn.replies.append(("HS|", status_lines))
        st = _run(hotseat.end_turn_and_wait(conn, 2, 63, timeout=20))
        assert st.leader == "LEADER_BARBAROSSA"
        codes = [code for _, code in conn.sent]
        accept = next(i for i, c in enumerate(codes) if "PlayerChange_Close" in c)
        end = next(i for i, c in enumerate(codes) if "ACTION_ENDTURN" in c)
        assert accept < end

    def test_save_name_and_save(self):
        conn = _FakeBridge()
        conn.replies.append(("NAME|", ["NAME|BARBAROSSA 63 150 AD"]))
        conn.replies.append(("SAVED|", ["SAVED|3"]))
        assert _run(hotseat.default_save_name(conn)) == "BARBAROSSA 63 150 AD"
        assert _run(hotseat.suggest_save_name(conn)) == "BARBAROSSA 63 150 AD MCP"
        assert hotseat.tagged_save_name("", "MCP").startswith("HOTSEAT MCP ")
        assert _run(hotseat.save_hotseat(conn, "X 1 Y MCP")) == "X 1 Y MCP"
        code = conn.sent[-1][1]
        assert 'gf.Name = "X 1 Y MCP"' in code
        assert "GetGameConfigurationSaveType" in code  # lands in Saves/Hotseat

    def test_save_failure_raises(self):
        conn = _FakeBridge()
        with pytest.raises(RuntimeError):
            _run(hotseat.save_hotseat(conn, "X"))


class TestLoadFlow:
    def test_not_found_lists_saves(self):
        conn = _FakeBridge(where="frontend")
        conn.replies.append(("FE|", ["FE|false|false"]))
        conn.replies.append(("QUERY_SENT", ["QUERY_SENT"]))
        conn.replies.append(("LOAD|", ["LOAD|true|NOT_FOUND|A 1 B; C 2 D"]))
        with pytest.raises(RuntimeError, match="not found.*A 1 B"):
            _run(hotseat.load_hotseat_save(conn, "NOPE"))
        # The load went to the front-end context and set hotseat defaults first.
        ctxs = [c for c, _ in conn.sent]
        assert CONTEXT_FRONTEND in ctxs and CONTEXT_INGAME not in ctxs
        assert any(
            "SetToDefaults(GameModeTypes.HOTSEAT)" in code for _, code in conn.sent
        )

    def test_silent_game_is_connection_error(self):
        conn = _FakeBridge(where=None)
        with pytest.raises(ConnectionError):
            _run(hotseat.load_hotseat_save(conn, "X"))
