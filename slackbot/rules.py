"""Pure decisions: who may start a run, which timer actions are due, approvals."""

from __future__ import annotations

from dataclasses import dataclass

from slackbot.config import Player, Settings
from slackbot.state import Turn
from slackbot.turn_tracker import TurnState


@dataclass(frozen=True)
class GameStatus:
    """What the game reports; built from civ_mcp.hotseat.HotseatStatus."""

    reachable: bool = False
    is_hotseat: bool = False
    leader: str = ""
    player_name: str = ""
    turn: int = 0
    turn_active: bool = False
    player_change_visible: bool = False
    description: str = ""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    player: Player | None = None


def check_game_ready(player: Player, game: GameStatus) -> str | None:
    """None if the game waits on this player's turn-change screen, else why not."""
    if not game.reachable:
        return "The game does not answer. Check that Civ 6 runs with the MCP Bridge mod."
    if not game.is_hotseat:
        return "The game is not in a hotseat session."
    if not player.matches_game(game.leader, game.player_name):
        return (
            f"The game waits on {game.leader or 'another player'} "
            f"('{game.player_name}'), not on {player.name}."
        )
    if not game.turn_active or not game.player_change_visible:
        return (
            f"{player.name}'s turn-change screen is not showing "
            "(a person may be playing this turn on the machine)."
        )
    return None


def authorize(
    slack_user: str, turn: TurnState, players: list[Player], game: GameStatus
) -> Decision:
    """A DM may start a run only if the sender owns the current turn in Slack and in the game."""
    by_id = {p.slack_id: p for p in players}
    me = by_id.get(slack_user)
    if me is None:
        return Decision(False, "You are not in the civ game.")
    if turn.player_id is None:
        return Decision(False, "No turn found in the channel yet.")
    if turn.player_id != slack_user:
        current = by_id[turn.player_id].name
        return Decision(False, f"It is not your turn. It is {current}'s turn.")
    why_not = check_game_ready(me, game)
    if why_not:
        return Decision(False, why_not)
    return Decision(True, player=me)


def due_actions(now: float, turn: Turn, settings: Settings) -> list[str]:
    """Timer actions ("ping", "warn") that are due and not yet done."""
    age = now - turn.started_at
    actions = []
    if age >= settings.ping_after_s and not turn.pinged:
        actions.append("ping")
    if age >= settings.warn_after_s and not turn.warned:
        actions.append("warn")
    return actions


def approval_met(
    approvers: set[str], current_player_id: str, order_ids: list[str], needed: int
) -> bool:
    """The current player alone is enough; otherwise `needed` distinct turn-order members."""
    valid = approvers & set(order_ids)
    return current_player_id in valid or len(valid) >= needed
