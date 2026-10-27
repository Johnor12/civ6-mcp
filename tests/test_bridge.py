"""Tests for the file-based MCP Bridge transport (no game needed)."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from civ_mcp import bridge
from civ_mcp.bridge import LuaError


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_long_bracket_escapes_closing_brackets(self):
        code = 'print("a]]b]=]c")'
        wrapped = bridge.BridgeConnection._long_bracket(code)
        # Level-2 brackets: neither ]] nor ]=] inside the code can close them.
        assert wrapped.startswith("[==[\n") and wrapped.endswith("]==]")
        assert "]==]" not in code

    def test_line_regex(self):
        m = bridge._LINE_RE.match("MCPBridge_InGame: MCPB|42|O|hello|world")
        assert m and m.group("ctx") == "MCPBridge_InGame"
        assert m.group("seq") == "42" and m.group("kind") == "O"
        assert m.group("text") == "hello|world"
        m = bridge._LINE_RE.match("Test: MCPB|7|X")
        assert m and m.group("kind") == "X" and m.group("text") is None
        assert bridge._LINE_RE.match("InGame: CLEAR") is None
        assert bridge._LINE_RE.match("MCPBridge_InGame: MCPB|x|O|bad") is None

    def test_log_tail_survives_truncation(self, tmp_path):
        log = tmp_path / "Lua.log"
        log.write_bytes(b"old line\n")
        tail = bridge._LogTail(log)
        tail.seek_end()
        assert tail.read_lines() == []
        with open(log, "ab") as f:
            f.write(b"one\r\ntwo\npart")
        assert tail.read_lines() == ["one", "two"]
        with open(log, "ab") as f:
            f.write(b"ial\n")
        assert tail.read_lines() == ["partial"]
        # The game truncates the log on launch: start over from the top.
        log.write_bytes(b"fresh\n")
        assert tail.read_lines() == ["fresh"]

    def test_mod_paths(self, tmp_path):
        assert bridge.installed_mod_dir(tmp_path) == tmp_path / "MCPBridge"
        assert not bridge.is_mod_installed(tmp_path)
        out = bridge.install_mod(tmp_path)
        assert out == tmp_path / "MCPBridge"
        assert bridge.is_mod_installed(tmp_path)
        assert (out / "UI" / "Test.lua").exists()
        # A second install never clobbers the live command file.
        cmd = out / "UI" / bridge.CMD_FILE_NAME
        cmd.write_text("MCP_CMD = { seq = 1, code = 'x' }\n")
        bridge.install_mod(tmp_path)
        assert "seq = 1" in cmd.read_text()


# ---------------------------------------------------------------------------
# Round trips against a fake mod that answers into a fake Lua.log
# ---------------------------------------------------------------------------


class _FakeMod:
    """Watches the command file like the mod does and writes replies to the log."""

    def __init__(
        self, cmd_path: Path, log_path: Path, context: str = "MCPBridge_InGame"
    ):
        self.cmd_path = cmd_path
        self.log_path = log_path
        self.context = context
        self.last_seq = -1
        self.replies: dict[
            str, list[str]
        ] = {}  # code substring -> log lines (without seq)
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop = True
        self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop:
            try:
                text = self.cmd_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            seq = None
            for line in text.splitlines():
                if line.startswith("MCP_CMD = { seq = "):
                    seq = int(line.split("seq = ")[1].split(",")[0])
            if seq is not None and seq > self.last_seq:
                self.last_seq = seq
                out = []
                for key, lines in self.replies.items():
                    if key in text:
                        out.extend(lines)
                with open(self.log_path, "ab") as f:
                    for l in out:
                        f.write(f"{self.context}: MCPB|{seq}|{l}\n".encode())
                    f.write(f"{self.context}: MCPB|{seq}|X\n".encode())
            time.sleep(0.01)


@pytest.fixture
def fake_game(tmp_path):
    mods = tmp_path / "Mods"
    bridge.install_mod(mods)
    log = tmp_path / "Lua.log"
    log.write_bytes(b"DebugHotloadCache: GameDebug initialized!\n")
    conn = bridge.BridgeConnection(mods_dir=mods, lua_log=log)
    conn.poll_interval = 0.005
    mod = _FakeMod(conn.cmd_path, log)
    mod.replies["PONG"] = ["O|PONG"]
    mod.start()
    yield conn, mod
    mod.stop()


class TestRoundTrip:
    def test_connect_and_execute(self, fake_game):
        conn, mod = fake_game
        mod.replies["turn"] = ["O|turn=63", "O|---END---"]
        _run(conn.connect())
        assert conn.is_connected
        assert _run(conn.execute('print("turn")')) == ["turn=63"]
        assert conn.last_context == "MCPBridge_InGame"
        assert _run(conn.where()) == "ingame"
        # The command file is cleared after every call.
        assert "MCP_CMD = nil" in conn.cmd_path.read_text()

    def test_partial_lines_are_joined(self, fake_game):
        conn, mod = fake_game
        mod.replies["big"] = ["P|aaa", "P|bbb", "O|ccc", "O|second"]
        _run(conn.connect())
        assert _run(conn.execute("big")) == ["aaabbbccc", "second"]

    def test_lua_errors_raise(self, fake_game):
        conn, mod = fake_game
        mod.replies["boom"] = ["E|runtime: boom"]
        mod.replies["errline"] = ["O|ERR:UNIT_NOT_FOUND"]
        _run(conn.connect())
        with pytest.raises(LuaError, match="boom"):
            _run(conn.execute("boom"))
        with pytest.raises(LuaError, match="UNIT_NOT_FOUND"):
            _run(conn.execute("errline"))

    def test_silence_is_a_connection_error(self, fake_game):
        conn, mod = fake_game
        _run(conn.connect())
        mod.stop()
        with pytest.raises(ConnectionError):
            _run(conn.execute("nobody home", timeout=0.3))
        assert not conn.is_connected

    def test_a_newer_connection_does_not_shut_out_an_older_one(self, fake_game):
        # The mod runs only a sequence number above the last one it ran,
        # whoever sent it. Another process (a second MCP server, the Slack
        # bot's turn poller) must not make this connection's commands stale.
        conn, mod = fake_game
        mod.replies["hello"] = ["O|hi"]
        _run(conn.connect())
        other = bridge.BridgeConnection(mods_dir=conn.cmd_path.parents[2], lua_log=conn.lua_log)
        other.poll_interval = 0.005
        _run(other.connect())
        assert _run(other.execute('print("hello")')) == ["hi"]
        assert _run(conn.execute('print("hello")', timeout=2.0)) == ["hi"]

    def test_one_command_at_a_time_across_connections(self, fake_game):
        conn, mod = fake_game
        mod.replies["hello"] = ["O|hi"]
        _run(conn.connect())
        other = bridge.BridgeConnection(mods_dir=conn.cmd_path.parents[2], lua_log=conn.lua_log)
        other.poll_interval = 0.005
        other.lock_wait = 0.2
        other._connected = True

        async def while_held():
            async with conn._exclusive():
                with pytest.raises(ConnectionError, match="stayed locked"):
                    await other.execute('print("hello")')

        _run(while_held())
        assert _run(other.execute('print("hello")')) == ["hi"]

    def test_overwritten_command_is_resent(self, tmp_path):
        mods = tmp_path / "Mods"
        bridge.install_mod(mods)
        log = tmp_path / "Lua.log"
        log.write_bytes(b"")
        conn = bridge.BridgeConnection(mods_dir=mods, lua_log=log)
        conn.poll_interval = 0.005
        conn.resend_after = 0.1
        conn._connected = True
        conn._tail.seek_end()
        mod = _FakeMod(conn.cmd_path, log)
        mod.replies["hello"] = ["O|hi"]
        # A stale writer clears the file before the mod ever reads it; the
        # mod only starts afterwards and must still see the command.
        threading.Timer(0.03, lambda: conn.cmd_path.write_text("MCP_CMD = nil\n")).start()
        threading.Timer(0.06, mod.start).start()
        try:
            assert _run(conn.execute('print("hello")', timeout=3.0)) == ["hi"]
        finally:
            mod.stop()

    def test_missing_mod_is_a_connection_error(self, tmp_path):
        conn = bridge.BridgeConnection(
            mods_dir=tmp_path / "nope", lua_log=tmp_path / "Lua.log"
        )
        with pytest.raises(ConnectionError, match="not installed"):
            _run(conn.connect())
