"""The turn log behind the Slack turn timer (rows on change only, workday hours) and the logger's save fallback."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import turn_logger  # noqa: E402

from civ_mcp import hotseat  # noqa: E402

MONDAY = datetime(2026, 10, 26)


@pytest.fixture(autouse=True)
def snapshots(tmp_path, monkeypatch) -> Path:
    """The bridge mod's snapshot folder, empty and the test's own, so no test reads the game's."""
    folder = tmp_path / "Snapshots"
    monkeypatch.setattr(turn_logger, "SNAPSHOTS_DIR", folder)
    return folder


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    """Day 0 is Monday 26 Oct 2026, day 5 the Saturday after."""
    return MONDAY + timedelta(days=day, hours=hour, minutes=minute)


def _save_file(folder: Path, name: str, at: datetime) -> Path:
    """An (empty) save file last modified at `at`."""
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / f"{name}.Civ6Save"
    f.write_bytes(b"")
    os.utime(f, (at.timestamp(), at.timestamp()))
    return f


class _FakeBridge:
    """Answers execute_in_context from (substring -> lines, or a callable giving them), as in test_hotseat.py."""

    def __init__(self):
        self.replies: list[tuple[str, object]] = []
        self.sent: list[str] = []

    async def where(self, timeout=2.0):
        return "ingame"

    async def execute_in_context(self, context, code, timeout=None):
        self.sent.append(code)
        for key, lines in self.replies:
            if key in code:
                return lines() if callable(lines) else lines
        return []

    def saves_sent(self) -> list[str]:
        """The names Network.SaveGame was asked for, in order."""
        return [m.group(1) for c in self.sent if (m := re.search(r'gf\.Name = "(.*?)"', c))]


def _game(saves: Path, controls: bool = True) -> _FakeBridge:
    """A hotseat game on Heldi's (Gitarja) turn 6, on the turn-change screen or (controls=False) playing.

    Its Network.SaveGame writes the file into `saves`, as the real one would.
    """
    b = "true" if controls else "false"
    conn = _FakeBridge()
    conn.replies.append(("HS|", [f"HS|6|3|LEADER_GITARJA|Heldi|true|true|true|{b}|{b}|true|false"]))
    conn.replies.append(("NAME|", ["NAME|GITARJA 6 3600 BC"]))

    def save():
        _save_file(saves, conn.saves_sent()[-1], datetime.now())
        return ["SAVED|3"]

    conn.replies.append(("Network.SaveGame", save))
    return conn


class TestWorkdayHours:
    def test_inside_one_day(self):
        assert turn_logger.workday_seconds(_at(0, 10), _at(0, 11, 30)) == 90 * 60

    def test_clipped_to_nine_to_five(self):
        assert turn_logger.workday_seconds(_at(0, 7), _at(0, 20)) == 8 * 3600

    def test_overnight_counts_only_working_hours(self):
        assert turn_logger.workday_seconds(_at(0, 16), _at(1, 10)) == 2 * 3600

    def test_weekend_does_not_count(self):
        # Friday 16:30 to Monday 9:45: half an hour, then three quarters.
        assert turn_logger.workday_seconds(_at(4, 16, 30), _at(7, 9, 45)) == 75 * 60

    def test_evening_to_morning_is_nothing(self):
        assert turn_logger.workday_seconds(_at(0, 18), _at(1, 9)) == 0
        assert turn_logger.workday_seconds(_at(1, 10), _at(0, 10)) == 0

    def test_deadline_inside_one_day(self):
        assert turn_logger.workday_deadline(_at(0, 10), 3600) == _at(0, 11)
        assert turn_logger.workday_deadline(_at(0, 10), 0) == _at(0, 10)

    def test_deadline_starts_counting_at_nine(self):
        assert turn_logger.workday_deadline(_at(0, 7), 3600) == _at(0, 10)
        assert turn_logger.workday_deadline(_at(0, 20), 0) == _at(1, 9)

    def test_deadline_skips_nights_and_weekends(self):
        # Friday 16:30: half an hour on Friday, the rest on Monday.
        assert turn_logger.workday_deadline(_at(4, 16, 30), 2 * 3600) == _at(7, 10, 30)
        assert turn_logger.workday_deadline(_at(0, 16), 3600) == _at(0, 17)

    def test_deadline_inverts_workday_seconds(self):
        for start in (_at(0, 8), _at(2, 13, 17), _at(4, 16, 59), _at(5, 12)):
            for seconds in (0, 60, 3600, 7200, 30000):
                assert turn_logger.workday_seconds(start, turn_logger.workday_deadline(start, seconds)) == seconds

    def test_is_workday(self):
        assert turn_logger.is_workday(_at(0, 9))
        assert turn_logger.is_workday(_at(4, 16, 59))
        assert not turn_logger.is_workday(_at(0, 8, 59))
        assert not turn_logger.is_workday(_at(0, 17))
        assert not turn_logger.is_workday(_at(5, 12))


