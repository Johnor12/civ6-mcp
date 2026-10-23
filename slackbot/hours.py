"""Office hours: the turn timer only runs while people are in the office."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo

# weekday (Monday = 0) -> (opens, closes). Missing days are closed.
OfficeHours = Mapping[int, tuple[time, time]]

DEFAULT_HOURS: dict[int, tuple[time, time]] = {
    **{d: (time(10, 0), time(16, 30)) for d in range(4)},  # Monday to Thursday
    4: (time(10, 0), time(15, 0)),  # Friday
}
DEFAULT_TZ = "America/New_York"


def working_seconds(
    start: float, end: float, hours: OfficeHours | None, tz: str = DEFAULT_TZ
) -> float:
    """Seconds between two timestamps that fall inside office hours.

    `hours` of None means no limit: plain elapsed time.
    """
    if end <= start:
        return 0.0
    if hours is None:
        return end - start
    zone = ZoneInfo(tz)
    day = datetime.fromtimestamp(start, zone).date()
    last = datetime.fromtimestamp(end, zone).date()
    total = 0.0
    while day <= last:
        window = hours.get(day.weekday())
        if window:
            opens = datetime.combine(day, window[0], tzinfo=zone).timestamp()
            closes = datetime.combine(day, window[1], tzinfo=zone).timestamp()
            total += max(0.0, min(end, closes) - max(start, opens))
        day += timedelta(days=1)
    return total
