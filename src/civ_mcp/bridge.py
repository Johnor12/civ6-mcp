"""File-based Lua bridge to Civilization VI — a FireTuner replacement.

Civ 6 shuts its FireTuner listener down inside any multiplayer session,
including local hotseat. This transport needs no socket: a small UI mod
("MCP Bridge", see ``mods/MCPBridge``) re-includes a command file a few
times a second and runs whatever Lua it finds there, with ``print``
captured and written to the game's ``Lua.log``. This module writes the
command file and tails the log.

Wire format (one command at a time)::

    mcp_bridge_cmd.lua   MCP_CMD = { seq = <n>, context = "InGame", code = [==[ ... ]==] }
    Lua.log              MCPBridge_InGame: MCPB|<n>|O|<one print line>
                         MCPBridge_InGame: MCPB|<n>|E|<error text>
                         MCPBridge_InGame: MCPB|<n>|X

The mod runs in two UI contexts, ``InGame`` (a loaded game, any mode) and
``FrontEnd`` (main menu, staging room). Only the UI Lua state exists, so
``execute_read`` and ``execute_write`` both run in ``InGame``; the few
GameCore-only APIs are pcall-guarded in the query builders.

The public surface mirrors :class:`civ_mcp.connection.GameConnection` so the
rest of the server does not care which transport it talks to.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path

from civ_mcp.connection import LuaError
from civ_mcp.lua._helpers import SENTINEL

log = logging.getLogger(__name__)

MOD_ID = "7c1f2a3e-5b6d-4e8f-9a0b-c1d2e3f4a5b6"
MOD_DIR_NAME = "MCPBridge"
CMD_FILE_NAME = "mcp_bridge_cmd.lua"

CONTEXT_INGAME = "InGame"
CONTEXT_FRONTEND = "FrontEnd"
# Lua.log prefixes the mod's lines with its UI context name: the in-game
# context is the mod's own; the front-end one rides on the game's "Test"
# context (see mods/MCPBridge/UI/Test.lua).
INGAME_LOG_CONTEXT = "MCPBridge_InGame"
FRONTEND_LOG_CONTEXT = "Test"

# Lua.log lines look like "<context>: MCPB|<seq>|<kind>|<text>" (the kind X
# carries no text). The context prefix is the mod's UI context name.
_LINE_RE = re.compile(
    r"^(?P<ctx>[^:]*): MCPB\|(?P<seq>\d+)\|(?P<kind>[OPEXS])(?:\|(?P<text>.*))?$"
)


def _documents_dir() -> Path:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        # CSIDL_PERSONAL = 5 (My Documents), SHGFP_TYPE_CURRENT = 0
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
            return Path(buf.value)
        return Path.home() / "Documents"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".local" / "share" / "aspyr-media"


def default_mods_dir() -> Path:
    """The folder Civ 6 scans for user mods on this platform."""
    override = os.environ.get("CIV_MCP_MODS_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return _documents_dir() / "My Games" / "Sid Meier's Civilization VI" / "Mods"
    if sys.platform == "darwin":
        return _documents_dir() / "Sid Meier's Civilization VI" / "Mods"
    return _documents_dir() / "Sid Meier's Civilization VI" / "Mods"


def default_lua_log() -> Path:
    override = os.environ.get("CIV_MCP_LUA_LOG")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(
            os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        )
        return (
            base / "Firaxis Games" / "Sid Meier's Civilization VI" / "Logs" / "Lua.log"
        )
    if sys.platform == "darwin":
        return (
            _documents_dir()
            / "Sid Meier's Civilization VI"
            / "Firaxis Games"
            / "Sid Meier's Civilization VI"
            / "Logs"
            / "Lua.log"
        )
    return _documents_dir() / "Sid Meier's Civilization VI" / "Logs" / "Lua.log"


def bundled_mod_dir() -> Path:
    """The copy of the mod shipped in this repository."""
    return Path(__file__).resolve().parent.parent.parent / "mods" / MOD_DIR_NAME


def installed_mod_dir(mods_dir: Path | None = None) -> Path:
    return (mods_dir or default_mods_dir()) / MOD_DIR_NAME


def is_mod_installed(mods_dir: Path | None = None) -> bool:
    d = installed_mod_dir(mods_dir)
    return (d / f"{MOD_DIR_NAME}.modinfo").exists() and (
        d / "UI" / CMD_FILE_NAME
    ).exists()


def install_mod(mods_dir: Path | None = None) -> Path:
    """Copy (or refresh) the bridge mod into the game's Mods folder."""
    import shutil

    src = bundled_mod_dir()
    if not (src / f"{MOD_DIR_NAME}.modinfo").exists():
        raise FileNotFoundError(f"bundled mod not found at {src}")
    dst = installed_mod_dir(mods_dir)
    dst.mkdir(parents=True, exist_ok=True)
    for path in src.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # Never clobber a command the running game may still be reading.
        if rel.as_posix() == f"UI/{CMD_FILE_NAME}" and target.exists():
            continue
        shutil.copyfile(path, target)
    return dst