class TestRecords:
    def test_rows_only_on_change(self, tmp_path):
        path = tmp_path / "turn_log.jsonl"
        assert turn_logger.record(63, "LEADER_POUNDMAKER", "Aaron", _at(0, 10), path)
        assert not turn_logger.record(63, "LEADER_POUNDMAKER", "Aaron", _at(0, 10, 5), path)
        assert turn_logger.record(63, "LEADER_BARBAROSSA", "Aidan", _at(0, 11), path)
        assert turn_logger.record(64, "LEADER_POUNDMAKER", "Aaron", _at(0, 12), path)
        rows = turn_logger.read_records(path)
        assert [(r.turn, r.player) for r in rows] == [(63, "Aaron"), (63, "Aidan"), (64, "Aaron")]
        assert rows[1].at == _at(0, 11)
        assert rows[1].leader_display == "Barbarossa"

    def test_bad_rows_are_skipped(self, tmp_path):
        path = tmp_path / "turn_log.jsonl"
        path.write_text("not json\n", encoding="utf-8")
        turn_logger.record(63, "LEADER_POUNDMAKER", "Aaron", _at(0, 10), path)
        assert [r.player for r in turn_logger.read_records(path)] == ["Aaron"]


class TestCurrentTurn:
    def test_first_row_of_the_turn_and_the_row_before(self, tmp_path):
        path = tmp_path / "turn_log.jsonl"
        assert turn_logger.current_turn(path) is None
        turn_logger.record(63, "LEADER_POUNDMAKER", "Aaron", _at(0, 10), path)
        first, previous = turn_logger.current_turn(path)
        assert first.player == "Aaron" and previous is None
        turn_logger.record(63, "LEADER_BARBAROSSA", "Aidan", _at(0, 11), path)
        turn_logger.record(5, "LEADER_ALEXANDER", "Player 1", _at(0, 14), path)  # another save looked at
        turn_logger.record(63, "LEADER_BARBAROSSA", "Aidan", _at(0, 15), path)
        first, previous = turn_logger.current_turn(path)
        assert first.at == _at(0, 11) and previous.player == "Aaron"


class TestDurationOfCurrentTurn:
    def test_empty_log(self, tmp_path):
        assert turn_logger.duration_of_current_turn(_at(0, 12), tmp_path / "turn_log.jsonl") is None

    def test_last_turn_in_workday_seconds(self, tmp_path):
        path = tmp_path / "turn_log.jsonl"
        turn_logger.record(63, "LEADER_POUNDMAKER", "Aaron", _at(0, 10), path)
        turn_logger.record(63, "LEADER_BARBAROSSA", "Aidan", _at(0, 16), path)
        rec, worked = turn_logger.duration_of_current_turn(_at(1, 10), path)
        assert rec.key == (63, "LEADER_BARBAROSSA", "Aidan") and rec.at == _at(0, 16)
        assert worked == 2 * 3600

    def test_loading_another_game_and_back_keeps_the_clock(self, tmp_path):
        path = tmp_path / "turn_log.jsonl"
        turn_logger.record(63, "LEADER_BARBAROSSA", "Aidan", _at(0, 11), path)
        turn_logger.record(5, "LEADER_ALEXANDER", "Player 1", _at(0, 14), path)
        turn_logger.record(63, "LEADER_BARBAROSSA", "Aidan", _at(0, 15), path)
        rec, worked = turn_logger.duration_of_current_turn(_at(0, 16), path)
        assert rec.key == (63, "LEADER_BARBAROSSA", "Aidan") and rec.at == _at(0, 11)
        assert worked == 5 * 3600


