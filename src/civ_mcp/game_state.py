"""High-level game state API: typed async methods over the bridge connection."""

from __future__ import annotations

import asyncio
import logging
import re

from civ_mcp import lua as lq
from civ_mcp.bridge import BridgeConnection
from civ_mcp.narrate import (
    narrate_combat_estimate,
    narrate_settle_candidates,
    narrate_test_trade,
)

log = logging.getLogger(__name__)


class GameState:
    """High-level async API for Civ 6 game state + actions."""

    def __init__(self, connection: BridgeConnection):
        self.conn = connection
        self._game_identity: tuple[str, int] | None = None  # (civ_type, seed)
        self._local_player_id: int = 0  # refreshed from the game (any slot in hotseat)

    async def get_game_identity(self) -> tuple[str, int]:
        """Return (civ_type_lower, random_seed) for the current game.

        Always queries the game so we detect new-game loads.  When the
        identity changes, all per-game cached state is reset.
        """
        code = (
            "local me = Game.GetLocalPlayer() "
            "local cfg = PlayerConfigurations[me] "
            'print("GAMESEED|" .. cfg:GetCivilizationTypeName() '
            '.. "|" .. tostring(GameConfiguration.GetValue("GAME_SYNC_RANDOM_SEED")) '
            '.. "|" .. tostring(me)) '
            'print("---END---")'
        )
        lines = await self.conn.execute(code)
        for line in lines:
            if line.startswith("GAMESEED|"):
                parts = line.split("|")
                civ = parts[1].replace("CIVILIZATION_", "").lower()
                seed = int(parts[2])
                # The local player is slot 0 in a fresh single-player game but
                # any slot in a save converted from hotseat.
                if len(parts) > 3 and parts[3].lstrip("-").isdigit():
                    self._local_player_id = int(parts[3])
                new_id = (civ, seed)
                if self._game_identity is not None and new_id != self._game_identity:
                    log.info("Game changed: %s → %s", self._game_identity, new_id)
                self._game_identity = new_id
                return self._game_identity
        return ("unknown", 0)

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    async def get_game_overview(self) -> lq.GameOverview:
        lines = await self.conn.execute(lq.build_overview_query())
        ov = lq.parse_overview_response(lines)
        self._local_player_id = ov.player_id
        return ov

    async def get_diary_snapshot(self) -> lq.DiarySnapshot:
        """Full per-turn snapshot for diary JSONL. InGame context."""
        lines = await self.conn.execute(lq.build_diary_full_query())
        return lq.parse_diary_full_response(lines)


    async def get_units(self) -> list[lq.UnitInfo]:
        lines = await self.conn.execute(lq.build_units_query())
        return lq.parse_units_response(lines)

    async def get_builder_tasks(
        self,
    ) -> tuple[list[lq.BuilderTask], list[lq.BuilderInfo]]:
        lines = await self.conn.execute(lq.build_builder_tasks_query())
        return lq.parse_builder_tasks(lines)

    async def get_spies(self) -> list[lq.SpyInfo]:
        lines = await self.conn.execute(lq.build_get_spies_query())
        return lq.parse_spies_response(lines)

    async def spy_travel(self, unit_index: int, target_x: int, target_y: int) -> str:
        lua = lq.build_spy_travel(unit_index, target_x, target_y)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def spy_mission(
        self, unit_index: int, mission_type: str, target_x: int, target_y: int
    ) -> str:
        lua = lq.build_spy_mission(unit_index, mission_type, target_x, target_y)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def get_threat_scan(self) -> list[lq.ThreatInfo]:
        lines = await self.conn.execute(lq.build_threat_scan_query())
        return lq.parse_threat_scan_response(lines)

    async def get_pathing_estimate(
        self, unit_index: int, target_x: int, target_y: int
    ) -> lq.PathingEstimate:
        lines = await self.conn.execute(
            lq.build_pathing_estimate_query(unit_index, target_x, target_y)
        )
        return lq.parse_pathing_estimate(lines)

    async def get_victory_progress(self) -> lq.VictoryProgress:
        lines = await self.conn.execute(lq.build_victory_progress_query())
        return lq.parse_victory_progress_response(lines)

    async def get_cities(self) -> tuple[list[lq.CityInfo], list[str]]:
        lines = await self.conn.execute(lq.build_cities_query())
        return lq.parse_cities_response(lines)

    async def get_map_area(
        self, center_x: int, center_y: int, radius: int = 2
    ) -> list[lq.TileInfo]:
        lines = await self.conn.execute(
            lq.build_map_area_query(center_x, center_y, radius)
        )
        return lq.parse_map_response(lines)

    async def get_world_snapshot(self) -> lq.WorldSnapshot:
        lines = await self.conn.execute(lq.build_world_snapshot_query(), timeout=20.0)
        return lq.parse_world_snapshot_response(lines)

    async def get_strategic_map(self) -> lq.StrategicMapData:
        lines = await self.conn.execute(lq.build_strategic_map_query())
        return lq.parse_strategic_map_response(lines)

    async def get_diplomacy(self) -> list[lq.CivInfo]:
        # Uses InGame context for GetDiplomaticAI access
        lines = await self.conn.execute(lq.build_diplomacy_query())
        return lq.parse_diplomacy_response(lines)

    async def get_tech_civics(self) -> lq.TechCivicStatus:
        # InGame on purpose: GetTurnsToProgressCivic / GetCulturalProgress exist
        # only there, so civic turns and progress are exact instead of estimated.
        lines = await self.conn.execute(lq.build_tech_civics_query())
        return lq.parse_tech_civics_response(lines)

    async def get_empire_resources(
        self,
    ) -> tuple[
        list[lq.ResourceStockpile],
        list[lq.OwnedResource],
        list[lq.NearbyResource],
        dict[str, int],
    ]:
        # InGame context needed for GetResourceStockpileCap etc.
        lines = await self.conn.execute(lq.build_empire_resources_query())
        return lq.parse_empire_resources_response(lines)

    # ------------------------------------------------------------------
    # Action methods (run in InGame context for UnitManager access)
    # ------------------------------------------------------------------

    async def move_unit(self, unit_index: int, target_x: int, target_y: int) -> str:
        # Pre-dismiss any blocking popups that would silently eat the move
        try:
            await self.dismiss_popup()
        except Exception:
            pass
        lua = lq.build_move_unit(unit_index, target_x, target_y)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)
        # Post-move: read the actual position (the move request is asynchronous)
        if result.startswith("MOVING_TO") or result.startswith("CAPTURE_MOVE"):
            try:
                pos_lines = await self.conn.execute(
                    lq.build_unit_position_query(
                        unit_index,
                        move_target_x=target_x,
                        move_target_y=target_y,
                    )
                )
                for line in pos_lines:
                    if line.startswith("POS|") and "GONE" not in line:
                        parts = line.split("|")
                        now_x, now_y = int(parts[1]), int(parts[2])
                        result += f"|now_at:{now_x},{now_y}"
                        from_match = re.search(r"\|from:(\d+),(\d+)", result)
                        if from_match:
                            from_x = int(from_match.group(1))
                            from_y = int(from_match.group(2))
                            if now_x == from_x and now_y == from_y:
                                reason = lq.parse_blocked_diagnostic(pos_lines)
                                result += f"|BLOCKED ({reason})"
                            else:
                                dx = now_x - from_x
                                dy = (
                                    now_y - from_y
                                )  # positive dy = south (higher Y = south in Civ 6)
                                result += f"|(moved dx:{dx:+d} dy:{dy:+d})"
                                tgt_match = re.search(
                                    r"(?:MOVING_TO|CAPTURE_MOVE)\|(\d+),(\d+)", result
                                )
                                if tgt_match:
                                    tx, ty = (
                                        int(tgt_match.group(1)),
                                        int(tgt_match.group(2)),
                                    )
                                    if (now_x, now_y) != (tx, ty):
                                        result += f"|STOPPED_MID_PATH (moves exhausted)"
                                    village = any(
                                        l == f"GOODY|{tx},{ty}" for l in lines
                                    ) and (now_x, now_y) == (tx, ty)
                                    changes = lq.describe_move_changes(
                                        lq.parse_unit_snapshot(lines),
                                        lq.parse_unit_snapshot(pos_lines),
                                        village,
                                    )
                                    if changes:
                                        result += "|" + changes
                        break
            except Exception:
                pass
        return result

    async def attack_unit(self, unit_index: int, target_x: int, target_y: int) -> str:
        # Pre-attack: dismiss any blocking popups that would silently eat the attack
        try:
            await self.dismiss_popup()
        except Exception:
            pass
        # Pre-attack: run combat estimator
        estimate_str = ""
        est: lq.CombatEstimate | None = None
        try:
            est_lua = lq.build_combat_estimate_query(unit_index, target_x, target_y)
            est_lines = await self.conn.execute(est_lua)
            est = lq.parse_combat_estimate(est_lines, 0, 0)
            if est:
                estimate_str = narrate_combat_estimate(est) + "\n"
        except Exception as e:
            log.debug("Combat estimate failed: %s", e)
        lua = lq.build_attack_unit(unit_index, target_x, target_y)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)
        # Combat followup: the game engine processes combat asynchronously
        # after RequestOperation.  Lua state within the same turn frame
        # does NOT reflect post-combat HP regardless of how long we wait.
        # Strategy: use the combat estimate as authoritative damage source
        # and only query the tile to detect if the target was eliminated.
        is_melee = result.startswith("MELEE_ATTACK")
        is_air = result.startswith("AIR_ATTACK")
        if result.startswith("RANGE_ATTACK") or is_melee or is_air:
            pre_hp = _extract_pre_hp(result)
            est_dmg = est.est_damage_to_defender if est else None
            local_id = self._local_player_id

            # Brief delay then check if target still exists on the tile
            await asyncio.sleep(0.3 if not is_melee else 0.5)
            followup: list[str] = []
            try:
                followup = await self.conn.execute(
                    lq.build_attack_followup_query(target_x, target_y)
                )
            except Exception as e:
                log.debug("Attack followup read failed: %s", e)

            try:
                followup_str = _format_attack_followup(followup, local_id)
                city_def = _extract_city_defense(followup)

                # Check if target was eliminated (no enemy units on tile)
                enemy_units = [
                    l
                    for l in followup
                    if l.startswith("UNIT|") and f"owner:{local_id}" not in l
                ]
                eliminated = not enemy_units

                # Build damage report from estimate (authoritative) or followup
                post_hp = _extract_post_hp(followup, local_id)
                damage_info = ""
                if eliminated and pre_hp is not None:
                    damage_info = f"|damage dealt:{pre_hp} (killed)"
                    followup_str = "Target eliminated"
                elif pre_hp is not None and post_hp is not None and post_hp < pre_hp:
                    # Followup reflects real change (can happen for city attacks)
                    damage_info = f"|damage dealt:{pre_hp - post_hp}"
                elif est_dmg and est_dmg > 0 and pre_hp is not None:
                    # Followup stale — use estimate as best available
                    capped_dmg = min(est_dmg, pre_hp)
                    est_post = pre_hp - capped_dmg
                    damage_info = f"|est damage dealt:~{capped_dmg}"
                    followup_str = (
                        f"~{est_post}/{pre_hp} (estimate — verify with get_units)"
                    )

                if city_def:
                    w_hp, w_max, g_hp, g_max = city_def
                    if w_max > 0:
                        damage_info += (
                            f"|city walls: {w_hp}/{w_max}, garrison: {g_hp}/{g_max}"
                        )

                result += damage_info + "\n  Post-combat: " + followup_str
            except Exception as e:
                log.debug("Attack followup formatting failed: %s", e)
        return estimate_str + result

    async def city_attack(self, city_id: int, target_x: int, target_y: int) -> str:
        lua = lq.build_city_attack(city_id, target_x, target_y)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)
        if result.startswith("CITY_RANGE_ATTACK"):
            pre_hp = _extract_pre_hp(result)
            await asyncio.sleep(0.3)
            followup: list[str] = []
            try:
                followup = await self.conn.execute(
                    lq.build_attack_followup_query(target_x, target_y)
                )
            except Exception:
                followup = []
            try:
                followup_str = _format_attack_followup(followup)
                post_hp = _extract_post_hp(followup)
                damage_info = ""
                if pre_hp is not None and post_hp is not None and post_hp < pre_hp:
                    damage_info = f"|damage dealt:{pre_hp - post_hp}"
                elif not any(l.startswith("UNIT|") for l in followup):
                    if pre_hp is not None:
                        damage_info = f"|damage dealt:{pre_hp} (killed)"
                    followup_str = "Target eliminated"

                result += damage_info + "\n  Post-combat: " + followup_str
            except Exception as e:
                log.debug("City attack followup failed: %s", e)
        return result

    async def resolve_city_capture(self, action: str) -> str:
        lua = lq.build_resolve_city_capture(action)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def found_city(self, unit_index: int) -> str:
        # Pre-dismiss any blocking popups (tech completion, era change, etc.)
        try:
            await self.dismiss_popup()
        except Exception:
            pass

        lua = lq.build_found_city(unit_index)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)

        if result.startswith("FOUNDED|"):
            # Extract coordinates from "FOUNDED|x,y"
            parts = result.split("|")[1].split(",")
            x, y = int(parts[0]), int(parts[1])
            # Verify city was actually created (RequestOperation is async)
            verify_lua = lq.build_verify_city_at(x, y)
            verify_lines = await self.conn.execute(verify_lua)
            # City IDs can be 0, so test `is None` rather than truthiness.
            city_id = lq.parse_verify_city_at(verify_lines)
            if city_id is None:
                # Retry once — popup may have blocked the async operation
                try:
                    await self.dismiss_popup()
                    lines = await self.conn.execute(lua)
                    retry_result = _action_result(lines)
                    if retry_result.startswith("FOUNDED|"):
                        verify_lines = await self.conn.execute(verify_lua)
                        city_id = lq.parse_verify_city_at(verify_lines)
                        if city_id is not None:
                            result = retry_result
                except Exception:
                    log.debug(
                        "found_city retry after popup dismiss failed", exc_info=True
                    )
                if city_id is None:
                    result = (
                        f"Error: FOUND_FAILED|Founding at {x},{y} was requested but "
                        "city did not appear despite popup dismissal."
                    )
            if city_id is not None and city_id >= 0:
                result += f"|city_id:{city_id}"

        # On settle failure, run the settle advisor to suggest alternatives
        if result.startswith("Error: CANNOT_FOUND") or result.startswith(
            "Error: FOUND_FAILED"
        ):
            try:
                advisor_result = await self.get_settle_advisor(unit_index)
                result += "\n\n" + advisor_result
            except Exception as e:
                log.debug("Settle advisor failed: %s", e)
        return result

    async def get_settle_advisor(self, unit_index: int) -> str:
        lua = lq.build_settle_advisor_query(unit_index)
        lines = await self.conn.execute(lua)
        candidates = lq.parse_settle_advisor_response(lines)
        if candidates:
            return narrate_settle_candidates(candidates)
        # Auto-fallback to global scan when no local candidates
        try:
            global_candidates = await self.get_global_settle_scan()
            if global_candidates:
                header = "No valid settle locations within 5 tiles. Best sites on revealed map:\n"
                return header + narrate_settle_candidates(global_candidates[:5])
        except Exception:
            log.debug("Global settle fallback failed", exc_info=True)
        return "No valid settle locations found within 5 tiles or on revealed map."

    async def get_global_settle_scan(self) -> list[lq.SettleCandidate]:
        lua = lq.build_global_settle_scan()
        lines = await self.conn.execute(lua)
        return lq.parse_settle_advisor_response(lines)

    async def fortify_unit(self, unit_index: int) -> str:
        lua = lq.build_fortify_unit(unit_index)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)
        if result.startswith("SLEEPING"):
            return "Unit is sleeping (this unit type cannot fortify)"
        return result

    async def skip_unit(self, unit_index: int) -> str:
        lua = lq.build_skip_unit(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def skip_remaining_units(self) -> str:
        # First try to fortify/heal combat units (InGame context)
        fortify_result = ""
        try:
            lua_fort = lq.build_fortify_remaining_units()
            fort_lines = await self.conn.execute(lua_fort)
            fortify_result = _action_result(fort_lines)
        except Exception as e:
            log.debug("Fortify remaining failed: %s", e)
        # Then skip anything still with moves (GameCore context)
        lua = lq.build_skip_remaining_units()
        lines = await self.conn.execute(lua)
        skip_result = _action_result(lines)
        if fortify_result and not fortify_result.startswith("Error"):
            return f"{fortify_result}\n{skip_result}"
        return skip_result

    async def automate_explore(self, unit_index: int) -> str:
        lua = lq.build_automate_explore(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def heal_unit(self, unit_index: int) -> str:
        lua = lq.build_heal_unit(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def alert_unit(self, unit_index: int) -> str:
        lua = lq.build_alert_unit(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def sleep_unit(self, unit_index: int) -> str:
        lua = lq.build_sleep_unit(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def delete_unit(self, unit_index: int) -> str:
        lua = lq.build_delete_unit(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def improve_tile(self, unit_index: int, improvement_name: str) -> str:
        lua = lq.build_improve_tile(unit_index, improvement_name)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def remove_feature(self, unit_index: int) -> str:
        lua = lq.build_remove_feature(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def repair_improvement(self, unit_index: int) -> str:
        lua = lq.build_repair_improvement(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def remove_improvement(self, unit_index: int) -> str:
        lua = lq.build_remove_improvement(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def sacrifice_builder_charges(self, unit_index: int) -> str:
        lua = lq.build_sacrifice_builder_charges(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def build_route(self, unit_index: int) -> str:
        lua = lq.build_build_route(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def set_city_production(
        self,
        city_id: int,
        item_type: str,
        item_name: str,
        target_x: int | None = None,
        target_y: int | None = None,
    ) -> str:
        itype = item_type.upper()

        lua = lq.build_produce_item(city_id, item_type, item_name, target_x, target_y)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)

        # If CanStartOperation failed but CanProduce passed, verify via readback
        if any("MAYBE:" in l for l in lines):
            try:
                verify_lines = await self.conn.execute(
                    lq.build_verify_production(city_id, item_name)
                )
                if any("CONFIRMED" in l for l in verify_lines):
                    turns = ""
                    for vl in verify_lines:
                        if vl.startswith("CONFIRMED|"):
                            turns = vl.split("|", 1)[1]
                    return f"PRODUCING|{item_name}|{turns} (bypassed stale CanStartOperation)"
                else:
                    hint = ""
                    if itype == "DISTRICT":
                        hint = f" Tried ({target_x},{target_y})."
                        try:
                            placements = await self.get_district_advisor(
                                city_id, item_name
                            )
                            if isinstance(placements, list) and placements:
                                alts = ", ".join(
                                    f"({p.x},{p.y}) Adj +{p.total_adjacency}"
                                    for p in placements[:5]
                                )
                                hint += f" Valid tiles: {alts}."
                        except Exception:
                            pass
                        hint += " Use get_district_advisor for details."
                    elif itype == "BUILDING":
                        # Check if Lua reported pillaged districts
                        pillaged_dists = ""
                        for ml in lines:
                            if "PILLAGED:" in ml:
                                pillaged_dists = ml.split("PILLAGED:", 1)[1]
                                break
                        if pillaged_dists:
                            hint = (
                                f" Prerequisite district is pillaged:"
                                f" {pillaged_dists}. Repair it first via"
                                " set_city_production(city_id, 'DISTRICT',"
                                " 'DISTRICT_NAME', x, y) — use get_cities"
                                " to find district coordinates."
                            )
                        else:
                            bld_info = item_name.replace("BUILDING_", "")
                            hint = (
                                f" Hint: {bld_info} may require a completed"
                                " district or prerequisite building."
                            )
                    return f"Error: CANNOT_START|{item_name} cannot start.{hint}"
            except Exception:
                log.debug("Production readback failed", exc_info=True)
                return f"Error: CANNOT_START|{item_name} (readback failed)"

        # OK-path verification. RequestOperation is fire-and-forget; even
        # when CanStartOperation returned true it can silently no-op if the
        # queue is in a degenerate state. Round-trip read to confirm.
        if result.startswith("PRODUCING|"):
            try:
                verify_lines = await self.conn.execute(
                    lq.build_verify_production(city_id, item_name)
                )
                if any("CONFIRMED" in vl for vl in verify_lines):
                    return result
                not_set = next(
                    (vl for vl in verify_lines if vl.startswith("NOT_SET|")),
                    "NOT_SET|unknown",
                )
                return (
                    f"Error: SILENT_FAILURE|{item_name} appeared to set but "
                    f"the game engine did not persist it ({not_set}). Retry "
                    f"the same call, or use purchase_item to force-commit "
                    f"with gold/faith."
                )
            except Exception:
                log.debug("OK-path production verify failed", exc_info=True)

        return result

    async def purchase_item(
        self,
        city_id: int,
        item_type: str,
        item_name: str,
        yield_type: str = "YIELD_GOLD",
    ) -> str:
        lua = lq.build_purchase_item(city_id, item_type, item_name, yield_type)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def list_city_production(self, city_id: int) -> list[lq.ProductionOption]:
        lua = lq.build_city_production_query(city_id)
        # Must use InGame context — bq:CanProduce() throws "Not Implemented" in GameCore
        lines = await self.conn.execute(lua)
        return lq.parse_city_production_response(lines)

    async def set_research(self, tech_name: str) -> str:
        lua = lq.build_set_research(tech_name)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)
        if "RESEARCHING" in result:
            # Verify InGame actually accepted it by comparing tech INDEX.
            # RequestPlayerOperation is fire-and-forget — it can silently no-op
            # while GetResearchingTech() still returns the OLD tech's index (!= -1).
            verify = await self.conn.execute(
                f"local me = Game.GetLocalPlayer(); "
                f"local idx = nil; "
                f"for row in GameInfo.Technologies() do "
                f'if row.TechnologyType == "{tech_name}" then idx = row.Index; break end '
                f"end; "
                f"local cur = Players[me]:GetTechs():GetResearchingTech(); "
                f"print(cur == idx and 'MATCH' or 'MISMATCH:'..tostring(cur)..'~='..tostring(idx)); "
                f'print("{lq.SENTINEL}")'
            )
            if not (verify and verify[0] == "MATCH"):
                return (
                    f"Error: SILENT_FAILURE|{tech_name} was requested but the game "
                    "did not switch research to it. Retry the call."
                )
        return result

    async def set_civic(self, civic_name: str) -> str:
        lua = lq.build_set_civic(civic_name)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)
        if "PROGRESSING" in result:
            # Verify InGame actually accepted it by comparing civic INDEX.
            verify = await self.conn.execute(
                f"local me = Game.GetLocalPlayer(); "
                f"local idx = nil; "
                f"for row in GameInfo.Civics() do "
                f'if row.CivicType == "{civic_name}" then idx = row.Index; break end '
                f"end; "
                f"local cur = Players[me]:GetCulture():GetProgressingCivic(); "
                f"print(cur == idx and 'MATCH' or 'MISMATCH:'..tostring(cur)..'~='..tostring(idx)); "
                f'print("{lq.SENTINEL}")'
            )
            if not (verify and verify[0] == "MATCH"):
                return (
                    f"Error: SILENT_FAILURE|{civic_name} was requested but the game "
                    "did not switch the civic to it. Retry the call."
                )
        return result

    # ------------------------------------------------------------------
    # Diplomacy methods
    # ------------------------------------------------------------------

    async def get_diplomacy_sessions(self) -> list[lq.DiplomacySession]:
        lua = lq.build_diplomacy_session_query()
        lines = await self.conn.execute(lua)
        return lq.parse_diplomacy_sessions(lines)

    # The engine needs a moment after a response to either close the session
    # or deliver the leader's next statement; a forced close while the view
    # is still animating has frozen the game before.
    RESPONSE_SETTLE_S = 0.5
    RESPONSE_SETTLE_ROUNDS = 8  # ~4 s for the reply statement to arrive

    async def _session_open(self, other_player_id: int) -> bool:
        lines = await self.conn.execute(
            lq.build_check_diplomacy_session_state(other_player_id)
        )
        return any("SESSION_OPEN" in l for l in lines)

    async def _leave_session(self, other_player_id: int) -> str:
        lines = await self.conn.execute(
            lq.build_diplomacy_respond(other_player_id, "EXIT")
        )
        result = _action_result(lines)
        await asyncio.sleep(self.RESPONSE_SETTLE_S)
        return result if result.startswith("Error") else "SESSION_CLOSED"

    async def diplomacy_respond(self, other_player_id: int, response: str) -> str:
        response = response.upper()
        if response == "EXIT":
            if not await self._session_open(other_player_id):
                return "Error: ERR:NO_SESSION"
            return f"OK:RESPONDED|EXIT|{await self._leave_session(other_player_id)}"

        # Capture dialogue text BEFORE the response to detect the goodbye phase
        pre_sessions = await self.get_diplomacy_sessions()
        pre_text = ""
        for s in pre_sessions:
            if s.other_player_id == other_player_id:
                pre_text = s.dialogue_text
                break

        # Phase 1: AddResponse only — the same call the leader-screen button
        # makes; the engine handles the session lifecycle.
        lua = lq.build_diplomacy_respond(other_player_id, response)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)
        if result.startswith("Error"):
            return result

        # Phase 2: wait for the engine to either close the session or deliver
        # the leader's next statement. A fixed 0.3 s was too short: the view
        # was still animating and a forced close froze the game.
        post_text = pre_text
        post_reason = ""
        for _ in range(self.RESPONSE_SETTLE_ROUNDS):
            await asyncio.sleep(self.RESPONSE_SETTLE_S)
            if not await self._session_open(other_player_id):
                return f"OK:RESPONDED|{response}|SESSION_CLOSED"
            for s in await self.get_diplomacy_sessions():
                if s.other_player_id == other_player_id:
                    post_text, post_reason = s.dialogue_text, s.reason_text
                    break
            if post_text and post_text != pre_text:
                break

        if not post_text or post_text == pre_text:
            # Nothing new to say: the leader is waiting for us to leave.
            log.info(
                "Goodbye phase (no new statement) for player %d — closing the session",
                other_player_id,
            )
            return (
                f"OK:RESPONDED|{response}|{await self._leave_session(other_player_id)}"
            )

        # Include the new dialogue text so the agent can see what the leader said
        dialogue_note = f'\nLeader says: "{post_text}"'
        if post_reason:
            dialogue_note += f'\nReason/agenda: "{post_reason}"'
        return f"OK:RESPONDED|{response}|SESSION_CONTINUES{dialogue_note}"

    async def send_diplomatic_action(self, other_player_id: int, action: str) -> str:
        if action.upper() == "OPEN_BORDERS":
            # Session-based OPEN_BORDERS causes AI turn hang.
            # Route through the trade deal API instead (mutual open borders).
            return await self.propose_trade(
                other_player_id,
                offer_items=[{"type": "AGREEMENT", "subtype": "OPEN_BORDERS"}],
                request_items=[{"type": "AGREEMENT", "subtype": "OPEN_BORDERS"}],
            )
        is_war = action.upper().endswith("_WAR") and action.upper().startswith(
            "DECLARE_"
        )
        lua = lq.build_send_diplo_action(other_player_id, action.upper())
        lines = await self.conn.execute(lua)
        result = _action_result(lines)

        if is_war and not result.startswith("ERR:"):
            # War session left open for ~8s so the leader animation plays.
            # Background task will close session + dismiss DiplomacyActionView.
            asyncio.create_task(self._cleanup_war_diplomacy(other_player_id))

        return result

    async def _cleanup_war_diplomacy(self, other_player_id: int) -> None:
        """Background: dismiss war declaration diplomacy view after animation.

        Two-phase cleanup (must be separate Lua calls — the engine fires
        OnDiplomacySessionClosed asynchronously so the view needs a frame
        to transition from CONVERSATION_MODE to OVERVIEW_MODE):
        1. CloseSession — view transitions to OVERVIEW_MODE
        2. NaturalWonderPopup trick — forces Close() from OVERVIEW_MODE
        """
        await asyncio.sleep(8)
        try:
            # Phase 1: close session → view goes to OVERVIEW_MODE
            lua1 = lq.build_war_close_session(other_player_id)
            await self.conn.execute(lua1)

            # Let engine process OnDiplomacySessionClosed
            await asyncio.sleep(1)

            # Phase 2: force-dismiss the OVERVIEW_MODE view
            lua2 = lq.build_war_dismiss_view()
            await self.conn.execute(lua2)
        except Exception as e:
            log.warning("War diplomacy cleanup failed: %s", e)

    # ------------------------------------------------------------------
    # Trade deal methods (InGame context)
    # ------------------------------------------------------------------

    async def get_deal_options(self, other_player_id: int) -> lq.DealOptions:
        lua = lq.build_deal_options_query(other_player_id)
        lines = await self.conn.execute(lua)
        return lq.parse_deal_options_response(lines)

    async def get_pending_deals(self) -> list[lq.PendingDeal]:
        lua = lq.build_pending_deals_query()
        lines = await self.conn.execute(lua)
        return lq.parse_pending_deals_response(lines)

    async def respond_to_deal(self, other_player_id: int, accept: bool) -> str:
        lua = lq.build_respond_to_deal(other_player_id, accept)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def propose_trade(
        self,
        other_player_id: int,
        offer_items: list[dict],
        request_items: list[dict],
    ) -> str:
        lua = lq.build_propose_trade(other_player_id, offer_items, request_items)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)
        # Dismiss diplomacy UI left open by the trade session.
        # After CloseSession, the game transitions DiplomacyActionView to
        # OVERVIEW_MODE (intel screen). Need a brief delay for the C++ UI
        # state machine to settle, then dismiss it in a separate call.
        await asyncio.sleep(0.3)
        try:
            await self.conn.execute(
                'pcall(function() ContextPtr:LookUpControl("/InGame/DiplomacyActionView"):SetHide(true) end) '
                "pcall(function() Events.HideLeaderScreen() end) "
                "LuaEvents.DiplomacyActionView_ShowIngameUI() "
                f'print("{lq.SENTINEL}")'
            )
        except Exception:
            pass
        return result

    async def test_trade(
        self,
        other_player_id: int,
        offer_items: list[dict],
        request_items: list[dict],
    ) -> str:
        lua = lq.build_test_trade(other_player_id, offer_items, request_items)
        lines = await self.conn.execute(lua)
        result = lq.parse_test_trade_response(lines)
        return narrate_test_trade(result)

    async def propose_peace(self, other_player_id: int) -> str:
        lua = lq.build_propose_peace(other_player_id)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)
        if result.startswith("Error"):
            return result
        # War state is async — verify with a second round-trip
        verify_lines = await self.conn.execute(
            lq.build_check_war_state(other_player_id)
        )
        at_peace = any("AT_PEACE" in l for l in verify_lines)
        name = result.split("|", 1)[1] if "|" in result else f"player {other_player_id}"
        if at_peace:
            return f"ACCEPTED|Peace established with {name}"
        else:
            return f"REJECTED|{name} rejected your peace offer"

    async def form_alliance(self, other_player_id: int, alliance_type: str) -> str:
        lua = lq.build_form_alliance(other_player_id, alliance_type.upper())
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Policy methods (InGame context)
    # ------------------------------------------------------------------

    async def get_policies(self) -> lq.GovernmentStatus:
        lua = lq.build_policies_query()
        lines = await self.conn.execute(lua)
        return lq.parse_policies_response(lines)

    async def set_policies(self, assignments: dict[int, str]) -> str:
        lua = lq.build_set_policies(assignments)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)
        if not result.startswith("Error"):
            # Post-verify: RequestPolicyChanges can silently no-op (e.g. during era transitions)
            status = await self.get_policies()
            slot_map = {s.slot_index: s.current_policy for s in status.slots}
            mismatches = []
            for idx, pol in assignments.items():
                expected = None if pol.upper() == "NONE" else pol
                actual = slot_map.get(idx)
                if actual != expected:
                    wanted = "EMPTY" if expected is None else pol
                    got = actual or "EMPTY"
                    mismatches.append(f"slot {idx} (wanted {wanted}, got {got})")
            if mismatches:
                result += (
                    f"\nWARN:SILENT_FAILURE — engine rejected: {', '.join(mismatches)}. "
                    "Try a different policy or retry next turn."
                )
        return result

    # ------------------------------------------------------------------
    # Governor methods (InGame context)
    # ------------------------------------------------------------------

    async def get_governors(self) -> lq.GovernorStatus:
        lua = lq.build_governors_query()
        lines = await self.conn.execute(lua)
        return lq.parse_governors_response(lines)

    async def appoint_governor(self, governor_type: str) -> str:
        lua = lq.build_appoint_governor(governor_type)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def assign_governor(self, governor_type: str, city_id: int) -> str:
        lua = lq.build_assign_governor(governor_type, city_id)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def promote_governor(self, governor_type: str, promotion_type: str) -> str:
        lua = lq.build_promote_governor(governor_type, promotion_type)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)
        if "PROMOTED" in result:
            # Verify promotion actually applied (RequestPlayerOperation is async)
            verify = await self.conn.execute(
                f"local me = Game.GetLocalPlayer(); "
                f"local pGovs = Players[me]:GetGovernors(); "
                f'local gov = GameInfo.Governors["{governor_type}"]; '
                f'local promo = GameInfo.GovernorPromotions["{promotion_type}"]; '
                f"if gov and promo then "
                f"  local g = pGovs:GetGovernor(gov.Hash); "
                f"  if g and g:HasPromotion(promo.Index) then "
                f'    print("VERIFIED") '
                f"  else "
                f'    print("ERR:PROMOTION_FAILED|{promotion_type} was not applied") '
                f"  end "
                f"else "
                f'  print("ERR:LOOKUP_FAILED") '
                f"end; "
                f'print("{lq.SENTINEL}")'
            )
            if any("ERR:" in l for l in verify):
                return _action_result(verify)
        return result

    # ------------------------------------------------------------------
    # Promotion methods
    # ------------------------------------------------------------------

    async def get_unit_promotions(self, unit_id: int) -> lq.UnitPromotionStatus:
        unit_index = unit_id % 65536
        lua = lq.build_unit_promotions_query(unit_index)
        lines = await self.conn.execute(lua)
        return lq.parse_unit_promotions_response(lines)

    async def promote_unit(self, unit_id: int, promotion_type: str) -> str:
        unit_index = unit_id % 65536
        lua = lq.build_promote_unit(unit_index, promotion_type)
        lines = await self.conn.execute(lua)  # GameCore context
        result = _action_result(lines)
        # GameCore SetPromotion doesn't clear the InGame NEEDS_PROMOTION
        # notification, which blocks end_turn until dismissed.
        # Use XP-threshold formula (matching end_turn handler) to decide
        # whether any unit still genuinely needs a promotion before dismissing.
        if not result.startswith("Error"):
            try:
                await self.conn.execute(
                    f"local me = Game.GetLocalPlayer(); "
                    f"local anyNeed = false; "
                    f"for i, u in Players[me]:GetUnits():Members() do "
                    f"  if u:GetX() ~= -9999 then "
                    f"    local ok, exp = pcall(function() return u:GetExperience() end); "
                    f"    if ok and exp then "
                    f"      local ui = GameInfo.Units[u:GetType()]; "
                    f'      local promClass = ui and ui.PromotionClass or ""; '
                    f'      if promClass ~= "" then '
                    f"        local promoCount = 0; "
                    f"        for p in GameInfo.UnitPromotions() do "
                    f"          if p.PromotionClass == promClass and exp:HasPromotion(p.Index) then "
                    f"            promoCount = promoCount + 1 "
                    f"          end "
                    f"        end; "
                    f"        local t1 = exp:GetExperienceForNextLevel(); "
                    f"        local xp = exp:GetExperiencePoints(); "
                    f"        local needed = t1 * (promoCount + 1) * (promoCount + 2) / 2; "
                    f"        if xp >= needed then "
                    f"          anyNeed = true "
                    f"        else "
                    f"          local stored = 0; "
                    f"          pcall(function() stored = exp:GetStoredPromotions() end); "
                    f"          if stored > 0 then "
                    f"            pcall(function() exp:ChangeStoredPromotions(-stored) end) "
                    f"          end "
                    f"        end "
                    f"      end "
                    f"    end "
                    f"  end "
                    f"  if anyNeed then break end "
                    f"end; "
                    f"if not anyNeed then "
                    f"  local list = NotificationManager.GetList(me); "
                    f"  if list then "
                    f"    for _, nid in ipairs(list) do "
                    f"      local e = NotificationManager.Find(me, nid); "
                    f"      if e and not e:IsDismissed() then "
                    f"        local bt = e:GetEndTurnBlocking(); "
                    f"        if bt and bt == EndTurnBlockingTypes.ENDTURN_BLOCKING_UNIT_PROMOTION then "
                    f"          pcall(function() NotificationManager.SendActivated(me, nid) end); "
                    f"          pcall(function() NotificationManager.Dismiss(me, nid) end) "
                    f"        else "
                    f"          local tn = ''; "
                    f"          pcall(function() tn = e:GetTypeName() end); "
                    f"          if tn == 'NOTIFICATION_UNIT_PROMOTION_AVAILABLE' then "
                    f"            pcall(function() NotificationManager.Dismiss(me, nid) end) "
                    f"          end "
                    f"        end "
                    f"      end "
                    f"    end "
                    f"  end "
                    f"end; "
                    f'print("OK"); print("{lq.SENTINEL}")'
                )
            except Exception:
                pass  # non-fatal — end_turn blocker handler will catch it
        return result

    # ------------------------------------------------------------------
    # City-state / Envoy methods (InGame context)
    # ------------------------------------------------------------------

    async def get_city_states(self) -> lq.EnvoyStatus:
        lua = lq.build_city_states_query()
        lines = await self.conn.execute(lua)
        return lq.parse_city_states_response(lines)

    async def send_envoy(self, city_state_player_id: int) -> str:
        lua = lq.build_send_envoy(city_state_player_id)
        lines = await self.conn.execute(lua)
        result = _action_result(lines)
        if result.startswith("OK:ENVOY_SENT"):
            # Verify token actually decremented (async race condition workaround)
            await asyncio.sleep(0.1)
            try:
                verify_lines = await self.conn.execute(
                    f"local me = Game.GetLocalPlayer(); "
                    f"print(Players[me]:GetInfluence():GetTokensToGive()); "
                    f'print("{lq.SENTINEL}")'
                )
                if verify_lines and verify_lines[0].strip().lstrip("-").isdigit():
                    actual = int(verify_lines[0].strip())
                    result += f" (verified remaining: {actual})"
            except Exception:
                log.debug("Envoy verification failed", exc_info=True)
        return result

    # ------------------------------------------------------------------
    # Pantheon methods (InGame context)
    # ------------------------------------------------------------------

    async def get_pantheon_status(self) -> lq.PantheonStatus:
        lua = lq.build_pantheon_status_query()
        lines = await self.conn.execute(lua)
        return lq.parse_pantheon_status_response(lines)

    async def choose_pantheon(self, belief_type: str) -> str:
        lua = lq.build_choose_pantheon(belief_type)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Religion founding methods (InGame context)
    # ------------------------------------------------------------------

    async def get_religion_founding_status(self) -> lq.ReligionFoundingStatus:
        lua = lq.build_religion_beliefs_query()
        lines = await self.conn.execute(lua)
        return lq.parse_religion_beliefs_response(lines)

    async def found_religion(
        self, religion_type: str, follower_belief: str, founder_belief: str
    ) -> str:
        lua = lq.build_found_religion(religion_type, follower_belief, founder_belief)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Unit upgrade methods (InGame context)
    # ------------------------------------------------------------------


    async def upgrade_unit(self, unit_id: int) -> str:
        unit_index = unit_id % 65536
        lua = lq.build_upgrade_unit(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Dedications / Commemorations
    # ------------------------------------------------------------------

    async def get_dedications(self) -> lq.DedicationStatus:
        lua = lq.build_dedications_query()
        lines = await self.conn.execute(lua)
        return lq.parse_dedications_response(lines)

    async def choose_dedication(self, dedication_index: int) -> str:
        lua = lq.build_choose_dedication(dedication_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # District / wonder advisor
    # ------------------------------------------------------------------

    async def get_district_advisor(
        self, city_id: int, district_type: str
    ) -> list[lq.DistrictPlacement] | str:
        """Returns placements, or an ERR: string when placement is impossible."""
        lines = await self.conn.execute(
            lq.build_district_advisor_query(city_id, district_type)
        )
        for line in lines:
            if line.startswith("ERR:"):
                return line
        return lq.parse_district_advisor_response(lines)

    async def get_wonder_advisor(
        self, city_id: int, wonder_name: str
    ) -> list[lq.WonderPlacement]:
        lines = await self.conn.execute(lq.build_wonder_advisor_query(city_id, wonder_name))
        return lq.parse_wonder_advisor_response(lines)

    # ------------------------------------------------------------------
    # Tile purchase methods (InGame context)
    # ------------------------------------------------------------------

    async def get_purchasable_tiles(self, city_id: int) -> list[lq.PurchasableTile]:
        lua = lq.build_purchasable_tiles_query(city_id)
        lines = await self.conn.execute(lua)
        return lq.parse_purchasable_tiles_response(lines)

    async def purchase_tile(self, city_id: int, x: int, y: int) -> str:
        lua = lq.build_purchase_tile(city_id, x, y)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Government change (InGame context)
    # ------------------------------------------------------------------

    async def change_government(self, government_type: str) -> str:
        lua = lq.build_change_government(government_type)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Great People (InGame context)
    # ------------------------------------------------------------------

    async def get_great_people(self) -> list[lq.GreatPersonInfo]:
        lua = lq.build_great_people_query()
        lines = await self.conn.execute(lua)
        return lq.parse_great_people_response(lines)

    async def get_gp_advisor(self, unit_index: int) -> lq.GPAdvisorResult | None:
        lua = lq.build_gp_advisor_query(unit_index)
        lines = await self.conn.execute(lua)
        return lq.parse_gp_advisor_response(lines)

    async def recruit_great_person(self, individual_id: int) -> str:
        lua = lq.build_recruit_great_person(individual_id)
        lines = await self.conn.execute(lua)
        return lines[0] if lines else "No response"

    async def patronize_great_person(
        self, individual_id: int, yield_type: str = "YIELD_GOLD"
    ) -> str:
        lua = lq.build_patronize_great_person(individual_id, yield_type)
        lines = await self.conn.execute(lua)
        return lines[0] if lines else "No response"

    async def get_religion_status(self) -> lq.ReligionStatus:
        lines = await self.conn.execute(lq.build_religion_status_query())
        return lq.parse_religion_status_response(lines)

    async def reject_great_person(self, individual_id: int) -> str:
        lua = lq.build_reject_great_person(individual_id)
        lines = await self.conn.execute(lua)
        return lines[0] if lines else "No response"

    # ------------------------------------------------------------------
    # Trade route methods (InGame context)
    # ------------------------------------------------------------------

    async def get_trade_routes(self) -> lq.TradeRouteStatus:
        lua = lq.build_trade_routes_query()
        lines = await self.conn.execute(
            lua
        )  # InGame context (GetOutgoingRoutes is InGame-only)
        return lq.parse_trade_routes_response(lines)

    async def get_trade_destinations(
        self, unit_index: int
    ) -> list[lq.TradeDestination]:
        lua = lq.build_trade_destinations_query(unit_index)
        lines = await self.conn.execute(lua)
        return lq.parse_trade_destinations_response(lines)

    async def make_trade_route(
        self, unit_index: int, target_x: int, target_y: int
    ) -> str:
        lua = lq.build_make_trade_route(unit_index, target_x, target_y)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Great Person activation (InGame context)
    # ------------------------------------------------------------------

    async def activate_great_person(self, unit_index: int) -> str:
        lua = lq.build_activate_great_person(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    async def spread_religion(self, unit_index: int) -> str:
        lua = lq.build_spread_religion(unit_index)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Trader teleport (InGame context)
    # ------------------------------------------------------------------

    async def teleport_to_city(
        self, unit_index: int, target_x: int, target_y: int
    ) -> str:
        lua = lq.build_teleport_to_city(unit_index, target_x, target_y)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # World Congress (InGame context)
    # ------------------------------------------------------------------

    async def get_world_congress(self) -> lq.WorldCongressStatus:
        lua = lq.build_world_congress_query()
        lines = await self.conn.execute(lua)
        return lq.parse_world_congress_response(lines)


    async def queue_wc_votes(self, votes: list[dict]) -> str:
        """Store agent voting preferences and register WC event handler."""
        lua = lq.build_register_wc_voter(votes=votes)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # City yield focus (InGame context)
    # ------------------------------------------------------------------

    async def set_city_focus(self, city_id: int, focus: str) -> str:
        lua = lq.build_set_yield_focus(city_id, focus)
        lines = await self.conn.execute(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    async def get_notifications(self) -> list[lq.GameNotification]:
        lua = lq.build_notifications_query()
        lines = await self.conn.execute(lua)
        return lq.parse_notifications_response(lines)

    # ------------------------------------------------------------------
    # Snapshot-diff for turn event detection
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Turn management
    # ------------------------------------------------------------------


    async def dismiss_popup(self) -> str:
        """Dismiss blocking popups that can be closed from outside their context.

        Popups that hold an engine event until their own close button runs
        (natural wonder, wonder built, era complete, tutorial advisor, ...)
        are only reported: they need ESC or a click at the keyboard.
        """
        lines = await self.conn.execute(_DISMISS_POPUP_LUA)
        dismissed = [l.split("|", 1)[1] for l in lines if l.startswith("DISMISSED|")]
        visible = [l.split("|", 1)[1] for l in lines if l.startswith("VISIBLE|")]
        parts = []
        if dismissed:
            parts.append(f"Dismissed: {', '.join(dismissed)}.")
        if visible:
            parts.append(
                "Still showing (needs the in-game close button or ESC): "
                f"{', '.join(visible)}."
            )
        if "PENDING|DiplomacyActionView" in lines:
            parts.append("Diplomacy session active — use respond_to_diplomacy.")
        if "PENDING|DiplomacyDealView" in lines:
            parts.append("Incoming trade deal pending — use get_pending_trades.")
        return " ".join(parts) if parts else "No popups to dismiss."


# Popups the UI state can close from the outside (DequeuePopup + SetHide).
_CLOSABLE_POPUPS = (
    "InGamePopup",
    "GenericPopup",
    "PopupDialog",
    "BoostUnlockedPopup",
    "GreatWorkShowcase",
    "WorldCongressPopup",
    "WorldCongressIntro",
)
# Popups that reference an engine event until their own Close() runs; hiding
# them from another context leaves the engine waiting (PLEASE WAIT), so they
# are reported, not touched.
_EVENT_POPUPS = (
    "TechCivicCompletedPopup",
    "NaturalWonderPopup",
    "NaturalDisasterPopup",
    "WonderBuiltPopup",
    "EraCompletePopup",
    "HistoricMoments",
    "MomentPopup",
    "ProjectBuiltPopup",
    "RockBandPopup",
    "RockBandMoviePopup",
    "TutorialUIRoot/AdvisorPopup",
)


def _build_dismiss_popup_lua() -> str:
    checks = [
        f'do local c = ContextPtr:LookUpControl("/InGame/{n}") '
        "if c and not c:IsHidden() then "
        "pcall(function() UIManager:DequeuePopup(c) end) "
        "pcall(function() Input.PopContext() end) "
        f'c:SetHide(true) print("DISMISSED|{n}") end end'
        for n in _CLOSABLE_POPUPS
    ]
    checks += [
        f'do local c = ContextPtr:LookUpControl("/InGame/{n}") '
        f'if c and not c:IsHidden() then print("VISIBLE|{n}") end end'
        for n in _EVENT_POPUPS
    ]
    checks += [
        f'do local c = ContextPtr:LookUpControl("/InGame/{n}") '
        f'if c and not c:IsHidden() then print("PENDING|{n}") end end'
        for n in ("DiplomacyActionView", "DiplomacyDealView")
    ]
    checks.append(
        "if UI.GetInterfaceMode() == InterfaceModeTypes.CINEMATIC then "
        'pcall(function() UI.ClearTemporaryPlotVisibility("NaturalDisaster") end) '
        'pcall(function() UI.ClearTemporaryPlotVisibility("NaturalWonder") end) '
        "pcall(function() Events.StopAllCameraAnimations() end) "
        "pcall(function() UILens.RestoreActiveLens() end) "
        "UI.SetInterfaceMode(InterfaceModeTypes.SELECTION) "
        'print("DISMISSED|cinematic_camera") end'
    )
    return " ".join(checks) + f' print("{lq.SENTINEL}")'


_DISMISS_POPUP_LUA = _build_dismiss_popup_lua()


def _action_result(lines: list[str]) -> str:
    """Parse OK:/ERR: prefixed action responses.

    Scans all lines for the first OK:/ERR: prefix, since LuaEvent
    callbacks (e.g. ShowIngameUI → BulkHide debug prints) can inject
    spurious output before the actual result line.
    """
    if not lines:
        return "Action completed (no response)."
    for line in lines:
        if line.startswith("OK:"):
            return line[3:]
        if line.startswith("ERR:"):
            return f"Error: {line[4:]}"
    # No OK/ERR found — return all lines for debugging
    return "\n".join(lines)


def _format_attack_followup(lines: list[str], attacker_owner: int = 0) -> str:
    """Format the GameCore follow-up read after an attack.

    Filters out units belonging to ``attacker_owner`` so that after a melee
    kill (where the attacker moves onto the target tile) we don't misreport
    our own unit's HP as the defender's.

    Also includes city wall/garrison HP when attacking a walled city.
    """
    parts = []
    for line in lines:
        if line.startswith("UNIT|"):
            fields = line.split("|")
            if len(fields) >= 4:
                # fields: UNIT|TYPE|hp/max|owner:N
                owner_str = fields[3]  # "owner:N"
                try:
                    owner_id = int(owner_str.split(":")[1])
                except (IndexError, ValueError):
                    owner_id = -1
                label = "(yours) " if owner_id == attacker_owner else ""
                parts.append(f"{label}{fields[1]} {fields[2]}")
            elif len(fields) >= 3:
                parts.append(f"{fields[1]} {fields[2]}")
    city_def = _extract_city_defense(lines)
    if city_def:
        wall_hp, wall_max, gar_hp, gar_max = city_def
        if wall_max > 0:
            parts.append(f"Walls {wall_hp}/{wall_max}")
        if gar_max > 0:
            parts.append(f"City garrison {gar_hp}/{gar_max}")
    if not parts:
        return "Target eliminated"
    return ", ".join(parts)


def _extract_pre_hp(result: str) -> int | None:
    """Extract pre-attack enemy HP from attack result line."""
    import re

    # Ranged/city: pre_hp:80/100
    m = re.search(r"pre_hp:(\d+)/", result)
    if m:
        return int(m.group(1))
    # Melee: enemy HP:100 -> 80/100
    m = re.search(r"enemy HP:(\d+) ->", result)
    if m:
        return int(m.group(1))
    return None


def _extract_post_hp(followup_lines: list[str], attacker_owner: int = 0) -> int | None:
    """Extract post-combat *enemy* HP from followup query lines.

    Followup format: UNIT|UNIT_TYPE|hp/max|owner:N
    Skips units belonging to ``attacker_owner`` (after melee kill, attacker
    occupies the target tile and would otherwise be misread as defender).
    Returns HP of first enemy unit found (None if eliminated).
    """
    for line in followup_lines:
        if line.startswith("UNIT|"):
            parts = line.split("|")
            if len(parts) >= 4:
                try:
                    owner_id = int(parts[3].split(":")[1])
                except (IndexError, ValueError):
                    owner_id = -1
                if owner_id == attacker_owner:
                    continue  # our unit, not the target
            if len(parts) >= 3:
                hp_part = parts[2].split("/")[0]
                try:
                    return int(hp_part)
                except ValueError:
                    pass
    return None


def _extract_city_defense(
    followup_lines: list[str],
) -> tuple[int, int, int, int] | None:
    """Extract wall and garrison HP from CITY_DEF followup line.

    Returns ``(wall_hp, wall_max, garrison_hp, garrison_max)`` or *None*
    when the target tile has no city defenses.
    """
    for line in followup_lines:
        if line.startswith("CITY_DEF|"):
            # CITY_DEF|wall:74/100|garrison:197/200
            wall_hp = wall_max = gar_hp = gar_max = 0
            for part in line.split("|")[1:]:
                if part.startswith("wall:"):
                    hp, mx = part[5:].split("/")
                    wall_hp, wall_max = int(hp), int(mx)
                elif part.startswith("garrison:"):
                    hp, mx = part[9:].split("/")
                    gar_hp, gar_max = int(hp), int(mx)
            return (wall_hp, wall_max, gar_hp, gar_max)
    return None
