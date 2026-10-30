"""The Slack bot's permissions: who gets the timer, who gets the button, and whose click plays a turn.

slack_bolt and python-dotenv belong to the script's own uv environment, so
they are stubbed when the project venv lacks them. The handlers are driven
through a fake App the way Slack would call them; Claude never runs.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def _stub(name: str, **attrs) -> None:
    try:
        __import__(name)
    except ImportError:
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module


_stub("slack_bolt", App=type("App", (), {"__init__": lambda self, **kwargs: None}))
_stub("slack_bolt.adapter")
_stub("slack_bolt.adapter.socket_mode", SocketModeHandler=object)
_stub("dotenv", dotenv_values=lambda path: {})

import slack_bot  # noqa: E402
import turn_announcer  # noqa: E402
from players import PLAYERS, SLACK_PLAYERS  # noqa: E402
from turn_logger import TURN_LIMIT_S, TurnRecord  # noqa: E402

BOT = "U0CC4C1D4BV"
JOHN = "U035CAQE34H"
AIDAN = "U05JN8AA770"
HELDI = "U0B1J1T47QC"
HALF_HOUR = 1800


def turn(player: str, number: int = 10, leader: str = "LEADER_NZINGA_MBANDE") -> TurnRecord:
    return TurnRecord(datetime(2026, 10, 30, 10, 0), number, leader, player)


def key(rec: TurnRecord) -> str:
    """A Play button's value."""
    return json.dumps(list(rec.key))


class FakeApp:
    def __init__(self) -> None:
        self.events: dict[str, object] = {}
        self.actions: dict[str, object] = {}

    def event(self, name):
        def register(fn):
            self.events[name] = fn
            return fn

        return register

    def action(self, name):
        def register(fn):
            self.actions[name] = fn
            return fn

        return register


class FakeClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []
        self.updates: list[dict] = []
        self.homes: list[tuple[str, dict]] = []  # (user, view) of each views.publish

    def conversations_open(self, users):
        return {"channel": {"id": f"D{users}"}}

    def chat_postMessage(self, channel, text, thread_ts=None, **kwargs):
        self.posts.append((channel, text))
        return {"channel": channel, "ts": "1.0"}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)

    def views_publish(self, user_id, view):
        self.homes.append((user_id, view))


class Bot:
    """Drives the registered handlers as Slack would."""

    def __init__(self, monkeypatch) -> None:
        self.app = FakeApp()
        self.client = FakeClient()
        self.replies: list[tuple[str, dict]] = []
        self.runs: list[tuple[str | None, str]] = []  # (conversation, prompt) of each Claude run
        self.turns: list[tuple[TurnRecord, float]] = []
        self.asked = 0  # whose_turn calls
        self.n = 0
        monkeypatch.setattr(slack_bot, "whose_turn", self._whose_turn)
        monkeypatch.setattr(
            slack_bot,
            "run_with_progress",
            lambda client, channel, thread_ts, prompt, claude: self.runs.append((channel, prompt)),
        )
        monkeypatch.setattr(
            slack_bot, "run_claude", lambda prompt, claude, report: self.runs.append((None, prompt)) or ""
        )
        monkeypatch.setattr(turn_announcer, "announce", lambda client: None)
        slack_bot.register(self.app, "claude", slack_bot.TurnLock())

    def on(self, *turns: tuple[TurnRecord, float]) -> None:
        """What whose_turn answers, in order; the last answer repeats."""
        self.turns = list(turns)

    def _whose_turn(self):
        self.asked += 1
        if not self.turns:
            return None
        return self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]

    def _say(self, text, **kwargs):
        self.replies.append((text, kwargs))

    def _event(self, user: str, text: str, channel: str) -> tuple[dict, dict, dict]:
        self.n += 1
        event = {"user": user, "text": text, "ts": f"{self.n}.0", "channel": channel}
        return {"event_id": f"Ev{self.n}"}, event, types.SimpleNamespace(bot_user_id=BOT)

    def dm(self, user: str, text: str) -> None:
        body, event, context = self._event(user, text, f"D{user}")
        event["channel_type"] = "im"
        self.app.events["message"](body, event, context, self.client, self._say)

    def mention(self, user: str, text: str) -> None:
        body, event, context = self._event(user, f"<@{BOT}> {text}", "C1")
        self.app.events["app_mention"](body, event, context, self.client, self._say)

    def click(self, user: str, value: str, channel: str = "C1") -> None:
        body = {"user": {"id": user}, "message": {"ts": "9.0", "text": "status"}, "channel": {"id": channel}}
        self.app.actions[slack_bot.PLAY_ACTION](lambda: None, body, {"value": value}, self.client, self._say)

    def open_home(self, user: str, tab: str = "home") -> None:
        self.app.events["app_home_opened"]({"user": user, "tab": tab}, self.client)

    def refresh_home(self, user: str) -> None:
        self.app.actions[slack_bot.HOME_REFRESH_ACTION](lambda: None, {"user": {"id": user}}, self.client)

    @property
    def home(self) -> str:
        """The text of the last Home view published, one line per section."""
        user, view = self.client.homes[-1]
        assert view["type"] == "home"
        return "\n".join(b["text"]["text"] for b in view["blocks"] if b["type"] == "section")

    @property
    def last(self) -> str:
        return self.replies[-1][0]

    @property
    def button(self) -> dict | None:
        for block in self.replies[-1][1].get("blocks") or []:
            for element in block.get("elements", []):
                if element.get("action_id") == slack_bot.PLAY_ACTION:
                    return element
        return None