class TestSaveOfTurn:
    FIRST = turn_logger.TurnRecord(_at(0, 10, 5), 6, "LEADER_GITARJA", "Heldi")
    PREVIOUS = turn_logger.TurnRecord(_at(0, 9, 30), 6, "LEADER_JULIUS_CAESAR", "Cameron")
    NAME = "GITARJA 6 3600 BC"

    def test_any_save_since_the_turn_can_have_begun(self, tmp_path):
        # The first row can be a poll late, so from 10:04 on every save is this turn's, whatever its name.
        before = _save_file(tmp_path, "AutoSave_0005", _at(0, 10, 3))
        assert turn_logger.save_of_turn(self.FIRST, self.PREVIOUS, self.NAME, [before]) is None
        since = _save_file(tmp_path, "my own name", _at(0, 10, 4))
        assert turn_logger.save_of_turn(self.FIRST, self.PREVIOUS, self.NAME, [before, since]) == since

    def test_nothing_from_before_the_previous_row(self, tmp_path):
        # Cameron was still on at 10:04:30, so a 10:04 save is his, within a poll of the first row or not.
        previous = turn_logger.TurnRecord(_at(0, 10, 4) + timedelta(seconds=30), 6, "LEADER_JULIUS_CAESAR", "Cameron")
        f = _save_file(tmp_path, "JULIUS CAESAR 6 3600 BC", _at(0, 10, 4))
        assert turn_logger.save_of_turn(self.FIRST, previous, self.NAME, [f]) is None

    def test_by_name_however_late_the_turn_was_logged(self, tmp_path):
        first = turn_logger.TurnRecord(_at(0, 12), 6, "LEADER_GITARJA", "Heldi")  # the logger came back hours late
        other = _save_file(tmp_path, "JULIUS CAESAR 6 3600 BC", _at(0, 9, 45))
        named = _save_file(tmp_path, "gitarja 6 3600 bc mcp", _at(0, 9, 46))
        assert turn_logger.save_of_turn(first, self.PREVIOUS, self.NAME, [other]) is None
        assert turn_logger.save_of_turn(first, self.PREVIOUS, self.NAME, [other, named]) == named

    def test_a_same_named_save_from_before_the_previous_row(self, tmp_path):
        # The same leader on the same turn of some other game, unless the log has nothing before this turn.
        stale = _save_file(tmp_path, "GITARJA 6 3600 BC", _at(0, 9))
        assert turn_logger.save_of_turn(self.FIRST, self.PREVIOUS, self.NAME, [stale]) is None
        assert turn_logger.save_of_turn(self.FIRST, None, self.NAME, [stale]) == stale

    def test_without_a_name_from_the_game_only_time_counts(self, tmp_path):
        f = _save_file(tmp_path, "GITARJA 6 3600 BC", _at(0, 9, 45))
        assert turn_logger.save_of_turn(self.FIRST, self.PREVIOUS, "", [f]) is None


class TestPoll:
    def _game(self, tmp_path, monkeypatch, controls=True):
        saves = tmp_path / "Hotseat"
        conn = _game(saves, controls)

        async def open_connection():
            return conn

        monkeypatch.setattr(hotseat, "open_connection", open_connection)
        return conn, saves, tmp_path / "turn_log.jsonl"

    def _poll(self, log, saves):
        return asyncio.run(turn_logger.poll(log, save=True, saves_dir=saves))

    def test_saves_a_turn_nobody_saved(self, tmp_path, monkeypatch):
        conn, saves, log = self._game(tmp_path, monkeypatch)
        st = self._poll(log, saves)
        assert st.player_name == "Heldi"
        assert [r.key for r in turn_logger.read_records(log)] == [(6, "LEADER_GITARJA", "Heldi")]
        assert conn.saves_sent() == ["GITARJA 6 3600 BC MCP"]
        assert (saves / "GITARJA 6 3600 BC MCP.Civ6Save").exists()
        # The next poll finds that save and leaves it at that.
        self._poll(log, saves)
        assert conn.saves_sent() == ["GITARJA 6 3600 BC MCP"]

    def test_a_save_past_the_turn_change_screen_says_so(self, tmp_path, monkeypatch):
        conn, saves, log = self._game(tmp_path, monkeypatch, controls=False)
        self._poll(log, saves)
        assert conn.saves_sent() == ["GITARJA 6 3600 BC MCP MIDTURN"]

    def test_the_players_own_save_is_enough(self, tmp_path, monkeypatch):
        conn, saves, log = self._game(tmp_path, monkeypatch)
        _save_file(saves, "GITARJA 6 3600 BC", datetime.now() - timedelta(minutes=1))
        self._poll(log, saves)
        assert conn.saves_sent() == []

    def test_the_games_autosave_is_not_enough(self, tmp_path, monkeypatch):
        conn, saves, log = self._game(tmp_path, monkeypatch)
        _save_file(saves / "auto", "AutoSave_0006", datetime.now() - timedelta(seconds=30))
        self._poll(log, saves)
        assert conn.saves_sent() == ["GITARJA 6 3600 BC MCP"]

    def test_log_current_turn_never_saves(self, tmp_path, monkeypatch):
        conn, saves, log = self._game(tmp_path, monkeypatch)
        assert turn_logger.log_current_turn(log)
        assert turn_logger.log_current_turn(log)
        assert [r.key for r in turn_logger.read_records(log)] == [(6, "LEADER_GITARJA", "Heldi")]
        assert conn.saves_sent() == []

    def test_no_game_writes_nothing(self, tmp_path, monkeypatch):
        async def open_connection():
            return None

        monkeypatch.setattr(hotseat, "open_connection", open_connection)
        log = tmp_path / "turn_log.jsonl"
        assert not turn_logger.log_current_turn(log)
        assert asyncio.run(turn_logger.poll(log, save=True, saves_dir=tmp_path)) is None
        assert not log.exists()

    def test_a_failed_save_is_logged_not_raised(self, tmp_path, monkeypatch, caplog):
        conn, saves, log = self._game(tmp_path, monkeypatch)
        conn.replies[-1] = ("Network.SaveGame", [])  # no SAVED| confirmation
        with caplog.at_level("ERROR", logger="turn_logger"):
            assert self._poll(log, saves) is not None
        assert "could not see that turn 6 has a save" in caplog.text
        assert turn_logger.read_records(log)  # the turn was logged all the same


