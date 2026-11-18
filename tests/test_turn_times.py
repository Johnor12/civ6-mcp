"""Turn times: rebuilding the player-turns from the log, placing their starts from rows and saves."""

from __future__ import annotations

import json
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
def game(tmp_path, monkeypatch):
    """A log and a save folder to fill: game.row(...), game.save(...), game.autosave(...), game.compute()."""

    class Game:
        log_path = tmp_path / "turn_log.jsonl"
        saves = tmp_path / "saves"

        def row(self, when: datetime, turn: int, leader: str, poll_s: int = POLL) -> None:
            """A row as the logger writes it, polling every `poll_s`."""
            monkeypatch.setattr(turn_logger, "POLL_S", poll_s)
            assert turn_logger.record(turn, leader, NAMES.get(leader, "Xi"), now=when, path=self.log_path)

        def legacy_row(self, when: datetime, turn: int, leader: str) -> None:
            """A row as the logger wrote it before rows carried poll_s."""
            row = {"at": when.astimezone().isoformat(timespec="seconds"), "turn": turn, "leader": leader,
                   "player": NAMES.get(leader, "Xi")}
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

        def taken(self, when: datetime, turn: int, leader: str, by: str = "player", poll_s: int = 0) -> None:
            """A taken log row: the player pressed Start Turn, or (by="claude") Claude took the seat."""
            row = {"at": when.astimezone().isoformat(timespec="seconds"), "turn": turn, "leader": leader,
                   "player": NAMES.get(leader, "Xi"), "by": by, "poll_s": poll_s}
            with open(turn_logger.taken_log_path(self.log_path), "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

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


def report(seq, today: date) -> dict:
    """The dashboard's data as the morning's run on `today` writes it."""
    data = turn_report.report_data(seq, today=today, now=datetime.combine(today, turn_report.POST_AT))
    assert data is not None
    return data


def summary(data: dict, scope: str) -> dict:
    return next(r for r in data["summary"] if r["scope"] == scope)


def records(data: dict, scope: str) -> dict[str, tuple]:
    return {r["record"]: (r["time"], r["player"], r["turn"], r["day"]) for r in data["records"] if r["scope"] == scope}


def players(data: dict, scope: str) -> dict[str, dict]:
    return {r["player"]: r for r in data["players"] if r["scope"] == scope}


def starts(seq) -> dict[tuple[int, str], datetime]:
    return {(pt.turn, pt.player): pt.start for pt in seq}


def test_poll_interval_follows_the_logger_history():
    assert turn_times.poll_s_at(datetime(2026, 10, 27, 10)) == 300
    assert turn_times.poll_s_at(datetime(2026, 10, 29, 16, 20, 59)) == 300
    assert turn_times.poll_s_at(datetime(2026, 10, 29, 16, 21)) == 60
    assert turn_times.poll_s_at(datetime(2027, 1, 1)) == 60  # rows written since 2026-11-03 carry their own


def test_each_legacy_row_gets_the_window_of_its_day(game):
    game.legacy_row(datetime(2026, 10, 29, 16, 10), 9, "LEADER_A")  # still 5-minute polls
    game.legacy_row(datetime(2026, 10, 29, 16, 50), 9, "LEADER_B")  # 1-minute polls by then
    seq, _ = game.compute(turn_times.poll_s_at)
    ann, bob = seq
    assert (ann.earliest, ann.latest, ann.start) == (
        datetime(2026, 10, 29, 16, 5), datetime(2026, 10, 29, 16, 10), datetime(2026, 10, 29, 16, 7, 30))
    assert (bob.earliest, bob.latest, bob.start) == (
        datetime(2026, 10, 29, 16, 49), datetime(2026, 10, 29, 16, 50), datetime(2026, 10, 29, 16, 49, 30))


def test_a_row_carries_its_own_window(game):
    # Rows from before poll_s went by the date; the 10-second rows after them by what they say.
    game.legacy_row(datetime(2026, 11, 3, 14, 0), 17, "LEADER_A")
    game.row(datetime(2026, 11, 3, 14, 30, 10), 17, "LEADER_B", poll_s=10)
    game.row(datetime(2026, 11, 3, 14, 31, 40), 17, "LEADER_C", poll_s=10)
    seq, _ = game.compute(turn_times.poll_s_at)
    ann, bob, cy = seq
    assert (ann.poll_s, bob.poll_s, cy.poll_s) == (60, 10, 10)
    assert (ann.earliest, ann.start) == (datetime(2026, 11, 3, 13, 59), datetime(2026, 11, 3, 13, 59, 30))
    assert (bob.earliest, bob.start) == (datetime(2026, 11, 3, 14, 30), datetime(2026, 11, 3, 14, 30, 5))
    assert turn_times.minutes(bob, cy.start) == 1.5


def test_the_record_writes_its_interval(game):
    game.row(at(10), 1, "LEADER_A", poll_s=10)
    game.legacy_row(at(10, 5), 1, "LEADER_B")
    assert [r.poll_s for r in turn_logger.read_records(game.log_path)] == [10, None]


def test_hms():
    assert turn_times.hms(4 + 22 / 60) == "4m 22s"
    assert turn_times.hms(95.4) == "1h 35m 24s"
    assert turn_times.hms(0.2) == "0m 12s"


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
    assert turn_report.report_data([]) is None


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


def test_report_data_has_yesterday_then_everything(game):
    _week(game)
    seq, _ = game.compute()
    data = report(seq, date(2026, 10, 29))  # a Thursday
    info, = data["info"]
    assert (info["day"], info["label"], info["since"]) == ("2026-10-28", "Yesterday (Wed 28 Oct)", "2026-10-27")
    assert info["headline"] == (
        "Yesterday (Wed 28 Oct): 3 turns (new record! Previous: 1, Tue 27 Oct). Average turn time 23m 20s."
    )
    assert (info["now_turn"], info["now_player"], info["now_since"]) == (2, "Ann", "2026-10-28T09:57:30")
    assert (info["day_start"], info["day_end"]) == ("2026-10-28T09:00:00", "2026-10-28T17:00:00")
    assert info["generated"] == "2026-10-29T09:00:00"

    yesterday = summary(data, "yesterday")
    assert (yesterday["turns"], yesterday["mean_turn_s"], yesterday["mean_turn"]) == (3, 1400, "23m 20s")
    assert (yesterday["mean_play_s"], yesterday["total_play"], yesterday["claude_turns"]) == (None, None, 0)
    assert records(data, "yesterday") == {
        "Slowest turn": ("30m 00s", "Cy", 1, "2026-10-28"),
        "Fastest turn": ("20m 00s", "Bob", 1, "2026-10-28"),  # ties Di; the first turn takes it
    }
    assert [(r["player"], r["turns"], r["mean_turn"]) for r in players(data, "yesterday").values()] == [
        ("Ann", 0, None), ("Bob", 1, "20m 00s"), ("Cy", 1, "30m 00s"), ("Di", 1, "20m 00s"),  # seat order
    ]

    everything = summary(data, "all")
    assert (everything["turns"], everything["mean_turn"], everything["median_turn"]) == (4, "20m 00s", "20m 00s")
    assert (everything["days"], everything["turns_per_day"]) == (2, 2.0)
    assert (everything["most_turns"], everything["most_day"]) == (3, "2026-10-28")
    assert (everything["fewest_turns"], everything["fewest_day"]) == (1, "2026-10-27")
    assert records(data, "all") == {
        "Slowest turn": ("30m 00s", "Cy", 1, "2026-10-28"),
        "Fastest turn": ("10m 00s", "Ann", 1, "2026-10-27"),
    }
    assert [(r["player"], r["turns"], r["mean_turn_s"]) for r in players(data, "all").values()] == [
        ("Ann", 1, 600), ("Bob", 1, 1200), ("Cy", 1, 1800), ("Di", 1, 1200),
    ]

    assert data["days"] == [
        {"day": "2026-10-27", "turns": 1, "mean_turn_s": 600, "mean_play_s": None, "claude_turns": 0},
        {"day": "2026-10-28", "turns": 3, "mean_turn_s": 1400, "mean_play_s": None, "claude_turns": 0},
    ]
    assert [(r["day"], r["player"], r["turns"], r["mean_turn_s"]) for r in data["player_days"]] == [
        ("2026-10-27", "Ann", 1, 600), ("2026-10-28", "Bob", 1, 1200), ("2026-10-28", "Cy", 1, 1800),
        ("2026-10-28", "Di", 1, 1200),
    ]
    assert [(r["player"], r["start"], r["end"], r["turn_s"], r["taken"], r["claude"]) for r in data["turns"]] == [
        ("Bob", "2026-10-27T16:47:30", "2026-10-28T09:07:30", 1200, None, False),
        ("Cy", "2026-10-28T09:07:30", "2026-10-28T09:37:30", 1800, None, False),
        ("Di", "2026-10-28T09:37:30", "2026-10-28T09:57:30", 1200, None, False),
    ]
    text = turn_report.dumps(data)
    assert json.loads(text) == data
    assert len(text.splitlines()) == sum(len(rows) + 2 for rows in data.values()) + 2  # a row to a line


def test_report_counts_each_players_turns(game):
    _week(game)
    game.row(at(24 + 10, 15), 2, "LEADER_B")  # Ann's turn 2: 15 min on Wednesday
    game.row(at(24 + 10, 20), 2, "LEADER_C")  # Bob's turn 2: 5 min on Wednesday; Cy's turn 2 in progress
    seq, _ = game.compute()
    data = report(seq, date(2026, 10, 29))
    yesterday = summary(data, "yesterday")
    assert (yesterday["turns"], yesterday["mean_turn"]) == (5, "18m 00s")
    assert records(data, "yesterday") == {
        "Slowest turn": ("30m 00s", "Cy", 1, "2026-10-28"), "Fastest turn": ("5m 00s", "Bob", 2, "2026-10-28"),
    }
    assert [(r["player"], r["turns"], r["mean_turn"]) for r in players(data, "yesterday").values()] == [
        ("Ann", 1, "15m 00s"), ("Bob", 2, "12m 30s"), ("Cy", 1, "30m 00s"), ("Di", 1, "20m 00s"),  # Bob: 20, then 5
    ]
    assert (summary(data, "all")["turns"], summary(data, "all")["mean_turn"]) == (6, "16m 40s")
    assert [(r["player"], r["turns"], r["mean_turn"], r["max_turn"]) for r in players(data, "all").values()] == [
        ("Ann", 2, "12m 30s", "15m 00s"), ("Bob", 2, "12m 30s", "20m 00s"), ("Cy", 1, "30m 00s", "30m 00s"),
        ("Di", 1, "20m 00s", "20m 00s"),
    ]


def test_report_gives_seconds(game):
    # Rows from the bridge's turn line carry the second the turn began.
    game.row(at(10), 1, "LEADER_A", poll_s=0)
    game.row(at(10, 4, 23), 1, "LEADER_B", poll_s=0)  # Ann: 4m 23s
    game.row(at(10, 11, 30), 1, "LEADER_C", poll_s=0)  # Bob: 7m 07s; Cy's turn in progress
    seq, _ = game.compute()
    data = report(seq, date(2026, 10, 28))
    assert data["info"][0]["headline"] == "Yesterday (Tue 27 Oct): 2 turns. Average turn time 5m 45s."
    assert (summary(data, "yesterday")["mean_turn_s"], summary(data, "yesterday")["mean_turn"]) == (345, "5m 45s")
    assert records(data, "yesterday") == {
        "Slowest turn": ("7m 07s", "Bob", 1, "2026-10-27"), "Fastest turn": ("4m 23s", "Ann", 1, "2026-10-27"),
    }
    assert [(r["player"], r["turn_s"]) for r in data["turns"]] == [("Ann", 263), ("Bob", 427)]


def test_monday_report_covers_friday(game):
    _week(game)
    seq, _ = game.compute()
    data = report(seq, date(2026, 11, 2))  # a Monday
    info, = data["info"]
    assert (info["day"], info["label"]) == ("2026-10-30", "Friday 30 Oct")
    assert info["headline"] == "Friday 30 Oct: no turns (record: 3, Wed 28 Oct)."
    assert info["day_end"] == "2026-10-30T16:00:00"  # Fridays end an hour early
    assert summary(data, "yesterday")["turns"] == 0 and data["turns"] == [] and records(data, "yesterday") == {}
    assert all(r["turns"] == 0 for r in players(data, "yesterday").values())
    # Every weekday up to the reported one has a row; the days without a turn do not count as the fewest.
    assert [(r["day"], r["turns"], r["mean_turn_s"]) for r in data["days"]] == [
        ("2026-10-27", 1, 600), ("2026-10-28", 3, 1400), ("2026-10-29", 0, None), ("2026-10-30", 0, None),
    ]
    everything = summary(data, "all")
    assert (everything["turns"], everything["days"], everything["fewest_turns"]) == (4, 2, 1)


def test_the_report_stops_at_the_previous_workday(game):
    _week(game)
    seq, _ = game.compute()
    data = report(seq, date(2026, 10, 28))  # Wednesday morning: Wednesday's three turns are for tomorrow's report
    assert summary(data, "all")["turns"] == 1
    assert [r["day"] for r in data["days"]] == ["2026-10-27"]
    assert turn_report.report_data(seq, today=date(2026, 10, 27)) is None  # nothing had finished by Monday


def test_workday_of():
    wednesday = TUESDAY + timedelta(days=1)
    assert turn_times.workday_of(wednesday + timedelta(hours=9)) == date(2026, 10, 28)
    assert turn_times.workday_of(wednesday + timedelta(hours=23, minutes=59)) == date(2026, 10, 28)
    assert turn_times.workday_of(wednesday + timedelta(hours=8, minutes=59)) == date(2026, 10, 27)  # overnight
    assert turn_times.workday_of(datetime(2026, 10, 31, 12)) == date(2026, 10, 30)  # Saturday: Friday
    assert turn_times.workday_of(datetime(2026, 11, 2, 8, 30)) == date(2026, 10, 30)  # Monday morning: Friday
    assert turn_times.workday_of(datetime(2026, 11, 2, 9)) == date(2026, 11, 2)


def test_a_turn_finished_overnight_counts_for_the_day_before(game):
    game.row(at(16, 35), 1, "LEADER_A", poll_s=0)  # Ann's turn begins Tuesday afternoon
    game.taken(at(24 + 8, 32), 1, "LEADER_A")  # she plays it at 8:32 Wednesday
    game.row(at(24 + 8, 46), 1, "LEADER_B", poll_s=0)  # and ends it at 8:46, before the workday
    game.taken(at(24 + 8, 57), 1, "LEADER_B")
    game.row(at(24 + 8, 58), 1, "LEADER_C", poll_s=0)  # Bob ends his at 8:58: Tuesday's too
    game.row(at(24 + 9, 30), 1, "LEADER_D", poll_s=0)  # Cy's, ending at 9:30, is Wednesday's
    seq, _ = game.compute()
    assert [pt.player for pt, _ in turn_times.ended_on(seq, date(2026, 10, 27))] == ["Ann", "Bob"]
    assert [pt.player for pt, _ in turn_times.ended_on(seq, date(2026, 10, 28))] == ["Cy"]
    assert turn_times.turns_per_day(seq) == {date(2026, 10, 27): 2, date(2026, 10, 28): 1}
    data = report(seq, date(2026, 10, 28))
    yesterday = summary(data, "yesterday")
    # Ann: 16:35 to 17:00, then 8:32 to 8:46; Bob, with no workday time, is left out of the average.
    assert (yesterday["turns"], yesterday["mean_turn"], yesterday["off_hours"]) == (2, "39m 00s", 1)
    assert (yesterday["mean_play"], yesterday["total_play"], yesterday["play_turns"]) == ("7m 30s", "15m 00s", 2)
    assert records(data, "yesterday") == {
        "Slowest turn": ("39m 00s", "Ann", 1, "2026-10-27"),  # no fastest: Bob's 1 minute does not count
        "Longest play time": ("14m 00s", "Ann", 1, "2026-10-27"),
        "Shortest play time": ("1m 00s", "Bob", 1, "2026-10-27"),
    }
    assert [(r["player"], r["turns"], r["mean_turn"], r["off_hours"], r["mean_play"])
            for r in players(data, "yesterday").values()] == [
        ("Ann", 1, "39m 00s", 0, "14m 00s"), ("Bob", 1, None, 1, "1m 00s"), ("Cy", 0, None, 0, None),
        ("Di", 0, None, 0, None),
    ]
    assert summary(data, "all")["turns"] == 2  # Cy's turn is Wednesday's


def test_report_holds_the_day_against_the_record(game):
    # Turns ending on Tue 27: 2, Wed 28: 3, Thu 29: 3, Sat 31: 5 (Friday's), Mon 02: 1; the last is in progress.
    k = 0
    for days, rows in ((0, 3), (1, 3), (2, 3), (4, 5), (6, 1)):
        for i in range(rows):
            game.row(at(24 * days + 10, 10 * i), 1 + k // 4, SEATS[k % 4][0], poll_s=0)
            k += 1
    seq, _ = game.compute()
    assert turn_times.turns_per_day(seq) == {
        date(2026, 10, 27): 2, date(2026, 10, 28): 3, date(2026, 10, 29): 3,
        date(2026, 10, 30): 5, date(2026, 11, 2): 1,
    }

    def turns_line(today: date) -> str:
        return report(seq, today)["info"][0]["headline"].split(". ")[0].rstrip(".")

    assert turns_line(date(2026, 10, 28)) == "Yesterday (Tue 27 Oct): 2 turns"  # the first day has no record to hold
    assert turns_line(date(2026, 10, 29)) == "Yesterday (Wed 28 Oct): 3 turns (new record! Previous: 2, Tue 27 Oct)"
    assert turns_line(date(2026, 10, 30)) == "Yesterday (Thu 29 Oct): 3 turns (ties the record, Wed 28 Oct)"
    # Saturday's turns count for Friday.
    assert turns_line(date(2026, 11, 2)) == "Friday 30 Oct: 5 turns (new record! Previous: 3, Wed 28 Oct)"
    assert turns_line(date(2026, 11, 3)) == (
        "Yesterday (Mon 02 Nov): 1 turn (record: 5, Fri 30 Oct), the fewest in a day so far"
    )
    everything = summary(report(seq, date(2026, 11, 3)), "all")
    assert (everything["most_turns"], everything["most_day"]) == (5, "2026-10-30")
    assert (everything["fewest_turns"], everything["fewest_day"]) == (1, "2026-11-02")
    assert (everything["days"], everything["turns_per_day"]) == (5, 2.8)


def test_previous_workday():
    assert turn_report.previous_workday(date(2026, 10, 28)) == date(2026, 10, 27)
    assert turn_report.previous_workday(date(2026, 11, 2)) == date(2026, 10, 30)  # Monday: Friday
    assert turn_report.previous_workday(date(2026, 11, 1)) == date(2026, 10, 30)  # Sunday: Friday


def test_next_post_is_the_coming_weekday_morning():
    assert turn_report.next_post(at(8, 59)) == at(9, 0)
    assert turn_report.next_post(at(9, 0)) == at(24 + 9, 0)
    assert turn_report.next_post(at(16, 30)) == at(24 + 9, 0)
    friday, monday = datetime(2026, 10, 30, 10), datetime(2026, 11, 2, 9, 0)
    assert turn_report.next_post(friday) == monday
    assert turn_report.next_post(datetime(2026, 10, 31, 8)) == monday  # Saturday
    assert turn_report.next_post(datetime(2026, 11, 2, 8, 30)) == monday


DATA = {"info": [{"headline": "Yesterday (Tue 27 Oct): 2 turns.", "generated": "2026-10-28T09:00:00"}]}
LINK = f"<{turn_report.ARTIFACT_URL}|Turn times: yesterday and the whole game>"


def test_post_never_raises(monkeypatch):
    class Client:
        def chat_postMessage(self, **kwargs):
            raise RuntimeError("slack is down")

    monkeypatch.setattr(turn_report, "refresh_dashboard", lambda: (DATA, None))
    assert turn_report.post(Client()) is False
    monkeypatch.setattr(turn_report, "refresh_dashboard", lambda: (_ for _ in ()).throw(ValueError("bad seat")))
    assert turn_report.post(Client()) is False


def test_post_sends_the_headline_and_the_link(monkeypatch):
    sent = []

    class Client:
        def chat_postMessage(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(turn_report, "refresh_dashboard", lambda: (DATA, None))
    assert turn_report.post(Client()) is True
    assert sent == [{"channel": turn_report.CHANNEL, "text": f"Yesterday (Tue 27 Oct): 2 turns.\n{LINK}"}]
    # A failed refresh still posts, and says the page is behind.
    monkeypatch.setattr(turn_report, "refresh_dashboard", lambda: (DATA, "refresh session abc failed"))
    assert turn_report.post(Client()) is True
    assert sent[1]["text"].splitlines()[:2] == ["Yesterday (Tue 27 Oct): 2 turns.", LINK]
    assert "could not be refreshed" in sent[1]["text"].splitlines()[2]
    monkeypatch.setattr(turn_report, "refresh_dashboard", lambda: (None, None))
    assert turn_report.post(Client()) is False and len(sent) == 2


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A stand-in for the claude CLI: session.calls lists what was run, session.done(...) is what the session writes."""

    class Session:
        started = "backgrounded · abc123\n  claude agents             list sessions\n"
        state = "working"
        result: dict | None = None

        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def done(self, **fields) -> None:
            self.result = fields

    fake = Session()
    monkeypatch.setattr(turn_report, "DONE_PATH", tmp_path / "turn_report_refresh.json")

    def claude(*args: str, timeout: float = 60):
        fake.calls.append(args)
        out = ""
        if args[0] == "--bg":
            out = fake.started
            if fake.result is not None:
                turn_report.DONE_PATH.write_text(json.dumps(fake.result), encoding="utf-8")
        elif args[0] == "agents":
            out = json.dumps([{"id": "abc123", "kind": "background", "state": fake.state}])
        return turn_report.subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(turn_report, "_claude", claude)
    monkeypatch.setattr(turn_report.time, "sleep", lambda s: None)
    return fake


def test_refresh_waits_for_the_sessions_result_file_and_removes_the_session(session):
    # Yesterday's result file is cleared first, so a session that writes nothing cannot pass for done.
    turn_report.DONE_PATH.write_text(json.dumps({"generated": "2026-10-28T09:00:00"}), encoding="utf-8")
    session.done(generated="2026-10-28T09:00:00", asset="/_blob/1")
    assert turn_report.refresh("2026-10-28T09:00:00") is None
    start, *_, stop, rm = session.calls
    assert start[0] == "--bg" and start[-2:] == ("--", turn_report.REFRESH_PROMPT)
    assert start[start.index("--strict-mcp-config") + 1:][:2] == ("--mcp-config", turn_report.NO_MCP)
    assert start[start.index("--settings") + 1] == turn_report.REFRESH_SETTINGS
    assert (stop, rm) == (("stop", "abc123"), ("rm", "abc123"))


def test_refresh_reports_what_went_wrong(session):
    session.done(generated="2026-10-28T09:00:00", error="step 2 failed: not signed in")
    assert turn_report.refresh("2026-10-28T09:00:00") == "step 2 failed: not signed in"
    assert session.calls[-2:] == [("stop", "abc123"), ("rm", "abc123")]

    session.calls.clear()
    session.result, session.state = None, "failed"
    assert turn_report.refresh("2026-10-28T09:00:00") == "refresh session abc123 failed"
    assert session.calls[-2:] == [("stop", "abc123"), ("rm", "abc123")]

    session.calls.clear()
    session.done(generated="2026-10-27T09:00:00", asset="/_blob/0")  # a result for some other morning's data
    session.state = "working"
    assert turn_report.refresh("2026-10-28T09:00:00", timeout_s=0.05) == "refresh session abc123 was not done after 0 s"

    session.calls.clear()
    session.started = "Workspace not trusted."
    assert turn_report.refresh("2026-10-28T09:00:00").startswith("claude --bg did not start a session: Workspace not")
    assert len(session.calls) == 1  # nothing to stop


def test_refresh_dashboard_writes_the_data_file_first(game, tmp_path, monkeypatch):
    _week(game)
    seq, skipped = game.compute()
    monkeypatch.setattr(turn_report, "DATA_PATH", tmp_path / "data" / "turn_report.json")
    monkeypatch.setattr(turn_report.turn_times, "compute", lambda: (seq, skipped))
    thursday = report(seq, date(2026, 10, 29))
    monkeypatch.setattr(turn_report, "report_data", lambda seq, today, now: thursday)
    seen = []

    def refresh(generated: str):
        seen.append(json.loads(turn_report.DATA_PATH.read_text(encoding="utf-8"))["info"][0]["generated"])
        return None if generated == seen[-1] else "the file is not this morning's"

    monkeypatch.setattr(turn_report, "refresh", refresh)
    data, why = turn_report.refresh_dashboard()
    assert why is None and seen == ["2026-10-29T09:00:00"] and data["info"][0]["day"] == "2026-10-28"

    def no_claude(generated: str):
        raise RuntimeError("claude is not on PATH")

    monkeypatch.setattr(turn_report, "refresh", no_claude)
    assert turn_report.refresh_dashboard()[1] == "RuntimeError: claude is not on PATH"


# Play time: from when the turn was taken (the taken log) to its end.

def _taken_day(game) -> None:
    game.row(at(10), 1, "LEADER_A", poll_s=0)
    game.taken(at(10, 5), 1, "LEADER_A")  # Ann: 30 min, 25 of play time
    game.row(at(10, 30), 1, "LEADER_B", poll_s=0)
    game.taken(at(11, 30), 1, "LEADER_B", by="claude")  # Bob: 1h 05m, Claude took it in 5 min
    game.taken(at(11, 30), 1, "LEADER_B", by="player")  # (a press after the stand-in that slipped through)
    game.row(at(11, 35), 1, "LEADER_C", poll_s=0)  # Cy: 10 min, nothing taken (an old row)
    game.row(at(11, 45), 1, "LEADER_D", poll_s=0)
    game.taken(at(11, 45, 2), 1, "LEADER_D", poll_s=2)  # Di: 15 min, read up to 2 s late
    game.row(at(12), 2, "LEADER_A", poll_s=0)
    game.taken(at(12, 3), 2, "LEADER_A")  # Ann's turn 2: 10 min, 7 of play time
    game.row(at(12, 10), 2, "LEADER_B", poll_s=0)  # in progress


def test_each_turn_gets_when_it_was_taken_and_by_whom(game):
    _taken_day(game)
    seq, _ = game.compute()
    ann, bob, cy, di, ann2, _ = seq
    assert (ann.taken, ann.claude) == (at(10, 5), False)
    assert (bob.taken, bob.claude) == (at(11, 30), True)
    assert (cy.taken, cy.claude) == (None, False)
    assert di.taken == at(11, 45, 1)  # the middle of its window
    ends = {(pt.turn, pt.player): end for pt, end in turn_times.finished(seq)}
    assert turn_times.take_minutes(ann, ends[(1, "Ann")]) == 25
    assert turn_times.take_minutes(bob, ends[(1, "Bob")]) == 5
    assert turn_times.take_minutes(cy, ends[(1, "Cy")]) is None
    assert turn_times.take_minutes(ann2, ends[(2, "Ann")]) == 7


def test_a_turn_taken_before_its_estimated_start_counts_from_the_start(game):
    game.row(at(10), 1, "LEADER_A")  # a 5-minute poll: the start is placed at 9:57:30
    game.taken(at(9, 56), 1, "LEADER_A", poll_s=2)
    game.row(at(10, 30), 1, "LEADER_B")
    seq, _ = game.compute()
    ann, bob = seq
    assert ann.taken == ann.start
    assert turn_times.take_minutes(ann, bob.start) == turn_times.minutes(ann, bob.start)


def test_play_time_counts_outside_the_workday(game):
    game.row(at(16, 35), 1, "LEADER_A", poll_s=0)  # Ann's turn begins Tuesday afternoon
    game.taken(at(24 + 8, 32), 1, "LEADER_A")  # she presses Start Turn at 8:32 Wednesday
    game.row(at(24 + 8, 46), 1, "LEADER_B", poll_s=0)  # and ends it at 8:46, before the workday
    seq, _ = game.compute()
    ann, bob = seq
    assert turn_times.minutes(ann, bob.start) == 25  # the total: 16:35 to 17:00
    assert turn_times.take_minutes(ann, bob.start) == 14  # play time: 8:32 to 8:46


def test_claudes_turns_are_not_the_players_play_time(game):
    _taken_day(game)
    seq, _ = game.compute()
    done = turn_times.finished(seq)
    averages = {a.player: a for a in turn_times.player_averages(done)}
    assert (averages["Ann"].taken, averages["Ann"].take_mean_minutes) == (2, 16)
    assert (averages["Bob"].turns, averages["Bob"].taken, averages["Bob"].take_mean_minutes) == (1, 0, None)
    assert averages["Bob"].mean_minutes == 65  # the total is still his
    assert (averages["Cy"].taken, averages["Cy"].take_mean_minutes) == (0, None)
    assert [pt.player for pt, _ in turn_times.claude_takes(done)] == ["Bob"]
    assert turn_times.mean_take_minutes(turn_times.claude_takes(done)) == 5
    assert [pt.player for pt, _ in turn_times.own_takes(done)] == ["Ann", "Di", "Ann"]


def test_report_gives_the_play_time(game):
    _taken_day(game)
    seq, _ = game.compute()
    data = report(seq, date(2026, 10, 28))
    di_take = turn_times.hms(14 + 59 / 60)  # from 11:45:01 to 12:00
    for scope in ("yesterday", "all"):  # one day: the same figures twice
        figures = summary(data, scope)
        assert (figures["turns"], figures["mean_turn"]) == (5, "26m 00s")
        # Ann 25 + 7, Di 14:59; not Claude's 5.
        assert figures["mean_play"] == turn_times.hms((25 + 14 + 59 / 60 + 7) / 3)
        assert (figures["total_play"], figures["play_turns"]) == ("46m 59s", 3)
        assert (figures["claude_turns"], figures["claude_players"], figures["claude_mean_play"]) == (1, "Bob", "5m 00s")
        assert records(data, scope) == {
            "Slowest turn": ("1h 05m 00s", "Bob", 1, "2026-10-27"),  # Claude's turn is still Bob's
            "Fastest turn": ("10m 00s", "Cy", 1, "2026-10-27"),  # ties Ann's turn 2
            "Longest play time": ("25m 00s", "Ann", 1, "2026-10-27"),
            "Shortest play time": ("7m 00s", "Ann", 2, "2026-10-27"),  # not Claude's 5
        }
        assert [(r["player"], r["turns"], r["mean_turn"], r["play_turns"], r["mean_play"], r["claude_turns"])
                for r in players(data, scope).values()] == [
            ("Ann", 2, "20m 00s", 2, "16m 00s", 0), ("Bob", 1, "1h 05m 00s", 0, None, 1),
            ("Cy", 1, "10m 00s", 0, None, 0), ("Di", 1, "15m 00s", 1, di_take, 0),
        ]
    assert data["info"][0]["headline"] == (
        "Yesterday (Tue 27 Oct): 5 turns. Average turn time 26m 00s, average play time 15m 40s."
    )
    assert [(r["player"], r["taken"], r["play_s"], r["claude"]) for r in data["turns"]] == [
        ("Ann", "2026-10-27T10:05:00", 1500, False), ("Bob", "2026-10-27T11:30:00", 300, True),
        ("Cy", None, None, False), ("Di", "2026-10-27T11:45:01", 899, False), ("Ann", "2026-10-27T12:03:00", 420, False),
    ]
    assert data["days"][0]["claude_turns"] == 1 and data["days"][0]["mean_play_s"] == 940


def test_longest_and_shortest_play_time_go_to_the_first_turn_that_reached_them(game):
    game.row(at(10), 1, "LEADER_A", poll_s=0)
    game.taken(at(10, 2), 1, "LEADER_A")  # Ann: 8 min of play time
    game.row(at(10, 10), 1, "LEADER_B", poll_s=0)
    game.taken(at(10, 15), 1, "LEADER_B")  # Bob: 5 min
    game.row(at(10, 20), 1, "LEADER_C", poll_s=0)
    game.taken(at(10, 22), 1, "LEADER_C")  # Cy: 8 min, ties Ann
    game.row(at(10, 30), 1, "LEADER_D", poll_s=0)
    game.taken(at(10, 30), 1, "LEADER_D", by="claude")  # Di: Claude took it, 1 min
    game.row(at(10, 31), 2, "LEADER_A", poll_s=0)
    game.taken(at(10, 36), 2, "LEADER_A")  # Ann's turn 2: 5 min, ties Bob
    game.row(at(10, 41), 2, "LEADER_B", poll_s=0)  # in progress
    seq, _ = game.compute()
    data = report(seq, date(2026, 10, 28))
    for scope in ("yesterday", "all"):
        found = records(data, scope)
        assert found["Longest play time"] == ("8m 00s", "Ann", 1, "2026-10-27")
        assert found["Shortest play time"] == ("5m 00s", "Bob", 1, "2026-10-27")


def test_a_player_with_some_turns_untimed_gets_the_count(game):
    game.row(at(10), 1, "LEADER_A", poll_s=0)
    game.row(at(10, 10), 1, "LEADER_B", poll_s=0)
    game.row(at(10, 20), 1, "LEADER_C", poll_s=0)
    game.row(at(10, 30), 1, "LEADER_D", poll_s=0)
    game.row(at(10, 40), 2, "LEADER_A", poll_s=0)
    game.taken(at(10, 41), 2, "LEADER_A")  # Ann's turn 2: 9 min of play time; turn 1 before the taken log
    game.row(at(10, 50), 2, "LEADER_B", poll_s=0)
    seq, _ = game.compute()
    data = report(seq, date(2026, 10, 28))
    yesterday = summary(data, "yesterday")
    assert (yesterday["turns"], yesterday["mean_turn"]) == (5, "10m 00s")
    assert (yesterday["mean_play"], yesterday["total_play"], yesterday["play_turns"]) == ("9m 00s", "9m 00s", 1)
    assert records(data, "yesterday") == {
        "Slowest turn": ("10m 00s", "Ann", 1, "2026-10-27"), "Fastest turn": ("10m 00s", "Ann", 1, "2026-10-27"),
    }  # one timed turn: no longest or shortest play time
    ann = players(data, "yesterday")["Ann"]
    assert (ann["turns"], ann["mean_turn"], ann["play_turns"], ann["mean_play"]) == (2, "10m 00s", 1, "9m 00s")


def test_each_scope_has_its_own_figures(game):
    game.row(at(16), 1, "LEADER_A", poll_s=0)
    game.taken(at(16, 10), 1, "LEADER_A")  # Ann: 30 min on Tuesday, 20 of play time
    game.row(at(16, 30), 1, "LEADER_B", poll_s=0)
    game.taken(at(24 + 9, 2), 1, "LEADER_B")  # Bob: 40 min, ends Wednesday, 8 of play time
    game.row(at(24 + 9, 10), 1, "LEADER_C", poll_s=0)  # Cy: 10 min on Wednesday, untimed
    game.row(at(24 + 9, 20), 1, "LEADER_D", poll_s=0)  # Di's turn in progress
    seq, _ = game.compute()
    data = report(seq, date(2026, 10, 29))
    yesterday, everything = summary(data, "yesterday"), summary(data, "all")
    assert (yesterday["turns"], yesterday["mean_turn"], yesterday["mean_play"], yesterday["total_play"]) == (
        2, "25m 00s", "8m 00s", "8m 00s",
    )
    assert records(data, "yesterday") == {  # one timed turn: no longest or shortest
        "Slowest turn": ("40m 00s", "Bob", 1, "2026-10-28"), "Fastest turn": ("10m 00s", "Cy", 1, "2026-10-28"),
    }
    assert (everything["turns"], everything["mean_turn"], everything["mean_play"], everything["total_play"]) == (
        3, "26m 40s", "14m 00s", "28m 00s",
    )
    assert records(data, "all") == {
        "Slowest turn": ("40m 00s", "Bob", 1, "2026-10-28"), "Fastest turn": ("10m 00s", "Cy", 1, "2026-10-28"),
        "Longest play time": ("20m 00s", "Ann", 1, "2026-10-27"),
        "Shortest play time": ("8m 00s", "Bob", 1, "2026-10-28"),
    }
    assert data["info"][0]["headline"].endswith("Average turn time 25m 00s, average play time 8m 00s.")


def test_no_play_time_without_a_timed_turn(game):
    _week(game)
    seq, _ = game.compute()
    data = report(seq, date(2026, 10, 29))
    for scope in ("yesterday", "all"):
        assert (summary(data, scope)["mean_play"], summary(data, scope)["total_play"]) == (None, None)
        assert not any("play" in record for record in records(data, scope))
    assert "play time" not in data["info"][0]["headline"]


# Turn time: wait plus play, and the turns with no workday time.

def test_turn_time_is_workday_time_plus_play_outside_it():
    def turn(start: datetime, taken: datetime | None) -> turn_times.PlayerTurn:
        return turn_times.PlayerTurn(1, 0, "LEADER_A", "Ann", start=start, taken=taken)

    # Waited on until 17:00, then played until 17:30.
    assert turn_times.overall_minutes(turn(at(16), at(17)), at(17, 30)) == 90
    # 16:30 to 17:00, then taken at 8:30 the next morning and finished at 9:15.
    assert turn_times.overall_minutes(turn(at(16, 30), at(24 + 8, 30)), at(24 + 9, 15)) == 75
    # Played through the lunch break: every minute of it counts once.
    assert turn_times.overall_minutes(turn(at(12), at(12, 10)), at(12, 40)) == 40
    # Played inside the workday, or never taken: the workday time.
    assert turn_times.overall_minutes(turn(at(10), at(10, 20)), at(10, 30)) == 30
    assert turn_times.overall_minutes(turn(at(16, 30), None), at(24 + 9, 15)) == 45


def test_turns_without_workday_time_are_left_out_of_the_averages(game):
    game.row(at(16, 30), 1, "LEADER_A", poll_s=0)  # Ann: 30 min, Tuesday
    game.row(at(17, 30), 1, "LEADER_B", poll_s=0)  # Bob: 17:30 to 18:00, no workday time
    game.row(at(18), 1, "LEADER_C", poll_s=0)  # Cy: 20 min, ends Wednesday at 9:20
    game.row(at(24 + 9, 20), 1, "LEADER_D", poll_s=0)  # Di: 10 min, Wednesday
    game.row(at(24 + 9, 30), 2, "LEADER_A", poll_s=0)  # in progress
    seq, skipped = game.compute()
    done = turn_times.finished(seq)
    assert [pt.player for pt, _ in turn_times.workday_turns(done)] == ["Ann", "Cy", "Di"]
    assert turn_times.mean_minutes(done) == turn_times.median_minutes(done) == 20  # not 15
    averages = turn_times.player_averages(done)
    assert [(a.player, a.turns, a.off_hours, a.mean_minutes) for a in averages] == [
        ("Ann", 1, 0, 30), ("Cy", 1, 0, 20), ("Di", 1, 0, 10), ("Bob", 1, 1, None),  # no average: last
    ]

    data = report(seq, date(2026, 10, 29))
    assert records(data, "all") == {
        "Slowest turn": ("30m 00s", "Ann", 1, "2026-10-27"),
        "Fastest turn": ("10m 00s", "Di", 1, "2026-10-28"),  # not Bob's 0
    }
    everything = summary(data, "all")
    assert (everything["turns"], everything["mean_turn"], everything["off_hours"]) == (4, "20m 00s", 1)
    assert [(r["player"], r["turns"], r["mean_turn"], r["off_hours"]) for r in players(data, "all").values()] == [
        ("Ann", 1, "30m 00s", 0), ("Bob", 1, None, 1), ("Cy", 1, "20m 00s", 0), ("Di", 1, "10m 00s", 0),
    ]
    tuesday = report(seq, date(2026, 10, 28))
    assert (summary(tuesday, "yesterday")["turns"], summary(tuesday, "yesterday")["mean_turn"]) == (2, "30m 00s")
    assert records(tuesday, "yesterday") == {"Slowest turn": ("30m 00s", "Ann", 1, "2026-10-27")}  # no fastest
    bob = next(line for line in turn_times.report(seq, skipped).splitlines() if line.startswith("Bob"))
    assert bob.split()[1:6] == ["1", "0", "1", "-", "-"]  # turns, guessed, off hrs, average, median


def test_play_after_the_workday_makes_a_slowest_turn_over_the_hour(game):
    game.row(at(16), 1, "LEADER_A", poll_s=0)
    game.taken(at(16, 50), 1, "LEADER_A")  # Ann waits 50 min, then plays until 17:40
    game.row(at(17, 40), 1, "LEADER_B", poll_s=0)  # Bob: 30 min of workday time, untimed
    game.row(at(24 + 9, 30), 1, "LEADER_C", poll_s=0)  # Cy: 20 min, untimed
    game.row(at(24 + 9, 50), 1, "LEADER_D", poll_s=0)  # in progress
    seq, _ = game.compute()
    data = report(seq, date(2026, 10, 29))
    assert (summary(data, "all")["turns"], summary(data, "all")["mean_turn"]) == (3, "50m 00s")  # 1h 40m, 30m, 20m
    assert records(data, "all") == {
        "Slowest turn": ("1h 40m 00s", "Ann", 1, "2026-10-27"),  # 1h of workday time, 40 min of play after it
        "Fastest turn": ("20m 00s", "Cy", 1, "2026-10-28"),
    }
    assert players(data, "all")["Ann"]["mean_turn"] == "1h 40m 00s"
