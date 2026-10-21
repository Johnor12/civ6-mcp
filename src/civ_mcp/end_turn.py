"""Ending a stand-in turn: resolve blockers, check the seat, end for real, save."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import civ_mcp.narrate as nr
from civ_mcp import hotseat
from civ_mcp import lua as lq
from civ_mcp.bridge import LuaError

if TYPE_CHECKING:
    from civ_mcp.game_state import GameState

log = logging.getLogger(__name__)


async def _get_turn_number(gs: GameState) -> int | None:
    """Read the current game turn number."""
    try:
        lines = await gs.conn.execute(
            'print(Game.GetCurrentGameTurn()); print("---END---")'
        )
        if lines:
            return int(lines[0])
    except (LuaError, ValueError, IndexError):
        pass
    return None


async def _resolve_blockers(gs: GameState, turn_at_entry: int | None) -> str | None:
    """Auto-resolve soft end-turn blockers; return a message for hard ones.

    Returns None when nothing blocks the turn (or the game says it can end
    anyway, or the turn already advanced externally), else the text to hand
    back to the agent.
    """
    # Check ALL EndTurnBlocking notifications at once, auto-resolve soft
    # blockers, and report remaining hard blockers in a single message.
    for _round in range(3):
        try:
            blocking_lines = await gs.conn.execute(
                lq.build_end_turn_blocking_query()
            )
            blockers = lq.parse_end_turn_blocking(blocking_lines)
            if not blockers:
                break  # nothing blocking

            resolved_any = False
            hard_blockers: list[tuple[str, str]] = []

            for blocking_type, blocking_msg in blockers:
                # --- Auto-resolvable soft blockers ---

                if blocking_type == "ENDTURN_BLOCKING_GOVERNOR_IDLE":
                    await gs.conn.execute(
                        f"local me = Game.GetLocalPlayer(); "
                        f"local list = NotificationManager.GetList(me); "
                        f"if list then "
                        f"  for _, nid in ipairs(list) do "
                        f"    local e = NotificationManager.Find(me, nid); "
                        f"    if e and not e:IsDismissed() then "
                        f"      local bt = e:GetEndTurnBlocking(); "
                        f"      if bt and bt == EndTurnBlockingTypes.ENDTURN_BLOCKING_GOVERNOR_IDLE then "
                        f"        pcall(function() NotificationManager.SendActivated(me, nid) end); "
                        f"        pcall(function() NotificationManager.Dismiss(me, nid) end) "
                        f"      end "
                        f"    end "
                        f"  end "
                        f"end; "
                        f'print("OK"); print("{lq.SENTINEL}")'
                    )
                    resolved_any = True
                    continue

                if blocking_type == "ENDTURN_BLOCKING_CONSIDER_GOVERNMENT_CHANGE":
                    await gs.conn.execute(
                        f"local me = Game.GetLocalPlayer(); "
                        f"Players[me]:GetCulture():SetGovernmentChangeConsidered(true); "
                        f'print("OK"); print("{lq.SENTINEL}")'
                    )
                    resolved_any = True
                    continue

                if blocking_type == "ENDTURN_BLOCKING_WORLD_CONGRESS_LOOK":
                    await gs.conn.execute(
                        f"local me = Game.GetLocalPlayer(); "
                        f"UI.RequestPlayerOperation(me, PlayerOperations.WORLD_CONGRESS_LOOKED_AT_AVAILABLE, {{}}); "
                        f"local list = NotificationManager.GetList(me); "
                        f"if list then "
                        f"  for _, nid in ipairs(list) do "
                        f"    pcall(function() "
                        f"      local e = NotificationManager.Find(me, nid); "
                        f"      if e and not e:IsDismissed() then "
                        f"        local bt = e:GetEndTurnBlocking(); "
                        f"        if bt and bt == EndTurnBlockingTypes.ENDTURN_BLOCKING_WORLD_CONGRESS_LOOK then "
                        f"          NotificationManager.Dismiss(me, nid) "
                        f"        end "
                        f"      end "
                        f"    end) "
                        f"  end "
                        f"end; "
                        f'local i = ContextPtr:LookUpControl("/InGame/WorldCongressIntro"); '
                        f"if i then i:SetHide(true) end; "
                        f'local p = ContextPtr:LookUpControl("/InGame/WorldCongressPopup"); '
                        f"if p then p:SetHide(true) end; "
                        f'print("OK"); print("{lq.SENTINEL}")'
                    )
                    resolved_any = True
                    continue

                if blocking_type == "ENDTURN_BLOCKING_WORLD_CONGRESS_SESSION":
                    # NEVER auto-resolve session blockers — the agent must
                    # call get_world_congress() and queue_wc_votes()
                    # to deploy diplomatic favor strategically.
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                # Catch-all for any other World Congress blocking types
                # (e.g. special session proposals, emergency discussions)
                # Replicates the game UI's "Pass" button: LOOKED_AT_AVAILABLE
                # + dismiss all WC-related blocking notifications.
                if "WORLD_CONGRESS" in blocking_type:
                    try:
                        wc_dismiss_lines = await gs.conn.execute(
                            f"local me = Game.GetLocalPlayer(); "
                            f"UI.RequestPlayerOperation(me, PlayerOperations.WORLD_CONGRESS_LOOKED_AT_AVAILABLE, {{}}); "
                            f"local dismissed = 0; "
                            f"local list = NotificationManager.GetList(me); "
                            f"if list then "
                            f"  for _, nid in ipairs(list) do "
                            f"    pcall(function() "
                            f"      local e = NotificationManager.Find(me, nid); "
                            f"      if e and not e:IsDismissed() then "
                            f"        local bt = e:GetEndTurnBlocking(); "
                            f"        if bt and bt ~= 0 then "
                            f"          for k, v in pairs(EndTurnBlockingTypes) do "
                            f'            if v == bt and k:find("WORLD_CONGRESS") then '
                            f"              NotificationManager.Dismiss(me, nid); "
                            f"              dismissed = dismissed + 1; "
                            f"              break "
                            f"            end "
                            f"          end "
                            f"        end "
                            f"      end "
                            f"    end) "
                            f"  end "
                            f"end; "
                            f'local i = ContextPtr:LookUpControl("/InGame/WorldCongressIntro"); '
                            f"if i then i:SetHide(true) end; "
                            f'local p = ContextPtr:LookUpControl("/InGame/WorldCongressPopup"); '
                            f"if p then p:SetHide(true) end; "
                            f'print("DISMISSED:" .. dismissed); print("{lq.SENTINEL}")'
                        )
                        if any(
                            "DISMISSED:" in l and not l.endswith(":0")
                            for l in wc_dismiss_lines
                        ):
                            resolved_any = True
                            log.info("Auto-dismissed WC blocker: %s", blocking_type)
                            continue
                    except Exception:
                        log.debug("WC catch-all auto-resolve failed", exc_info=True)
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                if blocking_type == "ENDTURN_BLOCKING_CONSIDER_DISLOYAL_CITY":
                    try:
                        result = await gs.resolve_city_capture("keep")
                        if "Error" not in result:
                            log.info("Auto-kept disloyal city: %s", result)
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug("Disloyal city auto-resolve failed", exc_info=True)
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                if blocking_type == "ENDTURN_BLOCKING_CONSIDER_RAZE_CITY":
                    try:
                        result = await gs.resolve_city_capture("keep")
                        if "Error" not in result:
                            log.info("Auto-kept captured city: %s", result)
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug("Captured city auto-resolve failed", exc_info=True)
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                if blocking_type == "ENDTURN_BLOCKING_GIVE_INFLUENCE_TOKEN":
                    try:
                        envoy_lines = await gs.conn.execute(
                            f"local me = Game.GetLocalPlayer(); "
                            f"local inf = Players[me]:GetInfluence(); "
                            f"local tokens = inf:GetTokensToGive(); "
                            f"if tokens == 0 then "
                            f"  inf:SetGivingTokensConsidered(true); "
                            f'  print("AUTO_RESOLVED"); '
                            f'else print("HAS_TOKENS|" .. tokens); end; '
                            f'print("{lq.SENTINEL}")'
                        )
                        if any("AUTO_RESOLVED" in l for l in envoy_lines):
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug("Envoy auto-resolve failed", exc_info=True)
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                if blocking_type == "ENDTURN_BLOCKING_PRODUCTION":
                    try:
                        corruption_lines = await gs.conn.execute(
                            f"local me = Game.GetLocalPlayer(); "
                            f"local corrupted = {{}}; "
                            f"for i, c in Players[me]:GetCities():Members() do "
                            f"  local bq = c:GetBuildQueue(); "
                            f"  if bq:GetSize() > 0 and bq:GetCurrentProductionTypeHash() == 0 then "
                            f'    table.insert(corrupted, Locale.Lookup(c:GetName()) .. " (id:" .. c:GetID() .. ")") '
                            f"  end "
                            f"end; "
                            f"if #corrupted > 0 then "
                            f'  print("CORRUPTED|" .. table.concat(corrupted, ",")) '
                            f'else print("CLEAN") end; '
                            f'print("{lq.SENTINEL}")'
                        )
                        is_corrupted = any(
                            cl.startswith("CORRUPTED|") for cl in corruption_lines
                        )
                        if is_corrupted:
                            city_names = next(
                                cl.split("|", 1)[1]
                                for cl in corruption_lines
                                if cl.startswith("CORRUPTED|")
                            )
                            dismiss_lines = await gs.conn.execute(
                                f"local me = Game.GetLocalPlayer(); "
                                f"local dismissed = 0; "
                                f"local list = NotificationManager.GetList(me); "
                                f"if list then "
                                f"  for _, nid in ipairs(list) do "
                                f"    local e = NotificationManager.Find(me, nid); "
                                f"    if e and not e:IsDismissed() then "
                                f"      local bt = e:GetEndTurnBlocking(); "
                                f"      if bt and bt == EndTurnBlockingTypes.ENDTURN_BLOCKING_PRODUCTION then "
                                f"        NotificationManager.Dismiss(me, nid); dismissed = dismissed + 1 "
                                f"      end "
                                f"    end "
                                f"  end "
                                f"end; "
                                f'print("DISMISSED|" .. dismissed); '
                                f'print("{lq.SENTINEL}")'
                            )
                            if any(
                                "DISMISSED|" in l and not l.endswith("|0")
                                for l in dismiss_lines
                            ):
                                log.info(
                                    "Auto-dismissed corrupted production for: %s",
                                    city_names,
                                )
                                resolved_any = True
                                continue
                    except Exception:
                        log.debug("Corruption check failed", exc_info=True)

                    # Empty-queue detection: RequestOperation can silently
                    # no-op, leaving cities with size==0 queues that block
                    # turn advancement. Name them in the blocker so the
                    # agent doesn't have to round-trip get_cities.
                    try:
                        empty_lines = await gs.conn.execute(
                            f"local me = Game.GetLocalPlayer(); "
                            f"local empty = {{}}; "
                            f"for i, c in Players[me]:GetCities():Members() do "
                            f"  local bq = c:GetBuildQueue(); "
                            f"  if bq:GetSize() == 0 then "
                            f'    table.insert(empty, Locale.Lookup(c:GetName()) .. " (id:" .. c:GetID() .. ")") '
                            f"  end "
                            f"end; "
                            f"if #empty > 0 then "
                            f'  print("EMPTY|" .. table.concat(empty, ", ")) '
                            f'else print("CLEAN") end; '
                            f'print("{lq.SENTINEL}")'
                        )
                        empty_cities = next(
                            (
                                el.split("|", 1)[1]
                                for el in empty_lines
                                if el.startswith("EMPTY|")
                            ),
                            None,
                        )
                        if empty_cities:
                            blocking_msg = (
                                f"Production — empty queue in {empty_cities}. "
                                f"Set production with set_city_production then "
                                f"retry end_turn."
                            )
                    except Exception:
                        log.debug("Empty-queue check failed", exc_info=True)

                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                # --- Stale research/civic notifications ---
                # If tech/civic is already set but the notification persists,
                # force-dismiss it (set_research may have been called but
                # the notification wasn't cleared — e.g. before MCP restart).
                if blocking_type in (
                    "ENDTURN_BLOCKING_RESEARCH",
                    "ENDTURN_BLOCKING_CIVIC",
                ):
                    try:
                        dismiss_lua = (
                            f"local me = Game.GetLocalPlayer() "
                            f"local pTechs = Players[me]:GetTechs() "
                            f"local pCulture = Players[me]:GetCulture() "
                            f"local researching = pTechs:GetResearchingTech() "
                            f"local civicing = pCulture:GetProgressingCivic() "
                            f"local isSet = false "
                            f'if "{blocking_type}" == "ENDTURN_BLOCKING_RESEARCH" and researching >= 0 then isSet = true end '
                            f'if "{blocking_type}" == "ENDTURN_BLOCKING_CIVIC" and civicing >= 0 then isSet = true end '
                            f"if isSet then "
                            f"  local list = NotificationManager.GetList(me) "
                            f"  if list then "
                            f"    for _, nid in ipairs(list) do "
                            f"      local e = NotificationManager.Find(me, nid) "
                            f"      if e and not e:IsDismissed() then "
                            f"        local bt = e:GetEndTurnBlocking() "
                            f"        if bt and bt == EndTurnBlockingTypes.{blocking_type} then "
                            f"          pcall(function() NotificationManager.SendActivated(me, nid) end) "
                            f"          pcall(function() NotificationManager.Dismiss(me, nid) end) "
                            f"        end "
                            f"      end "
                            f"    end "
                            f"  end "
                            f'  print("AUTO_CLEARED") '
                            f'else print("NOT_SET") end '
                            f'print("{lq.SENTINEL}")'
                        )
                        result_lines = await gs.conn.execute(dismiss_lua)
                        if any("AUTO_CLEARED" in l for l in result_lines):
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug(
                            "Research/civic notification auto-clear failed",
                            exc_info=True,
                        )
                    # Research/civic was unset — add diagnostic hint
                    kind = "tech" if "RESEARCH" in blocking_type else "civic"
                    enhanced_msg = (
                        (
                            f"{blocking_msg} (no {kind} selected — "
                            f"this can happen after diplomacy events or tech completion)"
                        )
                        if blocking_msg
                        else (
                            f"No {kind} selected — "
                            f"this can happen after diplomacy events or tech completion"
                        )
                    )
                    hard_blockers.append((blocking_type, enhanced_msg))
                    continue

                # --- Stale promotion notifications ---
                # GameCore SetPromotion doesn't consume XP or advance level,
                # so CanPromote() perpetually returns TRUE. Use XP-threshold
                # formula (matching promote_unit's post-promote dismiss) to
                # determine if any unit genuinely has enough XP for another
                # promotion: needed = T1 * (promoCount+1) * (promoCount+2) / 2
                if blocking_type == "ENDTURN_BLOCKING_UNIT_PROMOTION":
                    try:
                        # Step 1 (GameCore): Check XP formula AND zero out stored
                        # promotions on units that don't genuinely need one.
                        # ChangeStoredPromotions zeroes the engine counter that
                        # causes the blocker to regenerate after Dismiss().
                        check_lines = await gs.conn.execute(
                            f"local me = Game.GetLocalPlayer(); "
                            f"local anyNeed = false; "
                            f"local cleared = 0; "
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
                            f"            pcall(function() exp:ChangeStoredPromotions(-stored) end); "
                            f"            cleared = cleared + 1 "
                            f"          end "
                            f"        end "
                            f"      end "
                            f"    end "
                            f"  end "
                            f"end; "
                            f'print(anyNeed and "NEEDS_PROMO" or ("NO_PROMO_NEEDED|cleared=" .. cleared)); '
                            f'print("{lq.SENTINEL}")'
                        )
                        needs_promo = any(
                            "NEEDS_PROMO" == l.strip() for l in check_lines
                        )
                        log.debug(
                            "Promotion blocker: needs_promo=%s (check=%s)",
                            needs_promo,
                            [l for l in check_lines if "PROMO" in l or "cleared" in l],
                        )
                        if not needs_promo:
                            # Step 2: InGame dismiss — NotificationManager is InGame-only.
                            # Dismiss BOTH the end-turn blocker AND the regular notification
                            # (NOTIFICATION_UNIT_PROMOTION_AVAILABLE) which is a separate
                            # object that regenerates every turn due to stale CanPromote().
                            await gs.conn.execute(
                                f"local me = Game.GetLocalPlayer(); "
                                f"local list = NotificationManager.GetList(me); "
                                f"if list then "
                                f"  for _, nid in ipairs(list) do "
                                f"    local e = NotificationManager.Find(me, nid); "
                                f"    if e and not e:IsDismissed() then "
                                f"      local bt = e:GetEndTurnBlocking(); "
                                f"      if bt and bt == EndTurnBlockingTypes.ENDTURN_BLOCKING_UNIT_PROMOTION then "
                                f"        pcall(function() NotificationManager.SendActivated(me, nid) end); "
                                f"        pcall(function() NotificationManager.Dismiss(me, nid) end) "
                                f"      else "
                                f"        local tn = ''; "
                                f"        pcall(function() tn = e:GetTypeName() end); "
                                f"        if tn == 'NOTIFICATION_UNIT_PROMOTION_AVAILABLE' then "
                                f"          pcall(function() NotificationManager.Dismiss(me, nid) end) "
                                f"        end "
                                f"      end "
                                f"    end "
                                f"  end "
                                f"end; "
                                f'print("{lq.SENTINEL}")'
                            )
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug(
                            "Promotion notification auto-clear failed", exc_info=True
                        )
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                # --- Units blocking: auto-skip if all have 0 moves ---
                if blocking_type == "ENDTURN_BLOCKING_UNITS":
                    try:
                        check_lua = (
                            f"local me = Game.GetLocalPlayer(); "
                            f"local anyMoves = false; "
                            f"for _, u in Players[me]:GetUnits():Members() do "
                            f"  if u:GetX() ~= -9999 and u:GetMovesRemaining() > 0 then "
                            f"    anyMoves = true; break end end; "
                            f"if not anyMoves then "
                            f"  local op = GameInfo.UnitOperations['UNITOPERATION_SKIP_TURN']; "
                            f"  for _, u in Players[me]:GetUnits():Members() do "
                            f"    if u:GetX() ~= -9999 then "
                            f"      if UnitManager.FinishMoves then UnitManager.FinishMoves(u) "
                            f"      elseif op and UnitManager.CanStartOperation(u, op.Hash, nil, nil) then "
                            f"        UnitManager.RequestOperation(u, op.Hash) end "
                            f"    end "
                            f'  end; print("AUTO_SKIPPED") '
                            f'else print("UNITS_NEED_ORDERS") end; '
                            f'print("{lq.SENTINEL}")'
                        )
                        skip_lines = await gs.conn.execute(check_lua)
                        if any("AUTO_SKIPPED" in l for l in skip_lines):
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug("Auto-skip 0-move units failed", exc_info=True)
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                # --- Spy escape route: auto-pick fastest district ---
                if blocking_type == "ENDTURN_BLOCKING_SPY_CHOOSE_ESCAPE_ROUTE":
                    try:
                        escape_lines = await gs.conn.execute(
                            lq.build_spy_escape_route()
                        )
                        if any("OK:ESCAPE_ROUTE" in l for l in escape_lines):
                            log.info(
                                "Auto-resolved spy escape: %s",
                                next(
                                    (l for l in escape_lines if "OK:" in l),
                                    "",
                                ),
                            )
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug("Spy escape auto-resolve failed", exc_info=True)
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                # --- Unrecognized blocker → always hard ---
                hard_blockers.append((blocking_type, blocking_msg))

            # If we have hard blockers, check if turn advanced externally
            # (e.g. game auto-end-turn after skip_remaining_units)
            if hard_blockers:
                turn_now = await _get_turn_number(gs)
                if (
                    turn_now is not None
                    and turn_at_entry is not None
                    and turn_now > turn_at_entry
                ):
                    log.info(
                        "Turn advanced externally (%s -> %s), skipping blocker report",
                        turn_at_entry,
                        turn_now,
                    )
                    break  # fall through to snapshot/diff flow

                # Ask the game if turn can actually end despite our blockers.
                # Safe here — we haven't started AI processing yet (pre-end-turn phase).
                try:
                    can_end_lines = await gs.conn.execute(
                        f"local can = UI.CanEndTurn(); "
                        f'print(can and "CAN_END" or "CANNOT_END"); '
                        f'print("{lq.SENTINEL}")'
                    )
                    if any(l == "CAN_END" for l in can_end_lines):
                        log.info(
                            "UI.CanEndTurn()=true despite blockers %s — proceeding",
                            [bt for bt, _ in hard_blockers],
                        )
                        break  # fall through to end_turn request
                except Exception:
                    log.debug("UI.CanEndTurn check failed", exc_info=True)

                lines_out: list[str] = ["Cannot end turn — resolve these blockers:"]
                for bt, bm in hard_blockers:
                    hint = lq.BLOCKING_TOOL_MAP.get(
                        bt, "Resolve the blocking notification"
                    )
                    display = (
                        bt.replace("ENDTURN_BLOCKING_", "").replace("_", " ").title()
                    )
                    line = f"  - {display}"
                    if bm:
                        line += f" ({bm})"
                    line += f"  ->  {hint}"
                    lines_out.append(line)
                return "\n".join(lines_out)

            # All blockers were soft-resolved — loop to re-check
            if resolved_any:
                continue
            break  # no blockers left
        except Exception:
            log.debug("Blocking check failed, proceeding anyway", exc_info=True)
            break
    return None


async def _standin_preflight(gs: GameState, turn: int | None) -> str | None:
    """Everything a stand-in must settle before its turn may end.

    Returns the refusal text, or None when the turn is ready to end.
    """
    sessions = await gs.get_diplomacy_sessions()
    if sessions:
        names = ", ".join(
            f"{s.other_civ_name} ({s.other_leader_name})" for s in sessions
        )
        return (
            f"Cannot end turn: diplomacy encounter pending with {names}. "
            f"Use respond_to_diplomacy to handle it."
        )
    try:
        deals = await gs.get_pending_deals()
        if deals:
            return f"Cannot end turn: incoming trade deal pending.\n" + (
                nr.narrate_pending_deals(deals)
            )
    except Exception:
        log.debug("Pending deal check failed", exc_info=True)

    try:
        pre_dismiss = await gs.dismiss_popup()
        if "Dismissed" in pre_dismiss:
            log.info("Pre-end popup dismissed: %s", pre_dismiss)
    except Exception:
        log.debug("Pre-end dismiss failed", exc_info=True)

    blocker_msg = await _resolve_blockers(gs, turn)
    if blocker_msg is not None:
        return blocker_msg

    # Units with moves left are not a hard blocker for the engine (a human
    # could just press End Turn), but a stand-in must not leave a unit's
    # turn unplayed by accident. Report them instead of ending. "Idle" is
    # what the game itself calls ready to move: awake, with moves, not on
    # a route, not sleeping/sentried/fortified (IsReadyToMove; the older
    # moves-and-not-fortified heuristic is the fallback where it is nil).
    try:
        idle_lines = await gs.conn.execute(
            "local me = Game.GetLocalPlayer() "
            "local idle = {} "
            "for _, u in Players[me]:GetUnits():Members() do "
            "  if u:GetX() ~= -9999 and u:GetMovesRemaining() > 0 then "
            "    local ready = nil "
            "    pcall(function() ready = u:IsReadyToMove() end) "
            "    if ready == nil then ready = (u:GetFortifyTurns() == 0) end "
            "    if ready then "
            "      local e = GameInfo.Units[u:GetType()] "
            '      table.insert(idle, (e and e.UnitType or "UNIT") .. "@" .. u:GetX() .. "," .. u:GetY() .. " (idx:" .. u:GetID() .. ")") '
            "    end "
            "  end "
            "end "
            'if #idle > 0 then print("IDLE|" .. table.concat(idle, "; ")) else print("NONE") end '
            f'print("{lq.SENTINEL}")'
        )
        idle = next(
            (l.split("|", 1)[1] for l in idle_lines if l.startswith("IDLE|")), ""
        )
        if idle:
            return (
                f"Cannot end turn: these units still have moves — move, fortify, "
                f"sleep or skip each one first: {idle}"
            )
    except Exception:
        log.debug("Idle unit check failed", exc_info=True)
    return None




async def _interrupted_text(gs: GameState, st: hotseat.HotseatStatus) -> str:
    """Take the seat back after an interrupted end of turn and say what is new."""
    try:
        await hotseat.accept_turn(gs.conn)
    except Exception:
        log.debug("Re-accepting the turn after an interruption failed", exc_info=True)
    try:
        notes = nr.narrate_notifications(await gs.get_notifications())
    except Exception as e:  # noqa: BLE001 - the report must still come out
        notes = f"(notifications unavailable: {e})"
    return (
        f"Turn NOT ended — the game interrupted the end of turn and handed it back "
        f"to {st.leader_display}: something that needs this player's attention "
        "happened while automated units moved (a tribal village, a natural wonder, "
        "a first contact or an attack). The turn-change screen came up again and "
        "was dismissed; it is still this player's turn. Check the notifications "
        "below (a natural-wonder or first-meet popup needs ESC in the game), give "
        "any new orders, then call end_turn again; the diary entry is kept.\n\n"
        + notes
    )


@dataclass
class EndTurnResult:
    text: str
    ended: bool = False
    ended_turn: int = 0
    next_leader: str = ""
    next_name: str = ""
    save_name: str = ""


async def execute_hotseat_end_turn(gs: GameState, leader: str) -> EndTurnResult:
    """Really end the stand-in's turn, wait for the next player, save.

    Same preflight as any end turn (open diplomacy, incoming deals, hard
    blockers, units that still need orders), then ACTION_ENDTURN. Returns
    once the next human's turn-change screen is up and the game has been
    saved there as ``<LEADER> <turn> <year> MCP`` in the hotseat save folder.
    """
    conn = gs.conn
    st = await hotseat.ingame_status(conn)
    if st.screen != "ingame" or not st.is_hotseat:
        return EndTurnResult("Cannot end turn: the game is not in a hotseat session.")
    if st.leader != leader:
        return EndTurnResult(
            f"Cannot end turn: the game is on {st.leader}'s turn, not {leader}'s. "
            "Nothing was done."
        )
    if not st.turn_active:
        return EndTurnResult(
            f"Cannot end turn: {leader}'s turn is not active (already ended?)."
        )

    refusal = await _standin_preflight(gs, st.turn)
    if refusal is not None:
        return EndTurnResult(refusal)

    try:
        nxt = await hotseat.end_turn_and_wait(conn, st.local_player, st.turn)
    except hotseat.TurnInterrupted:
        return EndTurnResult(await _interrupted_text(gs, st))
    except RuntimeError as e:
        return EndTurnResult(f"Turn NOT ended — {e}")
    save_name = await hotseat.suggest_save_name(conn)
    try:
        await hotseat.save_hotseat(conn, save_name)
    except RuntimeError as e:
        save_name = ""
        save_note = (
            f" The save FAILED ({e}): save it by hand with the Save Game button on "
            f"{nxt.leader_display}'s turn-change screen."
        )
    else:
        save_note = f" It was saved as '{save_name}' in the hotseat save folder."
    turn_note = (
        f"turn {nxt.turn} begins" if nxt.turn != st.turn else f"still turn {nxt.turn}"
    )
    text = (
        f"TURN {st.turn} ENDED for {st.leader_display}. The game is now on the "
        f"turn-change screen of {nxt.leader_display} ('{nxt.player_name}', {turn_note})."
        + save_note
        + " Stand-in finished: every game tool is locked until begin_standin_turn is "
        "called again. Do not call end_turn again."
    )
    return EndTurnResult(
        text,
        ended=True,
        ended_turn=st.turn,
        next_leader=nxt.leader,
        next_name=nxt.player_name,
        save_name=save_name,
    )
