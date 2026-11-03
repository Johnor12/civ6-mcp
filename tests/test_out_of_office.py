"""The out-of-office service: how a vote is counted, whose turns get played and how often, and the shared run lock."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claude_runs  # noqa: E402
import out_of_office  # noqa: E402
import turn_announcer  # noqa: E402
import turn_logger  # noqa: E402
from claude_runs import RunLock  # noqa: E402
from players import SLACK_PLAYERS  # noqa: E402
from turn_logger import TurnRecord  # noqa: E402

AARON = "U0AN2M2PENR"
AIDAN = "U05JN8AA770"
HELDI = "U0B1J1T47QC"
JOHN = "U035CAQE34H"
RANDY = "U0B7FQ1GJ7N"
STRANGER = "U000STRANGER"
OPENED = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)  # today, so "out today" holds
CLOSED = OPENED + timedelta(seconds=out_of_office.VOTE_S)


def turn(player: str, number: int = 10, leader: str = "LEADER_NZINGA_MBANDE") -> TurnRecord:
    return TurnRecord(OPENED - timedelta(minutes=20), number, leader, player)


AIDAN_10 = turn("Aidan")
JAKE_10 = turn("Jake DuRegger", leader="LEADER_VICTORIA")
AIDAN_11 = turn("Aidan", 11)


class FakeSlack:
    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}  # ts -> message, in the game channel
        self.posts: list[tuple[str, str, str | None]] = []  # (channel, text, thread_ts)
        self.updates: list[dict] = []

    def chat_postMessage(self, channel, text, thread_ts=None, **kwargs):
        ts = f"{len(self.posts) + 1}.0"
        self.posts.append((channel, text, thread_ts))
        if channel == out_of_office.CHANNEL and thread_ts is None:
            self.messages[ts] = {"ts": ts, "text": text}
        return {"channel": channel, "ts": ts}

    def conversations_history(self, channel, latest, oldest, inclusive, limit):
        assert channel == out_of_office.CHANNEL and latest == oldest and inclusive
        return {"messages": [self.messages[latest]] if latest in self.messages else []}

    def chat_update(self, channel, ts, text, **kwargs):
        self.updates.append({"channel": channel, "ts": ts, "text": text, **kwargs})
        self.messages[ts]["text"] = text

    def conversations_open(self, users):
        return {"channel": {"id": f"D{users}"}}

    def react(self, ts: str, name: str, *users: str) -> None:
        self.messages[ts].setdefault("reactions", []).append({"name": name, "users": list(users), "count": len(users)})

    def thread(self, ts: str) -> list[str]:
        return [text for channel, text, thread in self.posts if thread == ts]


class Service:
    """Drives out_of_office.check with a fake game, a fake Slack and no Claude."""

    def __init__(self, monkeypatch, tmp_path) -> None:
        self.slack = FakeSlack()
        self.turns: list[TurnRecord] = []
        self.runs: list[tuple[str | None, str, str, list | None]] = []  # (dm, name, prompt, blocks)
        self.lock = RunLock(tmp_path / "claude_run.lock")
        monkeypatch.setattr(out_of_office, "STATE_PATH", tmp_path / "out_of_office.json")
        monkeypatch.setattr(out_of_office, "whose_turn", self._whose_turn)
        monkeypatch.setattr(turn_logger, "duration_of_current_turn", lambda now=None, path=None: self._peek())
        monkeypatch.setattr(turn_announcer, "announce", lambda client: None)
        monkeypatch.setattr(claude_runs, "play_absent_turn", self._play)

    def on(self, *turns: TurnRecord) -> None:
        """What the game answers, in order; the last answer repeats."""
        self.turns = list(turns)

    def _peek(self):
        return (self.turns[0], 0.0) if self.turns else None

    def _whose_turn(self):
        if not self.turns:
            return None
        return (self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]), 0.0

    def _play(self, client, claude, dm, name, prompt, intro, blocks=None):
        self.runs.append((dm, name, prompt, blocks))

    def vote(self, rec: TurnRecord = AIDAN_10, by: str = HELDI) -> str:
        requester, owner = SLACK_PLAYERS[by], out_of_office.player_named(rec.player)
        vote, new = out_of_office.open_vote(self.slack, requester, owner, rec, now=OPENED)
        assert new
        return vote["ts"]

    def check(self, now: datetime = CLOSED) -> None:
        out_of_office.check(self.slack, "claude", self.lock, now)

    def result(self, ts: str) -> str | None:
        with out_of_office.state() as s:
            return next(v["result"] for v in s["votes"] if v["ts"] == ts)


@pytest.fixture
def service(monkeypatch, tmp_path) -> Service:
    return Service(monkeypatch, tmp_path)


def test_vote_passes_and_the_turn_is_played_at_once(service):
    service.on(AIDAN_10, AIDAN_10, JAKE_10)
    ts = service.vote()
    service.slack.react(ts, "+1", HELDI, JOHN)
    service.slack.react(ts, "+1::skin-tone-3", RANDY)
    service.check()
    assert service.result(ts) == "passed"
    assert out_of_office.is_away("Aidan", CLOSED)
    assert service.slack.messages[ts]["text"].endswith(
        "*Vote passed* (Heldi, Randy, John): Aidan is out of office for the rest of today, so I'm playing their turns."
    )
    [(dm, name, prompt, blocks)] = service.runs
    assert dm == f"D{AIDAN}" and name == "Aidan"
    assert "because Aidan is out of office" in prompt and out_of_office.AWAY_NOTE in prompt
    assert any(e.get("action_id") == out_of_office.BACK_ACTION and e["value"] == "Aidan" for b in blocks for e in b.get("elements", []))
    assert service.slack.thread(ts) == ["Done: Aidan's turn 10 is over. It's now Jake DuRegger's turn."]


def test_the_players_veto_stops_the_vote(service):
    service.on(AIDAN_10)
    ts = service.vote()
    service.slack.react(ts, "+1", HELDI, JOHN, RANDY, AARON)
    service.slack.react(ts, "-1", AIDAN)
    service.check()
    assert service.result(ts) == "vetoed"
    assert service.slack.messages[ts]["text"].endswith("*Vote failed:* Aidan voted :-1:.")
    assert not out_of_office.is_away("Aidan", CLOSED) and not service.runs


def test_other_players_down_votes_do_not_stop_it(service):
    service.on(AIDAN_10, AIDAN_10, JAKE_10)
    ts = service.vote()
    service.slack.react(ts, "+1", HELDI, JOHN, RANDY)
    service.slack.react(ts, "-1", AARON)
    service.check()
    assert service.result(ts) == "passed" and service.runs


def test_only_players_up_votes_count(service):
    service.on(AIDAN_10)
    ts = service.vote()
    service.slack.react(ts, "+1", HELDI, STRANGER)
    service.slack.react(ts, "thumbsup", JOHN)
    service.check()
    assert service.result(ts) == "failed"
    assert service.slack.messages[ts]["text"].endswith("*Vote failed:* 2 of the 3 :+1: needed.")
    assert not service.runs


def test_a_vote_stays_open_for_its_ten_minutes(service):
    service.on(AIDAN_10)
    ts = service.vote()
    service.slack.react(ts, "+1", HELDI, JOHN, RANDY)
    service.check(CLOSED - timedelta(seconds=1))
    assert service.result(ts) is None and not service.runs and not service.slack.updates


def test_a_turn_that_ended_before_the_vote_closed_changes_nothing(service):
    service.on(JAKE_10)
    ts = service.vote()
    service.slack.react(ts, "+1", HELDI, JOHN, RANDY)
    service.check()
    assert service.result(ts) == "lapsed"
    assert "ended first" in service.slack.messages[ts]["text"]
    assert not out_of_office.is_away("Aidan", CLOSED) and not service.runs


def test_later_turns_that_day_are_played_once_each(service):
    service.on(AIDAN_10, AIDAN_10, JAKE_10)
    ts = service.vote()
    service.slack.react(ts, "+1", HELDI, JOHN, RANDY)
    service.check()
    service.on(JAKE_10)
    service.check(CLOSED + timedelta(minutes=5))
    service.on(AIDAN_11)  # not finished: the run left the turn up
    service.check(CLOSED + timedelta(hours=1))
    service.check(CLOSED + timedelta(hours=1, minutes=1))
    assert [name for _, name, _, _ in service.runs] == ["Aidan", "Aidan"]
    assert service.slack.thread(ts)[-1] == "Aidan's turn 11 was not finished. Someone at the PC may need to look at the game."


def test_out_of_office_ends_with_the_day(service):
    service.on(AIDAN_10, AIDAN_10, JAKE_10)
    ts = service.vote()
    service.slack.react(ts, "+1", HELDI, JOHN, RANDY)
    service.check()
    service.on(AIDAN_11)
    service.check(OPENED + timedelta(days=1))
    assert len(service.runs) == 1


def test_im_back_stops_the_next_turn(service):
    service.on(AIDAN_10, AIDAN_10, JAKE_10)
    ts = service.vote()
    service.slack.react(ts, "+1", HELDI, JOHN, RANDY)
    service.check()
    assert out_of_office.come_back("Aidan")
    service.on(AIDAN_11)
    service.check(CLOSED + timedelta(hours=1))
    assert len(service.runs) == 1 and not out_of_office.come_back("Aidan")


def test_a_busy_run_lock_puts_the_turn_off_to_the_next_poll(service, tmp_path):
    with out_of_office.state() as s:
        s["away"]["Aidan"] = {"day": OPENED.date().isoformat(), "vote": "1.0"}
    service.on(AIDAN_10, JAKE_10)
    bot = RunLock(tmp_path / "claude_run.lock")  # the Slack bot, mid-run
    assert bot.try_acquire("a run for <@U0B1J1T47QC>")
    service.check(CLOSED)
    assert not service.runs
    bot.release()
    service.check(CLOSED + timedelta(minutes=1))
    assert len(service.runs) == 1


def test_a_vote_for_a_turn_with_an_open_vote_is_the_same_vote(service):
    ts = service.vote()
    vote, new = out_of_office.open_vote(service.slack, SLACK_PLAYERS[JOHN], out_of_office.player_named("Aidan"), AIDAN_10, now=OPENED)
    assert not new and vote["ts"] == ts and len(service.slack.posts) == 1
    assert out_of_office.vote_on(AIDAN_10.key)["ts"] == ts and out_of_office.vote_on(JAKE_10.key) is None


def test_run_lock_is_held_across_processes(tmp_path):
    path = tmp_path / "claude_run.lock"
    lock = RunLock(path)
    assert lock.try_acquire("a run for <@U1>")
    assert not RunLock(path).try_acquire("other")  # another handle in this process
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); from claude_runs import RunLock; from pathlib import Path; "
        "l = RunLock(Path(sys.argv[2])); c = l.current; print(l.try_acquire('x'), c.label if c else None)"
    )
    scripts = str(Path(__file__).resolve().parent.parent / "scripts")
    out = subprocess.run([sys.executable, "-c", probe, scripts, str(path)], capture_output=True, text=True, check=True)
    assert out.stdout.split(maxsplit=1) == ["False", "a run for <@U1>\n"]
    lock.release()
    assert RunLock(path).current is None
    out = subprocess.run([sys.executable, "-c", probe, scripts, str(path)], capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["True", "None"]


def test_run_lock_ignores_what_a_dead_process_left(tmp_path):
    path = tmp_path / "claude_run.lock"
    path.touch()
    path.with_suffix(".json").write_text(json.dumps({"label": "a run for <@U1>", "started": 0}), encoding="utf-8")
    assert RunLock(path).current is None
    lock = RunLock(path)
    assert lock.try_acquire("a run for <@U2>") and lock.current.label == "a run for <@U2>"
    lock.release()
