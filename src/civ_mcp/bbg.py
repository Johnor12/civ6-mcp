"""Better Balanced Game kit of the leaders in play, bundled with the repo.

The data is built by ``scripts/extract_bbg.py``. Bundling it means a stand-in
needs no web access to read its civilization's abilities.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "bbg_leaders.json"
BBG_VERSION = "7.5.1"


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def kit_for(leader_type: str) -> str | None:
    """Civ and leader abilities, unique unit and infrastructure; None if not bundled."""
    entry = _load().get(leader_type.strip().upper())
    if entry is None:
        return None
    parts = [f"{entry['title']} (BBG {BBG_VERSION})"]
    parts += [f"## {a['name']}\n{a['text']}" for a in entry["abilities"]]
    return "\n\n".join(parts)
