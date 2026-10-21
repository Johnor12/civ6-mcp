"""Drive a local hotseat session through the MCP Bridge mod.

The flow mirrors what a person does at the keyboard:

    main menu ─► Multiplayer > Hotseat > Load Game ─► staging room ─► Start
    ─► PlayerChange screen ("<name>: Save Game / Start Turn") ─► play ─►
    End Turn ─► PlayerChange screen for the next human ─► Save Game

The Lua below was verified live on 2026-10-21 (build 1.0.12.68, 10-player
Better Balanced Game hotseat save).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from civ_mcp.bridge import (
    CONTEXT_FRONTEND,
    CONTEXT_INGAME,
    BridgeConnection,
    hotseat_saves_dir,
)
from civ_mcp.lua._helpers import SENTINEL

log = logging.getLogger(__name__)


def _lua_str(s: str) -> str:
    """Quote a Python string as a Lua string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def normalize_leader(leader: str) -> str:
    """'Poundmaker' / 'poundmaker' / 'LEADER_POUNDMAKER' -> 'LEADER_POUNDMAKER'."""
    s = leader.strip().upper().replace(" ", "_").replace("-", "_")
    if not s.startswith("LEADER_"):
        s = "LEADER_" + s
    return s


def list_hotseat_saves(limit: int = 30) -> str:
    """Saves in the game's hotseat folder (plus its auto/ and quick/ subfolders), newest first."""
    base = hotseat_saves_dir()
    found: list[tuple[float, str]] = []
    for sub, tag in (("", ""), ("auto", " [autosave]"), ("quick", " [quicksave]")):
        folder = base / sub if sub else base
        if not folder.is_dir():
            continue
        for f in folder.glob("*.Civ6Save"):
            found.append((f.stat().st_mtime, f.stem + tag))
    if not found:
        return f"No hotseat saves found in {base}."
    found.sort(reverse=True)
    lines = [f"Hotseat saves in {base} (newest first; load with load_hotseat_save):"]
    lines.extend(f"  {name}" for _, name in found[:limit])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass
class HotseatStatus:
    screen: str  # "frontend" | "ingame" | "none"
    in_session: bool = False  # front end: a (staging-room) session is open
    is_hotseat: bool = False
    any_multiplayer: bool = False
    turn: int = 0
    local_player: int = -1
    leader: str = ""
    player_name: str = ""
    turn_active: bool = False
    player_change_visible: bool = False  # the PlayerChange context is shown
    turn_controls_visible: bool = False  # ... with its Save/Start Turn buttons
    paused: bool = False
    has_password: bool = False

    @property
    def leader_display(self) -> str:
        return self.leader.replace("LEADER_", "").replace("_", " ").title()

    def describe(self) -> str:
        if self.screen == "frontend":
            where = "staging room" if self.in_session else "main menu"
            return f"Game is at the {where} (hotseat={self.is_hotseat})."
        if self.screen != "ingame":
            return "The game did not answer (loading, or the MCP Bridge mod is not active)."
        kind = (
            "hotseat"
            if self.is_hotseat
            else ("multiplayer" if self.any_multiplayer else "single-player")
        )
        parts = [
            f"In a {kind} game, turn {self.turn}: local player is {self.leader_display}"
            f" (slot {self.local_player}, player name '{self.player_name}'),"
            f" turn active={self.turn_active}."
        ]
        if self.turn_controls_visible:
            parts.append("The PlayerChange screen (Save Game / Start Turn) is showing.")
        elif self.player_change_visible:
            parts.append("The PlayerChange screen is in its 'please wait' state.")
        if self.paused:
            parts.append("The game is paused.")
        if self.has_password:
            parts.append("This player has a hotseat password (bypassed by the stand-in).")
        return " ".join(parts)


_FRONTEND_STATUS_LUA = (
    'print("FE|" .. tostring(Network.IsInSession()) .. "|" .. tostring(GameConfiguration.IsHotseat())) '
    f'print("{SENTINEL}")'
)

