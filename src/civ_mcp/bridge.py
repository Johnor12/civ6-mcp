"""File-based Lua bridge to Civilization VI through the MCP Bridge mod.

Civ 6 shuts its FireTuner listener down inside any multiplayer session,
including local hotseat, so this server never uses it. Instead a small UI
mod (``mods/MCPBridge``) re-includes a command file a few times a second
and runs whatever Lua it finds there, with ``print`` captured and written
to the game's ``Lua.log``. This module writes the command file and tails
the log.

Wire format (one command at a time)::

    mcp_bridge_cmd.lua   MCP_CMD = { seq = <n>, context = "InGame", code = [==[ ... ]==] }
    Lua.log              MCPBridge_InGame: MCPB|<n>|O|<one print line>
                         MCPBridge_InGame: MCPB|<n>|P|<partial line, joined with the next O>
                         MCPBridge_InGame: MCPB|<n>|E|<error text>
                         MCPBridge_InGame: MCPB|<n>|X

The mod runs in two UI contexts: ``InGame`` (a loaded game) and
``FrontEnd`` (main menu and staging room; its Lua.log prefix is ``Test``
because the mod rides on the game's Test context, see
``mods/MCPBridge/UI/Test.lua``). Only the UI Lua state exists, so every
query runs there; GameCore-only APIs are pcall-guarded in the builders.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import re
import shutil
import time
from ctypes import wintypes
from pathlib import Path

from civ_mcp.lua._helpers import SENTINEL

log = logging.getLogger(__name__)

MOD_DIR_NAME = "MCPBridge"
CMD_FILE_NAME = "mcp_bridge_cmd.lua"

CONTEXT_INGAME = "InGame"
CONTEXT_FRONTEND = "FrontEnd"
INGAME_LOG_CONTEXT = "MCPBridge_InGame"

_LINE_RE = re.compile(
    r"^(?P<ctx>[^:]*): MCPB\|(?P<seq>\d+)\|(?P<kind>[OPEXS])(?:\|(?P<text>.*))?$"
)


class LuaError(Exception):
    """The Lua chunk failed to compile or run, or printed an ERR: line."""


# ---------------------------------------------------------------------------
# Game folders (Windows; override with CIV_MCP_MODS_DIR / CIV_MCP_LUA_LOG)
# ---------------------------------------------------------------------------


def _documents_dir() -> Path:
    buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    # CSIDL_PERSONAL = 5 (My Documents) — honours a redirected Documents folder.
    if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
        return Path(buf.value)
    return Path.home() / "Documents"


def game_user_dir() -> Path:
    """``Documents\\My Games\\Sid Meier's Civilization VI``."""
    return _documents_dir() / "My Games" / "Sid Meier's Civilization VI"


def default_mods_dir() -> Path:
    override = os.environ.get("CIV_MCP_MODS_DIR")
    return Path(override) if override else game_user_dir() / "Mods"


def hotseat_saves_dir() -> Path:
    return game_user_dir() / "Saves" / "Hotseat"


def default_lua_log() -> Path:
    override = os.environ.get("CIV_MCP_LUA_LOG")
    if override:
        return Path(override)
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return base / "Firaxis Games" / "Sid Meier's Civilization VI" / "Logs" / "Lua.log"


def bundled_mod_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "mods" / MOD_DIR_NAME


def installed_mod_dir(mods_dir: Path | None = None) -> Path:
    return (mods_dir or default_mods_dir()) / MOD_DIR_NAME


def is_mod_installed(mods_dir: Path | None = None) -> bool:
    d = installed_mod_dir(mods_dir)
    return (d / f"{MOD_DIR_NAME}.modinfo").exists() and (d / "UI" / CMD_FILE_NAME).exists()


