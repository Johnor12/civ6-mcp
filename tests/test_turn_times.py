"""Turn times: rebuilding the player-turns from the log, placing their starts from rows and saves."""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import turn_logger  # noqa: E402
import turn_report  # noqa: E402
import turn_times  # noqa: E402

SEATS = (("LEADER_A", "ALPHA"), ("LEADER_B", "BRAVO"), ("LEADER_C", "CHARLIE"), ("LEADER_D", "DELTA"))
NAMES = {"LEADER_A": "Ann", "LEADER_B": "Bob", "LEADER_C": "Cy", "LEADER_D": "Di"}
TUESDAY = datetime(2026, 10, 27)
POLL = 300


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return TUESDAY + timedelta(hours=hour, minutes=minute, seconds=second)


@pytest.fixture(autouse=True)
def seats(monkeypatch):
    monkeypatch.setattr(turn_times, "SEATS", SEATS)


@pytest.fixture
def game(tmp_path):
    """A log and a save folder to fill: game.row(...), game.save(...), game.autosave(...), game.compute()."""

    class Game:
        log_path = tmp_path / "turn_log.jsonl"
        saves = tmp_path / "saves"

        def row(self, when: datetime, turn: int, leader: str) -> None:
            assert turn_logger.record(turn, leader, NAMES.get(leader, "Xi"), now=when, path=self.log_path)

        def _file(self, folder: Path, name: str, when: datetime) -> None:
            folder.mkdir(parents=True, exist_ok=True)
            f = folder / f"{name}.Civ6Save"
            f.write_bytes(b"")
            os.utime(f, (when.timestamp(), when.timestamp()))

        def save(self, when: datetime, name: str) -> None:
            self._file(self.saves, name, when)

        def autosave(self, when: datetime, n: int = 1) -> None:
            self._file(self.saves / "auto", f"AutoSave_{n:04d}", when)

        def compute(self, poll_at=lambda when: POLL):
            self.saves.mkdir(exist_ok=True)
            return turn_times.compute(self.log_path, self.saves, poll_at)

    return Game()


def starts(seq) -> dict[tuple[int, str], datetime]:
    return {(pt.turn, pt.player): pt.start for pt in seq}


def test_poll_interval_follows_the_logger_history():
    assert turn_times.poll_s_at(datetime(2026, 10, 27, 10)) == 300
    assert turn_times.poll_s_at(datetime(2026, 10, 29, 16, 20, 59)) == 300
    assert turn_times.poll_s_at(datetime(2026, 10, 29, 16, 21)) == 60
    assert turn_times.poll_s_at(datetime(2027, 1, 1)) == turn_logger.POLL_S
    assert turn_times.POLL_CHANGES[-1][1] == turn_logger.POLL_S  # keep POLL_CHANGES current


def test_each_row_gets_the_window_of_its_day(game):
    game.row(datetime(2026, 10, 29, 16, 10), 9, "LEADER_A")  # still 5-minute polls
    game.row(datetime(2026, 10, 29, 16, 50), 9, "LEADER_B")  # 1-minute polls by then
    seq, _ = game.compute(turn_times.poll_s_at)
    ann, bob = seq
    assert (ann.earliest, ann.latest, ann.start) == (
        datetime(2026, 10, 29, 16, 5), datetime(2026, 10, 29, 16, 10), datetime(2026, 10, 29, 16, 7, 30))
    assert (bob.earliest, bob.latest, bob.start) == (
        datetime(2026, 10, 29, 16, 49), datetime(2026, 10, 29, 16, 50), datetime(2026, 10, 29, 16, 49, 30))


def test_a_changed_logger_interval_is_flagged(game, monkeypatch, caplog):
    game.row(at(10), 1, "LEADER_A")
    monkeypatch.setattr(turn_logger, "POLL_S", 120)
    game.compute()
    assert "POLL_CHANGES ends at 60 s" in caplog.text


def test_a_row_places_the_start_half_a_poll_before_it(game):
    game.row(at(10), 1, "LEADER_A")
    game.row(at(10, 30), 1, "LEADER_B")
    seq, skipped = game.compute()
    assert [pt.player for pt in seq] == ["Ann", "Bob"]
    assert starts(seq) == {(1, "Ann"): at(9, 57, 30), (1, "Bob"): at(10, 27, 30)}
    assert seq[0].basis == "row" and skipped == []


def test_save_of_a_skipped_player_splits_the_gap(game):
    game.row(at(10), 1, "LEADER_A")
    game.row(at(11), 1, "LEADER_C")  # Bob finished between two polls
    game.save(at(10, 50), "BRAVO 1 4000 BC")
    seq, _ = game.compute()
    assert [pt.player for pt in seq] == ["Ann", "B", "Cy"]  # Bob was never logged, so his leader names him
    bob, cy = seq[1], seq[2]
    assert bob.basis == "save" and (bob.earliest, bob.latest) == (at(10), at(10, 50))
    assert bob.start == at(10, 25)  # halfway between Ann's row and Bob's save
    assert cy.start == at(10, 57, 30)  # halfway between Bob's save (or Cy's row less a poll) and Cy's row
    assert (cy.earliest, cy.latest) == (at(10, 55), at(11))


