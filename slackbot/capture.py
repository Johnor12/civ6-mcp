"""Screenshots of the civ machine (Windows desktop session)."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class Capture(Protocol):
    async def screenshot(self, label: str) -> Path | None: ...


class NullCapture:
    async def screenshot(self, label: str) -> Path | None:
        return None


class DesktopCapture:
    """Full-screen PNGs through mss. Needs an unlocked desktop."""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        out_dir.mkdir(parents=True, exist_ok=True)

    async def screenshot(self, label: str) -> Path | None:
        path = self.out_dir / f"{int(time.time())}-{label}.png"
        try:
            await asyncio.to_thread(self._grab, path)
        except Exception as e:  # noqa: BLE001 - capture must never break a turn
            log.warning("screenshot failed: %s", e)
            return None
        return path

    @staticmethod
    def _grab(path: Path) -> None:
        import mss
        import mss.tools

        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[1])
            mss.tools.to_png(shot.rgb, shot.size, output=str(path))
