"""Slack bot flows: pings, takeover approvals, results and screenshots (fake Slack, no game).

Needs slack-bolt and python-dotenv, which the bot's own inline script
metadata installs: uv run --with slack-bolt --with python-dotenv pytest
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

if sys.platform != "win32":  # civ_mcp.bridge imports msvcrt; the flows never touch the bridge
    sys.modules.setdefault("msvcrt", ModuleType("msvcrt"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
pytest.importorskip("slack_bolt")
pytest.importorskip("dotenv")
import slack_bot  # noqa: E402
import turn_logger  # noqa: E402
import turn_notices  # noqa: E402

MIKE, AARON, JAKE, HUNTER, JOHN = (slack_bot.player_named(n) for n in ("Mike", "Aaron", "Jake DuRegger", "Hunter", "John"))
LIMIT = slack_bot.TURN_LIMIT_S


def rec(player="Mike", turn=64, leader="LEADER_X"):
    return turn_logger.TurnRecord(datetime(2026, 10, 27, 10), turn, leader, player)


class FakeClient:
    def __init__(self):
        self.posts, self.updates, self.uploads, self.ephemeral = [], [], [], []

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"{len(self.posts)}.0", "channel": kw["channel"]}

    def chat_update(self, **kw):
        self.updates.append(kw)

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)

    def files_upload_v2(self, **kw):
        self.uploads.append(kw)

    def conversations_open(self, users):
        return {"channel": {"id": f"D-{users}"}}


@pytest.fixture
def svc(tmp_path):
    return slack_bot.Services(channel="CCHAN", notices=turn_notices.TurnNotices(tmp_path / "n.json"))


class TestNoticeTick:
    def test_ping_then_request(self, svc, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr(turn_logger, "duration_of_current_turn", lambda: (rec(), 3700))
        slack_bot.notice_tick(client, svc)
        assert len(client.posts) == 1 and f"<@{MIKE.slack_id}>" in client.posts[0]["text"]
        assert "blocks" not in client.posts[0]
        slack_bot.notice_tick(client, svc)  # not repeated
        assert len(client.posts) == 1

        monkeypatch.setattr(turn_logger, "duration_of_current_turn", lambda: (rec(), LIMIT + 5))
        slack_bot.notice_tick(client, svc)
        request = client.posts[-1]
        assert request["channel"] == "CCHAN"
        button = request["blocks"][1]["elements"][0]
        assert button["action_id"] == "civ_approve" and button["value"] == slack_bot.key_of(rec())
        slack_bot.notice_tick(client, svc)
        assert len(client.posts) == 2  # one ping, one request

    def test_a_late_start_posts_only_the_request(self, svc, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr(turn_logger, "duration_of_current_turn", lambda: (rec(), LIMIT + 60))
        slack_bot.notice_tick(client, svc)
        assert len(client.posts) == 1 and "blocks" in client.posts[0]

    def test_no_turn_logged(self, svc, monkeypatch):
        monkeypatch.setattr(turn_logger, "duration_of_current_turn", lambda: None)
        client = FakeClient()
        slack_bot.notice_tick(client, svc)
        assert client.posts == []


class FakeApp:
    """Collects the handlers that register() adds."""

    def __init__(self):
        self.handlers = {}

    def event(self, name):
        return lambda fn: self.handlers.setdefault(("event", name), fn)

    def action(self, name):
        return lambda fn: self.handlers.setdefault(("action", name), fn)


@pytest.fixture
def bot(svc, monkeypatch):
    """Registered handlers, with the game, Claude and the turn log replaced."""
    app, ran = FakeApp(), []
    state = {"turn": (rec(), LIMIT + 600)}
    monkeypatch.setattr(slack_bot, "whose_turn", lambda: state["turn"])
    monkeypatch.setattr(slack_bot, "run_turn", lambda *a, **k: ran.append(a))
    slack_bot.register(app, "claude", slack_bot.TurnLock(), svc)
    return SimpleNamespace(app=app, ran=ran, state=state, svc=svc)


def dm(bot, client, user, text="go"):
    said = []
    event = {"user": user.slack_id, "text": text, "channel_type": "im", "channel": "D1", "ts": str(len(said))}
    body = {"event_id": f"{user.slack_id}-{text}-{len(bot.ran)}-{id(said)}"}
    bot.app.handlers[("event", "message")](body, event, SimpleNamespace(bot_user_id="UBOT"), client, lambda **kw: said.append(kw["text"]))
    return said


def click(bot, client, user, value=None):
    body = {
        "user": {"id": user.slack_id},
        "channel": {"id": "CCHAN"},
        "actions": [{"value": value or slack_bot.key_of(bot.state["turn"][0])}],
    }
    bot.app.handlers[("action", "civ_approve")](lambda: None, body, client)


class TestApprovals:
    def test_before_the_limit_nothing_is_recorded(self, bot):
        bot.state["turn"] = (rec(), 600)
        said = dm(bot, FakeClient(), AARON)
        assert "Another player can have it played after 2h" in said[0] and bot.ran == []
        assert bot.svc.notices.get(slack_bot.key_of(rec())).approvers == []

    def test_one_dm_is_one_approval(self, bot):
        client = FakeClient()
        said = dm(bot, client, AARON)
        assert "Approval recorded (1/3)" in said[0] and bot.ran == []
        assert len(client.posts) == 1 and "blocks" in client.posts[0]  # request posted for the others

    def test_three_players_start_the_takeover(self, bot):
        client = FakeClient()
        dm(bot, client, AARON)
        click(bot, client, JAKE)
        assert "(2/3)" in client.ephemeral[-1]["text"] and bot.ran == []
        click(bot, client, HUNTER)
        assert len(bot.ran) == 1
        run_args = bot.ran[0]
        assert run_args[1].channel == "CCHAN" and run_args[3] is None  # run_turn(client, svc, dm, None, ...)
        assert bot.svc.notices.get(slack_bot.key_of(rec())).approvers == []  # cleared for a retry
        assert "approved by" in client.updates[-1]["text"] and client.updates[-1]["blocks"] == []

    def test_the_same_player_twice_counts_once(self, bot):
        client = FakeClient()
        dm(bot, client, AARON)
        said = dm(bot, client, AARON, "again")
        assert "(1/3)" in said[0] and bot.ran == []

    def test_the_owner_alone_can_approve(self, bot):
        client = FakeClient()
        dm(bot, client, AARON)  # posts the request
        click(bot, client, MIKE)
        assert len(bot.ran) == 1

    def test_a_stale_button_is_refused(self, bot):
        client = FakeClient()
        click(bot, client, AARON, value="63|LEADER_X|Mike")
        assert "out of date" in client.ephemeral[0]["text"] and bot.ran == []

    def test_a_non_player_is_refused(self, bot):
        client = FakeClient()
        click(bot, client, SimpleNamespace(slack_id="UNOBODY"))
        assert client.ephemeral and bot.ran == []

    def test_approvals_per_turn_only(self, bot):
        client = FakeClient()
        dm(bot, client, AARON)
        bot.state["turn"] = (rec(player="Hunter", turn=65), LIMIT + 60)
        said = dm(bot, client, AARON, "next")
        assert "(1/3)" in said[0]

    def test_one_needed_keeps_the_single_player_takeover(self, bot):
        bot.svc.approvals_needed = 1
        dm(bot, FakeClient(), AARON)
        assert len(bot.ran) == 1


class FakeSession:
    def __init__(self, tmp_path):
        self.pre, self.post = tmp_path / "pre.png", tmp_path / "post.png"
        self.mid = [tmp_path / "mid1.png", tmp_path / "mid2.png"]
        for p in (self.pre, self.post, *self.mid):
            p.write_bytes(b"x")

    def all(self):
        return [self.pre, *self.mid, self.post]


class TestOutcome:
    def test_ended_turn_pings_next_and_splits_pictures(self, svc, tmp_path):
        client, session = FakeClient(), FakeSession(tmp_path)
        after = rec(player="Hunter", turn=64)
        slack_bot.post_outcome(client, svc, MIKE, rec(), after, session)
        text = client.posts[0]["text"]
        assert "Claude played Mike's turn 64" in text and f"<@{HUNTER.slack_id}> you're up" in text
        public = [u for u in client.uploads if u["channel"] == "CCHAN"]
        private = [u for u in client.uploads if u["channel"] == f"D-{MIKE.slack_id}"]
        assert [u["title"] for u in public] == ["pre", "post"] and all(u["thread_ts"] == "1.0" for u in public)
        assert [u["title"] for u in private] == ["mid1", "mid2"]
        assert not any(p.exists() for p in session.all())  # cleaned up

    def test_unfinished_turn_keeps_every_picture_private(self, svc, tmp_path):
        client, session = FakeClient(), FakeSession(tmp_path)
        slack_bot.post_outcome(client, svc, MIKE, rec(), rec(), session)  # same turn: not ended
        assert "did not finish" in client.posts[0]["text"]
        assert {u["channel"] for u in client.uploads} == {f"D-{MIKE.slack_id}"}
        assert len(client.uploads) == 4

    def test_unknown_game_state_counts_as_not_finished(self, svc, tmp_path):
        client = FakeClient()
        slack_bot.post_outcome(client, svc, MIKE, rec(), None, FakeSession(tmp_path))
        assert "did not finish" in client.posts[0]["text"]

    def test_without_files_write_nothing_is_uploaded(self, svc, tmp_path):
        svc.uploads = False
        client, session = FakeClient(), FakeSession(tmp_path)
        slack_bot.post_outcome(client, svc, MIKE, rec(), rec(player="Hunter"), session)
        assert client.uploads == [] and not any(p.exists() for p in session.all())

    def test_no_session(self, svc):
        client = FakeClient()
        slack_bot.post_outcome(client, svc, MIKE, rec(), rec(player="Hunter"), None)
        assert len(client.posts) == 1 and client.uploads == []


def test_check_scopes(monkeypatch):
    class Client:
        def __init__(self, header, fail=False):
            self.header, self.fail = header, fail

        def auth_test(self):
            if self.fail:
                raise RuntimeError("invalid_auth")
            return SimpleNamespace(headers={"X-OAuth-Scopes": self.header} if self.header is not None else {})

    all_scopes = "app_mentions:read,im:history,im:write,chat:write,files:write"
    assert slack_bot.check_scopes(Client(all_scopes)) is True
    assert slack_bot.check_scopes(Client(all_scopes.replace(",files:write", ""))) is False
    assert slack_bot.check_scopes(Client(None)) is True  # header missing: not checked
    with pytest.raises(SystemExit, match="im:write"):
        slack_bot.check_scopes(Client("chat:write"))
    with pytest.raises(SystemExit, match="rejected"):
        slack_bot.check_scopes(Client("", fail=True))
