"""Bundled BBG kit: data is complete for the group's leaders and reaches the briefing."""

from __future__ import annotations

import json

from civ_mcp import bbg
from scripts_path import extract_module

# Leaders in the Slack bot's players.toml, as the game names them.
IN_PLAY = [
    "LEADER_CLEOPATRA", "LEADER_NZINGA_MBANDE", "LEADER_VICTORIA_ALT", "LEADER_SUK_AL_HASAN",
    "LEADER_JFD_STANISLAW", "LEADER_JULIUS_CAESAR", "LEADER_GITARJA", "LEADER_GENGHIS_KHAN",
    "LEADER_AMANITORE", "LEADER_LIME_THULE_DAVE",
]


def test_every_leader_in_play_has_a_kit():
    for leader in IN_PLAY:
        kit = bbg.kit_for(leader)
        assert kit is not None, leader
        assert kit.count("## ") >= 4, leader  # civ, leader, unit, infrastructure


def test_kit_text_is_clean():
    kit = bbg.kit_for("leader_amanitore")  # case does not matter
    assert "Ta-Seti" in kit and "Nubian Pyramid" in kit
    assert "<" not in kit and "&#" not in kit and "  " not in kit
    assert "BBG 7.5.1" in kit.splitlines()[0]


def test_unknown_leader():
    assert bbg.kit_for("LEADER_NOBODY") is None
    assert bbg.kit_for("") is None


def test_data_file_shape():
    data = json.loads(bbg.DATA_PATH.read_text(encoding="utf-8"))
    for key, entry in data.items():
        assert key.startswith("LEADER_") and entry["title"] and entry["abilities"]
        assert all(a["name"] and a["text"] for a in entry["abilities"])


def test_extract_script_cleans_icons_and_markup():
    ex = extract_module()
    page = (
        "<!--LOC_CIVILIZATION_NUBIA_NAME LOC_LEADER_AMANITORE_NAME-->"
        '<h2 class="civ-name">Nubia Amanitore <img src="x"></h2>'
        '<h3 class="civ-ability-name">Ta-Seti</h3>'
        '<p class="civ-ability-desc actual-text">+2 <img src="/i/ICON_GOLD.webp"/> Gold. '
        'Mines give +1 <img src="/i/ICON_PRODUCTION.webp"/>.<br>Next &amp; last</p>'
    )
    out = ex.extract(page)
    ability = out["LEADER_AMANITORE"]["abilities"][0]
    assert out["LEADER_AMANITORE"]["title"] == "Nubia Amanitore"
    assert ability["text"] == "+2 Gold. Mines give +1 Production.\nNext & last"


def _briefing(monkeypatch, leader):
    import asyncio
    from types import SimpleNamespace

    from civ_mcp import server

    monkeypatch.setattr(server._standin, "active_config", lambda *a, **k: SimpleNamespace(leader=leader))
    session = server.Session(conn=None, game=object(), guard=None)  # game calls fail: sections say "unavailable"
    return asyncio.new_event_loop().run_until_complete(server._turn_briefing(session))


def test_briefing_has_the_kit(monkeypatch):
    text = _briefing(monkeypatch, "LEADER_AMANITORE")
    assert "=== Civilization kit (BBG) ===" in text and "Ta-Seti" in text


def test_briefing_says_when_no_kit(monkeypatch):
    text = _briefing(monkeypatch, "LEADER_NOBODY")
    assert "No bundled BBG entry for LEADER_NOBODY" in text