def install_mod(mods_dir: Path | None = None) -> Path:
    """Copy (or refresh) the bridge mod into the game's Mods folder."""
    src = bundled_mod_dir()
    if not (src / f"{MOD_DIR_NAME}.modinfo").exists():
        raise FileNotFoundError(f"bundled mod not found at {src}")
    dst = installed_mod_dir(mods_dir)
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


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


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
        if size < self._pos:  # the game truncates the log on launch
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
        parts = (self._buf + chunk).split(b"\n")
        self._buf = parts.pop()  # incomplete last line
        return [p.rstrip(b"\r").decode("utf-8", errors="replace") for p in parts]


class BridgeConnection:
    """Runs Lua in the game through the MCP Bridge mod."""

    poll_interval = 0.05  # seconds between Lua.log reads while waiting
    default_timeout = 8.0  # the mod polls every 0.2 s
    resend_after = 0.5  # seconds without a reply before checking the command file

    def __init__(self, mods_dir: Path | None = None, lua_log: Path | None = None):
        self.cmd_path = installed_mod_dir(mods_dir) / "UI" / CMD_FILE_NAME
        self.lua_log = lua_log or default_lua_log()
        self._tail = _LogTail(self.lua_log)
        self._lock = asyncio.Lock()
        self._seq = int(time.time() * 1000)
        self._connected = False
        self._last_command: tuple[str | None, str] = (None, "")
        self.last_context: str = ""  # Lua.log context that answered the last command

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
            raise ConnectionError(f"Lua.log not found at {self.lua_log}. Is Civ 6 running?")
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
        if not self._connected:
            await self.connect()

    async def execute(self, lua_code: str, timeout: float | None = None) -> list[str]:
        """Run Lua in the loaded game's UI state. Returns the printed lines."""
        return await self.execute_in_context(CONTEXT_INGAME, lua_code, timeout)

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

    async def ping(self, timeout: float = 2.0) -> str | None:
        """Ask whichever context is alive to answer. Returns its log name or None."""
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

    async def _run(
        self, context: str | None, lua_code: str, timeout: float
    ) -> tuple[list[str], str]:
        self._seq += 1
        seq = self._seq
        self._tail.read_lines()  # drain whatever was logged since the last command
        self._write_command(seq, context, lua_code)
        try:
            return await self._collect(seq, timeout)
        finally:
            self._clear_command()

    async def _collect(self, seq: int, timeout: float) -> tuple[list[str], str]:
        deadline = time.monotonic() + timeout
        next_check = time.monotonic() + self.resend_after
        out: list[str] = []
        partial: list[str] = []
        ctx_name = ""
        while True:
            if not ctx_name and time.monotonic() >= next_check:
                # Another writer (a stale server instance) can overwrite the
                # command file before the mod reads it. Re-sending is safe:
                # the mod ignores a sequence number it has already run.
                next_check = time.monotonic() + self.resend_after
                if not self._command_pending(seq):
                    log.warning("Command %d was overwritten in %s; re-sending", seq, self.cmd_path)
                    self._write_command(seq, *self._last_command)
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

    @staticmethod
    def _long_bracket(code: str) -> str:
        """Wrap ``code`` in a Lua long string that cannot be terminated early."""
        level = 1
        while f"]{'=' * level}]" in code:
            level += 1
        eq = "=" * level
        # Lua skips a newline right after the opening bracket; add one so line
        # numbers in error messages stay honest.
        return f"[{eq}[\n{code}]{eq}]"

    def _command_pending(self, seq: int) -> bool:
        try:
            return f"seq = {seq}," in self.cmd_path.read_text(encoding="utf-8")
        except OSError:
            return True  # unreadable mid-replace; try again next time

    def _write_command(self, seq: int, context: str | None, code: str) -> None:
        self._last_command = (context, code)
        ctx = f'"{context}"' if context else "nil"
        self._atomic_write(
            "-- written by civ6-mcp; the MCP Bridge mod includes this file on every poll\n"
            f"MCP_CMD = {{ seq = {seq}, context = {ctx}, code = {self._long_bracket(code)} }}\n"
        )

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
                last_err = e  # the game has the file open for its include()
                time.sleep(0.01)
        raise ConnectionError(f"could not write {self.cmd_path}: {last_err}")
