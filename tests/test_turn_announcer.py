"""The turn announcer: who gets @'d in the game channel, when, and never twice."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import turn_announcer  # noqa: E402
import turn_logger  # noqa: E402

MONDAY = datetime(2026, 10, 26)
RANDY = "U0B7FQ1GJ7N"
HELDI = "U0B1J1T47QC"
BOT_USER, BOT_ID = "U0CC4C1D4BV", "B0CD50C6MDW"


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    """Day 0 is Monday 26 Oct 2026."""
    return MONDAY + timedelta(days=day, hours=hour, minutes=minute)


class FakeSlack:
    """conversations.history, chat.postMessage and auth.test on one channel, two messages a page."""

    PAGE = 2

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.posts: list[str] = []
        self.history_calls = 0
        self.fail_history = 0
        self.now = MONDAY

    def say(self, at: datetime, text: str, user: str = HELDI) -> None:
        self.messages.append({"ts": f"{at.timestamp():.6f}", "text": text, "user": user})

    def conversations_history(self, channel, oldest, limit, cursor=None, inclusive=False):
        assert channel == turn_announcer.CHANNEL
        self.history_calls += 1
        if self.fail_history:
            self.fail_history -= 1
            raise ConnectionError("slack is down")
        msgs = sorted(
            (m for m in self.messages if float(m["ts"]) > float(oldest) or (inclusive and float(m["ts"]) == float(oldest))),
            key=lambda m: -float(m["ts"]),
        )
        start = int(cursor or 0)
        more = start + self.PAGE < len(msgs)
        return {
            "messages": msgs[start : start + self.PAGE],
            "response_metadata": {"next_cursor": str(start + self.PAGE) if more else ""},
        }

    def chat_postMessage(self, channel, text):
        assert channel == turn_announcer.CHANNEL
        self.posts.append(text)
        self.messages.append({"ts": f"{self.now.timestamp():.6f}", "text": text, "user": BOT_USER, "bot_id": BOT_ID})
        return {"ok": True}

    def auth_test(self):
        return {"user_id": BOT_USER, "bot_id": BOT_ID}


@pytest.fixture
def slack(tmp_path, monkeypatch) -> FakeSlack:
    monkeypatch.setattr(turn_logger, "TURN_LOG_PATH", tmp_path / "turn_log.jsonl")
    monkeypatch.setattr(turn_logger, "log_current_turn", lambda path=None: True)  # no game
    monkeypatch.setattr(turn_announcer, "LOCK_PATH", tmp_path / "turn_announcer.lock")
    monkeypatch.setattr(turn_announcer, "_sent", set())
    monkeypatch.setattr(turn_announcer, "_bot_ids", None)
    return FakeSlack()


def _turn(at: datetime, player: str = "Randy", turn: int = 3, leader: str = "LEADER_GENGHIS_KHAN") -> None:
    turn_logger.record(turn, leader, player, at)


def _check(slack: FakeSlack, at: datetime) -> bool:
    slack.now = at
    return turn_announcer.announce(slack, now=at)


class TestTurnStart:
    def test_new_turn_pings_the_player_once(self, slack):
        _turn(_at(0, 10))
        assert _check(slack, _at(0, 10, 1))
        assert slack.posts == [f"<@{RANDY}> it's your turn: Genghis Khan, turn 3. Play it at the PC, or DM me your note and I'll play it for you."]
        calls = slack.history_calls
        assert not _check(slack, _at(0, 10, 6))
        assert slack.history_calls == calls  # remembered, the channel is not read again

    def test_a_ping_by_another_player_counts(self, slack):
        # The finishing player pinged Randy before the logger saw the turn change.
        slack.say(_at(0, 9, 59), f"<@{RANDY}|Randy Schwemmer>")
        _turn(_at(0, 10))
        assert not _check(slack, _at(0, 10, 1))
        assert slack.posts == []

    def test_a_mention_from_an_earlier_turn_does_not_count(self, slack):
        slack.say(_at(0, 9), f"<@{RANDY}>")
        _turn(_at(0, 10))
        assert _check(slack, _at(0, 10, 1))

    def test_a_mention_on_a_later_page_counts(self, slack):
        _turn(_at(0, 10))
        slack.say(_at(0, 10), f"<@{RANDY}> go")
        for minute in range(1, 6):
            slack.say(_at(0, 10, minute), "chatter")
        assert not _check(slack, _at(0, 10, 6))
        assert slack.history_calls == 3

    def test_next_player_is_pinged_when_the_turn_changes(self, slack):
        _turn(_at(0, 10))
        assert _check(slack, _at(0, 10, 1))
        _turn(_at(0, 10, 30), player="Heldi", leader="LEADER_GITARJA")
        assert _check(slack, _at(0, 10, 30))
        assert slack.posts[-1].startswith(f"<@{HELDI}> it's your turn: Gitarja, turn 3.")

    def test_a_player_not_in_players_is_skipped(self, slack):
        _turn(_at(0, 10), player="Player 1")
        assert not _check(slack, _at(0, 10, 1))
        assert slack.posts == []


class TestReminders:
    def test_one_hour_then_two_hours(self, slack):
        _turn(_at(0, 10))
        assert _check(slack, _at(0, 10, 1))
        assert not _check(slack, _at(0, 10, 55))
        assert _check(slack, _at(0, 11))
        assert slack.posts[-1].startswith(f"<@{RANDY}> reminder: your turn (Genghis Khan, turn 3) has been waiting 1h")
        assert not _check(slack, _at(0, 11, 5))
        assert _check(slack, _at(0, 12))
        assert "any other player can now have it played for you through this app" in slack.posts[-1]
        assert not _check(slack, _at(0, 14))
        assert len(slack.posts) == 3

    def test_only_the_bots_own_mention_counts_as_a_reminder(self, slack):
        _turn(_at(0, 10))
        slack.say(_at(0, 10), f"<@{RANDY}>")
        assert not _check(slack, _at(0, 10, 1))
        slack.say(_at(0, 11, 2), f"<@{RANDY}> hurry up")
        assert _check(slack, _at(0, 11, 5))
        assert "reminder" in slack.posts[-1]

    def test_a_restart_sends_nothing_twice(self, slack, monkeypatch):
        _turn(_at(0, 10))
        assert _check(slack, _at(0, 10, 1))
        assert _check(slack, _at(0, 11))
        monkeypatch.setattr(turn_announcer, "_sent", set())
        assert not _check(slack, _at(0, 11, 5))
        assert len(slack.posts) == 2

    def test_a_turn_found_late_gets_only_the_latest_message(self, slack):
        _turn(_at(0, 10))
        assert _check(slack, _at(0, 12, 30))
        assert len(slack.posts) == 1 and "through this app" in slack.posts[0]

    def test_the_clock_runs_in_workday_hours_only(self, slack):
        _turn(_at(4, 16, 30))  # Friday afternoon
        assert _check(slack, _at(4, 16, 31))
        assert not _check(slack, _at(7, 9, 25))  # Monday: 55 min so far
        assert _check(slack, _at(7, 9, 30))
        assert "reminder" in slack.posts[-1]


class TestFailures:
    def test_a_slack_error_is_retried_on_the_next_check(self, slack):
        _turn(_at(0, 10))
        slack.fail_history = 1
        assert not _check(slack, _at(0, 10, 1))
        assert _check(slack, _at(0, 10, 6))
        assert len(slack.posts) == 1

    def test_an_empty_turn_log_does_nothing(self, slack):
        assert not _check(slack, _at(0, 10))
        assert slack.history_calls == 0
