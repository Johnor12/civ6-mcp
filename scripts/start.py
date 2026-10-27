#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Start the turn logger, the turn announcer and the Slack bot side by side.

    uv run scripts/start.py

Each runs as its own `uv run` process in its own environment (the inline
metadata at the top of each script) and writes to this console. When one
of them exits, or on Ctrl+C, the others are stopped too.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SERVICES = ("turn_logger.py", "turn_announcer.py", "slack_bot.py")


def main() -> None:
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is not on PATH")
    procs = {name: subprocess.Popen([uv, "run", str(SCRIPTS / name)], cwd=SCRIPTS.parent) for name in SERVICES}
    code = 0
    try:
        while not (exited := [name for name, p in procs.items() if p.poll() is not None]):
            time.sleep(1)
        code = procs[exited[0]].returncode
        print(f"{exited[0]} exited with code {code}; stopping the rest.", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs.values():
            if p.poll() is None:
                # /T takes the script's Python down with its uv process.
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
