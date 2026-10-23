"""Players and settings."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
PLAYERS_PATH = Path(__file__).resolve().parent / "players.toml"


def norm(s: str) -> str:
    """Compare leader and player names across spellings (LEADER_X, 'x', 'X ')."""
    s = re.sub(r"^leader_", "", s.strip().lower())
    return re.sub(r"[^a-z0-9]", "", s)


@dataclass(frozen=True)
class Player:
    slack_id: str
    name: str
    leader: str
    aliases: tuple[str, ...] = ()

    def matches_game(self, game_leader: str, game_player_name: str = "") -> bool:
        """True if the game's leader or player name is this player's."""
        known = {norm(self.leader), norm(self.name), *(norm(a) for a in self.aliases)}
        known.discard("")
        return norm(game_leader) in known or (
            bool(game_player_name) and norm(game_player_name) in known
        )


def load_players(path: Path = PLAYERS_PATH) -> list[Player]:
    """Players in turn order."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    players = [
        Player(
            slack_id=p["slack_id"],
            name=p["name"],
            leader=p["leader"],
            aliases=tuple(p.get("aliases", ())),
        )
        for p in raw["players"]
    ]
    ids = [p.slack_id for p in players]
    if len(set(ids)) != len(ids):
        raise ValueError("players.toml has a duplicate slack_id")
    return players


@dataclass(frozen=True)
class Settings:
    channel_id: str = "C09AZ6CR7QE"  # #proj-diplomatic-relations
    # Channel history before this ts is ignored (the first turn @ of the game).
    history_start_ts: str = "1792694019.399939"
    repo_dir: Path = REPO_DIR
    db_path: Path = REPO_DIR / "slackbot" / "bot.db"
    claude_bin: str = "claude"
    run_timeout_s: float = 25 * 60
    max_agent_turns: int = 300
    ping_after_s: float = 60 * 60
    warn_after_s: float = 2 * 60 * 60
    approvals_needed: int = 3
    approval_ttl_s: float = 30 * 60
    poll_interval_s: float = 30
    bare_mention_max_chars: int = 40
    max_upload_mb: int = 200
    extra_env: dict[str, str] = field(default_factory=dict)


def settings_from_env(env: dict[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    d = Settings()
    return Settings(
        channel_id=e.get("CIV_CHANNEL_ID", d.channel_id),
        history_start_ts=e.get("CIV_HISTORY_START_TS", d.history_start_ts),
        repo_dir=Path(e["CIV_REPO_DIR"]) if "CIV_REPO_DIR" in e else d.repo_dir,
        db_path=Path(e["CIV_BOT_DB"]) if "CIV_BOT_DB" in e else d.db_path,
        claude_bin=e.get("CLAUDE_BIN", d.claude_bin),
        run_timeout_s=float(e.get("CIV_RUN_TIMEOUT_S", d.run_timeout_s)),
        max_agent_turns=int(e.get("CIV_MAX_AGENT_TURNS", d.max_agent_turns)),
        ping_after_s=float(e.get("CIV_PING_AFTER_S", d.ping_after_s)),
        warn_after_s=float(e.get("CIV_WARN_AFTER_S", d.warn_after_s)),
        approvals_needed=int(e.get("CIV_APPROVALS_NEEDED", d.approvals_needed)),
        approval_ttl_s=float(e.get("CIV_APPROVAL_TTL_S", d.approval_ttl_s)),
        poll_interval_s=float(e.get("CIV_POLL_INTERVAL_S", d.poll_interval_s)),
    )
