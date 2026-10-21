"""Hotseat stand-in mode — play one absent player's turn of a hotseat game.

Two ways to get at the hotseat game:

* **hotseat mode** (the normal one): the server talks to the live hotseat
  session through the MCP Bridge mod (``bridge.py``), because FireTuner is
  dead inside any multiplayer session. ``begin_standin_turn`` arms the mode
  for one leader; ``end_turn`` really ends the turn, waits for the next
  human's turn-change screen and saves there.
* **single mode** (legacy detour): ``scripts/hotseat_standin.py to-single``
  converts the hotseat save into a single-player save, the turn is played
  there and ``end_turn`` only *finishes* the turn and saves it for
  conversion back. Kept for machines without the bridge mod.

In both modes a marker file (``~/.civ6-mcp/standin.json``) records which
leader the agent may act for. While it is active and matches the loaded
game, the server:

* refuses every game tool if the game's local player is not that leader
  (so a finished stand-in turn, or a human's turn, can never be touched);
* writes only the agent's own diary rows, under a stable run id, so the
  diary persists across sessions and holds no other player's figures;
* never kills or restarts the game process on connection trouble;
* removes ``run_lua``.

Without an active marker, game tools are refused inside a hotseat session
altogether: call ``begin_standin_turn`` first.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

STANDIN_PATH = Path.home() / ".civ6-mcp" / "standin.json"
STANDIN_RUN_ID = "standin"
DEFAULT_OUT_SAVE = "MCP_STANDIN_OUT"

MODE_HOTSEAT = "hotseat"
MODE_SINGLE = "single"


@dataclass
class StandinConfig:
    """Contents of ``~/.civ6-mcp/standin.json``."""

    active: bool = False
    mode: str = (
        MODE_HOTSEAT  # "hotseat" (live session via the bridge) | "single" (detour)
    )
    leader: str = ""  # e.g. LEADER_POUNDMAKER — the player the agent controls
    player_name: str = ""  # the hotseat player name shown on the turn-change screen
    hotseat_save: str = ""  # the hotseat save the turn started from (no extension)
    single_save: str = (
        ""  # single mode: the converted single-player save that was loaded
    )
    out_save: str = (
        DEFAULT_OUT_SAVE  # single mode: where end_turn saves the finished turn
    )
    started_turn: int | None = None  # game turn when begin_standin_turn armed the mode
    started_at: str = ""
    finished_turn: int | None = None  # game turn ended/finished by end_turn, if any
    finished_at: str = ""  # ISO timestamp of that
    finished_save: str = ""  # hotseat mode: the save written after the turn ended
    next_player: str = ""  # hotseat mode: whose turn-change screen the game was left on
    note: str = ""  # free text from the absent player (what they wanted done)

    @property
    def leader_display(self) -> str:
        return self.leader.replace("LEADER_", "").replace("_", " ").title()

    @property
    def is_hotseat_mode(self) -> bool:
        return self.mode != MODE_SINGLE


def load_config(path: Path = STANDIN_PATH) -> StandinConfig | None:
    """Return the marker's config, or None when there is no marker."""
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("Could not read stand-in marker %s: %s", path, e)
        return None
    known = {f for f in StandinConfig.__dataclass_fields__}
    cfg = StandinConfig(**{k: v for k, v in raw.items() if k in known})
    if "mode" not in raw:
        # Markers written before hotseat mode existed came from to-single.
        cfg.mode = MODE_SINGLE if raw.get("single_save") else MODE_HOTSEAT
    return cfg


