from datetime import datetime
from zoneinfo import ZoneInfo

from slackbot.config import Settings
from slackbot.hours import DEFAULT_HOURS, working_seconds
from slackbot.rules import due_actions
from slackbot.state import Turn

NY = ZoneInfo("America/New_York")


def ts(month, day, hour, minute=0):
    """2026 local New York time to a timestamp. Oct 22 is a Thursday."""
    return datetime(2026, month, day, hour, minute, tzinfo=NY).timestamp()


def secs(a, b):
    return working_seconds(a, b, DEFAULT_HOURS)


def test_weekday_windows():
    assert secs(ts(10, 22, 8), ts(10, 22, 11)) == 3600  # opens at 10:00
    assert secs(ts(10, 22, 10), ts(10, 22, 18)) == 6.5 * 3600  # Thursday closes 16:30
    assert secs(ts(10, 23, 10), ts(10, 23, 18)) == 5 * 3600  # Friday closes 15:00


def test_overnight_and_weekend_do_not_count():
    assert secs(ts(10, 22, 16), ts(10, 23, 10, 30)) == 3600  # 30 min Thu + 30 min Fri
    assert secs(ts(10, 23, 14, 30), ts(10, 26, 10, 30)) == 3600  # Fri to Monday
    assert secs(ts(10, 24, 12), ts(10, 25, 12)) == 0  # Saturday to Sunday


def test_dst_change_over_the_weekend():
    # DST ends Sunday Nov 1: Friday is EDT, Monday is EST.
    assert secs(ts(10, 30, 14), ts(11, 2, 10, 30)) == 3600 + 1800


def test_no_limit_and_backwards():
    assert working_seconds(100.0, 400.0, None) == 300.0
    assert secs(ts(10, 22, 12), ts(10, 22, 11)) == 0


def test_timer_actions_follow_office_hours():
    s = Settings(ping_after_s=3600, warn_after_s=7200)
    turn = Turn(1, "UA", "1", ts(10, 22, 16), False, False)  # Thursday 16:00
    assert due_actions(ts(10, 22, 16, 30), turn, s) == []  # 30 min counted
    assert due_actions(ts(10, 22, 23), turn, s) == []  # night does not count
    assert due_actions(ts(10, 23, 10, 30), turn, s) == ["ping"]
    assert due_actions(ts(10, 23, 11, 30), turn, s) == ["ping", "warn"]


def test_working_elapsed_uses_settings():
    assert Settings().working_elapsed(ts(10, 22, 16, 20), ts(10, 23, 10, 5)) == 15 * 60
    assert Settings(office_hours=None).working_elapsed(0, 50) == 50