def test_autosave_pins_the_first_seat(game):
    game.row(at(10), 1, "LEADER_D")
    game.row(at(10, 30), 2, "LEADER_A")
    game.autosave(at(10, 26))
    game.save(at(10, 26, 5), "ALPHA 2 3960 BC")
    seq, _ = game.compute()
    ann = seq[1]
    assert (ann.turn, ann.player, ann.basis) == (2, "Ann", "autosave")
    assert ann.start == ann.earliest == ann.latest == at(10, 26)
    (di, end), = turn_times.finished(seq)
    assert end == at(10, 26) and di.start == at(9, 57, 30)


def test_players_without_evidence_are_spaced_evenly(game):
    game.row(at(10), 1, "LEADER_A")
    game.row(at(10, 30), 1, "LEADER_D")
    seq, _ = game.compute()
    bob, cy, di = seq[1:]
    assert bob.basis == cy.basis == "interpolated"
    assert di.start == at(10, 27, 30)
    # Ann was still playing at her row, so Bob and Cy share 10:00 to 10:27:30.
    assert bob.start == at(10, 9, 10) and cy.start == at(10, 18, 20)
    assert (bob.earliest, bob.latest) == (at(10), at(10, 27, 30))


def test_mcp_and_midturn_saves_count_like_any_other(game):
    game.row(at(10), 1, "LEADER_A")
    game.row(at(10, 20), 1, "LEADER_C")
    game.save(at(10, 4), "BRAVO 1 4000 BC MCP")
    game.save(at(10, 12), "BRAVO 1 4000 BC MCP MIDTURN")
    seq, _ = game.compute()
    bob = seq[1]
    assert len(bob.saves) == 2
    assert bob.start == at(10, 2)  # halfway between Ann's row and the first save
    assert seq[2].start == at(10, 17, 30)  # Bob was still on at 10:12; Cy's row less a poll is later


def test_a_save_the_log_contradicts_is_ignored(game):
    game.row(at(10), 1, "LEADER_A")
    game.row(at(10, 30), 1, "LEADER_B")
    game.save(at(10, 45), "ALPHA 1 4000 BC")  # after Bob's row: not Ann's turn any more
    seq, skipped = game.compute()
    assert seq[0].saves == [] and seq[0].basis == "row"
    assert len(skipped) == 1 and skipped[0].startswith("ALPHA 1 4000 BC")


def test_saves_of_strangers_and_other_turns_are_left_alone(game):
    game.row(at(10), 1, "LEADER_A")
    game.row(at(10, 30), 1, "LEADER_B")
    game.save(at(10, 10), "POUNDMAKER 1 4000 BC")  # another game
    game.save(at(10, 10), "ALPHA 7 3520 BC")  # a turn the log does not cover
    game.save(at(10, 10), "QuickSave")
    game.save(at(9), "ALPHA 1 4000 BC")  # before the log began
    seq, skipped = game.compute()
    assert all(pt.saves == [] for pt in seq)
    assert skipped == ["POUNDMAKER 1 4000 BC: no seat is named 'POUNDMAKER'"]


def test_an_autosave_that_opens_no_logged_turn_is_reported(game):
    game.row(at(10), 1, "LEADER_A")
    game.row(at(10, 30), 1, "LEADER_B")
    game.autosave(at(10, 10))
    seq, skipped = game.compute()
    assert all(pt.autosave is None for pt in seq)
    assert len(skipped) == 1 and "AutoSave_0001" in skipped[0]


def test_a_row_that_goes_backwards_is_skipped(game, caplog):
    game.row(at(10), 1, "LEADER_A")
    game.row(at(10, 20), 1, "LEADER_C")
    game.row(at(10, 25), 1, "LEADER_B")  # an earlier save reloaded
    game.row(at(10, 40), 1, "LEADER_D")
    seq, _ = game.compute()
    assert [(pt.turn, pt.player) for pt in seq] == [(1, "Ann"), (1, "Bob"), (1, "Cy"), (1, "Di")]
    assert seq[1].rows == [] and seq[2].rows == [at(10, 20)]
    assert "skipped" in caplog.text


def test_a_leader_not_in_seats_is_an_error(game):
    game.row(at(10), 1, "LEADER_A")
    game.row(at(10, 30), 1, "LEADER_X")
    with pytest.raises(ValueError, match="LEADER_X is not in SEATS"):
        game.compute()


def test_empty_log_gives_nothing(game):
    assert game.compute() == ([], [])
    assert turn_times.report([], []) == "The log has only the turn in progress; nothing to measure yet."
    assert turn_report.message([]) == ""


