import asyncio
import json
from types import SimpleNamespace

from slackbot.config import Settings
from slackbot.runner import AgentRunner, build_prompt, split_for_slack

FUTURE = "2999-01-01T00:00:00+00:00"


class FakeProc:
    def __init__(self, out=b"", err=b"", code=0, hang=False):
        self.out, self.err, self.returncode, self.hang = out, err, code, hang
        self.stdin_seen = None
        self.killed = False
        self.pid = 1

    async def communicate(self, data):
        self.stdin_seen = data
        if self.hang:
            await asyncio.sleep(10)
        return self.out, self.err

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


def make_runner(proc, marker, **kw):
    seen = {}

    async def spawn(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return proc

    r = AgentRunner(Settings(**kw), spawn=spawn, read_marker=lambda: marker)
    return r, seen


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def marker(**kw):
    base = dict(
        started_at=FUTURE, finished_turn=7, pre_turn_save="PRE", finished_save="POST", next_player="RAY"
    )
    return SimpleNamespace(**{**base, **kw})


def test_prompt_and_command():
    assert build_prompt("LEADER_X", " no wars ") == "take LEADER_X's turn — they said: no wars"
    assert "Play a steady" in build_prompt("LEADER_X", "")
    proc = FakeProc(json.dumps({"result": "Done.", "is_error": False}).encode())
    r, seen = make_runner(proc, marker())
    out = run(r.run("LEADER_X", "go"))
    assert out.ok and out.text == "Done." and out.finished_save == "POST"
    assert "--allowedTools" in seen["args"] and "mcp__civ6" in seen["args"]
    assert "go" not in " ".join(seen["args"])  # prompt travels on stdin only
    assert b"take LEADER_X" in proc.stdin_seen


def test_not_ok_when_turn_not_ended():
    proc = FakeProc(json.dumps({"result": "Done.", "is_error": False}).encode())
    r, _ = make_runner(proc, marker(finished_turn=None))
    out = run(r.run("L", "x"))
    assert not out.ok and "not ended" in out.text


def test_not_ok_when_marker_is_old():
    proc = FakeProc(b'{"result": "ok"}')
    r, _ = make_runner(proc, marker(started_at="2000-01-01T00:00:00+00:00"))
    out = run(r.run("L", "x"))
    assert not out.ok and "no stand-in turn" in out.text


def test_error_exit_and_timeout():
    r, _ = make_runner(FakeProc(b"", b"boom", code=1), marker())
    out = run(r.run("L", "x"))
    assert not out.ok and "boom" in out.text
    proc = FakeProc(hang=True)
    r, _ = make_runner(proc, marker(), run_timeout_s=0.05)
    out = run(r.run("L", "x"))
    assert not out.ok and "Timed out" in out.text and proc.killed


def test_spawn_failure():
    async def spawn(*a, **k):
        raise FileNotFoundError("claude")

    out = run(AgentRunner(Settings(), spawn=spawn, read_marker=lambda: None).run("L", "x"))
    assert not out.ok and "Could not start" in out.text


def test_split_for_slack():
    assert split_for_slack("short") == ["short"]
    parts = split_for_slack("\n".join(["x" * 50] * 100), limit=500)
    assert all(len(p) <= 500 for p in parts) and len(parts) > 1