HELDI_6 = (6, "LEADER_GITARJA", "Heldi")


class TestTurnLines:
    """Rows from the bridge mod's turn lines in Lua.log, and the loop that reads them."""

    def test_a_turn_line_gives_an_exact_row(self, tmp_path, game_log):
        log = tmp_path / "turn_log.jsonl"
        game_log.turn_line(_at(0, 10, 5), 6, "LEADER_GITARJA", "Heldi", slot=6)
        assert turn_logger.log_turn_lines(turn_logger.turn_watch.default().read(), log)
        (row,) = turn_logger.read_records(log)
        assert (row.at, row.key, row.poll_s) == (_at(0, 10, 5), HELDI_6, 0)

    def test_lines_the_log_already_covers_are_passed_over(self, tmp_path, game_log):
        log = tmp_path / "turn_log.jsonl"
        turn_logger.record(6, "LEADER_GITARJA", "Heldi", _at(0, 10, 5), log, poll_s=0)
        game_log.turn_line(_at(0, 9), 6, "LEADER_JULIUS_CAESAR", "Cameron")  # before the last row
        game_log.turn_line(_at(0, 10, 5), 6, "LEADER_GITARJA", "Heldi")  # that row
        game_log.turn_line(_at(0, 10, 30), 6, "LEADER_GITARJA", "Heldi", why="load")  # a reload of it
        game_log.turn_line(_at(0, 11), 6, "LEADER_GENGHIS_KHAN", "Randy")  # one the logger missed
        assert turn_logger.log_turn_lines(turn_logger.turn_watch.default().read(), log)
        assert [(r.at, r.player) for r in turn_logger.read_records(log)] == [(_at(0, 10, 5), "Heldi"), (_at(0, 11), "Randy")]

    def test_a_poll_takes_the_time_of_the_turns_line(self, tmp_path, monkeypatch, game_log):
        _, _, log = TestPoll()._game(tmp_path, monkeypatch)
        line_at = datetime.now().replace(microsecond=0) - timedelta(seconds=40)
        game_log.turn_line(line_at, 6, "LEADER_GITARJA", "Heldi")
        asyncio.run(turn_logger.poll(log))
        (row,) = turn_logger.read_records(log)
        assert (row.at, row.poll_s) == (line_at, 0)

    def test_a_poll_without_a_line_says_how_late_it_can_be(self, tmp_path, monkeypatch, game_log):
        _, _, log = TestPoll()._game(tmp_path, monkeypatch)
        game_log.turn_line(_at(0, 9), 6, "LEADER_JULIUS_CAESAR", "Cameron")  # the latest line is another turn's
        asyncio.run(turn_logger.poll(log))
        asyncio.run(turn_logger.poll(log, poll_s=turn_logger.SCREEN_POLL_S))
        (row,) = turn_logger.read_records(log)
        assert row.key == HELDI_6 and row.poll_s == turn_logger.POLL_S

    def test_a_turn_line_sets_off_a_save_check_at_once(self, tmp_path, monkeypatch, game_log):
        conn, saves, log = TestPoll()._game(tmp_path, monkeypatch)
        watch = turn_logger.turn_watch.default()
        next_poll = turn_logger.tick(watch, 0.0, log, saves, now=100.0)  # the startup poll
        assert next_poll == 100.0 + turn_logger.POLL_S and conn.saves_sent() == ["GITARJA 6 3600 BC MCP"]
        asked = len(conn.sent)
        _save_file(saves, "GITARJA 6 3600 BC MCP", datetime.now() - timedelta(hours=2))  # a round ago, really
        assert turn_logger.tick(watch, next_poll, log, saves, now=101.0) == next_poll
        assert len(conn.sent) == asked  # nothing new, no poll due: the game is not asked
        game_log.turn_line(datetime.now().replace(microsecond=0), 7, "LEADER_GITARJA", "Heldi")
        conn.replies[0] = ("HS|", ["HS|7|3|LEADER_GITARJA|Heldi|true|true|true|true|true|true|false"])
        conn.replies[1] = ("NAME|", ["NAME|GITARJA 7 3560 BC"])
        assert turn_logger.tick(watch, next_poll, log, saves, now=102.0) == 102.0 + turn_logger.POLL_S
        assert [(r.key, r.poll_s) for r in turn_logger.read_records(log)][-1] == ((7, "LEADER_GITARJA", "Heldi"), 0)
        assert conn.saves_sent()[-1] == "GITARJA 7 3560 BC MCP"

    def test_the_games_own_screen_line_sets_off_a_poll_too(self, tmp_path, monkeypatch, game_log):
        _, saves, log = TestPoll()._game(tmp_path, monkeypatch)
        watch = turn_logger.turn_watch.default()
        turn_logger.record(6, "LEADER_JULIUS_CAESAR", "Cameron", _at(0, 9), log, poll_s=0)
        game_log.screen_line(6)  # no turn line: a bridge mod from before them
        turn_logger.tick(watch, 1e9, log, saves, now=0.0)
        assert [(r.key, r.poll_s) for r in turn_logger.read_records(log)][-1] == (HELDI_6, turn_logger.SCREEN_POLL_S)

    def test_after_an_exact_row_nothing_from_before_it_counts(self, tmp_path):
        # Cameron took 30 seconds, so his own save is newer than SAVE_LOOKBACK_S before Heldi's turn.
        previous = turn_logger.TurnRecord(_at(0, 10, 4) + timedelta(seconds=30), 6, "LEADER_JULIUS_CAESAR", "Cameron", 0)
        first = turn_logger.TurnRecord(_at(0, 10, 5), 6, "LEADER_GITARJA", "Heldi", poll_s=0)
        cameron = _save_file(tmp_path, "JULIUS CAESAR 6 3600 BC MCP", _at(0, 10, 4) + timedelta(seconds=31))
        assert turn_logger.save_of_turn(first, previous, "GITARJA 6 3600 BC", [cameron]) is None

    def test_the_autosave_that_opens_the_game_turn_is_not_the_first_seats_save(self, tmp_path):
        # Turn 22 on 2026-11-05: Kiviuq (last seat) ended at 10:35:01, Aaron's turn line has the same
        # second, and the game's AutoSave_0022 was written a few ms into it; the Load Game list shows
        # it as Kiviuq's. Aaron's turn needs a save of its own.
        previous = turn_logger.TurnRecord(_at(0, 10, 25), 21, "LEADER_LIME_THULE_DAVE", "John", 0)
        first = turn_logger.TurnRecord(_at(0, 10, 35) + timedelta(seconds=1), 22, "LEADER_CLEOPATRA", "Aaron", 0)
        name = "CLEOPATRA 22 2320 BC"
        autosave = _save_file(tmp_path / "auto", "AutoSave_0022", first.at + timedelta(milliseconds=5))
        later = _save_file(tmp_path / "auto", "AutoSave_0022 again", first.at + timedelta(minutes=5))  # a load writes it again
        assert turn_logger.save_of_turn(first, previous, name, [autosave, later]) is None
        own = _save_file(tmp_path, f"{name} MCP", first.at + timedelta(seconds=1))
        assert turn_logger.save_of_turn(first, previous, name, [autosave, later, own]) == own

    def test_a_quicksave_still_counts(self, tmp_path):
        first = turn_logger.TurnRecord(_at(0, 10, 5), 6, "LEADER_GITARJA", "Heldi", poll_s=0)
        quick = _save_file(tmp_path / "quick", "QuickSave", _at(0, 10, 6))
        assert turn_logger.save_of_turn(first, None, "GITARJA 6 3600 BC", [quick]) == quick

    def test_a_row_from_a_long_poll_counts_saves_from_its_whole_window(self, tmp_path):
        first = turn_logger.TurnRecord(_at(0, 10, 5), 6, "LEADER_GITARJA", "Heldi", poll_s=turn_logger.POLL_S)
        f = _save_file(tmp_path, "AutoSave_0006", _at(0, 9, 55))  # ten minutes before the row
        assert turn_logger.save_of_turn(first, None, "GITARJA 6 3600 BC", [f]) == f
        exact = turn_logger.TurnRecord(_at(0, 10, 5), 6, "LEADER_GITARJA", "Heldi", poll_s=0)
        assert turn_logger.save_of_turn(exact, None, "", [f]) is None