def test_player_averages_slowest_first(game):
    game.row(at(10), 1, "LEADER_A")
    game.row(at(10, 10), 1, "LEADER_B")
    game.row(at(11), 1, "LEADER_C")
    game.row(at(11, 5), 1, "LEADER_D")
    game.row(at(11, 15), 2, "LEADER_A")
    game.row(at(11, 25), 2, "LEADER_B")
    game.row(at(11, 30), 2, "LEADER_C")  # in progress
    seq, _ = game.compute()
    averages = turn_times.player_averages(turn_times.finished(seq))
    assert [(a.player, a.turns, round(a.mean_minutes, 1)) for a in averages] == [
        ("Bob", 2, 27.5),  # 50 min, then 5 min
        ("Ann", 2, 10.0),
        ("Di", 1, 10.0),  # ties go by seat
        ("Cy", 1, 5.0),
    ]
    assert averages[0].max_minutes == 50.0 and averages[0].median_minutes == 27.5


def test_minutes_are_workday_time(game):
    game.row(at(16, 40), 1, "LEADER_A")
    game.row(at(24 + 9, 10), 1, "LEADER_B")  # Ann held the turn overnight
    seq, _ = game.compute()
    (ann, end), = turn_times.finished(seq)
    assert (ann.start, end) == (at(16, 37, 30), at(24 + 9, 7, 30))
    assert turn_times.minutes(ann, end) == 22.5 + 7.5  # to 17:00, then from 9:00
    assert turn_times.ended_on(seq, date(2026, 10, 28)) == [(ann, end)]
    assert turn_times.ended_on(seq, date(2026, 10, 27)) == []


def _week(game) -> None:
    game.row(at(16, 40), 1, "LEADER_A")
    game.row(at(16, 50), 1, "LEADER_B")  # Ann: 10 min, on Tuesday
    game.row(at(24 + 9, 10), 1, "LEADER_C")  # Bob: held overnight, 20 min of workday time, ends Wednesday
    game.row(at(24 + 9, 40), 1, "LEADER_D")  # Cy: 30 min on Wednesday
    game.row(at(24 + 10), 2, "LEADER_A")  # Di: 20 min on Wednesday; Ann's turn 2 is in progress


def test_report_message_has_yesterday_then_everything(game):
    _week(game)
    seq, _ = game.compute()
    text = turn_report.message(seq, today=date(2026, 10, 29))  # a Thursday
    assert text.splitlines() == [
        f"*Turn times* in workday hours ({turn_logger.WORKDAY}), slowest first.",
        "",
        "*Yesterday (Wed 28 Oct): 3 turns, 23m average*",
        "1. Cy: 30m",
        "2. Bob: 20m",
        "3. Di: 20m",
        "No turn: Ann",
        "",
        "*Since Tue 27 Oct: 4 turns, 20m average*",
        "1. Cy: 30m",
        "2. Bob: 20m",
        "3. Di: 20m",
        "4. Ann: 10m",
    ]


def test_monday_report_covers_friday(game):
    _week(game)
    seq, _ = game.compute()
    lines = turn_report.message(seq, today=date(2026, 11, 2)).splitlines()  # a Monday
    assert lines[2] == "*Friday 30 Oct: 0 turns*"
    assert lines[3:5] == ["", "*Since Tue 27 Oct: 4 turns, 20m average*"]


def test_previous_workday():
    assert turn_report.previous_workday(date(2026, 10, 28)) == date(2026, 10, 27)
    assert turn_report.previous_workday(date(2026, 11, 2)) == date(2026, 10, 30)  # Monday: Friday
    assert turn_report.previous_workday(date(2026, 11, 1)) == date(2026, 10, 30)  # Sunday: Friday


def test_next_post_is_the_coming_weekday_nine_oclock():
    assert turn_report.next_post(at(8, 59)) == at(9)
    assert turn_report.next_post(at(9)) == at(24 + 9)
    assert turn_report.next_post(at(16, 30)) == at(24 + 9)
    friday, monday = datetime(2026, 10, 30, 10), datetime(2026, 11, 2, 9)
    assert turn_report.next_post(friday) == monday
    assert turn_report.next_post(datetime(2026, 10, 31, 8)) == monday  # Saturday
    assert turn_report.next_post(datetime(2026, 11, 2, 8, 30)) == monday


def test_post_never_raises(monkeypatch):
    class Client:
        def chat_postMessage(self, **kwargs):
            raise RuntimeError("slack is down")

    monkeypatch.setattr(turn_report, "build", lambda: "report")
    assert turn_report.post(Client()) is False
    monkeypatch.setattr(turn_report, "build", lambda: (_ for _ in ()).throw(ValueError("bad seat")))
    assert turn_report.post(Client()) is False


def test_post_sends_the_message(monkeypatch):
    sent = []

    class Client:
        def chat_postMessage(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(turn_report, "build", lambda: "report")
    assert turn_report.post(Client()) is True
    assert sent == [{"channel": turn_report.CHANNEL, "text": "report"}]
    monkeypatch.setattr(turn_report, "build", lambda: "")
    assert turn_report.post(Client()) is False and len(sent) == 1
