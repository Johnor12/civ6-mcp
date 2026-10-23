from slackbot.config import Player, Settings, load_players, norm
from slackbot.rules import GameStatus, approval_met, authorize, check_game_ready, due_actions
from slackbot.state import Turn
from slackbot.turn_tracker import ChannelMessage, current_turn, next_in_order

ORDER = ["UA", "UB", "UC"]


def m(ts, text, user="UX"):
    return ChannelMessage(str(ts), user, text)


class TestTracker:
    def test_last_bare_mention_wins(self):
        msgs = [m(1, "<@UA|Aaron>"), m(2, "gg"), m(3, "<@UB> your turn")]
        t = current_turn(msgs, ORDER)
        assert (t.player_id, t.since_ts) == ("UB", "3")

    def test_chatter_and_repeats_do_not_move_the_turn(self):
        msgs = [
            m(1, "<@UA>"),
            m(2, "<@UB> can you check the map when you get a chance, long message here"),
            m(3, "<@UA> <@UB>"),
            m(4, "<@UA> ping"),
            m(5, "<@UZ>"),  # not in the game
        ]
        t = current_turn(msgs, ORDER)
        assert (t.player_id, t.since_ts) == ("UA", "1")

    def test_wraps_and_flags_out_of_order(self):
        t = current_turn([m(1, "<@UC>"), m(2, "<@UA>")], ORDER)
        assert t.player_id == "UA" and not t.anomalies
        t = current_turn([m(1, "<@UA>"), m(2, "<@UC>")], ORDER)
        assert t.player_id == "UC" and len(t.anomalies) == 1

    def test_unsorted_input_and_self_mention(self):
        t = current_turn([m(3, "<@UC>"), m(1, "<@UA>"), m(2, "<@UB>", user="UB")], ORDER)
        assert t.player_id == "UC"

    def test_empty(self):
        assert current_turn([], ORDER).player_id is None

    def test_next_in_order(self):
        assert next_in_order(ORDER, "UC") == "UA"


ME = Player("UA", "Aaron", "Cleopatra")
OTHER = Player("UB", "Bob", "Trajan", aliases=("LEADER_TRAJAN_ALT",))
GAME_OK = GameStatus(
    reachable=True, is_hotseat=True, leader="LEADER_CLEOPATRA", player_name="RAY",
    turn=5, turn_active=True, player_change_visible=True,
)


class TestRules:
    def test_norm_and_aliases(self):
        assert norm("LEADER_CLEOPATRA") == norm("Cleopatra")
        assert OTHER.matches_game("LEADER_TRAJAN_ALT")
        assert ME.matches_game("X", "aaron")  # by player name

    def test_authorize_ok(self):
        from slackbot.turn_tracker import TurnState

        d = authorize("UA", TurnState("UA", "1"), [ME, OTHER], GAME_OK)
        assert d.allowed and d.player == ME

    def test_authorize_refusals(self):
        from slackbot.turn_tracker import TurnState

        turn = TurnState("UA", "1")
        assert "not in the civ game" in authorize("UZ", turn, [ME, OTHER], GAME_OK).reason
        assert "not your turn" in authorize("UB", turn, [ME, OTHER], GAME_OK).reason
        assert "No turn" in authorize("UA", TurnState(), [ME, OTHER], GAME_OK).reason
        wrong = GameStatus(**{**GAME_OK.__dict__, "leader": "LEADER_TRAJAN", "player_name": "Bob2"})
        assert "waits on" in authorize("UA", turn, [ME, OTHER], wrong).reason
        busy = GameStatus(**{**GAME_OK.__dict__, "player_change_visible": False})
        assert "turn-change screen" in authorize("UA", turn, [ME, OTHER], busy).reason
        assert "does not answer" in check_game_ready(ME, GameStatus())

    def test_due_actions(self):
        s = Settings(ping_after_s=100, warn_after_s=200, office_hours=None)
        t = Turn(1, "UA", "1", 1000.0, False, False)
        assert due_actions(1050, t, s) == []
        assert due_actions(1100, t, s) == ["ping"]
        assert due_actions(1200, t, s) == ["ping", "warn"]
        assert due_actions(1200, Turn(1, "UA", "1", 1000.0, True, True), s) == []

    def test_approval_rule(self):
        assert approval_met({"UA"}, "UA", ORDER, 3)  # current player alone
        assert not approval_met({"UB", "UC"}, "UA", ORDER, 3)
        assert approval_met({"UB", "UC", "UA"}, "UA", ORDER, 3)
        assert approval_met({"UB", "UC", "UD"}, "UA", ["UA", "UB", "UC", "UD"], 3)
        assert not approval_met({"UB", "UC", "UZ"}, "UA", ORDER, 3)  # outsiders do not count


def test_real_players_file():
    players = load_players()
    assert len(players) == 10
    assert players[0].name == "Aaron Sykes" and players[-1].name == "John McGue"
    assert len({p.slack_id for p in players}) == 10
