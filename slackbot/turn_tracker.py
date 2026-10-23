"""Work out whose turn it is from the @-mentions in the channel."""

from __future__ import annotations

import re
from dataclasses import dataclass

MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")


@dataclass(frozen=True)
class ChannelMessage:
    ts: str
    user: str | None
    text: str


@dataclass(frozen=True)
class TurnState:
    player_id: str | None = None
    since_ts: str | None = None  # ts of the message that started this turn
    anomalies: tuple[str, ...] = ()


def next_in_order(order: list[str], player_id: str) -> str:
    return order[(order.index(player_id) + 1) % len(order)]


def turn_target(text: str, order: list[str], max_extra_chars: int) -> str | None:
    """The player a "your turn" message names, or None if it is not one.

    Only short messages with exactly one turn-order @-mention count; chatter
    that mentions people does not move the turn.
    """
    mentioned = [m for m in MENTION_RE.findall(text) if m in order]
    if len(set(mentioned)) != 1:
        return None
    rest = MENTION_RE.sub("", text).strip()
    return mentioned[0] if len(rest) <= max_extra_chars else None


def current_turn(
    messages: list[ChannelMessage], order: list[str], max_extra_chars: int = 40
) -> TurnState:
    """Replay channel messages oldest first; the last valid @ is the current turn."""
    player: str | None = None
    since: str | None = None
    anomalies: list[str] = []
    for msg in sorted(messages, key=lambda m: float(m.ts)):
        target = turn_target(msg.text, order, max_extra_chars)
        if target is None or target == player or msg.user == target:
            continue
        if player is not None and target != next_in_order(order, player):
            anomalies.append(f"{msg.ts}: {target} followed {player} out of order")
        player, since = target, msg.ts
    return TurnState(player, since, tuple(anomalies))