class TestReloads:
    """Rows in the reload log, beside the turn log, for every save loaded into the game."""

    def _reload_rows(self, log):
        path = turn_logger.reload_log_path(log)
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_a_turn_replayed_from_its_start(self, tmp_path, game_log):
        log = tmp_path / "turn_log.jsonl"
        watch = turn_logger.turn_watch.default()
        game_log.turn_line(_at(1, 16), 18, "LEADER_CLEOPATRA", "Aaron")
        game_log.start_turn(0)
        turn_logger.log_turn_lines(watch.read(), log)
        game_log.relaunch()
        game_log.load()
        game_log.turn_line(_at(1, 17, 20), 18, "LEADER_CLEOPATRA", "Aaron")
        assert turn_logger.log_turn_lines(watch.read(), log) is False  # the same turn: no turn row
        assert turn_logger.log_reloads(watch.reloads, log) == 1
        (row,) = self._reload_rows(log)
        assert row == {
            "at": _at(1, 17, 20).astimezone().isoformat(timespec="seconds"),
            "turn": 18,
            "leader": "LEADER_CLEOPATRA",
            "player": "Aaron",
            "relaunched": True,
            "menu": True,
            "mid_turn": False,
            "had_started": True,
            "turn_began": _at(1, 16).astimezone().isoformat(timespec="seconds"),
            "snapshot": None,
        }
        assert turn_logger.reload_log_path(log) == tmp_path / "reload_log.jsonl"

    def test_a_load_offered_again_is_written_once(self, tmp_path, game_log):
        log = tmp_path / "turn_log.jsonl"
        game_log.relaunch()
        game_log.load()
        game_log.turn_line(_at(1, 17, 20), 18, "LEADER_CLEOPATRA", "Aaron")
        first = turn_logger.turn_watch.TurnWatch(game_log.path).read()
        assert turn_logger.log_reloads(first, log) == 1
        again = turn_logger.turn_watch.TurnWatch(game_log.path).read()  # the logger restarted
        assert turn_logger.log_reloads(again, log) == 0
        (row,) = self._reload_rows(log)
        assert row["had_started"] is None and row["turn_began"] is None

    def test_turn_began_is_from_before_the_load(self, tmp_path, game_log):
        # A save of a turn the turn log never had: the row the load itself gives is not when it began.
        log = tmp_path / "turn_log.jsonl"
        watch = turn_logger.turn_watch.default()
        game_log.load(menu=False)
        game_log.turn_line(_at(1, 17, 20), 18, "LEADER_CLEOPATRA", "Aaron")
        assert turn_logger.log_turn_lines(watch.read(), log)
        turn_logger.log_reloads(watch.reloads, log)
        (row,) = self._reload_rows(log)
        assert row["turn_began"] is None and row["menu"] is False

    def test_the_loop_writes_each_load_once(self, tmp_path, monkeypatch, game_log):
        conn, saves, log = TestPoll()._game(tmp_path, monkeypatch)
        watch = turn_logger.turn_watch.default()
        game_log.load()  # the game's launch
        game_log.turn_line(datetime.now().replace(microsecond=0) - timedelta(minutes=30), 6, "LEADER_GITARJA", "Heldi")
        turn_logger.tick(watch, 0.0, log, saves, now=100.0)
        assert [r["relaunched"] for r in self._reload_rows(log)] == [True]
        game_log.start_turn(0)
        turn_logger.tick(watch, 1e9, log, saves, now=101.0)
        game_log.load()
        game_log.turn_line(datetime.now().replace(microsecond=0), 6, "LEADER_GITARJA", "Heldi", why="load")
        assert turn_logger.tick(watch, 1e9, log, saves, now=102.0) == 1e9  # the same turn: no row, no poll
        _, row = self._reload_rows(log)
        assert (row["player"], row["mid_turn"], row["had_started"], row["relaunched"]) == ("Heldi", True, True, False)
        assert row["turn_began"] == turn_logger.read_records(log)[0].at.astimezone().isoformat(timespec="seconds")
        assert watch.reloads == []
        assert turn_logger.tick(watch, 1e9, log, saves, now=103.0) == 1e9  # nothing new, no poll due
        assert len(self._reload_rows(log)) == 2


