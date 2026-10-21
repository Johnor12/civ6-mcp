"""Tests for hotseat stand-in mode: marker file, save-header conversion, guard."""

from __future__ import annotations

import asyncio
import importlib.util
import struct
import sys
from pathlib import Path

import pytest

from civ_mcp import standin
from civ_mcp.connection import GameConnection, LuaError

# ---------------------------------------------------------------------------
# scripts/hotseat_standin.py is a script, not a package module — load it by path
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "hotseat_standin.py"
_spec = importlib.util.spec_from_file_location("hotseat_standin", _SCRIPT)
hs = importlib.util.module_from_spec(_spec)
sys.modules["hotseat_standin"] = hs
_spec.loader.exec_module(hs)


# ---------------------------------------------------------------------------
# Synthetic save header built from the record layouts the script reads
# ---------------------------------------------------------------------------


def _str_rec(marker: bytes, value: str) -> bytes:
    raw = value.encode("ascii") + b"\x00"
    ln = len(raw)
    return (
        marker
        + struct.pack("<I", 5)
        + bytes([ln & 0xFF, (ln >> 8) & 0xFF, (ln >> 16) & 0xFF])
        + bytes([0x21, 0x01, 0x00, 0x00, 0x00])
        + raw
    )


def _int_rec(marker: bytes, value: int) -> bytes:
    return marker + struct.pack("<I", 2) + bytes(8) + struct.pack("<I", value)


def _bool_rec(marker: bytes, value: bool) -> bytes:
    return (
        marker + struct.pack("<I", 1) + bytes(8) + bytes([1 if value else 0]) + bytes(4)
    )


def _actor(civ: str, leader: str, level: str, ai_human: int, cur: bool | None) -> bytes:
    rec = hs.START_ACTOR + bytes(4)
    rec += _str_rec(hs.ACTOR_NAME, civ)
    rec += _str_rec(hs.ACTOR_TYPE, level)
    rec += _int_rec(hs.ACTOR_AI_HUMAN, ai_human)
    rec += _str_rec(hs.LEADER_NAME, leader)
    if level == hs.FULL_CIV:
        rec += _bool_rec(hs.PLAYER_ALIVE, True)
        rec += _bool_rec(hs.IS_CURRENT_TURN, bool(cur))
    return rec


def _save(slots: list[tuple[str, str, str, int, bool | None]], turn: int = 63) -> bytes:
    """Header with a GAME_TURN record, the given actor slots, then a fake zlib body."""
    data = b"CIV6" + _int_rec(hs.GAME_TURN, turn)
    for civ, leader, level, aih, cur in slots:
        data += _actor(civ, leader, level, aih, cur)
    data += b"MOD_TITLE" + bytes(8) + b"\x78\x9c" + bytes(32)
    return data


HOTSEAT = _save(
    [
        (
            "CIVILIZATION_BABYLON",
            "LEADER_MINOR_CIV_BABYLON",
            "CIVILIZATION_LEVEL_CITY_STATE",
            5,
            None,
        ),
        ("CIVILIZATION_MACEDON", "LEADER_ALEXANDER", hs.FULL_CIV, hs.HUMAN, False),
        ("CIVILIZATION_CREE", "LEADER_POUNDMAKER", hs.FULL_CIV, hs.HUMAN, True),
        ("CIVILIZATION_BRAZIL", "LEADER_PEDRO", hs.FULL_CIV, hs.HUMAN, False),
        ("CIVILIZATION_BARBARIAN", "", "CIVILIZATION_LEVEL_TRIBE", 1, None),
    ]
)


class TestHeaderParsing:
    def test_reads_full_civ_slots_only(self):
        slots = hs.read_slots(HOTSEAT)
        assert [s.leader for s in slots] == [
            "LEADER_ALEXANDER",
            "LEADER_POUNDMAKER",
            "LEADER_PEDRO",
        ]
        assert all(s.is_human for s in slots)
        assert [s.current_turn for s in slots] == [False, True, False]
        assert hs.game_turn(HOTSEAT) == 63

    def test_to_single_keeps_one_human(self):
        out, changed = hs.to_single(HOTSEAT, "LEADER_POUNDMAKER")
        slots = {s.leader: s for s in hs.read_slots(out)}
        assert slots["LEADER_POUNDMAKER"].ai_human == hs.HUMAN
        assert slots["LEADER_ALEXANDER"].ai_human == hs.AI
        assert slots["LEADER_PEDRO"].ai_human == hs.AI
        assert len(changed) == 2
        # Only the two flag words differ — the rest of the file is untouched
        diff = [i for i, (a, b) in enumerate(zip(HOTSEAT, out)) if a != b]
        assert len(diff) == 2
        # City-state / barbarian slots untouched
        assert out.count(_int_rec(hs.ACTOR_AI_HUMAN, 5)) == 1

    def test_to_single_unknown_leader(self):
        with pytest.raises(SystemExit):
            hs.to_single(HOTSEAT, "LEADER_GANDHI")

    def test_round_trip_restores_flags(self):
        single, _ = hs.to_single(HOTSEAT, "LEADER_POUNDMAKER")
        # Simulate what the game does to a single-player save: clear the
        # current-turn flag of the human slot.
        buf = bytearray(single)
        pm = next(s for s in hs.read_slots(single) if s.leader == "LEADER_POUNDMAKER")
        buf[pm.current_turn_pos] = 0
        restored, changed = hs.to_hotseat(bytes(buf), HOTSEAT)
        assert restored == HOTSEAT
        assert any("current_turn False->True" in c for c in changed)

    def test_to_hotseat_rejects_foreign_save(self):
        other = _save(
            [("CIVILIZATION_INDIA", "LEADER_GANDHI", hs.FULL_CIV, hs.HUMAN, True)]
        )
        with pytest.raises(SystemExit):
            hs.to_hotseat(other, HOTSEAT)