def save_config(cfg: StandinConfig, path: Path = STANDIN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    tmp.replace(path)


def active_config(path: Path = STANDIN_PATH) -> StandinConfig | None:
    """The marker's config when stand-in mode is switched on, else None."""
    cfg = load_config(path)
    return cfg if cfg is not None and cfg.active else None


def marker_exists(path: Path = STANDIN_PATH) -> bool:
    return path.exists()


# ---------------------------------------------------------------------------
# Live checks against the game
# ---------------------------------------------------------------------------

_LOCAL_LEADER_LUA = (
    "local me = Game.GetLocalPlayer() "
    "local cfg = PlayerConfigurations[me] "
    'print("LEADER|" .. me .. "|" .. tostring(cfg and cfg:GetLeaderTypeName()) '
    '.. "|" .. tostring(GameConfiguration.IsAnyMultiplayer()) '
    '.. "|" .. tostring(Players[me] and Players[me]:IsTurnActive()) '
    '.. "|" .. tostring(GameConfiguration.IsHotseat())) '
    'print("---END---")'
)

_VERIFY_TTL = 20.0  # seconds a successful leader check stays valid


class StandinGuard:
    """Checks that the loaded game is the one the marker describes.

    The check costs one Lua round trip, so a passing result is cached for a
    short while; a failing result is never cached.
    """

    def __init__(self) -> None:
        self._ok_until: float = 0.0
        self._ok_for: str = ""

    async def _probe(self, conn) -> tuple[int, str, bool, bool, bool] | None:
        lines = await conn.execute_write(_LOCAL_LEADER_LUA)
        info = next((l for l in lines if l.startswith("LEADER|")), None)
        if info is None:
            return None
        _, pid, leader, any_mp, turn_active, hotseat = (info.split("|") + [""] * 6)[:6]
        return (
            int(pid) if pid.lstrip("-").isdigit() else -1,
            leader,
            any_mp == "true",
            turn_active == "true",
            hotseat == "true",
        )

    async def check(self, conn, cfg: StandinConfig | None) -> str | None:
        """Return an error message when the game does not match ``cfg``.

        With ``cfg`` None (no active marker) the only rule is that a hotseat
        session is off limits until ``begin_standin_turn`` names a player.
        """
        key = cfg.leader if cfg is not None else "<none>"
        now = time.monotonic()
        if self._ok_for == key and now < self._ok_until:
            return None
        probe = await self._probe(conn)
        if probe is None:
            return "Stand-in guard: could not read the local player from the game."
        pid, leader, any_mp, turn_active, hotseat = probe

        if cfg is None:
            if hotseat:
                return (
                    "This is a hotseat session. Game tools are locked until "
                    "begin_standin_turn(leader) names the absent player whose "
                    f"turn this is (the game says it is {leader}'s turn)."
                )
            self._ok_for, self._ok_until = key, now + _VERIFY_TTL
            return None

        if cfg.is_hotseat_mode:
            if not hotseat:
                return (
                    "Stand-in guard: the game is not in a hotseat session. Use "
                    "load_hotseat_save to open the hotseat save, then "
                    "begin_standin_turn."
                )
            if leader != cfg.leader:
                finished = (
                    f" The stand-in turn for {cfg.leader_display} was already ended "
                    f"(turn {cfg.finished_turn}, saved as '{cfg.finished_save}')."
                    if cfg.finished_turn is not None
                    else ""
                )
                return (
                    f"Stand-in guard: the game is on {leader}'s turn (slot {pid}), "
                    f"not {cfg.leader}'s.{finished} Refusing to act for another "
                    "player. If that player is also absent, call "
                    "begin_standin_turn with their leader."
                )
            if not turn_active:
                return (
                    f"Stand-in guard: {cfg.leader} is the local player but their "
                    "turn is not active (already ended?). Nothing to do."
                )
        else:
            if any_mp:
                return (
                    "Stand-in guard (single mode): the game is in a multiplayer/"
                    "hotseat session but the marker was written by to-single. "
                    f"Load the converted single-player save '{cfg.single_save}' or "
                    "clear the marker and use begin_standin_turn."
                )
            if leader != cfg.leader:
                return (
                    f"Stand-in guard: the local player is {leader} (id {pid}) but this "
                    f"stand-in session is for {cfg.leader}. Refusing to act for another "
                    f"player. Load '{cfg.single_save}' or fix ~/.civ6-mcp/standin.json."
                )
        self._ok_for, self._ok_until = key, now + _VERIFY_TTL
        return None

    def invalidate(self) -> None:
        self._ok_until = 0.0


def status_text(cfg: StandinConfig) -> str:
    """One-paragraph summary for get_game_overview / end_turn."""
    who = cfg.leader_display + (f" ('{cfg.player_name}')" if cfg.player_name else "")
    if cfg.is_hotseat_mode:
        lines = [
            f"STAND-IN MODE (hotseat): you are playing {who}'s turn in the live "
            "hotseat game. Every other civilization is a human player. end_turn "
            "ends the turn for real: it hands the game to the next player's "
            "turn-change screen and saves there. Call it exactly once when the "
            "turn is done. Do not load other saves."
        ]
    else:
        lines = [
            f"STAND-IN MODE (single-player detour): playing {who}'s turn of the "
            f"hotseat game (from '{cfg.hotseat_save}'). end_turn will NOT advance "
            f"the game: it finishes the turn and saves it as '{cfg.out_save}' for "
            "conversion back to hotseat. Do not try to load other saves."
        ]
    if cfg.note:
        lines.append(f"Player's note: {cfg.note}")
    if cfg.finished_turn is not None:
        if cfg.is_hotseat_mode:
            lines.append(
                f"This stand-in turn was already ended (turn {cfg.finished_turn}, "
                f"{cfg.finished_at}, saved as '{cfg.finished_save}'); the game is "
                f"on {cfg.next_player or 'the next player'}'s turn-change screen. "
                "All game tools are locked until begin_standin_turn is called again."
            )
        else:
            lines.append(
                f"This turn was already finished and saved (turn {cfg.finished_turn}, "
                f"{cfg.finished_at}). Only act again if the operator asked for changes."
            )
    return "\n".join(lines)
