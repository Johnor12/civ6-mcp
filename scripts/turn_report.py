#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "slack-sdk>=3.33",
#     "python-dotenv>=1.0",
# ]
# ///
"""Turn report: every workday morning, refreshes the turn times dashboard and posts its link in #proj-diplomatic-relations.

    uv run scripts/turn_report.py            # refresh and post at POST_AT every workday, for as long as it runs
    uv run scripts/turn_report.py --now      # refresh and post once, now, and exit
    uv run scripts/turn_report.py --refresh  # refresh the dashboard once, post nothing, and exit
    uv run scripts/turn_report.py --data     # write the data file only, and exit
    uv run scripts/turn_report.py --print    # print the Slack message, write and post nothing, and exit

The report is a dashboard artifact on claude.ai (ARTIFACT_URL). Its page is
fixed; what changes each morning is its one data file. This script writes
that file (DATA_PATH) from scripts/turn_times.py, the turn log plus the
hotseat saves, and then has Claude Code put it on the dashboard: only a
Claude Code session can write to an artifact, and `claude -p` cannot, so it
starts a background session (`claude --bg`) whose prompt is to follow
scripts/turn_report_refresh.md, waits for the file that session writes when
it is done (DONE_PATH) and removes the session. The Slack post is one
sentence on the previous workday (headline) and the link. When the refresh
fails the post still goes out and says the page is not up to date.

A turn counts towards the workday it ended in, and a workday lasts until
the next one begins at 9:00 (turn_times.workday_of), so a turn finished
overnight, over the weekend or on a holiday (turn_logger.HOLIDAYS) is the
previous workday's, and a holiday gets no report of its own. The report covers
every turn up to the previous workday ("yesterday"; Friday on a Monday);
turns of a workday still under way are left for its own report. Turn time
is wait plus play (turn_times.overall_minutes): the workday time
(turn_logger.WORKDAY, what the bot's turn limit counts) from the turn's
start to its end, plus its play time outside the workday. A turn with no
workday time, begun and finished outside it, counts as a turn but not in
the average turn time or as the fastest. Play time runs from Start Turn to
the turn's end at any hour, over the turns that have one (the taken log
began on 4 Nov 2026), and leaves out the turns Claude took, which are
counted on their own.

Nothing but the data file is written; the turn log is the history. A
workday on which the process is not up at POST_AT gets no report.
scripts/start.py runs this next to the turn logger, the announcer and the
bot. The bot token comes from the repo's .env and the bot must be a member
of the channel. The dashboard is private until its owner shares it from
the page's Share menu.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path

import turn_logger  # scripts/turn_logger.py, next to this script
import turn_times
from turn_announcer import CHANNEL

REPO_ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("turn_report")

POST_AT = clock(9, 0)  # local time, Monday to Friday less holidays: the moment the previous workday closes (turn_times.workday_of)
SLEEP_S = 300  # how long to sleep at a stretch while waiting for POST_AT

ARTIFACT_URL = "https://claude.ai/artifact/R7iddDEahbRBdTJ3KrGmTD"
DATA_PATH = REPO_ROOT / "scripts" / "data" / "turn_report.json"  # git-ignored, rewritten each morning
DONE_PATH = REPO_ROOT / "scripts" / "data" / "turn_report_refresh.json"  # written by the refresh session
REFRESH_DOC = REPO_ROOT / "scripts" / "turn_report_refresh.md"  # what the refresh session is told to do
REFRESH_PROMPT = f"Read {REFRESH_DOC.relative_to(REPO_ROOT).as_posix()} and do exactly what it says."
REFRESH_MODEL = "sonnet"  # an upload and three small tool calls
REFRESH_TIMEOUT_S = 10 * 60
REFRESH_POLL_S = 5
# What the refresh session may do without asking. Nobody is there to answer a prompt.
REFRESH_TOOLS = ("Artifact", "ArtifactData", "ToolSearch", "Read", "Write")
NO_MCP = '{"mcpServers":{}}'  # the session must not start a second civ6 server next to the game's
# A background session may not write into the shared checkout unless told it may; this one writes DONE_PATH there.
REFRESH_SETTINGS = '{"worktree":{"bgIsolation":"none"}}'
# Set when this script is started from inside a Claude Code session; the
# refresh session should not think it is nested.
PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")
BACKGROUNDED = re.compile(r"backgrounded · (\w+)")


def previous_workday(day: date) -> date:
    """The working day before `day`: Friday for a Monday (or a weekend day), skipping holidays (turn_logger.is_day_off)."""
    day -= timedelta(days=1)
    while turn_logger.is_day_off(day):
        day -= timedelta(days=1)
    return day


def _turns(n: int) -> str:
    return f"{n} turn" + ("s" if n != 1 else "")


def _s(minutes: float | None) -> int | None:
    """Minutes as whole seconds, the unit of every duration in the data file; None stays None."""
    return None if minutes is None else round(minutes * 60)


def _t(minutes: float | None) -> str | None:
    """Minutes as the text the dashboard prints ('31m 08s'), beside the seconds its charts plot; None stays None."""
    return None if minutes is None else turn_times.hms(minutes)


Done = list[tuple[turn_times.PlayerTurn, datetime]]


def _by_day(done: Done) -> dict[date, Done]:
    """The finished turns under the workday each counts towards (turn_times.workday_of its end), in day order."""
    days: dict[date, Done] = {}
    for pt, end in done:
        days.setdefault(turn_times.workday_of(end), []).append((pt, end))
    return dict(sorted(days.items()))


def _workdays(first: date, last: date) -> list[date]:
    out, day = [], first
    while day <= last:
        if not turn_logger.is_day_off(day):
            out.append(day)
        day += timedelta(days=1)
    return out


def _summary(scope: str, done: Done) -> dict:
    """One scope's figures: its turns, turn time (mean and median over the turns with workday time),
    play time (the turns their player took) and the turns Claude took."""
    own = turn_times.own_takes(done)
    claude = turn_times.claude_takes(done)
    mean, median = turn_times.mean_minutes(done), turn_times.median_minutes(done)
    play, total = turn_times.mean_take_minutes(own), turn_times.total_take_minutes(own)
    claude_play = turn_times.mean_take_minutes(claude)
    return {
        "scope": scope,
        "turns": len(done),
        "mean_turn_s": _s(mean), "mean_turn": _t(mean),
        "median_turn_s": _s(median), "median_turn": _t(median),
        "off_hours": len(done) - len(turn_times.workday_turns(done)),
        "play_turns": len(own),
        "mean_play_s": _s(play), "mean_play": _t(play),
        "total_play_s": _s(total), "total_play": _t(total),
        "claude_turns": len(claude),
        "claude_players": ", ".join(dict.fromkeys(pt.player for pt, _ in claude)),
        "claude_mean_play_s": _s(claude_play), "claude_mean_play": _t(claude_play),
    }


def _records(scope: str, done: Done) -> list[dict]:
    """The scope's extremes, each with whose turn it was and the workday it counts towards.

    Slowest turn over every turn, fastest over the turns with workday time,
    longest and shortest play time over the turns their player took; each
    is missing while fewer than two turns compete for it. Ties go to the
    first turn that reached them.
    """
    own = turn_times.timed_takes(turn_times.own_takes(done))
    candidates = (
        ("Slowest turn", turn_times.timed_turns(done), max),
        ("Fastest turn", turn_times.timed_turns(turn_times.workday_turns(done)), min),
        ("Longest play time", own, max),
        ("Shortest play time", own, min),
    )
    rows = []
    for record, timed, pick in candidates:
        if len(timed) < 2:
            continue
        pt, end, mins = pick(timed, key=lambda t: t[2])
        rows.append({
            "scope": scope, "record": record, "seconds": _s(mins), "time": _t(mins), "player": pt.player, "turn": pt.turn,
            "day": turn_times.workday_of(end).isoformat(),
        })
    return rows


def _players(scope: str, done: Done, seats: dict[str, int]) -> list[dict]:
    """A row per player in seat order, the ones with no turn in `done` too (turns 0, no times)."""
    averages = {a.player: a for a in turn_times.player_averages(done)}
    claude: dict[str, int] = {}
    for pt, _ in turn_times.claude_takes(done):
        claude[pt.player] = claude.get(pt.player, 0) + 1
    rows = []
    for player, seat in sorted(seats.items(), key=lambda kv: kv[1]):
        a = averages.get(player)
        rows.append({
            "scope": scope, "player": player, "seat": seat + 1,
            "turns": a.turns if a else 0,
            "mean_turn_s": _s(a.mean_minutes) if a else None, "mean_turn": _t(a.mean_minutes) if a else None,
            "median_turn_s": _s(a.median_minutes) if a else None, "median_turn": _t(a.median_minutes) if a else None,
            "max_turn_s": _s(a.max_minutes) if a else None, "max_turn": _t(a.max_minutes) if a else None,
            "off_hours": a.off_hours if a else 0,
            "play_turns": a.taken if a else 0,
            "mean_play_s": _s(a.take_mean_minutes) if a else None, "mean_play": _t(a.take_mean_minutes) if a else None,
            "claude_turns": claude.get(player, 0),
        })
    return rows


def _record_note(counts: dict[date, int], day: date) -> str:
    """' (record: 36, Fri 30 Oct)': workday `day`'s turns against the most on any earlier one; '' without one.

    ' (new record! Previous: 30, Thu 29 Oct)', ' (ties the record, Thu 29 Oct)'
    or ' (record: 36, Fri 30 Oct)'. The record's day is the first that reached it.
    """
    earlier = {d: n for d, n in counts.items() if d < day}
    if not earlier:
        return ""
    count, best = counts.get(day, 0), max(earlier.values())
    when = f"{min(d for d, n in earlier.items() if n == best):%a %d %b}"
    if count > best:
        return f" (new record! Previous: {best}, {when})"
    if count == best:
        return f" (ties the record, {when})"
    return f" (record: {best}, {when})"


def _headline(label: str, yesterday: dict, counts: dict[date, int], day: date) -> str:
    """The sentence under the dashboard's title and in the Slack post: the previous workday in short.

    'Yesterday (Tue 17 Nov): 15 turns (record: 35, Fri 30 Oct), the fewest in a
    day so far. Average turn time 31m 08s, average play time 6m 38s.'
    """
    if not yesterday["turns"]:
        return f"{label}: no turns{_record_note(counts, day)}."
    text = f"{label}: {_turns(yesterday['turns'])}{_record_note(counts, day)}"
    earlier = [n for d, n in counts.items() if d < day]
    if len(earlier) > 1 and yesterday["turns"] < min(earlier):
        text += ", the fewest in a day so far"
    times = [
        f"average {what} {turn_times.hms(yesterday[key] / 60)}"
        for what, key in (("turn time", "mean_turn_s"), ("play time", "mean_play_s"))
        if yesterday[key] is not None
    ]
    return text + (". " + ", ".join(times).capitalize() if times else "") + "."


def report_data(
    seq: list[turn_times.PlayerTurn], today: date | None = None, now: datetime | None = None
) -> dict | None:
    """Everything the dashboard shows, as tables of rows; None while no turn has finished by the previous workday.

    Durations are whole seconds (`*_s`, `seconds`), the ones the page prints
    with their text beside them (the same name without `_s`, or `time`),
    days ISO dates, moments ISO local times. `scope` is "yesterday" (the workday before `today`) or
    "all" (every turn up to it).

    - info: one row. When it was generated, the day reported on and its
      label, its working hours and grace period, the headline, the first
      day and the turn in progress.
    - summary: a row per scope (_summary). The "all" row adds the days with
      a turn, the turns per such day, and the most and fewest turns on one.
    - records: the extremes of each scope (_records).
    - days: a row per working day from the first to the reported one, with its
      turns (0 for a day without one) and average turn and play time.
    - players: a row per scope and player (_players).
    - player_days: a row per day and player with a turn that day.
    - turns: the reported day's turns, each with its start, Start Turn and end.
    """
    now = now or datetime.now()
    today = today or now.date()
    day = previous_workday(today)
    done = [(pt, end) for pt, end in turn_times.finished(seq) if turn_times.workday_of(end) <= day]
    if not done:
        return None
    by_day = _by_day(done)
    counts = {d: len(turns) for d, turns in by_day.items()}
    yesterday = by_day.get(day, [])
    seats = {pt.player: pt.seat for pt in seq}
    label = f"Yesterday ({day:%a %d %b})" if day == today - timedelta(days=1) else f"{day:%A %d %b}"

    summary = [_summary("yesterday", yesterday), _summary("all", done)]
    most, fewest = max(counts.values()), min(counts.values())
    summary[1] |= {
        "days": len(counts),
        "turns_per_day": round(len(done) / len(counts), 1),
        "most_turns": most,
        "most_day": min(d for d, n in counts.items() if n == most).isoformat(),
        "fewest_turns": fewest,
        "fewest_day": min(d for d, n in counts.items() if n == fewest).isoformat(),
        "game_turns_per_day": round((done[-1][0].turn - done[0][0].turn) / len(counts), 1),
    }

    days, player_days = [], []
    for d in _workdays(min(by_day), day):
        turns = by_day.get(d, [])
        own = turn_times.own_takes(turns)
        days.append({
            "day": d.isoformat(), "turns": len(turns),
            "mean_turn_s": _s(turn_times.mean_minutes(turns)),
            "mean_play_s": _s(turn_times.mean_take_minutes(own)),
            "claude_turns": len(turn_times.claude_takes(turns)),
        })
        for a in sorted(turn_times.player_averages(turns), key=lambda a: a.seat):
            player_days.append({
                "day": d.isoformat(), "player": a.player, "turns": a.turns,
                "mean_turn_s": _s(a.mean_minutes), "mean_play_s": _s(a.take_mean_minutes),
            })

    current = seq[-1]
    spans = turn_logger.FRIDAY_SPANS if day.weekday() == 4 else turn_logger.WORKDAY_SPANS
    info = {
        "generated": now.isoformat(timespec="seconds"),
        "day": day.isoformat(),
        "label": label,
        "day_start": datetime.combine(day, spans[0][0]).isoformat(timespec="seconds"),
        "day_end": datetime.combine(day, spans[-1][1]).isoformat(timespec="seconds"),
        "grace_start": datetime.combine(day, turn_logger.GRACE_START).isoformat(timespec="seconds"),
        "grace_end": datetime.combine(day, turn_logger.GRACE_END).isoformat(timespec="seconds"),
        "headline": _headline(label, summary[0], counts, day),
        "since": min(by_day).isoformat(),
        "workday": turn_logger.WORKDAY,
        "now_turn": current.turn,
        "now_player": current.player,
        "now_since": current.start.isoformat(timespec="seconds"),
    }
    return {
        "info": [info],
        "summary": summary,
        "records": _records("yesterday", yesterday) + _records("all", done),
        "days": days,
        "players": _players("yesterday", yesterday, seats) + _players("all", done, seats),
        "player_days": player_days,
        "turns": [
            {
                "turn": pt.turn, "player": pt.player, "seat": pt.seat + 1,
                "start": pt.start.isoformat(timespec="seconds"),
                "taken": pt.taken.isoformat(timespec="seconds") if pt.taken else None,
                "end": end.isoformat(timespec="seconds"),
                "turn_s": _s(turn_times.overall_minutes(pt, end)),
                "play_s": _s(turn_times.take_minutes(pt, end)),
                "claude": pt.claude,
            }
            for pt, end in yesterday
        ],
    }


def dumps(data: dict) -> str:
    """The data file's text: JSON with one row to a line, so a day's change reads as a few lines."""
    tables = []
    for name, rows in data.items():
        body = ",\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
        tables.append(f"{json.dumps(name)}: [\n{body}\n]")
    return "{\n" + ",\n".join(tables) + "\n}\n"


def build(today: date | None = None, now: datetime | None = None) -> dict | None:
    """Read the turn log and the hotseat saves and work out the report (report_data)."""
    seq, skipped = turn_times.compute()
    for s in skipped:
        log.warning("a save fits no turn in the log: %s", s)
    return report_data(seq, today, now) if seq else None


def write_data(data: dict, path: Path | None = None) -> Path:
    path = path or DATA_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(data), encoding="utf-8")
    return path


def message(data: dict, stale: bool = False) -> str:
    """The Slack post: the headline and the dashboard's link; `stale` says the page did not get this morning's data."""
    text = f"{data['info'][0]['headline']}\n<{ARTIFACT_URL}|Turn times: yesterday and the whole game>"
    if stale:
        text += "\n_The page could not be refreshed this morning, so it still shows the last report._"
    return text


def _claude(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("claude is not on PATH")
    env = {k: v for k, v in os.environ.items() if k not in PARENT_SESSION_VARS}
    return subprocess.run(
        [claude, *args], cwd=REPO_ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def _session_failed(session: str) -> bool:
    """Whether `claude agents` lists the background session as failed. False when it cannot be asked."""
    try:
        listed = json.loads(_claude("agents", "--json", "--all").stdout)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
        return False
    return any(a.get("id") == session and a.get("state") == "failed" for a in listed)


def refresh(generated: str, timeout_s: float = REFRESH_TIMEOUT_S) -> str | None:
    """Have a background Claude Code session put DATA_PATH on the dashboard. None when it did, else why not.

    The session follows REFRESH_DOC and ends by writing DONE_PATH with the
    data file's `generated` stamp, plus an `error` when a step failed. The
    session is stopped and removed afterwards either way; its conversation
    stays in `claude --resume`.
    """
    DONE_PATH.unlink(missing_ok=True)
    started = _claude(
        "--bg", "--model", REFRESH_MODEL, "--strict-mcp-config", "--mcp-config", NO_MCP, "--settings", REFRESH_SETTINGS,
        "--allowedTools", *REFRESH_TOOLS, "--", REFRESH_PROMPT,
    )
    found = BACKGROUNDED.search(started.stdout)
    if not found:
        return f"claude --bg did not start a session: {(started.stdout + started.stderr).strip()[-500:]}"
    session = found[1]
    log.info("refresh session %s started", session)
    try:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(REFRESH_POLL_S)
            try:
                done = json.loads(DONE_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                done = None  # not written yet, or half written
            if isinstance(done, dict) and done.get("generated") == generated:
                return str(done["error"]) if done.get("error") else None
            if _session_failed(session):
                return f"refresh session {session} failed"
        return f"refresh session {session} was not done after {timeout_s:.0f} s"
    finally:
        for verb in ("stop", "rm"):
            try:
                _claude(verb, session)
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                log.warning("claude %s %s failed: %s", verb, session, exc)


def refresh_dashboard() -> tuple[dict | None, str | None]:
    """Write the data file and refresh the dashboard with it: (the data, why the refresh failed or None).

    The data is None, and nothing is refreshed, while no turn has finished.
    """
    data = build()
    if data is None:
        return None, None
    write_data(data)
    try:
        why = refresh(data["info"][0]["generated"])
    except Exception as exc:  # claude missing, a timeout of the CLI itself
        why = f"{type(exc).__name__}: {exc}"
    if why:
        log.error("the dashboard was not refreshed: %s", why)
    else:
        log.info("dashboard refreshed: %s", ARTIFACT_URL)
    return data, why


def post(client) -> bool:
    """Refresh the dashboard and post its link in the channel. True when something was posted. Never raises."""
    try:
        data, why = refresh_dashboard()
        if data is None:
            log.info("no finished turn in the log yet; nothing to post")
            return False
        text = message(data, stale=why is not None)
        client.chat_postMessage(channel=CHANNEL, text=text)
    except Exception:
        log.exception("posting the turn report failed")
        return False
    log.info("posted to %s:\n%s", CHANNEL, text)
    return True


def next_post(now: datetime) -> datetime:
    """The next POST_AT after `now` that falls on a working day (not a weekend day or a holiday)."""
    due = datetime.combine(now.date(), POST_AT)
    if due <= now:
        due += timedelta(days=1)
    while turn_logger.is_day_off(due.date()):
        due += timedelta(days=1)
    return due


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--now", action="store_true", help="refresh the dashboard and post once, now, and exit")
    parser.add_argument("--refresh", action="store_true", help="refresh the dashboard once, post nothing, and exit")
    parser.add_argument("--data", action="store_true", help="write the data file only, and exit")
    parser.add_argument("--print", action="store_true", help="print the Slack message, write and post nothing, and exit")
    args = parser.parse_args()
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.print or args.data:
        data = build()
        if data is None:
            print("No finished turn in the log yet.")
        elif args.data:
            print(write_data(data))
        else:
            print(message(data))
        return
    if args.refresh:
        data, why = refresh_dashboard()
        sys.exit(0 if data is not None and why is None else 1)

    # Imported here so the tests, in the project venv, can import this module without them.
    from dotenv import dotenv_values
    from slack_sdk import WebClient

    token = dotenv_values(REPO_ROOT / ".env").get("SLACK_BOT_TOKEN")
    if not token:
        raise SystemExit(f"SLACK_BOT_TOKEN must be set in {REPO_ROOT / '.env'}")
    client = WebClient(token=token)
    if args.now:
        sys.exit(0 if post(client) else 1)

    while True:
        due = next_post(datetime.now())
        log.info("next report at %s in %s", due.strftime("%a %d %b %H:%M"), CHANNEL)
        while (left := (due - datetime.now()).total_seconds()) > 0:
            time.sleep(min(left, SLEEP_S))
        post(client)


if __name__ == "__main__":
    main()
