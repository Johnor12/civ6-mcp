"""The hotseat game's players: who may use the Slack bot, which of them is the admin, and whom the turn announcer pings.

Imported by scripts/slack_bot.py and scripts/turn_announcer.py; standard
library only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Player:
    name: str  # hotseat player name in the game
    slack_id: str
    admin: bool = False  # may have any other player's turn played before its timer runs out


# Everyone in the game. They play under their first names, except Jake
# (full name); John's hotseat name is "John" with the quotes, and names
# match without quotes or case.
PLAYERS = (
    Player("Aaron", "U0AN2M2PENR"),  # Aaron Sykes
    Player("Aidan", "U05JN8AA770"),  # Aidan O'Keefe
    Player("Jake DuRegger", "U09E350TA30"),
    Player("Jeffson", "U0A6V0J4QP4"),  # Jeffson Lin
    Player("Mike", "U07UB8TFRNK"),  # Mike Dombrowski
    Player("Cameron", "U08U40V8GCD"),  # Cameron Gonzalez
    Player("Heldi", "U0B1J1T47QC"),  # Heldi Valikaj
    Player("Randy", "U0B7FQ1GJ7N"),  # Randy Schwemmer
    Player("Hunter", "U07G75S7JBS"),  # Hunter Boyd
    Player("John", "U035CAQE34H", admin=True),  # John McGue, runs the game
)
SLACK_PLAYERS = {p.slack_id: p for p in PLAYERS}


def same_name(a: str, b: str) -> bool:
    """Hotseat names compare without surrounding quotes or case: '"John"' is John."""
    def norm(name: str) -> str:
        return name.strip().strip("\"'“”‘’").strip().casefold()

    return norm(a) == norm(b)


def player_named(name: str) -> Player | None:
    return next((p for p in PLAYERS if same_name(p.name, name)), None)
