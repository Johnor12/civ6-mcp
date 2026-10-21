"""World snapshot: everything on every revealed tile, in one query.

A human player absorbs the whole map passively. The stand-in only knows what
it is told, so this query dumps the revealed world generically: an ASCII hex
grid of terrain/feature/water per tile, plus every resource, improvement,
district, wonder, unit and city the plot API exposes, and a per-owner
territory summary. Nothing here is special-cased by type: whatever a tile
carries is reported.
"""

from __future__ import annotations

from civ_mcp.lua._helpers import _LUA_RES_VISIBLE, SENTINEL
from civ_mcp.lua.models import (
    SeenCity,
    SeenUnit,
    Territory,
    TileMark,
    WorldSnapshot,
)


def build_world_snapshot_query() -> str:
    """InGame context: ASCII grid rows plus one line per notable attribute."""
    return (
        """
local me = Game.GetLocalPlayer()
local vis = PlayersVisibility[me]
local pTech = Players[me]:GetTechs()
"""
        + _LUA_RES_VISIBLE
        + """
local w, h = Map.GetGridSize()
local tc = {GRASS="g", PLAINS="p", DESERT="d", TUNDRA="t", SNOW="s", COAST="c", OCEAN="o"}
local fc = {FOREST="f", JUNGLE="j", MARSH="m", FLOODPLAINS="F", FLOODPLAINS_GRASSLAND="F",
            FLOODPLAINS_PLAINS="F", OASIS="a", REEF="r", ICE="i", VOLCANO="v",
            GEOTHERMAL_FISSURE="e", VOLCANIC_SOIL="l"}
local myUnits = {}
for _, u in Players[me]:GetUnits():Members() do
    local ux, uy = u:GetX(), u:GetY()
    if ux ~= -9999 then
        local e = GameInfo.Units[u:GetType()]
        local ut = (e and e.UnitType or "UNIT"):gsub("UNIT_", "")
        table.insert(myUnits, {ux, uy, ut, u:GetID()})
    end
end
local function nearest(x, y)
    local best, who = 999, ""
    for _, mu in ipairs(myUnits) do
        local d = Map.GetPlotDistance(mu[1], mu[2], x, y)
        if d < best then best = d; who = mu[3] .. "#" .. mu[4] end
    end
    return best, who
end
local refd = {}
local terr = {}
local minx, miny, maxx, maxy = w, h, -1, -1
for y = 0, h - 1 do
    for x = 0, w - 1 do
        local plot = Map.GetPlot(x, y)
        if plot and vis:IsRevealed(plot:GetIndex()) then
            if x < minx then minx = x end
            if x > maxx then maxx = x end
            if y < miny then miny = y end
            if y > maxy then maxy = y end
        end
    end
end
if maxx < 0 then
    print("NOTHING_REVEALED")
else
print("BBOX|" .. minx .. "," .. miny .. "|" .. maxx .. "," .. maxy .. "|" .. w .. "," .. h)
for y = miny, maxy do
    local cells = {}
    for x = minx, maxx do
        local plot = Map.GetPlot(x, y)
        local cell = "   "
        if plot and vis:IsRevealed(plot:GetIndex()) then
            local visible = vis:IsVisible(plot:GetIndex())
            local fog = visible and "" or "~"
            local o = plot:GetOwner()
            if o >= 0 then refd[o] = true end
            local tt = GameInfo.Terrains[plot:GetTerrainType()].TerrainType:gsub("TERRAIN_", "")
            local base, kind = tt:match("^(%a+)_?(%a*)$")
            local t = tc[base or ""] or "?"
            if kind == "HILLS" then t = t:upper() elseif kind == "MOUNTAIN" then t = "M" end
            local f = "."
            local fIdx = plot:GetFeatureType()
            if fIdx >= 0 then
                local fi = GameInfo.Features[fIdx]
                if fi then
                    local fn = fi.FeatureType:gsub("FEATURE_", "")
                    if fi.NaturalWonder then f = "W" else f = fc[fn] or "?" end
                end
            end
            if plot:IsCity() then f = (o == me) and "@" or "#" end
            local wch = " "
            if plot:IsWater() then wch = " "
            elseif plot:IsRiver() then wch = "~"
            elseif plot:IsFreshWater() then wch = "="
            elseif plot:IsCoastalLand() then wch = "," end
            cell = t .. f .. wch
            if o >= 0 then
                local tr = terr[o]
                if not tr then tr = {0, x, y, x, y}; terr[o] = tr end
                tr[1] = tr[1] + 1
                if x < tr[2] then tr[2] = x end
                if y < tr[3] then tr[3] = y end
                if x > tr[4] then tr[4] = x end
                if y > tr[5] then tr[5] = y end
            end
            local impIdx = plot:GetImprovementType()
            local resIdx = plot:GetResourceType()
            if resIdx >= 0 then
                local re = GameInfo.Resources[resIdx]
                if re and resVisible(re) then
                    local cls = (re.ResourceClassType or ""):gsub("RESOURCECLASS_", "")
                    local mark = ""
                    if cls == "LUXURY" then mark = "+" elseif cls == "STRATEGIC" then mark = "*" end
                    local rn = re.ResourceType:gsub("RESOURCE_", "")
                    print("RES|" .. rn .. mark .. "|" .. x .. "," .. y .. fog .. "|" .. (impIdx >= 0 and "1" or "0") .. "|" .. o)
                end
            end
            if impIdx >= 0 then
                local ii = GameInfo.Improvements[impIdx]
                local iname = ii and ii.ImprovementType:gsub("IMPROVEMENT_", "") or "?"
                if plot:IsImprovementPillaged() then iname = iname .. ":PILLAGED" end
                local d, who = nearest(x, y)
                print("IMP|" .. iname .. "|" .. x .. "," .. y .. fog .. "|" .. o .. "|" .. d .. "|" .. who)
            end
            local dIdx = plot:GetDistrictType()
            if dIdx >= 0 then
                local di = GameInfo.Districts[dIdx]
                local dn = di and di.DistrictType:gsub("DISTRICT_", "") or "?"
                if dn ~= "CITY_CENTER" then
                    print("DIST|" .. dn .. "|" .. x .. "," .. y .. fog .. "|" .. o)
                end
            end
            local okW, wIdx = pcall(function() return plot:GetWonderType() end)
            if okW and wIdx and wIdx >= 0 then
                local wi = GameInfo.Buildings[wIdx]
                local wn = wi and wi.BuildingType:gsub("BUILDING_", "") or "?"
                print("WONDER|" .. wn .. "|" .. x .. "," .. y .. fog .. "|" .. o)
            end
            if visible then
                local pu = Map.GetUnitsAt(x, y)
                if pu then
                    for u in pu:Units() do
                        local o2 = u:GetOwner()
                        if o2 ~= me then
                            refd[o2] = true
                            local e = GameInfo.Units[u:GetType()]
                            local un = e and e.UnitType:gsub("UNIT_", "") or "?"
                            local hp = u:GetMaxDamage() - u:GetDamage()
                            local d, who = nearest(x, y)
                            print("UNIT|" .. o2 .. "|" .. un .. "|" .. x .. "," .. y .. "|" .. hp .. "/" .. u:GetMaxDamage() .. "|" .. d .. "|" .. who)
                        end
                    end
                end
            end
        end
        table.insert(cells, cell)
    end
    print("ROW|" .. y .. "|" .. table.concat(cells, ""))
end
for pid = 0, 63 do
    local p = Players[pid]
    if p and p:IsAlive() then
        for _, c in p:GetCities():Members() do
            local cx, cy = c:GetX(), c:GetY()
            local plot = Map.GetPlot(cx, cy)
            if plot and vis:IsRevealed(plot:GetIndex()) then
                refd[pid] = true
                local nm = Locale.Lookup(c:GetName()):gsub("|", "/")
                local cap = c:IsCapital() and "1" or "0"
                print("CITY|" .. pid .. "|" .. nm .. "|" .. cx .. "," .. cy .. "|" .. c:GetPopulation() .. "|" .. cap)
            end
        end
    end
end
for o, tr in pairs(terr) do
    print("TERR|" .. o .. "|" .. tr[1] .. "|" .. tr[2] .. "," .. tr[3] .. "|" .. tr[4] .. "," .. tr[5])
end
for pid, _ in pairs(refd) do
    local label = "player" .. pid
    if pid == me then label = "YOU"
    elseif pid == 63 then label = "Barbarian"
    else
        local cfg = PlayerConfigurations[pid]
        local p = Players[pid]
        if cfg and p then
            label = Locale.Lookup(cfg:GetCivilizationShortDescription())
            if not p:IsMajor() then label = label .. " (city-state)" end
            label = label:gsub("|", "/")
        end
    end
    print("OWNER|" .. pid .. "|" .. label)
end
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)
    )


def _xy(token: str) -> tuple[int, int, bool]:
    """Parse "12,31~" into (12, 31, fogged=True)."""
    fog = token.endswith("~")
    xs, ys = token.rstrip("~").split(",")
    return int(xs), int(ys), fog


def parse_world_snapshot_response(lines: list[str]) -> WorldSnapshot:
    snap = WorldSnapshot()
    for line in lines:
        p = line.split("|")
        tag = p[0]
        try:
            if tag == "BBOX" and len(p) >= 4:
                snap.min_x, snap.min_y, _ = _xy(p[1])
                snap.max_x, snap.max_y, _ = _xy(p[2])
                snap.map_w, snap.map_h, _ = _xy(p[3])
            elif tag == "ROW" and len(p) >= 3:
                snap.rows.append((int(p[1]), "|".join(p[2:])))
            elif tag == "RES" and len(p) >= 5:
                x, y, fog = _xy(p[2])
                snap.resources.append(
                    TileMark(p[1], x, y, fog, int(p[4]), improved=p[3] == "1")
                )
            elif tag == "IMP" and len(p) >= 6:
                x, y, fog = _xy(p[2])
                snap.improvements.append(
                    TileMark(p[1], x, y, fog, int(p[3]), int(p[4]), p[5])
                )
            elif tag == "DIST" and len(p) >= 4:
                x, y, fog = _xy(p[2])
                snap.districts.append(TileMark(p[1], x, y, fog, int(p[3])))
            elif tag == "WONDER" and len(p) >= 4:
                x, y, fog = _xy(p[2])
                snap.wonders.append(TileMark(p[1], x, y, fog, int(p[3])))
            elif tag == "UNIT" and len(p) >= 7:
                x, y, _ = _xy(p[3])
                hp, max_hp = p[4].split("/")
                snap.units.append(
                    SeenUnit(
                        int(p[1]), p[2], x, y, int(hp), int(max_hp), int(p[5]), p[6]
                    )
                )
            elif tag == "CITY" and len(p) >= 6:
                x, y, _ = _xy(p[3])
                snap.cities.append(
                    SeenCity(int(p[1]), p[2], x, y, int(p[4]), p[5] == "1")
                )
            elif tag == "TERR" and len(p) >= 5:
                x0, y0, _ = _xy(p[3])
                x1, y1, _ = _xy(p[4])
                snap.territory.append(
                    Territory(int(p[1]), int(p[2]), x0, y0, x1, y1)
                )
            elif tag == "OWNER" and len(p) >= 3:
                snap.owners[int(p[1])] = p[2]
            elif tag == "NOTHING_REVEALED":
                snap.nothing_revealed = True
        except ValueError:
            continue
    return snap