# ---------------------------------------------------------------------------
# Marker file
# ---------------------------------------------------------------------------


class TestMarker:
    def test_missing_marker(self, tmp_path):
        assert standin.load_config(tmp_path / "standin.json") is None
        assert standin.active_config(tmp_path / "standin.json") is None

    def test_round_trip_and_unknown_keys(self, tmp_path):
        path = tmp_path / "standin.json"
        cfg = standin.StandinConfig(
            active=True, leader="LEADER_POUNDMAKER", hotseat_save="POUNDMAKER 63 150 AD"
        )
        standin.save_config(cfg, path)
        path.write_text(path.read_text().replace("{", '{"future_key": 1,', 1))
        loaded = standin.active_config(path)
        assert loaded is not None
        assert loaded.leader == "LEADER_POUNDMAKER"
        assert loaded.out_save == standin.DEFAULT_OUT_SAVE
        assert loaded.leader_display == "Poundmaker"

    def test_inactive_marker(self, tmp_path):
        path = tmp_path / "standin.json"
        standin.save_config(standin.StandinConfig(active=False, leader="X"), path)
        assert standin.load_config(path) is not None
        assert standin.active_config(path) is None

    def test_status_text_mentions_note_and_finish(self):
        cfg = standin.StandinConfig(
            active=True,
            leader="LEADER_POUNDMAKER",
            hotseat_save="POUNDMAKER 63 150 AD",
            note="keep building campuses",
            finished_turn=63,
            finished_at="2026-10-21T18:00:00+00:00",
        )
        text = standin.status_text(cfg)
        assert "Poundmaker" in text
        assert "keep building campuses" in text
        assert "already finished" in text
        assert "end_turn will NOT advance" in text


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, line: str):
        self.line = line
        self.calls = 0

    async def execute_read(self, lua, timeout=5.0):
        self.calls += 1
        return [self.line]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestGuard:
    def test_passes_and_caches(self):
        guard = standin.StandinGuard()
        cfg = standin.StandinConfig(active=True, leader="LEADER_POUNDMAKER")
        conn = _FakeConn("LEADER|2|LEADER_POUNDMAKER|false|true")
        assert _run(guard.check(conn, cfg)) is None
        assert _run(guard.check(conn, cfg)) is None
        assert conn.calls == 1  # second call served from the cache

    def test_refuses_other_player(self):
        guard = standin.StandinGuard()
        cfg = standin.StandinConfig(
            active=True, leader="LEADER_POUNDMAKER", single_save="X"
        )
        conn = _FakeConn("LEADER|0|LEADER_ALEXANDER|false|true")
        msg = _run(guard.check(conn, cfg))
        assert msg and "LEADER_ALEXANDER" in msg and "Refusing" in msg
        assert _run(guard.check(conn, cfg)) is not None
        assert conn.calls == 2  # failures are never cached

    def test_refuses_multiplayer_session(self):
        guard = standin.StandinGuard()
        cfg = standin.StandinConfig(active=True, leader="LEADER_POUNDMAKER")
        conn = _FakeConn("LEADER|2|LEADER_POUNDMAKER|true|true")
        msg = _run(guard.check(conn, cfg))
        assert msg and "multiplayer/hotseat" in msg


# ---------------------------------------------------------------------------
# Stale Lua state index after a save load / menu round trip
# ---------------------------------------------------------------------------


class TestStaleLuaState:
    def test_rediscovers_states_and_retries(self, monkeypatch):
        conn = GameConnection()
        conn.gamecore_index = 3
        conn.ingame_index = 7
        calls: list[int] = []

        async def fake_locked(state_index, lua, timeout):
            calls.append(state_index)
            if state_index == 7:
                raise LuaError("ERR:Invalid Lua State")
            return ["ok"]

        async def fake_reconnect():
            conn.gamecore_index = 11
            conn.ingame_index = 12

        async def fake_ensure():
            return None

        monkeypatch.setattr(conn, "_locked_execute", fake_locked)
        monkeypatch.setattr(conn, "reconnect", fake_reconnect)
        monkeypatch.setattr(conn, "ensure_connected", fake_ensure)

        assert _run(conn._execute_and_collect(7, "print(1)", 5.0)) == ["ok"]
        assert calls == [7, 12]

    def test_other_lua_errors_propagate(self, monkeypatch):
        conn = GameConnection()
        conn.ingame_index = 7

        async def fake_locked(state_index, lua, timeout):
            raise LuaError("ERR:Runtime Error: boom")

        async def fake_ensure():
            return None

        monkeypatch.setattr(conn, "_locked_execute", fake_locked)
        monkeypatch.setattr(conn, "ensure_connected", fake_ensure)
        with pytest.raises(LuaError):
            _run(conn._execute_and_collect(7, "print(1)", 5.0))
