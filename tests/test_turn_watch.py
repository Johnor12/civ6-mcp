"""civ_mcp.turn_watch: whose turn it is from the bridge mod's turn lines in Lua.log."""

from __future__ import annotations

from datetime import datetime

from civ_mcp import turn_watch
from civ_mcp.turn_watch import TurnStart, TurnWatch

T0 = datetime(2026, 11, 3, 16, 20, 5)


def test_parse_a_turn_line():
    start = turn_watch.parse(
        f"MCPBridge_InGame: MCPB|0|T|{int(T0.timestamp())}|18|1|LEADER_NZINGA_MBANDE|Aidan|begin"
    )
    assert start == TurnStart(T0, 18, 1, "LEADER_NZINGA_MBANDE", "Aidan")
    assert start.key == (18, "LEADER_NZINGA_MBANDE", "Aidan")
    assert start.leader_display == "Nzinga Mbande"


def test_parse_names_with_spaces_and_quotes_and_a_load():
    start = turn_watch.parse(f'MCPBridge_InGame: MCPB|0|T|{int(T0.timestamp())}|18|2|LEADER_VICTORIA|"Jake DuRegger"|load\r')
    assert (start.player, start.loaded) == ('"Jake DuRegger"', True)


def test_a_turn_line_printed_inside_a_bridge_command_still_counts():
    # Should the event fire while a command runs, the bridge's print wraps the line.
    line = f"MCPBridge_InGame: MCPB|1793740441034|O|MCPB|0|T|{int(T0.timestamp())}|18|0|LEADER_CLEOPATRA|Aaron|begin"
    assert turn_watch.parse(line).player == "Aaron"


def test_other_lines_are_not_turn_lines():
    for line in (
        "MCPBridge_InGame: MCPB|1793740441034|O|HS|18|0|LEADER_CLEOPATRA|Aaron|true|true|true|true|true|true|false",
        "MCPBridge_InGame: MCPB|0|S|MCP Bridge v3 started in InGame",
        "PlayerChange: BuildTurnControls: CurrentGameTurn=18",
        "MCPBridge_InGame: MCPB|0|T|notatime|18|0|LEADER_CLEOPATRA|Aaron|begin",
    ):
        assert turn_watch.parse(line) is None


def test_first_read_takes_the_whole_log_then_only_what_is_new(game_log):
    game_log.write("Test.LUA loaded!", "PlayerChange: BuildTurnControls: CurrentGameTurn=17")
    game_log.turn_line(T0, 17, "LEADER_AMANITORE", "Hunter", slot=8)
    watch = TurnWatch(game_log.path)
    assert [s.player for s in watch.read()] == ["Hunter"] and watch.screens == 1
    assert watch.read() == [] and watch.screens == 1
    game_log.screen_line(17)
    game_log.turn_line(T0.replace(minute=25), 17, "LEADER_LIME_THULE_DAVE", "John", slot=9)
    assert [s.player for s in watch.read()] == ["John"] and watch.screens == 2


def test_a_line_cut_off_mid_write_is_read_once_it_is_complete(game_log):
    watch = TurnWatch(game_log.path)
    line = f"MCPBridge_InGame: MCPB|0|T|{int(T0.timestamp())}|18|0|LEADER_CLEOPATRA|Aaron|begin"
    with open(game_log.path, "ab") as f:
        f.write(line[:30].encode())
    assert watch.read() == []
    with open(game_log.path, "ab") as f:
        f.write(line[30:].encode() + b"\r\n")
    assert [s.player for s in watch.read()] == ["Aaron"]


def test_latest_is_the_last_turn_line_read(game_log):
    watch = TurnWatch(game_log.path)
    watch.read()
    assert watch.latest is None
    game_log.turn_line(T0, 18, "LEADER_CLEOPATRA", "Aaron")
    game_log.turn_line(T0.replace(minute=40), 18, "LEADER_NZINGA_MBANDE", "Aidan", slot=1)
    game_log.turn_line(T0.replace(minute=50), 18, "LEADER_CLEOPATRA", "Aaron", why="load")  # back to Aaron's save
    watch.read()
    assert (watch.latest.player, watch.latest.loaded) == ("Aaron", True)


def test_a_restarted_game_is_read_from_the_top(game_log):
    watch = TurnWatch(game_log.path)
    game_log.write("x" * 500)
    game_log.turn_line(T0, 18, "LEADER_CLEOPATRA", "Aaron")
    assert [s.player for s in watch.read()] == ["Aaron"]
    game_log.path.write_bytes(b"")  # Civ 6 launched again
    game_log.turn_line(T0.replace(hour=17), 18, "LEADER_CLEOPATRA", "Aaron", why="load")
    assert [(s.player, s.loaded) for s in watch.read()] == [("Aaron", True)]


def test_default_is_one_watch_per_process():
    assert turn_watch.default() is turn_watch.default()