@pytest.fixture
def bot(monkeypatch) -> Bot:
    return Bot(monkeypatch)


def test_only_john_is_admin():
    assert [p.name for p in PLAYERS if p.admin] == ["John"]
    assert SLACK_PLAYERS[JOHN].admin


def test_stranger_is_refused(bot):
    bot.on((turn("Aidan"), HALF_HOUR))
    bot.dm("U000STRANGER", "play Aidan's turn")
    assert bot.last == slack_bot.REJECTION
    assert not bot.runs


def test_rival_before_the_limit_gets_the_timer(bot):
    bot.on((turn("Aidan"), HALF_HOUR))
    bot.dm(HELDI, "whose turn is it")
    assert "Aidan's turn" in bot.last and "Another player can have it played after" in bot.last
    assert bot.button is None and not bot.runs


def test_rival_after_the_limit_gets_the_button(bot):
    rec = turn("Aidan")
    bot.on((rec, TURN_LIMIT_S))
    bot.dm(HELDI, "whose turn is it")
    assert bot.button and bot.button["value"] == key(rec)
    assert not bot.runs


def test_admin_before_the_limit_gets_the_button(bot):
    rec = turn("Aidan")
    bot.on((rec, HALF_HOUR))
    bot.dm(JOHN, "take Aidan's turn")
    assert "as the admin you can have me play it for Aidan now" in bot.last
    assert bot.button and bot.button["value"] == key(rec)
    assert not bot.runs  # nothing the admin wrote reaches Claude


def test_admin_mention_before_the_limit_gets_the_button(bot):
    rec = turn("Aidan")
    bot.on((rec, HALF_HOUR))
    bot.mention(JOHN, "take Aidan's turn")
    assert bot.button and bot.button["value"] == key(rec)
    assert not bot.runs


def test_admin_on_their_own_turn_is_an_own_turn(bot):
    bot.on((turn("John", leader="LEADER_LIME_THULE_DAVE"), HALF_HOUR))
    bot.dm(JOHN, "finish the hunter's house")
    assert bot.runs and "finish the hunter's house" in bot.runs[0][1]
    assert not bot.replies  # nothing is posted before the run


def test_rival_click_before_the_limit_is_refused(bot):
    rec = turn("Aidan")
    bot.on((rec, HALF_HOUR))
    bot.click(HELDI, key(rec))
    assert "Only the admin can have it played before" in bot.last
    assert not bot.runs and not bot.client.updates and not bot.client.posts


def test_admin_click_before_the_limit_plays_the_turn(bot):
    rec = turn("Aidan")
    bot.on((rec, HALF_HOUR), (turn("Jake DuRegger", leader="LEADER_VICTORIA"), 0))
    bot.click(JOHN, key(rec))
    assert bot.client.updates and bot.client.updates[0]["blocks"] == []  # the button is gone
    conversation, prompt = bot.runs[0]
    assert conversation == f"D{AIDAN}"
    assert "by John, the game's admin" in prompt and slack_bot.ADMIN_NOTE in prompt
    assert "timer ran out" not in prompt and "take Aidan's turn" not in prompt
    assert any("John, the game's admin, asked me" in text for channel, text in bot.client.posts if channel == f"D{AIDAN}")
    assert "as you, the admin, asked" in bot.replies[0][0]
    assert bot.last == "Done: Aidan's turn 10 is over. It's now Jake DuRegger's turn."


