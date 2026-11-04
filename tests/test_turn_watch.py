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


AARON_18 = (18, "LEADER_CLEOPATRA", "Aaron")


def _aaron_playing(game_log, watch):
    """The game launched and loaded, Aaron's turn 18 begins and he presses Start Turn; the watch has read it all."""
    game_log.relaunch()
    game_log.load()
    game_log.turn_line(T0.replace(hour=9), 17, "LEADER_LIME_THULE_DAVE", "John", slot=9)
    game_log.screen_line(18)
    game_log.turn_line(T0, 18, "LEADER_CLEOPATRA", "Aaron")
    game_log.start_turn(0)
    watch.read()
    watch.reloads.clear()


def test_turn_lines_without_a_load_are_not_reloads(game_log):
    watch = TurnWatch(game_log.path)
    game_log.write("Test.LUA loaded!")
    game_log.turn_line(T0, 18, "LEADER_CLEOPATRA", "Aaron")
    game_log.start_turn(0)
    game_log.screen_line(18)  # an end of turn the game interrupted: the same screen again, no load
    game_log.turn_line(T0.replace(minute=30), 18, "LEADER_CLEOPATRA", "Aaron")
    assert [s.reload for s in watch.read()] == [None, None] and watch.reloads == []


def test_a_relaunch_back_to_the_start_of_a_turn_already_played(game_log):
    # 3 Nov 2026: Aaron's turn 18 began at 16:00; Civ 6 was launched again at 17:13 and his
    # turn-18 save, picked in the Load Game menu, put the game back on his turn-change screen.
    watch = TurnWatch(game_log.path)
    _aaron_playing(game_log, watch)
    game_log.relaunch()
    game_log.load()
    game_log.screen_line(18)
    game_log.turn_line(T0.replace(hour=17), 18, "LEADER_CLEOPATRA", "Aaron")
    (start,) = watch.read()
    assert start.key == AARON_18 and not start.loaded
    assert start.reload == turn_watch.Reload(relaunched=True, menu=True, had_started=True)
    assert watch.reloads == [start]


def test_a_load_in_game_into_the_middle_of_the_turn(game_log):
    watch = TurnWatch(game_log.path)
    _aaron_playing(game_log, watch)
    game_log.load()
    game_log.turn_line(T0.replace(minute=50), 18, "LEADER_CLEOPATRA", "Aaron", why="load")
    (start,) = watch.read()
    assert start.loaded and start.reload == turn_watch.Reload(relaunched=False, menu=True, had_started=True)


def test_a_load_before_the_player_started_and_by_the_tooling(game_log):
    watch = TurnWatch(game_log.path)
    game_log.turn_line(T0, 18, "LEADER_CLEOPATRA", "Aaron")
    watch.read()
    game_log.relaunch()  # say, the game closed overnight on Aaron's turn-change screen
    game_log.load(menu=False)
    game_log.turn_line(T0.replace(hour=17), 18, "LEADER_CLEOPATRA", "Aaron")
    (start,) = watch.read()
    assert start.reload == turn_watch.Reload(relaunched=True, menu=False, had_started=False)


def test_start_turn_counts_only_for_the_slot_whose_turn_it_is(game_log):
    watch = TurnWatch(game_log.path)
    game_log.turn_line(T0, 18, "LEADER_CLEOPATRA", "Aaron")
    game_log.start_turn(1)
    game_log.load()
    game_log.turn_line(T0.replace(minute=50), 18, "LEADER_CLEOPATRA", "Aaron")
    assert watch.read()[-1].reload.had_started is False


def test_a_first_read_takes_the_launch_load_with_the_turn_unseen(game_log):
    # The watch starts after the game did: the turn began before the launch, out of its sight.
    game_log.relaunch()
    game_log.load()
    game_log.turn_line(T0, 18, "LEADER_CLEOPATRA", "Aaron")
    game_log.start_turn(0)
    game_log.load()
    game_log.turn_line(T0.replace(minute=50), 18, "LEADER_CLEOPATRA", "Aaron", why="load")
    first, second = TurnWatch(game_log.path).read()
    assert first.reload == turn_watch.Reload(relaunched=True, menu=True, had_started=None)
    assert second.reload == turn_watch.Reload(relaunched=False, menu=True, had_started=True)


def test_a_load_that_never_finished_marks_nothing(game_log):
    watch = TurnWatch(game_log.path)
    game_log.load()
    game_log.turn_line(T0, 18, "LEADER_CLEOPATRA", "Aaron")
    watch.read()
    game_log.write("LoadGameMenu: LoadGameMenu::OnLoadYes() leaving the network session.")  # and it failed
    game_log.turn_line(T0.replace(minute=50), 18, "LEADER_NZINGA_MBANDE", "Aidan", slot=1)
    assert [s.reload for s in watch.read()] == [None]
    game_log.load(menu=False)  # a later load by the tooling is not taken for a menu pick
    game_log.turn_line(T0.replace(minute=55), 18, "LEADER_NZINGA_MBANDE", "Aidan", slot=1)
    assert watch.read()[0].reload.menu is False