class TestSnapshots:
    """The bridge mod's saves from each opening of the pause menu, named in reload rows and pruned."""

    def test_a_reload_names_the_last_snapshot_before_it(self, tmp_path, game_log, snapshots):
        log = tmp_path / "turn_log.jsonl"
        watch = turn_logger.turn_watch.default()
        turn_logger.record(18, "LEADER_NZINGA_MBANDE", "Aidan", _at(1, 15), log, poll_s=0)
        _save_file(snapshots, "Aidan turn 18 2026-10-27 09.00.00", _at(1, 15, 30))  # the turn before Aaron's
        game_log.turn_line(_at(1, 16), 18, "LEADER_CLEOPATRA", "Aaron")
        game_log.start_turn(0)
        turn_logger.log_turn_lines(watch.read(), log)
        _save_file(snapshots, "Aaron turn 18 2026-10-27 09.00.00", _at(1, 16, 5))  # saved at his turn-change screen
        left = _save_file(snapshots, "Aaron turn 18 2026-10-27 09.00.00 b", _at(1, 17, 12))  # the menu he exited by
        game_log.relaunch()
        game_log.load()
        game_log.turn_line(_at(1, 17, 20), 18, "LEADER_CLEOPATRA", "Aaron")
        _save_file(snapshots, "Aaron turn 18 2026-10-27 17.20.00", _at(1, 17, 21))  # the replay's own
        watch.read()
        assert turn_logger.log_reloads(watch.reloads, log) == 1
        (row,) = turn_logger.read_reloads(log)
        assert row["snapshot"] == left.name

    def test_no_snapshot_since_the_turn_began_is_none(self, tmp_path, game_log, snapshots):
        log = tmp_path / "turn_log.jsonl"
        watch = turn_logger.turn_watch.default()
        _save_file(snapshots, "Aidan turn 18 2026-10-27 09.00.00", _at(1, 15, 30))
        game_log.turn_line(_at(1, 16), 18, "LEADER_CLEOPATRA", "Aaron")
        turn_logger.log_turn_lines(watch.read(), log)
        game_log.load()  # a crash: no pause menu on the way out
        game_log.turn_line(_at(1, 17, 20), 18, "LEADER_CLEOPATRA", "Aaron")
        watch.read()
        turn_logger.log_reloads(watch.reloads, log)
        assert turn_logger.read_reloads(log)[0]["snapshot"] is None

    def test_old_snapshots_go_unless_a_reload_names_them(self, tmp_path, snapshots):
        log = tmp_path / "turn_log.jsonl"
        now = _at(10, 12).timestamp()
        old = _save_file(snapshots, "Aidan turn 18 a", _at(1, 15))
        named = _save_file(snapshots, "Aaron turn 18 a", _at(1, 17))
        recent = _save_file(snapshots, "Aaron turn 25 a", _at(9, 12))
        turn_logger.reload_log_path(log).write_text(
            json.dumps({"at": _at(1, 17, 20).astimezone().isoformat(), "turn": 18, "leader": "LEADER_CLEOPATRA",
                        "player": "Aaron", "snapshot": named.name}) + "\n",
            encoding="utf-8",
        )
        assert turn_logger.prune_snapshots(log, now=now) == 1
        assert not old.exists() and named.exists() and recent.exists()

    def test_a_missing_folder_is_no_snapshot(self, tmp_path):
        assert turn_logger.snapshot_before(_at(1, 17), None, tmp_path / "nowhere") is None
        assert turn_logger.prune_snapshots(tmp_path / "turn_log.jsonl", tmp_path / "nowhere") == 0


