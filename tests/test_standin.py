"""Stand-in marker file and guard."""

from __future__ import annotations

import asyncio

from civ_mcp import standin


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestMarker:
    def test_missing_marker(self, tmp_path):
        assert standin.load_config(tmp_path / "standin.json") is None
        assert standin.active_config(tmp_path / "standin.json") is None

    def test_round_trip_ignores_unknown_keys(self, tmp_path):
        path = tmp_path / "standin.json"
        standin.save_config(
            standin.StandinConfig(active=True, leader="LEADER_POUNDMAKER", note="no wars"),
            path,
        )
        # Old markers carried a "mode" key; it must not break loading.
        path.write_text(path.read_text().replace("{", '{"mode": "single",', 1))
        loaded = standin.active_config(path)
        assert loaded is not None
        assert loaded.leader == "LEADER_POUNDMAKER" and loaded.note == "no wars"
        assert loaded.leader_display == "Poundmaker"

    def test_inactive_marker(self, tmp_path):
        path = tmp_path / "standin.json"
        standin.save_config(standin.StandinConfig(active=False, leader="X"), path)
        assert standin.load_config(path) is not None
        assert standin.active_config(path) is None

    def test_status_text(self):
        cfg = standin.StandinConfig(
            active=True, leader="LEADER_POUNDMAKER", player_name="Ray", note="finish the campus"
        )
        text = standin.status_text(cfg)
        assert "Poundmaker" in text and "Ray" in text and "finish the campus" in text
        assert "ends the turn for real" in text
        cfg.finished_turn = 63
        cfg.finished_save = "BARBAROSSA 63 150 AD MCP"
        cfg.next_player = "Barbarossa ('Aidan')"
        text = standin.status_text(cfg)
        assert "already ended" in text and "BARBAROSSA 63 150 AD MCP" in text
        assert "Aidan" in text and "locked" in text


class _FakeConn:
    def __init__(self, line: str):
        self.line = line
        self.calls = 0

    async def execute(self, lua, timeout=None):
        self.calls += 1
        return [self.line]


# LEADER|<slot>|<leader>|<turn active>|<hotseat>
_HS_POUNDMAKER = "LEADER|2|LEADER_POUNDMAKER|true|true"
_HS_ALEXANDER = "LEADER|0|LEADER_ALEXANDER|true|true"
_SP_POUNDMAKER = "LEADER|2|LEADER_POUNDMAKER|true|false"


class TestGuard:
    def test_passes_on_own_turn_and_caches(self):
        guard = standin.StandinGuard()
        cfg = standin.StandinConfig(active=True, leader="LEADER_POUNDMAKER")
        conn = _FakeConn(_HS_POUNDMAKER)
        assert _run(guard.check(conn, cfg)) is None
        assert _run(guard.check(conn, cfg)) is None
        assert conn.calls == 1  # second call served from the cache
        guard.invalidate()
        assert _run(guard.check(conn, cfg)) is None
        assert conn.calls == 2

    def test_no_marker_locks_everything_without_asking_the_game(self):
        guard = standin.StandinGuard()
        conn = _FakeConn(_HS_POUNDMAKER)
        msg = _run(guard.check(conn, None))
        assert msg and "begin_standin_turn" in msg
        assert conn.calls == 0

    def test_refuses_next_player_after_turn_ended(self):
        guard = standin.StandinGuard()
        cfg = standin.StandinConfig(
            active=True,
            leader="LEADER_POUNDMAKER",
            finished_turn=63,
            finished_save="BARBAROSSA 63 150 AD MCP",
        )
        conn = _FakeConn(_HS_ALEXANDER)
        msg = _run(guard.check(conn, cfg))
        assert msg and "LEADER_ALEXANDER" in msg and "Refusing" in msg
        assert "already ended" in msg and "begin_standin_turn" in msg
        assert _run(guard.check(conn, cfg)) is not None
        assert conn.calls == 2  # failures are never cached

    def test_refuses_outside_hotseat(self):
        guard = standin.StandinGuard()
        cfg = standin.StandinConfig(active=True, leader="LEADER_POUNDMAKER")
        msg = _run(guard.check(_FakeConn(_SP_POUNDMAKER), cfg))
        assert msg and "not in a hotseat session" in msg

    def test_refuses_when_turn_not_active(self):
        guard = standin.StandinGuard()
        cfg = standin.StandinConfig(active=True, leader="LEADER_POUNDMAKER")
        msg = _run(guard.check(_FakeConn("LEADER|2|LEADER_POUNDMAKER|false|true"), cfg))
        assert msg and "not active" in msg

    def test_cache_is_per_leader(self):
        guard = standin.StandinGuard()
        conn = _FakeConn(_HS_POUNDMAKER)
        assert _run(guard.check(conn, standin.StandinConfig(active=True, leader="LEADER_POUNDMAKER"))) is None
        assert _run(guard.check(conn, standin.StandinConfig(active=True, leader="LEADER_ALEXANDER"))) is not None
        assert conn.calls == 2


def test_pre_turn_save_round_trip(tmp_path):
    path = tmp_path / "standin.json"
    standin.save_config(
        standin.StandinConfig(active=True, leader="LEADER_X", pre_turn_save="X 5 PRE MCP"),
        path,
    )
    assert standin.active_config(path).pre_turn_save == "X 5 PRE MCP"
