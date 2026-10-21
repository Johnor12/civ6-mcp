"""Diary rows: append, read, merge reflections on retry, format."""

from civ_mcp import diary


def _row(turn: int, **reflections) -> dict:
    return {
        "turn": turn,
        "score": 120,
        "cities": 3,
        "pop": 11,
        "science": 12.5,
        "culture": 8.0,
        "gold": 150.0,
        "gold_per_turn": 4.0,
        "faith": 20.0,
        "favor": 0,
        "exploration_pct": 31,
        "era": "ERA_CLASSICAL",
        "era_score": 14,
        "stockpiles": {"IRON": 6},
        "note": "finish the campus",
        "reflections": reflections,
    }


def test_append_read_and_format(tmp_path):
    path = diary.diary_path("cree", 42)
    assert path.name == "diary_cree_42.jsonl"
    path = tmp_path / path.name
    diary.append_entry(path, _row(63, tactical="moved the warrior", planning="campus"))
    diary.append_entry(path, _row(64, tactical="founded a city"))
    entries = diary.read_entries(path)
    assert [e["turn"] for e in entries] == [63, 64]
    text = diary.format_entry(entries[0])
    assert text.startswith("=== Turn 63 ===")
    assert "Score: 120" in text and "IRON: 6" in text
    assert "Player's note: finish the campus" in text
    assert "tactical: moved the warrior" in text


def test_merge_reflections_extends_the_turn_row(tmp_path):
    path = tmp_path / "d.jsonl"
    diary.append_entry(path, _row(63, tactical="first try", tooling="No issues"))
    merged = diary.merge_reflections(path, 63, {"tactical": "second try", "tooling": "No issues"})
    assert merged is not None
    assert merged["reflections"]["tactical"] == "first try | second try"
    assert merged["reflections"]["tooling"] == "No issues"  # unchanged text is not repeated
    assert diary.read_entries(path)[0]["reflections"]["tactical"] == "first try | second try"
    assert diary.merge_reflections(path, 99, {"tactical": "x"}) is None


def test_read_missing_and_corrupt(tmp_path):
    assert diary.read_entries(tmp_path / "nope.jsonl") == []
    path = tmp_path / "d.jsonl"
    path.write_text('{"turn": 1}\nnot json\n', encoding="utf-8")
    assert diary.read_entries(path) == [{"turn": 1}]