_INGAME_STATUS_LUA = (
    "local me = Game.GetLocalPlayer() "
    "local pc = PlayerConfigurations[me] "
    "local pl = Players[me] "
    'local pcx = ContextPtr:LookUpControl("/InGame/PlayerChange") '
    'local pa = ContextPtr:LookUpControl("/InGame/PlayerChange/PopupAlphaIn") '
    "local name = pc and tostring(pc:GetPlayerName()) or '' "
    "name = string.gsub(name, '|', '/') "
    "local pw = pc and pc:GetHotseatPassword() or '' "
    'print("HS|" .. table.concat({ '
    "tostring(Game.GetCurrentGameTurn()), tostring(me), "
    "tostring(pc and pc:GetLeaderTypeName()), name, "
    "tostring(pl ~= nil and pl:IsTurnActive()), "
    "tostring(GameConfiguration.IsHotseat()), tostring(GameConfiguration.IsAnyMultiplayer()), "
    "tostring(pcx ~= nil and not pcx:IsHidden()), "
    "tostring(pcx ~= nil and not pcx:IsHidden() and pa ~= nil and not pa:IsHidden()), "
    "tostring(GameConfiguration.IsPaused()), "
    "tostring(pw ~= nil and pw ~= '') "
    '}, "|")) '
    f'print("{SENTINEL}")'
)


def _b(s: str) -> bool:
    return s == "true"


async def ingame_status(conn: BridgeConnection, timeout: float = 6.0) -> HotseatStatus:
    lines = await conn.execute_in_context(CONTEXT_INGAME, _INGAME_STATUS_LUA, timeout)
    for line in lines:
        if line.startswith("HS|"):
            p = line.split("|")
            if len(p) < 12:
                break
            return HotseatStatus(
                screen="ingame",
                turn=int(p[1]) if p[1].lstrip("-").isdigit() else 0,
                local_player=int(p[2]) if p[2].lstrip("-").isdigit() else -1,
                leader=p[3],
                player_name=p[4],
                turn_active=_b(p[5]),
                is_hotseat=_b(p[6]),
                any_multiplayer=_b(p[7]),
                player_change_visible=_b(p[8]),
                turn_controls_visible=_b(p[9]),
                paused=_b(p[10]),
                has_password=_b(p[11]),
            )
    return HotseatStatus(screen="none")


async def frontend_status(conn: BridgeConnection, timeout: float = 6.0) -> HotseatStatus:
    lines = await conn.execute_in_context(CONTEXT_FRONTEND, _FRONTEND_STATUS_LUA, timeout)
    for line in lines:
        if line.startswith("FE|"):
            p = line.split("|")
            return HotseatStatus(screen="frontend", in_session=_b(p[1]), is_hotseat=_b(p[2]))
    return HotseatStatus(screen="none")


async def status(conn: BridgeConnection) -> HotseatStatus:
    """Where is the game right now? Never raises for a silent game."""
    try:
        where = await conn.where(timeout=3.0)
    except Exception:
        where = None
    try:
        if where == "ingame":
            return await ingame_status(conn)
        if where == "frontend":
            return await frontend_status(conn)
    except Exception as e:  # noqa: BLE001 — a status probe must not throw
        log.debug("hotseat status probe failed: %s", e)
    return HotseatStatus(screen="none")


