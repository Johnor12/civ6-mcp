"""Whose hotseat turn it is, read from the game's Lua.log instead of asked of the game.

The MCP Bridge mod (v3, mods/MCPBridge/UI/MCPBridge_InGame.lua) prints a
turn line whenever a hotseat turn starts, from the game's own
LocalPlayerTurnBegin event, and when a save is loaded into a turn already
under way:

    MCPBridge_InGame: MCPB|0|T|<unix time>|<turn>|<slot>|<leader type>|<player name>|begin
    MCPBridge_InGame: MCPB|0|T|<unix time>|<turn>|<slot>|<leader type>|<player name>|load

The game itself prints "PlayerChange: BuildTurnControls: CurrentGameTurn=<n>"
at the same moment, without saying whose turn it is; a TurnWatch counts
those as `screens`. Before turn lines existed, that line fell between every
pair of whose_turn answers that named different players, 143 of 143 from
27 Oct to 3 Nov 2026.

A TurnWatch follows Lua.log the way the bridge does (bridge._LogTail): its
first read takes the whole log, later reads only what was written since,
and a log the game truncated on launch is read again from the start. The
player name and leader type are the ones hotseat.whose_turn reports, so a
TurnStart's key matches a turn_logger.TurnRecord's.

Lua.log is a feed, not a record: Civ 6 empties it every time it is
launched and keeps no copy. The turn logger (scripts/turn_logger.py) is
its one reader here: it copies each turn line into
scripts/data/turn_log.jsonl within a second, and the other services read
that file. default() is the watch of the process. Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from civ_mcp.bridge import _LogTail, default_lua_log

TURN_LINE = re.compile(
    r"MCPB\|0\|T\|(?P<at>\d+)\|(?P<turn>-?\d+)\|(?P<slot>-?\d+)\|(?P<leader>[^|]*)\|(?P<player>[^|]*)\|(?P<why>begin|load)\s*$"
)
SCREEN_LINE = re.compile(r"^PlayerChange: BuildTurnControls: CurrentGameTurn=\d+")


@dataclass(frozen=True)
class TurnStart:
    """One turn line: a hotseat turn that started, or a save loaded into one."""

    at: datetime  # local time, naive; the game's clock, to the second
    turn: int
    slot: int  # the player's slot in the game, 0 for the first seat
    leader: str  # e.g. LEADER_POUNDMAKER
    player: str  # hotseat player name shown on the turn-change screen
    loaded: bool = False  # a save loaded into the turn, not the turn beginning

    @property
    def key(self) -> tuple[int, str, str]:
        return self.turn, self.leader, self.player

    @property
    def leader_display(self) -> str:
        return self.leader.replace("LEADER_", "").replace("_", " ").title()


def parse(line: str) -> TurnStart | None:
    """The turn line in a line of Lua.log, or None."""
    if "|T|" not in line:
        return None
    m = TURN_LINE.search(line)
    if m is None:
        return None
    return TurnStart(
        at=datetime.fromtimestamp(int(m["at"])),
        turn=int(m["turn"]),
        slot=int(m["slot"]),
        leader=m["leader"],
        player=m["player"],
        loaded=m["why"] == "load",
    )


class TurnWatch:
    """Follows Lua.log for turn lines and the game's turn-change screen lines."""

    def __init__(self, path: Path | None = None):
        self._tail = _LogTail(path or default_lua_log())
        self.latest: TurnStart | None = None  # the last turn line read
        self.screens = 0  # the game's own turn-change screen lines read

    def read(self) -> list[TurnStart]:
        """The turn lines written since the last read, oldest first; the whole log's on the first read."""
        starts = []
        for line in self._tail.read_lines():
            start = parse(line)
            if start is not None:
                self.latest = start
                starts.append(start)
            elif SCREEN_LINE.search(line):
                self.screens += 1
        return starts


_default: TurnWatch | None = None


def default() -> TurnWatch:
    """This process's watch of the game's Lua.log."""
    global _default
    if _default is None:
        _default = TurnWatch()
    return _default

