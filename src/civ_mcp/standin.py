"""Stand-in marker and guard: which absent hotseat player the agent may act for.

``begin_standin_turn`` writes ``~/.civ6-mcp/standin.json`` naming one leader.
Before every game tool the guard checks that the loaded game is a hotseat
session whose local player is that leader with their turn active; anything
else is refused, so a finished stand-in turn, or a human's turn, can never
be touched. Without an active marker every game tool is refused.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

STANDIN_PATH = Path.home() / ".civ6-mcp" / "standin.json"


@dataclass
class StandinConfig:
    active: bool = False
    leader: str = ""  # e.g. LEADER_POUNDMAKER — the player the agent controls
    player_name: str = ""  # hotseat player name shown on the turn-change screen
    hotseat_save: str = ""  # the save the turn was loaded from, if any
    started_turn: int | None = None
    started_at: str = ""
    finished_turn: int | None = None  # set once end_turn has ended the turn
    finished_at: str = ""
    finished_save: str = ""  # the save written after the turn ended
    next_player: str = ""  # whose turn-change screen the game was left on
    note: str = ""  # what the absent player asked for

    @property
    def leader_display(self) -> str:
        return self.leader.replace("LEADER_", "").replace("_", " ").title()


def load_config(path: Path = STANDIN_PATH) -> StandinConfig | None:
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("Could not read stand-in marker %s: %s", path, e)
        return None
    known = StandinConfig.__dataclass_fields__
    return StandinConfig(**{k: v for k, v in raw.items() if k in known})


def save_config(cfg: StandinConfig, path: Path = STANDIN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    tmp.replace(path)


def active_config(path: Path = STANDIN_PATH) -> StandinConfig | None:
    cfg = load_config(path)
    return cfg if cfg is not None and cfg.active else None


_LOCAL_LEADER_LUA = (
    "local me = Game.GetLocalPlayer() "
    "local cfg = PlayerConfigurations[me] "
    'print("LEADER|" .. me .. "|" .. tostring(cfg and cfg:GetLeaderTypeName()) '
    '.. "|" .. tostring(Players[me] and Players[me]:IsTurnActive()) '
    '.. "|" .. tostring(GameConfiguration.IsHotseat())) '
    'print("---END---")'
)

_VERIFY_TTL = 20.0  # seconds a passing check stays valid


class StandinGuard:
    """Checks that the loaded game matches the marker. Passes are cached briefly."""

    def __init__(self) -> None:
        self._ok_until = 0.0
        self._ok_for = ""

    def invalidate(self) -> None:
        self._ok_until = 0.0

    async def check(self, conn, cfg: StandinConfig | None) -> str | None:
        """Return a refusal message, or None when the agent may act."""
        if cfg is None:
            return (
                "No stand-in is armed: game tools are locked. Call hotseat_status, "
                "then begin_standin_turn(leader) for the absent player whose "
                "turn-change screen is showing."
            )
        now = time.monotonic()
        if self._ok_for == cfg.leader and now < self._ok_until:
            return None
        lines = await conn.execute(_LOCAL_LEADER_LUA)
        info = next((l for l in lines if l.startswith("LEADER|")), None)
        if info is None:
            return "Stand-in guard: could not read the local player from the game."
        _, pid, leader, turn_active, hotseat = (info.split("|") + [""] * 5)[:5]
        if hotseat != "true":
            return (
                "Stand-in guard: the game is not in a hotseat session. Use "
                "load_hotseat_save to open the hotseat save, then begin_standin_turn."
            )
        if leader != cfg.leader:
            finished = (
                f" The stand-in turn for {cfg.leader_display} was already ended "
                f"(turn {cfg.finished_turn}, saved as '{cfg.finished_save}')."
                if cfg.finished_turn is not None
                else ""
            )
            return (
                f"Stand-in guard: the game is on {leader}'s turn (slot {pid}), not "
                f"{cfg.leader}'s.{finished} Refusing to act for another player. If "
                "that player is also absent, call begin_standin_turn with their leader."
            )
        if turn_active != "true":
            return (
                f"Stand-in guard: {cfg.leader} is the local player but their turn "
                "is not active (already ended?). Nothing to do."
            )
        self._ok_for, self._ok_until = cfg.leader, now + _VERIFY_TTL
        return None


def status_text(cfg: StandinConfig) -> str:
    """One-paragraph summary for get_game_overview / hotseat_status."""
    who = cfg.leader_display + (f" ('{cfg.player_name}')" if cfg.player_name else "")
    lines = [
        f"STAND-IN MODE: you are playing {who}'s turn in the live hotseat game. "
        "Every other civilization is a human player. end_turn ends the turn for "
        "real: it hands the game to the next player's turn-change screen and saves "
        "there. Call it exactly once when the turn is done. Do not load other saves."
    ]
    if cfg.note:
        lines.append(f"Player's note: {cfg.note}")
    if cfg.finished_turn is not None:
        lines.append(
            f"This stand-in turn was already ended (turn {cfg.finished_turn}, "
            f"{cfg.finished_at}, saved as '{cfg.finished_save}'); the game is on "
            f"{cfg.next_player or 'the next player'}'s turn-change screen. All game "
            "tools are locked until begin_standin_turn is called again."
        )
    return "\n".join(lines)