async def wait_for(conn: BridgeConnection, where: str, timeout: float) -> bool:
    """Wait until the given context ("ingame"/"frontend") answers a ping."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if await conn.where(timeout=1.5) == where:
                return True
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return False


# ---------------------------------------------------------------------------
# Loading a hotseat save: main menu -> staging room -> launch -> PlayerChange
# ---------------------------------------------------------------------------


def _load_lua(save_name: str) -> str:
    name = _lua_str(save_name)
    name_ext = _lua_str(save_name + ".Civ6Save")
    return (
        # The host-game screen does this before showing its Load button;
        # without it the staging room shows every other slot as "Computer".
        "GameConfiguration.SetToDefaults(GameModeTypes.HOTSEAT) "
        "MCP_HS = { done = false, result = '' } "
        "local function OnResults(fileList, qid) "
        "  UI.CloseFileListQuery(qid) "
        "  LuaEvents.FileListQueryResults.Remove(OnResults) "
        "  local names = {} "
        "  for i, s in ipairs(fileList) do "
        "    names[#names+1] = tostring(s.Name) "
        f"    if s.Name == {name} or s.Name == {name_ext} then "
        "      MCP_HS.result = 'FOUND' MCP_HS.done = true "
        "      Network.LeaveGame() "
        "      Network.LoadGame(s, ServerType.SERVER_TYPE_HOTSEAT) "
        "      return "
        "    end "
        "  end "
        "  MCP_HS.result = 'NOT_FOUND|' .. table.concat(names, '; ') MCP_HS.done = true "
        "end "
        "LuaEvents.FileListQueryResults.Add(OnResults) "
        "local opts = SaveLocationOptions.NORMAL + SaveLocationOptions.AUTOSAVE "
        "  + SaveLocationOptions.QUICKSAVE + SaveLocationOptions.LOAD_METADATA "
        "UI.QuerySaveGameList(SaveLocations.LOCAL_STORAGE, SaveTypes.HOTSEAT, opts) "
        'print("QUERY_SENT") '
        f'print("{SENTINEL}")'
    )


_LOAD_POLL_LUA = (
    'print("LOAD|" .. tostring(MCP_HS and MCP_HS.done) .. "|" .. tostring(MCP_HS and MCP_HS.result)) '
    f'print("{SENTINEL}")'
)

_LAUNCH_LUA = (
    "if not GameConfiguration.IsHotseat() then "
    f'  print("ERR:NOT_HOTSEAT") print("{SENTINEL}") return '
    "end "
    "Network.LaunchGame() "
    'print("LAUNCHED") '
    f'print("{SENTINEL}")'
)


async def load_hotseat_save(
    conn: BridgeConnection, save_name: str, timeout: float = 240.0
) -> str:
    """Load a hotseat save from wherever the game is and start it.

    Returns a human-readable report; raises ConnectionError/RuntimeError on
    failure. On success the game sits on the PlayerChange screen of the
    player whose turn it is.
    """
    t0 = time.monotonic()
    where = await conn.where(timeout=3.0)
    if where is None:
        raise ConnectionError(
            "The game did not answer. Is Civ 6 running with the MCP Bridge mod enabled?"
        )
    if where == "ingame":
        log.info("load_hotseat_save: leaving the current game for the main menu")
        await conn.execute_in_context(
            CONTEXT_INGAME, f'print("EXIT") print("{SENTINEL}") Events.ExitToMainMenu()', 6.0
        )
        if not await wait_for(conn, "frontend", 90.0):
            raise RuntimeError("Timed out waiting for the main menu after leaving the game.")
        await asyncio.sleep(2.0)
    fe = await frontend_status(conn)
    if fe.in_session:
        # A staging room is already open: leave it so the load below does
        # not join the wrong session.
        await conn.execute_in_context(
            CONTEXT_FRONTEND, f'Network.LeaveGame() print("LEFT") print("{SENTINEL}")', 6.0
        )
        await asyncio.sleep(2.0)

    await conn.execute_in_context(CONTEXT_FRONTEND, _load_lua(save_name), 8.0)
    result = ""
    for _ in range(40):
        await asyncio.sleep(0.5)
        lines = await conn.execute_in_context(CONTEXT_FRONTEND, _LOAD_POLL_LUA, 6.0)
        for line in lines:
            if line.startswith("LOAD|true|"):
                result = line.split("|", 2)[2]
        if result:
            break
    if not result:
        raise RuntimeError("The save list query did not complete.")
    if result.startswith("NOT_FOUND"):
        names = result.split("|", 1)[1] if "|" in result else ""
        raise RuntimeError(f"Hotseat save '{save_name}' not found. Saves the game lists: {names}")

    # Staging room: the session exists once the save is parsed and the
    # players are configured; then "Start Game".
    deadline = time.monotonic() + 90.0
    while True:
        await asyncio.sleep(1.0)
        try:
            fe = await frontend_status(conn)
        except Exception:
            fe = HotseatStatus(screen="none")
        if fe.in_session and fe.is_hotseat:
            break
        if time.monotonic() > deadline:
            raise RuntimeError("Timed out waiting for the hotseat staging room.")
    await asyncio.sleep(1.5)
    lines = await conn.execute_in_context(CONTEXT_FRONTEND, _LAUNCH_LUA, 8.0)
    if not any(l == "LAUNCHED" for l in lines):
        raise RuntimeError(f"Could not start the game from the staging room: {lines}")

    # The in-game context appears early in the load; the PlayerChange screen
    # appears when the load screen closes.
    remaining = max(30.0, timeout - (time.monotonic() - t0))
    if not await wait_for(conn, "ingame", remaining):
        raise RuntimeError("Timed out waiting for the game to load.")
    deadline = time.monotonic() + 120.0
    st = HotseatStatus(screen="none")
    while time.monotonic() < deadline:
        try:
            st = await ingame_status(conn)
        except Exception:
            st = HotseatStatus(screen="none")
        if st.screen == "ingame" and (st.turn_controls_visible or st.turn_active):
            break
        await asyncio.sleep(1.0)
    took = int(time.monotonic() - t0)
    return f"Loaded hotseat save '{save_name}' in {took}s. {st.describe()}"


# ---------------------------------------------------------------------------
# PlayerChange screen, end turn, save
# ---------------------------------------------------------------------------

# What PlayerChange.lua's OnOk does, from another context (its OK button
# cannot be clicked from Lua). The password check is skipped on purpose: the
# operator running the stand-in is the authority here.
_ACCEPT_TURN_LUA = (
    "local me = Game.GetLocalPlayer() "
    "local pc = PlayerConfigurations[me] "
    "if pc and pc:GetWantsPause() then pc:SetWantsPause(false) Network.BroadcastPlayerInfo() end "
    "pcall(function() EffectsManager:ResumeAllEffects() end) "
    "pcall(function() LuaEvents.PlayerChange_Close(me) end) "
    'local pcx = ContextPtr:LookUpControl("/InGame/PlayerChange") '
    "if pcx and not pcx:IsHidden() then UIManager:DequeuePopup(pcx) end "
    'local pa = ContextPtr:LookUpControl("/InGame/PlayerChange/PopupAlphaIn") '
    "if pa then pa:SetHide(true) end "
    'print("ACCEPTED") '
    f'print("{SENTINEL}")'
)


async def accept_turn(conn: BridgeConnection) -> HotseatStatus:
    """Dismiss the PlayerChange screen and unpause, like pressing Start Turn."""
    await conn.execute_in_context(CONTEXT_INGAME, _ACCEPT_TURN_LUA, 8.0)
    await asyncio.sleep(0.5)
    return await ingame_status(conn)


_END_TURN_LUA = (
    "local me = Game.GetLocalPlayer() "
    "UI.RequestAction(ActionTypes.ACTION_ENDTURN) "
    'print("SENT|" .. tostring(me)) '
    f'print("{SENTINEL}")'
)

_BLOCKER_LUA = (
    "local me = Game.GetLocalPlayer() "
    "local b = NotificationManager.GetFirstEndTurnBlocking(me) "
    "local name = 'NONE' "
    "for k, v in pairs(EndTurnBlockingTypes) do if v == b then name = k end end "
    'print("BLOCK|" .. name) '
    f'print("{SENTINEL}")'
)


async def end_turn_and_wait(
    conn: BridgeConnection, prev_player: int, prev_turn: int, timeout: float = 240.0
) -> HotseatStatus:
    """Send ACTION_ENDTURN and wait for the next human's PlayerChange screen.

    In hotseat the engine hands the local seat to the next living human (or,
    after the last one, runs the AI civilizations and city-states and starts
    the next turn with the first human). Either way the PlayerChange screen
    comes up for that player.
    """
    await conn.execute_in_context(CONTEXT_INGAME, _END_TURN_LUA, 8.0)
    deadline = time.monotonic() + timeout
    checked_blocker = False
    st = HotseatStatus(screen="none")
    while time.monotonic() < deadline:
        await asyncio.sleep(0.75)
        try:
            st = await ingame_status(conn)
        except Exception:
            continue  # the game may be busy processing the turn
        if st.screen != "ingame":
            continue
        changed = st.local_player != prev_player or st.turn != prev_turn
        if changed and st.turn_controls_visible:
            return st
        if not changed and not checked_blocker and time.monotonic() > deadline - timeout + 12:
            checked_blocker = True
            try:
                lines = await conn.execute_in_context(CONTEXT_INGAME, _BLOCKER_LUA, 6.0)
            except Exception:
                lines = []
            blocker = next((l.split("|", 1)[1] for l in lines if l.startswith("BLOCK|")), "?")
            if blocker not in ("NONE", "ENDTURN_BLOCKING_TYPE_NONE", "?"):
                raise RuntimeError(
                    f"The game refused to end the turn: blocker {blocker}. "
                    "Resolve it (units, production, research, ...) and call end_turn again."
                )
    raise RuntimeError(
        "Timed out waiting for the next player's turn-change screen after End Turn. "
        f"Last seen: {st.describe()}"
    )


_SAVE_NAME_LUA = (
    "local me = Game.GetLocalPlayer() "
    "local pc = PlayerConfigurations[me] "
    "local leader = Locale.ToUpper(Locale.Lookup(pc:GetLeaderName())) "
    "local turn = Game.GetCurrentGameTurn() "
    "local year = Calendar.MakeYearStr(turn) "
    "local name = leader .. ' ' .. turn .. ' ' .. year .. ' MCP' "
    "name = string.gsub(name, '[\\\\/:*?\"<>|]', '') "
    'print("NAME|" .. name) '
    f'print("{SENTINEL}")'
)


async def suggest_save_name(conn: BridgeConnection) -> str:
    """'<NEXT PLAYER'S LEADER> <turn> <year> MCP', the players' own convention plus a tag."""
    lines = await conn.execute_in_context(CONTEXT_INGAME, _SAVE_NAME_LUA, 6.0)
    for line in lines:
        if line.startswith("NAME|"):
            return line.split("|", 1)[1].strip()
    return f"HOTSEAT MCP {int(time.time())}"


def _save_lua(name: str) -> str:
    return (
        "local gf = {} "
        f"gf.Name = {_lua_str(name)} "
        "gf.Location = SaveLocations.LOCAL_STORAGE "
        "gf.Type = Network.GetGameConfigurationSaveType() "  # hotseat saves go to Saves/Hotseat
        "gf.IsAutosave = false "
        "gf.IsQuicksave = false "
        "Network.SaveGame(gf) "
        'print("SAVED|" .. tostring(gf.Type)) '
        f'print("{SENTINEL}")'
    )


async def save_hotseat(conn: BridgeConnection, name: str) -> str:
    """Save the session under ``name`` in the hotseat save folder."""
    lines = await conn.execute_in_context(CONTEXT_INGAME, _save_lua(name), 8.0)
    if not any(l.startswith("SAVED|") for l in lines):
        raise RuntimeError(f"Network.SaveGame did not confirm: {lines}")
    return name
