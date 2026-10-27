"""Screenshots of the game PC around a Claude run. Needs an unlocked desktop session.

A ShotSession takes one picture when the run starts, up to `max_mid` while it
runs and one when it ends. Taking a picture never raises: a failed grab is
skipped. Standard library plus mss (imported only when a picture is taken).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("screenshots")

SHOTS_DIR = Path(__file__).resolve().parent / "data" / "shots"
MID_INTERVAL_S = 120
MAX_MID = 2  # with the first and last picture: at most four per run

Grab = Callable[[str], "Path | None"]


def grab_screen(label: str, out_dir: Path | None = None) -> Path | None:
    """Save the primary monitor as a PNG, or None if that fails."""
    out_dir = out_dir or SHOTS_DIR
    path = out_dir / f"{int(time.time())}-{label}.png"
    try:
        import mss
        import mss.tools

        out_dir.mkdir(parents=True, exist_ok=True)
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[1])
            mss.tools.to_png(shot.rgb, shot.size, output=str(path))
        return path
    except Exception as e:  # noqa: BLE001 - a missing picture must not break a turn
        log.warning("screenshot %s failed: %s", label, e)
        return None


class ShotSession:
    def __init__(self, grab: Grab = grab_screen, interval_s: float = MID_INTERVAL_S, max_mid: int = MAX_MID):
        self._grab = grab
        self._interval_s = interval_s
        self._max_mid = max_mid
        self.pre: Path | None = None
        self.mid: list[Path] = []
        self.post: Path | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.pre = self._grab("pre")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        for i in range(self._max_mid):
            if self._stop.wait(self._interval_s):
                return
            path = self._grab(f"mid{i + 1}")
            if path is not None:
                self.mid.append(path)

    def finish(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        self.post = self._grab("post")

    def all(self) -> list[Path]:
        return [p for p in (self.pre, *self.mid, self.post) if p is not None]