class _LogTail:
    """Incremental reader for Lua.log that survives truncation on game restart."""

    def __init__(self, path: Path):
        self.path = path
        self._pos = 0
        self._buf = b""

    def seek_end(self) -> None:
        try:
            self._pos = self.path.stat().st_size
        except OSError:
            self._pos = 0
        self._buf = b""

    def read_lines(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._pos:
            # The game truncates the log on launch.
            self._pos = 0
            self._buf = b""
        if size == self._pos:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self._pos)
                chunk = f.read(size - self._pos)
        except OSError:
            return []
        self._pos += len(chunk)
        data = self._buf + chunk
        parts = data.split(b"\n")
        self._buf = parts.pop()  # incomplete last line
        return [p.rstrip(b"\r").decode("utf-8", errors="replace") for p in parts]


class BridgeConnection:
    """Drives the game through the MCP Bridge mod. Same surface as GameConnection."""

    poll_interval = 0.05  # seconds between Lua.log reads while waiting
    default_timeout = 8.0  # the mod polls every 0.2 s, so allow for that

    def __init__(
        self,
        mods_dir: Path | None = None,
        lua_log: Path | None = None,
    ):
        self.mods_dir = mods_dir or default_mods_dir()
        self.cmd_path = installed_mod_dir(self.mods_dir) / "UI" / CMD_FILE_NAME
        self.lua_log = lua_log or default_lua_log()
        self._tail = _LogTail(self.lua_log)
        self._lock = asyncio.Lock()
        self.end_turn_lock = asyncio.Lock()
        self._seq = int(time.time() * 1000)
        self._connected = False
        # Compatibility with GameConnection consumers. The bridge has one
        # Lua state per UI context; both "indexes" are the InGame context.
        self.lua_states: dict[int, str] = {0: CONTEXT_INGAME, 1: CONTEXT_FRONTEND}
        self.gamecore_index: int | None = 0
        self.ingame_index: int | None = 0
        self.last_context: str = ""  # context that answered the last command
        self.transport_name = "bridge"

    # ------------------------------------------------------------------
    # lifecycle (mirrors GameConnection)
    # ------------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if not self.cmd_path.exists():
            raise ConnectionError(
                f"MCP Bridge mod is not installed ({self.cmd_path} missing). "
                "Run `uv run python scripts/install_bridge_mod.py` and enable "
                "'MCP Bridge' under Additional Content > Mods."
            )
        if not self.lua_log.exists():
            raise ConnectionError(
                f"Lua.log not found at {self.lua_log}. Is Civ 6 running?"
            )
        self._tail.seek_end()
        self._clear_command()
        ctx = await self.ping(timeout=3.0)
        if ctx is None:
            raise ConnectionError(
                "MCP Bridge mod did not answer. Is Civ 6 running (not loading) "
                "with the 'MCP Bridge' mod enabled? If the game was just started, "
                "wait for the main menu and try again."
            )
        self._connected = True
        log.info("Bridge connected (game answered from the %s context)", ctx)

    async def disconnect(self) -> None:
        self._connected = False

    async def ensure_connected(self) -> None:
        if self._connected:
            return
        await self.connect()

    async def reconnect(self) -> None:
        await self.disconnect()
        await self.connect()

    async def _ensure_game_states(self) -> None:
        await self.ensure_connected()

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    async def execute_read(
        self, lua_code: str, timeout: float | None = None
    ) -> list[str]:
        return await self.execute_in_context(CONTEXT_INGAME, lua_code, timeout)

    async def execute_write(
        self, lua_code: str, timeout: float | None = None
    ) -> list[str]:
        return await self.execute_in_context(CONTEXT_INGAME, lua_code, timeout)

    async def execute_in_state(
        self, state_index: int, lua_code: str, timeout: float | None = None
    ) -> list[str]:
        ctx = self.lua_states.get(state_index)
        if ctx is None:
            raise LuaError(f"ERR:Invalid Lua State {state_index}")
        return await self.execute_in_context(ctx, lua_code, timeout)

    async def execute_frontend(
        self, lua_code: str, timeout: float | None = None
    ) -> list[str]:
        return await self.execute_in_context(CONTEXT_FRONTEND, lua_code, timeout)

    async def ping(self, timeout: float = 2.0) -> str | None:
        """Ask whichever context is alive to answer. Returns its name or None."""
        try:
            async with self._lock:
                lines, ctx = await self._run(None, 'print("PONG")', timeout)
        except (LuaError, TimeoutError):
            return None
        return ctx if any(l == "PONG" for l in lines) else None

    async def where(self, timeout: float = 2.0) -> str | None:
        """'ingame', 'frontend' or None, from whichever context answers a ping."""
        ctx = await self.ping(timeout)
        if ctx is None:
            return None
        return "ingame" if ctx == INGAME_LOG_CONTEXT else "frontend"

    async def execute_in_context(
        self, context: str | None, lua_code: str, timeout: float | None = None
    ) -> list[str]:
        await self.ensure_connected()
        t = self.default_timeout if timeout is None else max(timeout, 2.0)
        async with self._lock:
            try:
                lines, _ = await self._run(context, lua_code, t)
            except TimeoutError as e:
                # The context we targeted is probably gone (save load, menu).
                self._connected = False
                raise ConnectionError(str(e)) from e
            return lines

    async def _run(
        self, context: str | None, lua_code: str, timeout: float
    ) -> tuple[list[str], str]:
        self._seq += 1
        seq = self._seq
        # Drain whatever was logged since the last command so stale lines
        # never match a new sequence number (they cannot — seq is unique —
        # but reading them keeps the tail position current).
        self._tail.read_lines()
        self._write_command(seq, context, lua_code)
        try:
            return await self._collect(seq, timeout)
        finally:
            self._clear_command()

    async def _collect(self, seq: int, timeout: float) -> tuple[list[str], str]:
        deadline = time.monotonic() + timeout
        out: list[str] = []
        partial: list[str] = []  # P fragments of the line in progress
        ctx_name = ""
        while True:
            for raw in self._tail.read_lines():
                m = _LINE_RE.match(raw)
                if not m or int(m.group("seq")) != seq:
                    continue
                ctx_name = m.group("ctx")
                kind = m.group("kind")
                text = m.group("text") or ""
                if kind == "P":
                    partial.append(text)
                elif kind == "O":
                    if partial:
                        text = "".join(partial) + text
                        partial = []
                    if text.strip() == SENTINEL:
                        continue
                    if text.startswith("ERR:"):
                        raise LuaError(text)
                    out.append(text)
                elif kind == "E":
                    raise LuaError("ERR:" + text)
                elif kind == "X":
                    self.last_context = ctx_name
                    return out, ctx_name
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"MCP Bridge: no reply from the game within {timeout:.0f}s "
                    f"(seq {seq}). The game may be loading, or the bridge mod "
                    "is not active in the current screen."
                )
            await asyncio.sleep(self.poll_interval)

    # ------------------------------------------------------------------
    # command file
    # ------------------------------------------------------------------
    @staticmethod
    def _long_bracket(code: str) -> str:
        """Wrap ``code`` in a Lua long string that cannot be terminated early."""
        level = 1
        while f"]{'=' * level}]" in code:
            level += 1
        eq = "=" * level
        # A newline right after the opening bracket is skipped by Lua, so add
        # one explicitly to keep line numbers in error messages honest.
        return f"[{eq}[\n{code}]{eq}]"

    def _write_command(self, seq: int, context: str | None, code: str) -> None:
        ctx = f'"{context}"' if context else "nil"
        body = (
            "-- written by civ6-mcp; the MCP Bridge mod includes this file on every poll\n"
            f"MCP_CMD = {{ seq = {seq}, context = {ctx}, code = {self._long_bracket(code)} }}\n"
        )
        self._atomic_write(body)

    def _clear_command(self) -> None:
        self._atomic_write(
            "-- written by civ6-mcp; the MCP Bridge mod includes this file on every poll\n"
            "MCP_CMD = nil\n"
        )

    def _atomic_write(self, text: str) -> None:
        tmp = self.cmd_path.with_suffix(".tmp")
        data = text.encode("utf-8")
        last_err: OSError | None = None
        for _ in range(40):
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, self.cmd_path)
                return
            except PermissionError as e:
                # The game had the file open for its include(); retry shortly.
                last_err = e
                time.sleep(0.01)
        raise ConnectionError(f"could not write {self.cmd_path}: {last_err}")
