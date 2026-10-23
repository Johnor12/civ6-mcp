"""End-to-end bot flows with fake Slack, game, runner and capture."""

import asyncio
from pathlib import Path

from slackbot.bot import CivBot
from slackbot.config import Player, Settings
from slackbot.rules import GameStatus
from slackbot.runner import RunResult
from slackbot.state import Store
from slackbot.turn_tracker import ChannelMessage

A = Player("UA", "Aaron", "Cleopatra")
B = Player("UB", "Bob", "Trajan")
C = Player("UC", "Cara", "Gitarja")
D = Player("UD", "Dan", "Amanitore")
PLAYERS = [A, B, C, D]


class FakeSlack:
    def __init__(self):
        self.history_msgs = []
        self.posts, self.updates, self.dms, self.uploads = [], [], [], []

    async def history(self, oldest):
        return [m for m in self.history_msgs if float(m.ts) > float(oldest)]

    async def post(self, text, *, thread_ts=None, blocks=None):
        ts = f"{2_000_000 + len(self.posts)}.0"
        self.posts.append(dict(ts=ts, text=text, thread_ts=thread_ts, blocks=blocks))
        # A top-level post lands in channel history, as in real Slack.
        if thread_ts is None:
            self.history_msgs.append(ChannelMessage(ts, "UBOT", text))
        return ts

    async def update(self, ts, text, blocks=None):
        self.updates.append((ts, text, blocks))

    async def dm(self, user_id, text):
        self.dms.append((user_id, text))

    async def upload(self, path, *, thread_ts, title):
        self.uploads.append((path, title))


class FakeGame:
    def __init__(self, leader="LEADER_CLEOPATRA"):
        self.leader = leader
        self.ready = True

    async def status(self):
        return GameStatus(
            reachable=True, is_hotseat=True, leader=self.leader, player_name="X", turn=5,
            turn_active=True, player_change_visible=self.ready, description="game text",
        )


class FakeRunner:
    def __init__(self, ok=True, hold=None):
        self.ok, self.hold, self.calls = ok, hold, []

    async def run(self, game_leader, note):
        self.calls.append((game_leader, note))
        if self.hold:
            await self.hold.wait()
        return RunResult(self.ok, "Built a campus. <!channel>", "PRE 5", "NEXT 5 MCP", "Bob")


class FakeCapture:
    async def screenshot(self, label):
        return Path(f"/tmp/{label}.png")

    async def start_recording(self):
        pass

    async def stop_recording(self):
        return None


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


def make(ok=True, hold=None, **settings):
    slack = FakeSlack()
    slack.history_msgs.append(ChannelMessage("1000000.0", "UX", "<@UA>"))
    clock = Clock()
    s = Settings(history_start_ts="999999.0", warn_after_s=7200, ping_after_s=3600, **settings)
    game, runner = FakeGame(), FakeRunner(ok, hold)
    bot = CivBot(s, PLAYERS, Store(), slack, game, runner, FakeCapture(), clock)
    return bot, slack, game, runner, clock


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_dm_runs_turn_and_pings_next_player():
    bot, slack, game, runner, _ = make()
    run(bot.handle_dm("UA", "settle the coast"))
    assert runner.calls == [("LEADER_CLEOPATRA", "settle the coast")]
    assert any(p["text"] == "<@UB> your turn." and p["thread_ts"] is None for p in slack.posts)
    dm_text = "\n".join(t for u, t in slack.dms if u == "UA")
    assert "finished your turn" in dm_text and "Built a campus" in dm_text
    assert "<!channel>" not in dm_text  # agent text cannot ping the channel
    assert [t for _, t in slack.uploads] == ["pre", "post"]
    assert run(bot.refresh_turn()).player_id == "UB"  # the bot's own @ moved the turn


def test_dm_refused_on_someone_elses_turn():
    bot, slack, _, runner, _ = make()
    run(bot.handle_dm("UB", "do things"))
    assert runner.calls == [] and "not your turn" in slack.dms[-1][1]
    run(bot.handle_dm("UZ", "hi"))
    assert "not in the civ game" in slack.dms[-1][1]


def test_dm_refused_when_game_disagrees():
    bot, slack, game, runner, _ = make()
    game.leader = "LEADER_TRAJAN"
    run(bot.handle_dm("UA", "go"))
    assert runner.calls == [] and "waits on" in slack.dms[-1][1]
    game.leader, game.ready = "LEADER_CLEOPATRA", False
    run(bot.handle_dm("UA", "go"))
    assert runner.calls == [] and "turn-change screen" in slack.dms[-1][1]


