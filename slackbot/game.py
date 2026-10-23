"""Ask the running game whose turn-change screen is up."""

from __future__ import annotations

import logging
from typing import Protocol

from slackbot.rules import GameStatus

log = logging.getLogger(__name__)


class GameChecker(Protocol):
    async def status(self) -> GameStatus: ...


class BridgeGameChecker:
    """Reads game state through the repo's own bridge, without a Claude run.

    Each check opens a new connection. Never call it during a run: the mod
    then skips the agent's commands (see CivBot._probe_game).
    """

    async def status(self) -> GameStatus:
        from civ_mcp import hotseat
        from civ_mcp.bridge import BridgeConnection

        conn = BridgeConnection()
        try:
            st = await hotseat.status(conn)
        except Exception as e:  # noqa: BLE001 - a failed probe means "not ready"
            log.warning("game status probe failed: %s", e)
            return GameStatus(description=f"probe failed: {e}")
        finally:
            await conn.disconnect()
        return GameStatus(
            reachable=st.screen == "ingame",
            is_hotseat=st.is_hotseat,
            leader=st.leader,
            player_name=st.player_name,
            turn=st.turn,
            turn_active=st.turn_active,
            player_change_visible=st.player_change_visible,
            description=st.describe(),
        )
