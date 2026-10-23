import asyncio
from types import SimpleNamespace

from slackbot.preflight import (
    REQUIRED_SCOPES,
    env_problems,
    missing_scopes,
    parse_scopes,
    slack_problems,
)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeClient:
    def __init__(self, scopes="chat:write", fail_auth=None, fail_history=None, headers=True):
        self.scopes, self.fail_auth, self.fail_history, self.headers = (
            scopes, fail_auth, fail_history, headers,
        )

    async def auth_test(self):
        if self.fail_auth:
            raise RuntimeError(self.fail_auth)
        return SimpleNamespace(headers={"X-OAuth-Scopes": self.scopes} if self.headers else {})

    async def conversations_history(self, **kw):
        if self.fail_history:
            raise RuntimeError(self.fail_history)
        return {}


ALL = ",".join(sorted(REQUIRED_SCOPES))


def test_env_problems():
    assert env_problems({"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1"}) == []
    assert len(env_problems({})) == 2
    swapped = env_problems({"SLACK_BOT_TOKEN": "xapp-1", "SLACK_APP_TOKEN": "xoxb-1"})
    assert len(swapped) == 2 and "should start with" in swapped[0]


def test_scope_helpers():
    assert parse_scopes("a, b,,c") == {"a", "b", "c"}
    assert missing_scopes(REQUIRED_SCOPES) == []
    assert missing_scopes(["chat:write"]) == sorted(REQUIRED_SCOPES - {"chat:write"})


def test_slack_problems():
    assert run(slack_problems(FakeClient(ALL), "C1")) == []
    out = run(slack_problems(FakeClient("chat:write,im:history")))
    assert "files:write" in out[0] and "channels:history" in out[0]
    assert "rejected" in run(slack_problems(FakeClient(fail_auth="invalid_auth")))[0]
    assert "could not be checked" in run(slack_problems(FakeClient(headers=False)))[0]
    out = run(slack_problems(FakeClient(ALL, fail_history="not_in_channel"), "C1"))
    assert "Invite" in out[0] and "not_in_channel" in out[0]


def test_manifest_matches_required_scopes():
    import pathlib
    import re

    text = (pathlib.Path(__file__).resolve().parents[1] / "manifest.yml").read_text()
    scopes = set(re.findall(r"^\s+- ([a-z]+:[a-z]+)$", text, re.M))
    assert scopes == set(REQUIRED_SCOPES)
    assert "socket_mode_enabled: true" in text
    assert "is_enabled: true" in text
    assert "- message.im" in text
    assert "messages_tab_enabled: true" in text
