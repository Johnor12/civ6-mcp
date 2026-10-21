"""World snapshot parser and narration."""

from civ_mcp import lua as lq
from civ_mcp import narrate as nr

LINES = [
    "BBOX|6,25|12,31|74,46",
    "ROW|25|Gf,GF~gf pf~         ",
    "ROW|26|c  c  p@~p. Gs       ",
    "ROW|27|   Gf~Gs~g. Gf       ",
    "RES|STONE|7,25|0|0",
    "RES|STONE|8,27|1|0",
    "RES|PEARLS+|6,26~|0|-1",
    "IMP|GOODY_HUT|12,31|-1|1|WARRIOR#1",
    "IMP|QUARRY|8,27|0|4|WARRIOR#1",
    "IMP|BARBARIAN_CAMP|14,27~|-1|3|SCOUT#0",
    "DIST|CAMPUS|9,27|0",
    "UNIT|63|WARRIOR|12,32|70/100|2|WARRIOR#1",
    "UNIT|1|SCOUT|11,28|100/100|1|WARRIOR#1",
    "CITY|0|Pella|8,26|2|1",
    "CITY|1|Istanbul|31,15|1|1",
    "TERR|0|7|6,25|10,28",
    "TERR|1|3|30,15|32,16",
    "OWNER|0|YOU",
    "OWNER|1|Ottomans",
    "OWNER|63|Barbarian",
]


def test_parse_world_snapshot():
    snap = lq.parse_world_snapshot_response(LINES)
    assert (snap.min_x, snap.min_y, snap.max_x, snap.max_y) == (6, 25, 12, 31)
    assert (snap.map_w, snap.map_h) == (74, 46)
    assert [y for y, _ in snap.rows] == [25, 26, 27]
    assert snap.rows[0][1].startswith("Gf,GF~")
    assert len(snap.resources) == 3
    pearls = snap.resources[2]
    assert pearls.name == "PEARLS+" and pearls.fogged and pearls.owner_id == -1
    assert snap.resources[1].improved and not snap.resources[0].improved
    hut = snap.improvements[0]
    assert hut.name == "GOODY_HUT" and hut.unit_distance == 1
    assert hut.nearest_unit == "WARRIOR#1"
    assert snap.improvements[2].fogged
    assert snap.districts[0].name == "CAMPUS"
    assert snap.units[0].owner_id == 63 and snap.units[0].hp == 70
    assert snap.cities[1].name == "Istanbul" and snap.cities[1].is_capital
    assert snap.territory[0].tiles == 7
    assert snap.owners == {0: "YOU", 1: "Ottomans", 63: "Barbarian"}


def test_narrate_world_snapshot():
    text = nr.narrate_world_snapshot(lq.parse_world_snapshot_response(LINES))
    assert "x 6-12, y 25-31" in text
    # grid rows carry their y label; odd rows are indented one char
    assert "y=26  c  c  p@~" in text
    assert "y=25   Gf,GF~" in text
    assert "Your cities: Pella (8,26) pop 2 capital" in text
    assert "Other cities: Istanbul [Ottomans] (31,15) pop 1 capital" in text
    assert "STONE: (7,25)! (8,27)" in text
    assert "PEARLS+: (6,26)~" in text
    assert "GOODY_HUT: (12,31)[1 from WARRIOR#1]" in text
    assert "BARBARIAN_CAMP: (14,27)~[3 from SCOUT#0]" in text
    assert "QUARRY: (8,27)" in text and "QUARRY: (8,27)[" not in text
    assert "Barbarian: WARRIOR (12,32) 70/100hp[2 from WARRIOR#1]" in text
    assert "Ottomans: SCOUT (11,28) 100/100hp" in text
    assert "YOU 7 tiles x6-10 y25-28" in text


def test_narrate_nothing_revealed():
    snap = lq.parse_world_snapshot_response(["NOTHING_REVEALED"])
    assert nr.narrate_world_snapshot(snap) == "Nothing revealed yet."
