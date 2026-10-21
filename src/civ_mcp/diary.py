"""Per-player diary: one JSONL row per stand-in turn, under ``~/.civ6-mcp``.

The diary is the agent's memory across sessions. A row holds the player's
figures for the turn plus the five reflection fields passed to ``end_turn``.
Rows are keyed by civilization and game seed, so every absent player of a
hotseat game has their own file and never sees another player's rows.
"""

from __future__ import annotations

import json
from pathlib import Path

DIARY_DIR = Path.home() / ".civ6-mcp"
REFLECTION_FIELDS = ("tactical", "strategic", "tooling", "planning", "hypothesis")


def diary_path(civ: str, seed: int) -> Path:
    return DIARY_DIR / f"diary_{civ}_{seed}.jsonl"


def append_entry(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def read_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def merge_reflections(path: Path, turn: int, new: dict) -> dict | None:
    """Append new reflection text to the last row for ``turn`` (end_turn retries).

    Returns the merged row, or None when no row for that turn exists.
    """
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        try:
            row = json.loads(lines[i])
        except json.JSONDecodeError:
            continue
        if row.get("turn") != turn:
            continue
        existing = row.get("reflections") or {}
        for field in REFLECTION_FIELDS:
            new_val = (new.get(field) or "").strip()
            old_val = (existing.get(field) or "").strip()
            if new_val and new_val != old_val:
                existing[field] = f"{old_val} | {new_val}" if old_val else new_val
        row["reflections"] = existing
        lines[i] = json.dumps(row, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return row
    return None


def format_entry(e: dict) -> str:
    r = e.get("reflections") or {}
    score_line = (
        f"  Score: {e.get('score', '?')} | Cities: {e.get('cities', '?')} | "
        f"Pop: {e.get('pop', '?')} | Sci: {e.get('science', '?')} | "
        f"Cul: {e.get('culture', '?')} | Gold: {e.get('gold', '?')} "
        f"({e.get('gold_per_turn', '?')}/t) | Faith: {e.get('faith', '?')} | "
        f"Favor: {e.get('favor', '?')} | Explored: {e.get('exploration_pct', '?')}% | "
        f"Era: {e.get('era', '?')} ({e.get('era_score', '?')})"
    )
    lines = [f"=== Turn {e.get('turn', '?')} ===", score_line]
    if e.get("stockpiles"):
        lines.append(
            "  Resources: " + ", ".join(f"{k}: {v}" for k, v in e["stockpiles"].items())
        )
    if e.get("note"):
        lines.append(f"  Player's note: {e['note']}")
    lines.extend(f"  {k}: {v}" for k, v in r.items())
    return "\n".join(lines)
