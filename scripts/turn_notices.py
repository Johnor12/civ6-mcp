"""Timer notices and takeover approvals for the current turn.

The Slack bot posts a ping at PING_AFTER_S of workday time and, at
TURN_LIMIT_S, a request that players can approve so Claude plays the turn.
This module keeps that state in a small JSON file, so a bot restart does not
repeat a ping or lose approvals. Standard library only.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("turn_notices")

NOTICES_PATH = Path(__file__).resolve().parent / "data" / "turn_notices.json"
PING_AFTER_S = 1 * 3600
APPROVALS_NEEDED = 3


@dataclass
class Notice:
    key: str  # "<turn>|<leader>|<player>": one record per turn
    pinged: bool = False
    warned: bool = False
    request_ts: str = ""  # the channel message with the Approve button
    approvers: list[str] = field(default_factory=list)


def turn_key(turn: int, leader: str, player: str) -> str:
    return f"{turn}|{leader}|{player}"


def due_actions(notice: Notice, worked_s: float, limit_s: float, ping_s: float = PING_AFTER_S) -> list[str]:
    """Timer actions ("ping", "warn") that are due and not done yet."""
    actions = []
    if worked_s >= ping_s and not notice.pinged:
        actions.append("ping")
    if worked_s >= limit_s and not notice.warned:
        actions.append("warn")
    return actions


def approval_met(approvers: set[str], owner_id: str | None, needed: int = APPROVALS_NEEDED) -> bool:
    """The turn's owner alone, or `needed` different players, may start a takeover."""
    return (owner_id is not None and owner_id in approvers) or len(approvers) >= needed


class TurnNotices:
    """The notice for the current turn, saved to disk. Safe across threads."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or NOTICES_PATH
        self._lock = threading.Lock()

    def _load(self, key: str) -> Notice:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("key") == key:
                known = Notice.__dataclass_fields__
                return Notice(**{k: v for k, v in raw.items() if k in known})
        except (OSError, ValueError, TypeError):
            pass
        return Notice(key=key)

    def _save(self, notice: Notice) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(notice)), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not save %s: %s", self.path, e)

    def get(self, key: str) -> Notice:
        with self._lock:
            return self._load(key)

    def update(self, key: str, **changes) -> Notice:
        with self._lock:
            notice = self._load(key)
            for name, value in changes.items():
                setattr(notice, name, value)
            self._save(notice)
            return notice

    def add_approval(self, key: str, user_id: str) -> Notice:
        with self._lock:
            notice = self._load(key)
            if user_id not in notice.approvers:
                notice.approvers.append(user_id)
            self._save(notice)
            return notice

    def clear_approvals(self, key: str) -> None:
        self.update(key, approvers=[])