class TestTaken:
    """Rows in the taken log, beside the turn log: when each turn was taken, by the player or by Claude."""

    def _rows(self, log):
        return [json.loads(line) for line in turn_logger.taken_log_path(log).read_text(encoding="utf-8").splitlines()]

    def test_the_first_press_of_each_turn_and_taker(self, tmp_path, game_log):
        log = tmp_path / "turn_log.jsonl"
        watch = turn_logger.turn_watch.default()
        game_log.turn_line(_at(1, 16), 18, "LEADER_CLEOPATRA", "Aaron")
        game_log.taken_line(_at(1, 16, 2), 18, "LEADER_CLEOPATRA", "Aaron")
        watch.read()
        assert turn_logger.log_taken(watch.taken, log) == 1
        game_log.load()
        game_log.turn_line(_at(1, 17, 20), 18, "LEADER_CLEOPATRA", "Aaron")  # replayed from its start
        game_log.taken_line(_at(1, 17, 21), 18, "LEADER_CLEOPATRA", "Aaron")
        game_log.taken_line(_at(1, 17, 50), 18, "LEADER_CLEOPATRA", "Aaron", why="standin")  # then left to Claude
        game_log.taken_line(_at(1, 17, 50), 18, "LEADER_CLEOPATRA", "Aaron")
        watch.read()
        assert turn_logger.log_taken(watch.taken, log) == 1
        assert self._rows(log) == [
            {"at": _at(1, 16, 2).astimezone().isoformat(timespec="seconds"), "turn": 18, "leader": "LEADER_CLEOPATRA",
             "player": "Aaron", "by": "player", "poll_s": 0},
            {"at": _at(1, 17, 50).astimezone().isoformat(timespec="seconds"), "turn": 18, "leader": "LEADER_CLEOPATRA",
             "player": "Aaron", "by": "claude", "poll_s": 0},
        ]
        assert turn_logger.taken_log_path(log) == tmp_path / "taken_log.jsonl"
        assert [(r.at, r.key, r.by) for r in turn_logger.read_taken(log)] == [
            (_at(1, 16, 2), (18, "LEADER_CLEOPATRA", "Aaron"), "player"),
            (_at(1, 17, 50), (18, "LEADER_CLEOPATRA", "Aaron"), "claude"),
        ]

    def test_a_restart_adds_nothing(self, tmp_path, game_log):
        log = tmp_path / "turn_log.jsonl"
        game_log.turn_line(_at(1, 16), 18, "LEADER_CLEOPATRA", "Aaron")
        game_log.taken_line(_at(1, 16, 2), 18, "LEADER_CLEOPATRA", "Aaron")
        for _ in range(2):  # the logger, then the logger restarted
            watch = turn_logger.turn_watch.TurnWatch(game_log.path)
            watch.read()
            turn_logger.log_taken(watch.taken, log)
        assert len(self._rows(log)) == 1

    def test_the_loop_writes_the_taken_log(self, tmp_path, monkeypatch, game_log):
        _, saves, log = TestPoll()._game(tmp_path, monkeypatch)
        watch = turn_logger.turn_watch.default()
        started = datetime.now().replace(microsecond=0) - timedelta(minutes=5)
        game_log.turn_line(started, 6, "LEADER_GITARJA", "Heldi", slot=3)
        turn_logger.tick(watch, 0.0, log, saves, now=100.0)
        game_log.taken_line(started + timedelta(minutes=2), 6, "LEADER_GITARJA", "Heldi", slot=3, why="standin")
        game_log.start_turn(3)  # Claude's press, with a copy of the mod from before "play" lines
        assert turn_logger.tick(watch, 1e9, log, saves, now=101.0) == 1e9  # no poll for it
        (row,) = turn_logger.read_taken(log)
        assert (row.at, row.key, row.by, row.poll_s) == (started + timedelta(minutes=2), HELDI_6, "claude", 0)
        assert watch.taken == []

    def test_a_bad_row_is_skipped(self, tmp_path):
        log = tmp_path / "turn_log.jsonl"
        turn_logger.taken_log_path(log).write_text('{"at": "nope"}\n', encoding="utf-8")
        assert turn_logger.read_taken(log) == []
