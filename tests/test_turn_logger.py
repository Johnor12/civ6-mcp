"""The turn log behind the Slack turn timer (rows on change only, workday hours) and the logger's save fallback."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import turn_logger  # noqa: E402

from civ_mcp import hotseat  # noqa: E402

MONDAY = datetime(2026, 10, 26)


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
        # The first row can be a poll late, so from 10:00 on every save is this turn's, whatever its name.
        before = _save_file(tmp_path, "AutoSave_0005", _at(0, 9, 59))
        assert turn_logger.save_of_turn(self.FIRST, self.PREVIOUS, self.NAME, [before]) is None
        since = _save_file(tmp_path, "my own name", _at(0, 10))
        assert turn_logger.save_of_turn(self.FIRST, self.PREVIOUS, self.NAME, [before, since]) == since

    def test_nothing_from_before_the_previous_row(self, tmp_path):
        # Cameron was still on at 10:03, so a 10:02 save is his, within a poll of the first row or not.
        previous = turn_logger.TurnRecord(_at(0, 10, 3), 6, "LEADER_JULIUS_CAESAR", "Cameron")
        f = _save_file(tmp_path, "JULIUS CAESAR 6 3600 BC", _at(0, 10, 2))
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

    def test_an_autosave_is_enough(self, tmp_path, monkeypatch):
        conn, saves, log = self._game(tmp_path, monkeypatch)
        _save_file(saves / "auto", "AutoSave_0006", datetime.now() - timedelta(minutes=2))
        self._poll(log, saves)
        assert conn.saves_sent() == []

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
