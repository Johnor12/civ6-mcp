"""The turn log behind the Slack turn timer: rows on change only, workday hours."""

from __future__ import annotations

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


class TestLogCurrentTurn:
    def test_records_what_the_game_says(self, tmp_path, monkeypatch):
        path = tmp_path / "turn_log.jsonl"
        st = hotseat.HotseatStatus(screen="ingame", is_hotseat=True, turn=63, leader="LEADER_GITARJA", player_name="Heldi")

        async def whose_turn():
            return st

        monkeypatch.setattr(hotseat, "whose_turn", whose_turn)
        assert turn_logger.log_current_turn(path)
        assert turn_logger.log_current_turn(path)
        assert [r.key for r in turn_logger.read_records(path)] == [(63, "LEADER_GITARJA", "Heldi")]

    def test_no_game_writes_nothing(self, tmp_path, monkeypatch):
        path = tmp_path / "turn_log.jsonl"

        async def whose_turn():
            return None

        monkeypatch.setattr(hotseat, "whose_turn", whose_turn)
        assert not turn_logger.log_current_turn(path)
        assert not path.exists()
