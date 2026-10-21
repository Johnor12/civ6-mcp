"""Hotseat stand-in mode — play one absent player's turn of a hotseat game.

FireTuner shuts down the moment Civ 6 enters a hotseat session, so the MCP
server cannot touch a hotseat game directly. ``scripts/hotseat_standin.py``
converts the hotseat save into a single-player save in which the absent
player is the only human, the agent plays that turn in single-player mode,
and the script converts the result back. The conversion script writes a
marker file that this module reads; while the marker is active and matches
the loaded game, the server:

* never sends ``ACTION_ENDTURN`` — ``end_turn`` finishes the turn and saves
  instead (the other civilizations are humans; the AI must not play them);
* refuses to act if the game's local player is not the expected leader;
* writes only the agent's own diary rows, under a stable run id, so the
  diary persists across sessions and holds no other player's figures;
* never kills or restarts the game process on connection trouble.
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


@dataclass
class StandinConfig:
    """Contents of ``~/.civ6-mcp/standin.json``."""

    active: bool = False
    leader: str = ""  # e.g. LEADER_POUNDMAKER — the player the agent controls
    hotseat_save: str = ""  # the hotseat save the turn started from (no extension)
    single_save: str = ""  # the converted single-player save that was loaded
    out_save: str = DEFAULT_OUT_SAVE  # where end_turn saves the finished turn
    finished_turn: int | None = None  # game turn saved by end_turn, if any
    finished_at: str = ""  # ISO timestamp of that save
    note: str = ""  # free text from the absent player (what they wanted done)

    @property
    def leader_display(self) -> str:
        return self.leader.replace("LEADER_", "").replace("_", " ").title()


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
    return StandinConfig(**{k: v for k, v in raw.items() if k in known})


def save_config(cfg: StandinConfig, path: Path = STANDIN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    tmp.replace(path)


def active_config(path: Path = STANDIN_PATH) -> StandinConfig | None:
    """The marker's config when stand-in mode is switched on, else None."""
    cfg = load_config(path)
    return cfg if cfg is not None and cfg.active else None


# ---------------------------------------------------------------------------
# Live checks against the game
# ---------------------------------------------------------------------------

_LOCAL_LEADER_LUA = (
    "local me = Game.GetLocalPlayer() "
    "local cfg = PlayerConfigurations[me] "
    'print("LEADER|" .. me .. "|" .. tostring(cfg and cfg:GetLeaderTypeName()) '
    '.. "|" .. tostring(GameConfiguration.IsAnyMultiplayer()) '
    '.. "|" .. tostring(Players[me] and Players[me]:IsTurnActive())) '
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

    async def check(self, conn, cfg: StandinConfig) -> str | None:
        """Return an error message when the game does not match ``cfg``."""
        now = time.monotonic()
        if self._ok_for == cfg.leader and now < self._ok_until:
            return None
        lines = await conn.execute_read(_LOCAL_LEADER_LUA)
        info = next((l for l in lines if l.startswith("LEADER|")), None)
        if info is None:
            return "Stand-in guard: could not read the local player from the game."
        _, pid, leader, any_mp, turn_active = (info.split("|") + ["", "", "", ""])[:5]
        if any_mp == "true":
            return (
                "Stand-in guard: the game is in a multiplayer/hotseat session. "
                "FireTuner does not work there — load the converted single-player "
                f"save '{cfg.single_save}' instead."
            )
        if leader != cfg.leader:
            return (
                f"Stand-in guard: the local player is {leader} (id {pid}) but this "
                f"stand-in session is for {cfg.leader}. Refusing to act for another "
                f"player. Load '{cfg.single_save}' or fix ~/.civ6-mcp/standin.json."
            )
        self._ok_for = cfg.leader
        self._ok_until = now + _VERIFY_TTL
        return None

    def invalidate(self) -> None:
        self._ok_until = 0.0


def status_text(cfg: StandinConfig) -> str:
    """One-paragraph summary for get_game_overview / end_turn."""
    lines = [
        f"STAND-IN MODE: playing {cfg.leader_display}'s turn of the hotseat game "
        f"(from '{cfg.hotseat_save}'). end_turn will NOT advance the game: it "
        f"finishes the turn and saves it as '{cfg.out_save}' for conversion back "
        f"to hotseat. Do not try to load other saves."
    ]
    if cfg.note:
        lines.append(f"Player's note: {cfg.note}")
    if cfg.finished_turn is not None:
        lines.append(
            f"This turn was already finished and saved (turn {cfg.finished_turn}, "
            f"{cfg.finished_at}). Only act again if the operator asked for changes."
        )
    return "\n".join(lines)