def test_rival_click_after_the_limit_plays_the_timed_out_turn(bot):
    rec = turn("Aidan")
    bot.on((rec, TURN_LIMIT_S), (turn("Jake DuRegger", leader="LEADER_VICTORIA"), 0))
    bot.click(HELDI, key(rec))
    conversation, prompt = bot.runs[0]
    assert conversation == f"D{AIDAN}"
    assert "because a turn timer ran out" in prompt and slack_bot.TIMED_OUT_NOTE in prompt
    assert "admin" not in prompt
    assert bot.last == "Done: Aidan's turn 10 is over. It's now Jake DuRegger's turn."


def test_admin_click_after_the_limit_is_an_ordinary_timed_out_turn(bot):
    rec = turn("Aidan")
    bot.on((rec, TURN_LIMIT_S))
    bot.click(JOHN, key(rec))
    assert "because a turn timer ran out" in bot.runs[0][1]
    assert "admin" not in bot.runs[0][1]
    assert "was not finished" in bot.last  # whose_turn still answers the same turn


def test_click_on_a_turn_that_ended_plays_nothing(bot):
    bot.on((turn("Jake DuRegger", leader="LEADER_VICTORIA"), 0))
    bot.click(JOHN, key(turn("Aidan")))
    assert "That turn is over" in bot.last
    assert not bot.runs and not bot.client.updates


def test_owner_click_is_sent_to_dm(bot):
    rec = turn("Aidan")
    bot.on((rec, HALF_HOUR))
    bot.click(AIDAN, key(rec))
    assert bot.last == "It's your turn: DM me what you want done with it."
    assert not bot.runs


def test_home_tab_tells_the_owner_to_dm(bot):
    bot.on((turn("Aidan"), HALF_HOUR))
    bot.open_home(AIDAN)
    assert bot.client.homes[-1][0] == AIDAN
    assert "It's your turn (Nzinga Mbande, turn 10)" in bot.home and "DM me what you want done" in bot.home
    assert not bot.runs and not bot.replies


def test_home_tab_gives_a_rival_the_timer(bot):
    bot.on((turn("Aidan"), HALF_HOUR))
    bot.open_home(HELDI)
    assert "It's Aidan's turn" in bot.home and "so in 1h 30m. DM me then for the button" in bot.home


def test_home_tab_after_the_limit_points_a_rival_to_the_button(bot):
    bot.on((turn("Aidan"), TURN_LIMIT_S))
    bot.open_home(HELDI)
    assert "past the 2h limit: DM me for a button" in bot.home


def test_home_tab_tells_the_admin_about_the_button(bot):
    bot.on((turn("Aidan"), HALF_HOUR))
    bot.open_home(JOHN)
    assert "as the admin you can DM me for a button" in bot.home


def test_home_tab_without_a_game(bot):
    bot.open_home(HELDI)
    assert bot.home == slack_bot.NO_GAME


def test_home_tab_refuses_a_stranger_without_asking_the_game(bot):
    bot.on((turn("Aidan"), HALF_HOUR))
    bot.open_home("U000STRANGER")
    assert bot.home == slack_bot.REJECTION
    assert not bot.asked  # whose_turn was not called


def test_messages_tab_publishes_nothing(bot):
    bot.on((turn("Aidan"), HALF_HOUR))
    bot.open_home(HELDI, tab="messages")
    assert not bot.client.homes


def test_home_refresh_republishes(bot):
    bot.on((turn("Aidan"), HALF_HOUR))
    bot.refresh_home(HELDI)
    assert len(bot.client.homes) == 1 and "It's Aidan's turn" in bot.home


def test_home_tab_shows_a_run_in_progress():
    view = slack_bot.home_view(SLACK_PLAYERS[HELDI], (turn("Aidan"), HALF_HOUR), slack_bot.Run(JOHN, 0.0))
    text = "\n".join(b["text"]["text"] for b in view["blocks"] if b["type"] == "section")
    assert "It's Aidan's turn" in text and f"Busy with the run <@{JOHN}> started" in text
