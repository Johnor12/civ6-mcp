"""Screenshots and screen recording on the civ machine (Windows)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class Capture(Protocol):
    async def screenshot(self, label: str) -> Path | None: ...
    async def start_recording(self) -> None: ...
    async def stop_recording(self) -> Path | None: ...


class NullCapture:
    async def screenshot(self, label: str) -> Path | None:
        return None

    async def start_recording(self) -> None:
        return None

    async def stop_recording(self) -> Path | None:
        return None


class DesktopCapture:
    """mss for screenshots, ffmpeg gdigrab for video. Needs an unlocked desktop."""

    def __init__(self, out_dir: Path, ffmpeg_bin: str = "ffmpeg", fps: int = 4) -> None:
        self.out_dir = out_dir
        self.ffmpeg_bin = ffmpeg_bin
        self.fps = fps
        self._proc: asyncio.subprocess.Process | None = None
        self._video: Path | None = None
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

    async def start_recording(self) -> None:
        exe = shutil.which(self.ffmpeg_bin)
        if exe is None:
            log.warning("ffmpeg not found; no recording")
            return
        self._video = self.out_dir / f"{int(time.time())}-turn.mp4"
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe, "-y", "-f", "gdigrab", "-framerate", str(self.fps), "-i", "desktop",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32",
                "-pix_fmt", "yuv420p", str(self._video),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as e:
            log.warning("could not start ffmpeg: %s", e)
            self._proc = None

    async def stop_recording(self) -> Path | None:
        proc, self._proc = self._proc, None
        if proc is None or self._video is None:
            return None
        try:
            if proc.stdin:
                proc.stdin.write(b"q")  # ffmpeg finishes the file on "q"
                await proc.stdin.drain()
            await asyncio.wait_for(proc.wait(), 20)
        except Exception as e:  # noqa: BLE001
            log.warning("ffmpeg did not stop cleanly: %s", e)
            proc.kill()
        return self._video if self._video.exists() and self._video.stat().st_size > 0 else None