def test_failed_run_does_not_ping_next():
    bot, slack, _, runner, _ = make(ok=False)
    run(bot.handle_dm("UA", "go"))
    assert not any("your turn." in p["text"] for p in slack.posts if p["thread_ts"] is None)
    assert any("did not finish" in p["text"] for p in slack.posts)
    assert "could not finish" in slack.dms[-1][1] or "could not finish" in slack.dms[-2][1]


def test_second_run_is_refused_while_busy():
    async def scenario():
        hold = asyncio.Event()
        bot, slack, _, runner, _ = make(hold=hold)
        first = asyncio.create_task(bot.handle_dm("UA", "one"))
        await asyncio.sleep(0.01)
        await bot.handle_dm("UA", "two")
        hold.set()
        await first
        return runner.calls, slack.dms

    calls, dms = run(scenario())
    assert len(calls) == 1 and any("already playing" in t for _, t in dms)


def test_timers_then_approval_by_current_player_alone():
    bot, slack, game, runner, clock = make()
    run(bot.tick())
    assert slack.posts == []
    clock.t += 3601
    run(bot.tick())
    assert "open for 1 hour" in slack.posts[-1]["text"]
    clock.t += 3600
    run(bot.tick())
    run(bot.tick())  # no duplicates
    req_posts = [p for p in slack.posts if p["blocks"]]
    assert len(req_posts) == 1
    req_id = req_posts[0]["blocks"][1]["elements"][0]["value"]
    out = run(bot.handle_approve("UA", req_id))
    assert "Approved" in out.reply and out.job is not None
    run(out.job)
    assert runner.calls and "steady" in runner.calls[0][1]
    assert slack.posts and any(p["text"] == "<@UB> your turn." for p in slack.posts)
    assert "approved by Aaron" in slack.updates[-1][1]
    assert "closed" in run(bot.handle_approve("UB", req_id)).reply


def test_approval_needs_three_others():
    bot, slack, game, runner, clock = make(approvals_needed=3)
    clock.t += 7300
    run(bot.tick())
    req_id = [p for p in slack.posts if p["blocks"]][0]["blocks"][1]["elements"][0]["value"]
    assert "1/3" in run(bot.handle_approve("UB", req_id)).reply
    assert "1/3" in run(bot.handle_approve("UB", req_id)).reply  # same person twice
    assert "2/3" in run(bot.handle_approve("UC", req_id)).reply
    out = run(bot.handle_approve("UD", req_id))
    assert out.job is not None
    run(out.job)
    assert len(runner.calls) == 1


def test_takeover_prompt_resets_approvals():
    bot, slack, _, runner, clock = make()
    clock.t += 7300
    run(bot.tick())
    req_id = [p for p in slack.posts if p["blocks"]][0]["blocks"][1]["elements"][0]["value"]
    run(bot.handle_approve("UB", req_id))
    run(bot.handle_dm("UC", "takeover focus on science"))
    assert "cleared" in slack.dms[-1][1]
    assert bot.store.approvers(req_id) == set()
    assert "focus on science" in str(slack.updates[-1][2])
    run(bot.handle_dm("UC", "takeover"))
    assert "focus on science" in slack.dms[-1][1]


def test_approval_is_void_after_turn_moves_or_expires():
    bot, slack, _, runner, clock = make()
    clock.t += 7300
    run(bot.tick())
    req_id = [p for p in slack.posts if p["blocks"]][0]["blocks"][1]["elements"][0]["value"]
    slack.history_msgs.append(ChannelMessage("1007300.0", "UX", "<@UB>"))
    assert "moved on" in run(bot.handle_approve("UA", req_id)).reply
    assert runner.calls == []

    bot, slack, _, runner, clock = make(approval_ttl_s=60)
    clock.t += 7300
    run(bot.tick())
    req_id = [p for p in slack.posts if p["blocks"]][0]["blocks"][1]["elements"][0]["value"]
    clock.t += 61
    assert "expired" in run(bot.handle_approve("UA", req_id)).reply
    assert runner.calls == []


def test_approval_waits_for_game_ready():
    bot, slack, game, runner, clock = make()
    clock.t += 7300
    run(bot.tick())
    req_id = [p for p in slack.posts if p["blocks"]][0]["blocks"][1]["elements"][0]["value"]
    game.ready = False
    out = run(bot.handle_approve("UA", req_id))
    assert "not ready" in out.reply and out.job is None
    game.ready = True
    out = run(bot.handle_approve("UA", req_id))
    assert out.job is not None
    out.job.close()


def test_status_and_help():
    bot, slack, *_ = make()
    run(bot.handle_dm("UA", "status"))
    assert "Slack turn: Aaron" in slack.dms[-1][1] and "game text" in slack.dms[-1][1]
    run(bot.handle_dm("UA", "help"))
    assert "takeover" in slack.dms[-1][1]
