"""MCP server that plays an absent player's turn in a local Civ 6 hotseat game.

The game is driven through the MCP Bridge mod (``bridge.py``). The hotseat
tools (``hotseat_status``, ``load_hotseat_save``, ``begin_standin_turn``,
``end_turn``) move the game between the turn-change screens; every other
tool reads or acts for the one leader the stand-in marker names, and is
refused otherwise (``standin.py``).
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from mcp.server.fastmcp import Context, FastMCP

from civ_mcp import bbg as _bbg
from civ_mcp import diary as _diary
from civ_mcp import hotseat as _hotseat
from civ_mcp import narrate as nr
from civ_mcp import standin as _standin
from civ_mcp.bridge import BridgeConnection, LuaError
from civ_mcp.end_turn import execute_hotseat_end_turn
from civ_mcp.game_state import GameState

log = logging.getLogger(__name__)


@dataclass
class Session:
    conn: BridgeConnection
    game: GameState
    guard: _standin.StandinGuard


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[Session]:
    conn = BridgeConnection()
    cfg = _standin.active_config()
    if cfg is not None:
        log.info("Stand-in marker active for %s", cfg.leader)
    try:
        yield Session(conn=conn, game=GameState(conn), guard=_standin.StandinGuard())
    finally:
        await conn.disconnect()


mcp = FastMCP(
    "Civilization VI hotseat stand-in",
    instructions=(
        "Plays one absent player's turn in a local Civ 6 hotseat game. Start with "
        "hotseat_status, then begin_standin_turn(leader) for the player named on the "
        "turn-change screen; its reply is the turn briefing (diary, overview, cities, units, world map). Finish with end_turn "
        "exactly once."
    ),
    lifespan=lifespan,
)


def _session(ctx: Context) -> Session:
    return ctx.request_context.lifespan_context


def _game(ctx: Context) -> GameState:
    return _session(ctx).game


# Tools that run without the stand-in guard: they are how the operator gets
# the right save loaded and the right player armed.
_UNGUARDED = frozenset(
    {"hotseat_status", "list_saves", "load_hotseat_save", "begin_standin_turn"}
)


def _summary(result: str) -> str:
    line = result.split("\n", 1)[0].strip()
    return line[:120] + "..." if len(line) > 120 else line


async def _logged(
    ctx: Context, tool_name: str, params: dict[str, Any], fn: Callable[[], Awaitable[str]]
) -> str:
    """Run a tool behind the stand-in guard with timing and error handling."""
    s = _session(ctx)
    if tool_name not in _UNGUARDED:
        try:
            refusal = await s.guard.check(s.conn, _standin.active_config())
        except ConnectionError as e:
            refusal = str(e)
        except Exception as e:  # noqa: BLE001 — refuse rather than act blind
            refusal = f"Stand-in guard: could not verify the local player ({e})."
        if refusal:
            log.warning("%s refused: %s", tool_name, refusal)
            return refusal
    start = time.monotonic()
    try:
        result = await fn()
    except (LuaError, ValueError) as e:
        result = f"Error: {e}"
    except ConnectionError as e:
        result = str(e)
    ms = int((time.monotonic() - start) * 1000)
    log.info("%s(%s) %dms: %s", tool_name, params or "", ms, _summary(result))
    return result


async def _narrate(query_fn: Callable[[], Awaitable[Any]], narrate_fn: Callable[..., str]) -> str:
    return narrate_fn(await query_fn())


# ---------------------------------------------------------------------------
# Hotseat stand-in
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def hotseat_status(ctx: Context) -> str:
    """Where the game is and whose turn it is, plus the stand-in marker state.

    Safe to call anywhere: main menu, staging room, in a game. Reports the
    local player, whether the turn-change screen is showing, and whether a
    stand-in is armed. Call it first, and whenever a tool is refused by the
    stand-in guard.
    """
    st = await _hotseat.status(_session(ctx).conn)
    text = st.describe()
    cfg = _standin.active_config()
    if cfg is not None:
        text += "\n\n" + _standin.status_text(cfg)
    elif st.screen == "ingame" and st.is_hotseat:
        text += (
            "\n\nNo stand-in is armed: game tools are locked. Call "
            "begin_standin_turn(leader) for the player whose turn this is."
        )
    return text


@mcp.tool(annotations={"readOnlyHint": True})
async def list_saves(ctx: Context) -> str:
    """List the saves in the game's hotseat save folder, newest first.

    Names are given without extension; pass one to load_hotseat_save.
    Autosaves and quicksaves are tagged.
    """
    return _hotseat.list_hotseat_saves()


@mcp.tool(annotations={"destructiveHint": True})
async def load_hotseat_save(ctx: Context, save_name: str) -> str:
    """Load a hotseat save and start it, ending on its turn-change screen.

    Works from the main menu or from inside another game (that game is left
    WITHOUT saving). Goes Multiplayer > Hotseat > Load Game > Start Game by
    itself and returns when the "<name>: Save Game / Start Turn" screen of
    the player whose turn it is comes up (about half a minute for a big
    save). Then call begin_standin_turn for that player.

    Args:
        save_name: Name in the hotseat save folder, without extension,
                   e.g. "POUNDMAKER 63 150 AD" (see list_saves).
    """
    s = _session(ctx)
    try:
        result = await _hotseat.load_hotseat_save(s.conn, save_name)
    except (ConnectionError, RuntimeError) as e:
        return f"Error: {e}"
    s.guard.invalidate()
    cfg = _standin.load_config()
    if cfg is not None:
        cfg.hotseat_save = save_name
        _standin.save_config(cfg)
    return (
        result + "\nNext: begin_standin_turn(leader) for the absent player named on "
        "the turn-change screen."
    )


@mcp.tool()
async def begin_standin_turn(ctx: Context, leader: str, note: str = "") -> str:
    """Take the seat of one absent hotseat player for this turn.

    Checks that the game is in a hotseat session and that it is this
    leader's turn, presses Start Turn for them (dismisses the turn-change
    screen and unpauses), and arms stand-in mode: from now on every game
    tool acts for this player only, and end_turn ends the turn for real,
    hands the game to the next human's turn-change screen and saves there.

    Args:
        leader: The absent player's leader, e.g. "Poundmaker" or
                "LEADER_POUNDMAKER" (as shown by hotseat_status).
        note: What the absent player asked for this turn, in their words.

    The reply ends with the turn briefing: everything this player can
    see (diary, overview, notifications, pending deals, cities, units and
    the world map). get_turn_briefing refreshes it mid-turn.
    """
    s = _session(ctx)
    lid = _hotseat.normalize_leader(leader)
    st = await _hotseat.status(s.conn)
    if st.screen != "ingame":
        return f"Cannot begin: {st.describe()} Use load_hotseat_save first."
    if not st.is_hotseat:
        return "Cannot begin: this is not a hotseat session. Use load_hotseat_save."
    if st.leader != lid:
        return (
            f"Cannot begin: the game is on {st.leader}'s turn ('{st.player_name}'), "
            f"not {lid}'s. If that player is the absent one, call begin_standin_turn "
            "with their leader; otherwise the humans play up to the absent "
            "player's turn first."
        )
    if not st.turn_active:
        return f"Cannot begin: {lid}'s turn is not active (already ended?)."
    pre_save = ""
    if st.player_change_visible:
        # Rollback point: saved on the turn-change screen, before Start Turn.
        try:
            pre_save = await _hotseat.save_hotseat(
                s.conn, f"{st.leader_display.upper()} {st.turn} PRE MCP"
            )
        except Exception as e:  # noqa: BLE001 - a failed save must not block the turn
            log.warning("pre-turn save failed: %s", e)
            pre_save = f"(pre-turn save failed: {e})"
        st = await _hotseat.accept_turn(s.conn)

    cfg = _standin.load_config() or _standin.StandinConfig()
    cfg.pre_turn_save = pre_save
    cfg.active = True
    cfg.leader = lid
    cfg.player_name = st.player_name
    cfg.note = note or ""
    cfg.started_turn = st.turn
    cfg.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cfg.finished_turn = None
    cfg.finished_at = ""
    cfg.finished_save = ""
    cfg.next_player = ""
    _standin.save_config(cfg)
    s.guard.invalidate()
    head = (
        f"Stand-in armed for {cfg.leader_display} ('{st.player_name}'), turn "
        f"{st.turn}. The turn-change screen was dismissed and the game is "
        "unpaused; it is this player's turn.\n"
        + _standin.status_text(cfg)
    )
    try:
        briefing = await _turn_briefing(s)
    except Exception as e:  # noqa: BLE001 - the seat is taken; report, don't fail
        briefing = (
            f"(turn briefing unavailable: {e}; call get_turn_briefing or the "
            "individual query tools)"
        )
    return head + "\n\n" + briefing


async def _units_text(gs: GameState) -> str:
    units = await gs.get_units()
    try:
        threats = await gs.get_threat_scan()
    except Exception:
        threats = None
    try:
        trade_status = await gs.get_trade_routes()
    except Exception:
        trade_status = None
    return nr.narrate_units(units, threats, trade_status)


async def _cities_text(gs: GameState) -> str:
    cities, distances = await gs.get_cities()
    return nr.narrate_cities(cities, distances)


async def _turn_briefing(s: Session) -> str:
    """Everything the player can see this turn, in one reply."""
    gs = s.game
    parts: list[str] = []

    async def section(title: str, fn: Callable[[], Awaitable[str]]) -> None:
        try:
            text = await fn()
        except Exception as e:  # noqa: BLE001 - one failed section must not hide the rest
            text = f"(unavailable: {e})"
        parts.append(f"=== {title} ===\n{text}")

    async def diary_text() -> str:
        civ, seed = await gs.get_game_identity()
        entries = _diary.read_entries(_diary.diary_path(civ, seed))
        if not entries:
            return "No diary entries yet for this player."
        more = f" ({len(entries) - 1} earlier: get_diary)" if len(entries) > 1 else ""
        return _diary.format_entry(entries[-1]) + more

    async def overview_text() -> str:
        text = nr.narrate_overview(await gs.get_game_overview())
        cfg = _standin.active_config()
        if cfg is not None:
            text += "\n\n" + _standin.status_text(cfg)
        return text

    async def kit_text() -> str:
        cfg = _standin.active_config()
        leader = cfg.leader if cfg is not None else ""
        kit = _bbg.kit_for(leader) if leader else None
        if kit is not None:
            return kit
        return (
            f"No bundled BBG entry for {leader or 'this leader'}. Rely on the game "
            "tools, whose numbers already include BBG."
        )

    await section("Overview", overview_text)
    await section("Civilization kit (BBG)", kit_text)
    await section("Diary (last entry)", diary_text)
    await section(
        "Notifications", lambda: _narrate(gs.get_notifications, nr.narrate_notifications)
    )
    await section(
        "Pending trades", lambda: _narrate(gs.get_pending_deals, nr.narrate_pending_deals)
    )
    await section(
        "Pending diplomacy",
        lambda: _narrate(gs.get_diplomacy_sessions, nr.narrate_diplomacy_sessions),
    )
    await section("Map pins", lambda: _narrate(gs.get_map_pins, nr.narrate_map_pins))
    await section("Cities", lambda: _cities_text(gs))
    await section("Units", lambda: _units_text(gs))
    await section(
        "World", lambda: _narrate(gs.get_world_snapshot, nr.narrate_world_snapshot)
    )
    return "\n\n".join(parts)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_turn_briefing(ctx: Context) -> str:
    """Everything this player can see, in one reply.

    Overview, last diary entry, notifications, pending trades and
    diplomacy, map pins, cities, units with nearby threats, and the world snapshot:
    an ASCII hex map of every revealed tile plus every resource,
    improvement, district, wonder, unit and city on them. begin_standin_turn
    returns the same briefing; call this to refresh it after moving.
    """
    s = _session(ctx)
    return await _logged(ctx, "get_turn_briefing", {}, lambda: _turn_briefing(s))


@mcp.tool(annotations={"destructiveHint": True})
async def end_turn(
    ctx: Context,
    tactical: str = "",
    strategic: str = "",
    tooling: str = "",
    planning: str = "",
    hypothesis: str = "",
) -> str:
    """End the stand-in's turn for real and hand the game to the next player.

    Refuses while a diplomacy encounter or incoming deal is open, while an
    end-turn blocker remains (production, research, promotion, ...) or
    while any unit still needs orders. Otherwise it ends the turn, waits
    for the next human's turn-change screen, saves there and locks every
    game tool until the next begin_standin_turn. Call it exactly once.

    All 5 reflection parameters are required and must be non-empty. They
    form this player's diary, the memory for the next stand-in:
        tactical: What happened this turn — combat, movements, improvements.
        strategic: Current standing vs rivals — yields, city count, plans.
        tooling: Tool issues or observations. Write "No issues" if none.
        planning: Concrete actions for the next few turns.
        hypothesis: Predictions — neighbours' behaviour, risks, timelines.

    If end_turn is refused and you call it again after resolving the
    blocker, the diary entry from the first call is kept and extended — do
    not repeat reflections.
    """
    s = _session(ctx)
    gs = s.game
    cfg = _standin.active_config()
    if cfg is None:
        return await _logged(ctx, "end_turn", {}, _refused)
    if cfg.finished_turn is not None:
        return (
            f"This stand-in turn was already ended (turn {cfg.finished_turn}, saved "
            f"as '{cfg.finished_save}'). Nothing to do."
        )

    reflections = {
        "tactical": tactical,
        "strategic": strategic,
        "tooling": tooling,
        "planning": planning,
        "hypothesis": hypothesis,
    }
    missing = [k for k, v in reflections.items() if not v.strip()]
    if missing:
        return (
            f"Empty reflections: {', '.join(missing)}. Provide non-empty entries "
            "for all 5 fields: tactical, strategic, tooling, planning, hypothesis."
        )

    # Write the diary row BEFORE ending the turn, so it exists even if the
    # session is interrupted while the engine processes the turn.
    try:
        await _write_diary(s, cfg, reflections)
    except Exception:
        log.warning("Diary: failed to write entry", exc_info=True)

    outcome = None

    async def _run() -> str:
        nonlocal outcome
        outcome = await execute_hotseat_end_turn(gs, cfg.leader)
        return outcome.text

    result = await _logged(ctx, "end_turn", {}, _run)
    if outcome is None or not outcome.ended:
        return result
    s.guard.invalidate()
    cfg.finished_turn = outcome.ended_turn
    cfg.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cfg.finished_save = outcome.save_name
    cfg.next_player = (
        f"{outcome.next_leader.replace('LEADER_', '').replace('_', ' ').title()} "
        f"('{outcome.next_name}')"
    )
    _standin.save_config(cfg)
    return result


async def _refused() -> str:  # the guard answers before this runs
    return "No stand-in is armed."


async def _write_diary(s: Session, cfg: _standin.StandinConfig, reflections: dict) -> None:
    gs = s.game
    ov = await gs.get_game_overview()
    civ, seed = await gs.get_game_identity()
    path = _diary.diary_path(civ, seed)
    # A player's game turn happens once per stand-in, so a row for this turn
    # means end_turn was refused earlier: extend it instead of adding another.
    if _diary.merge_reflections(path, ov.turn, reflections) is not None:
        log.info("Diary: merged reflections into the existing row for turn %s", ov.turn)
        return
    snap = await gs.get_diary_snapshot()
    me = next((p for p in snap.players if p.pid == ov.player_id), None)
    row: dict[str, Any] = asdict(me) if me is not None else {"pid": ov.player_id}
    row["is_agent"] = True
    row["turn"] = ov.turn
    row["game"] = f"{civ}_{seed}"
    row["timestamp"] = datetime.now(timezone.utc).isoformat()
    row["player_name"] = cfg.player_name
    row["note"] = cfg.note
    ag = snap.agent
    row["diplo_states"] = ag.diplo_states
    row["suzerainties"] = ag.suzerainties
    row["envoys_available"] = ag.envoys_available
    row["envoys_sent"] = ag.envoys_sent
    row["gp_points"] = ag.gp_points
    row["governors"] = ag.governors
    row["trade_routes"] = {
        "capacity": ag.trade_capacity,
        "active": ag.trade_active,
        "domestic": ag.trade_domestic,
        "international": ag.trade_international,
    }
    row["reflections"] = reflections
    _diary.append_entry(path, row)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_diary(
    ctx: Context,
    last_n: int = 5,
    turn: Optional[int] = None,
    from_turn: Optional[int] = None,
    to_turn: Optional[int] = None,
) -> str:
    """Read this player's diary: what earlier stand-ins did and planned.

    Args:
        last_n: Number of most recent entries to return (default 5, max 50).
                Used when turn/from_turn/to_turn are not specified.
        turn: Return the single entry for this turn number.
        from_turn: Return entries from this turn onward (inclusive).
        to_turn: Return entries up to this turn (inclusive).

    Each player of the hotseat game has their own diary (keyed by civ and
    game seed). Call this right after begin_standin_turn.
    """
    gs = _game(ctx)

    async def _run() -> str:
        civ, seed = await gs.get_game_identity()
        entries = _diary.read_entries(_diary.diary_path(civ, seed))
        if not entries:
            return f"No diary entries yet for this player ({civ}, seed {seed})."
        if turn is not None:
            entries = [e for e in entries if e.get("turn") == turn]
        elif from_turn is not None or to_turn is not None:
            lo = from_turn if from_turn is not None else 0
            hi = to_turn if to_turn is not None else 999999
            entries = [e for e in entries if lo <= e.get("turn", 0) <= hi]
        else:
            entries = entries[-min(max(last_n, 1), 50) :]
        if not entries:
            return "No diary entries match the query."
        return "\n\n".join(_diary.format_entry(e) for e in entries)

    return await _logged(ctx, "get_diary", {}, _run)


# ---------------------------------------------------------------------------
# Query tools (read-only)
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_game_overview(ctx: Context) -> str:
    """Get a high-level summary of the current game state.

    Returns turn number, civilization, yields (gold/science/culture/faith),
    current research and civic, counts of cities and units, and the
    stand-in status paragraph (whose turn you are playing, their note).
    """
    gs = _game(ctx)

    async def _run() -> str:
        text = nr.narrate_overview(await gs.get_game_overview())
        cfg = _standin.active_config()
        if cfg is not None:
            text += "\n\n" + _standin.status_text(cfg)
        return text

    return await _logged(ctx, "get_game_overview", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_units(ctx: Context) -> str:
    """List all your units with position, type, movement, and health.

    Each unit shows its id and idx (needed for action commands), nearby
    threats, and trader status.
    """
    gs = _game(ctx)
    return await _logged(ctx, "get_units", {}, lambda: _units_text(gs))


@mcp.tool(annotations={"readOnlyHint": True})
async def get_spies(ctx: Context) -> str:
    """List all your spy units with position, rank, city, and available missions.

    Offensive missions only become available once the spy has physically
    arrived in the target city. Use spy_action with action='travel' first.
    """
    gs = _game(ctx)
    return await _logged(ctx, "get_spies", {}, lambda: _narrate(gs.get_spies, nr.narrate_spies))


@mcp.tool()
async def spy_action(ctx: Context, unit_id: int, action: str, target_x: int, target_y: int) -> str:
    """Send a spy to a city or launch a spy mission.

    Args:
        unit_id: The spy's composite ID (from get_spies output)
        action: 'travel' to move spy to a city, or a mission type to launch a mission.
            Mission types: COUNTERSPY, GAIN_SOURCES, SIPHON_FUNDS, STEAL_TECH_BOOST,
            SABOTAGE_PRODUCTION, GREAT_WORK_HEIST, RECRUIT_PARTISANS,
            NEUTRALIZE_GOVERNOR, FABRICATE_SCANDAL
        target_x: X coordinate of the target city tile
        target_y: Y coordinate of the target city tile

    Travel targets are your own cities and city-states; travel is queued
    end-of-turn. The spy must be IN the target city to launch a mission.
    """
    gs = _game(ctx)
    unit_index = unit_id % 65536
    params = {"unit_id": unit_id, "action": action, "target_x": target_x, "target_y": target_y}

    async def _run() -> str:
        if action.lower() == "travel":
            return await gs.spy_travel(unit_index, target_x, target_y)
        return await gs.spy_mission(unit_index, action.upper(), target_x, target_y)

    return await _logged(ctx, "spy_action", params, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_cities(ctx: Context) -> str:
    """List all your cities with yields, population, production, growth, and loyalty.

    Each city shows its id (needed for production commands), unimproved
    resource tiles and pillaged improvements. Cities losing loyalty show
    warnings with flip timers.
    """
    gs = _game(ctx)
    return await _logged(ctx, "get_cities", {}, lambda: _cities_text(gs))


@mcp.tool(annotations={"readOnlyHint": True})
async def get_city_production(ctx: Context, city_id: int) -> str:
    """List what a city can produce or purchase right now, with costs.

    Args:
        city_id: City ID (from get_cities output)
    """
    gs = _game(ctx)

    async def _run() -> str:
        return nr.narrate_city_production(await gs.list_city_production(city_id))

    return await _logged(ctx, "get_city_production", {"city_id": city_id}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_map_area(ctx: Context, center_x: int, center_y: int, radius: int = 2) -> str:
    """Get terrain info for tiles around a point.

    Args:
        center_x: X coordinate of center tile
        center_y: Y coordinate of center tile
        radius: How many tiles out from center (default 2, max 4)
    """
    radius = min(radius, 4)
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_map_area",
        {"center_x": center_x, "center_y": center_y, "radius": radius},
        lambda: _narrate(lambda: gs.get_map_area(center_x, center_y, radius), nr.narrate_map),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_settle_advisor(ctx: Context, unit_id: int) -> str:
    """List best settle locations near a settler unit (top 5 by score).

    Args:
        unit_id: The settler's composite ID (from get_units output)
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_settle_advisor",
        {"unit_id": unit_id},
        lambda: gs.get_settle_advisor(unit_id % 65536),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pathing_estimate(ctx: Context, unit_id: int, target_x: int, target_y: int) -> str:
    """Estimate how many turns a unit needs to reach a destination.

    Args:
        unit_id: The unit's composite ID (from get_units output)
        target_x: Destination X coordinate
        target_y: Destination Y coordinate
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_pathing_estimate",
        {"unit_id": unit_id, "target_x": target_x, "target_y": target_y},
        lambda: _narrate(
            lambda: gs.get_pathing_estimate(unit_id % 65536, target_x, target_y),
            nr.narrate_pathing_estimate,
        ),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_global_settle_advisor(ctx: Context) -> str:
    """Find the best settle locations across the entire revealed map (top 10)."""
    gs = _game(ctx)

    async def _run() -> str:
        candidates = await gs.get_global_settle_scan()
        if not candidates:
            return "No valid settle locations found on revealed map."
        return nr.narrate_settle_candidates(candidates)

    return await _logged(ctx, "get_global_settle_advisor", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_builder_tasks(ctx: Context) -> str:
    """Get a prioritized task board for all your builders.

    Scans your territory for tiles needing improvements and matches them
    with idle builders (URGENT > HIGH > NORMAL). Call this before issuing
    builder orders.
    """
    gs = _game(ctx)

    async def _run() -> str:
        tasks, builders = await gs.get_builder_tasks()
        return nr.narrate_builder_tasks(tasks, builders)

    return await _logged(ctx, "get_builder_tasks", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_empire_resources(ctx: Context) -> str:
    """Summarize stockpiles, owned resources (improved/unimproved) and unclaimed resources nearby."""
    gs = _game(ctx)

    async def _run() -> str:
        return nr.narrate_empire_resources(*await gs.get_empire_resources())

    return await _logged(ctx, "get_empire_resources", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_strategic_map(ctx: Context) -> str:
    """Fog-of-war boundaries per city and unclaimed resources across the revealed map."""
    gs = _game(ctx)
    return await _logged(
        ctx, "get_strategic_map", {}, lambda: _narrate(gs.get_strategic_map, nr.narrate_strategic_map)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_diplomacy(ctx: Context) -> str:
    """Diplomatic status with all met civilizations.

    Shows diplomatic state, relationship modifiers, grievances, delegations
    and embassies, available diplomatic actions, military strength, and
    visible foreign cities. The other civilizations are human players:
    leader agendas and AI relationship modifiers mean nothing here.
    """
    gs = _game(ctx)
    return await _logged(
        ctx, "get_diplomacy", {}, lambda: _narrate(gs.get_diplomacy, nr.narrate_diplomacy)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_tech_civics(ctx: Context) -> str:
    """Current research and civic with turns remaining, and the available choices."""
    gs = _game(ctx)
    return await _logged(
        ctx, "get_tech_civics", {}, lambda: _narrate(gs.get_tech_civics, nr.narrate_tech_civics)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pending_trades(ctx: Context) -> str:
    """Incoming trade deal offers from other players, with what they offer and want.

    Use respond_to_trade to accept or reject; end_turn refuses while one is open.
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_pending_trades",
        {},
        lambda: _narrate(gs.get_pending_deals, nr.narrate_pending_deals),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_policies(ctx: Context) -> str:
    """Current government, policy slots, and unlocked policies grouped by slot type."""
    gs = _game(ctx)
    return await _logged(
        ctx, "get_policies", {}, lambda: _narrate(gs.get_policies, nr.narrate_policies)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_notifications(ctx: Context) -> str:
    """All active game notifications; action-required items name the tool that resolves them."""
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_notifications",
        {},
        lambda: _narrate(gs.get_notifications, nr.narrate_notifications),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pending_diplomacy(ctx: Context) -> str:
    """Open diplomacy encounters (e.g. a first meeting), with dialogue and buttons.

    These block end_turn; answer with respond_to_diplomacy.
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_pending_diplomacy",
        {},
        lambda: _narrate(gs.get_diplomacy_sessions, nr.narrate_diplomacy_sessions),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_governors(ctx: Context) -> str:
    """Governor points, appointed governors with assignments and promotions, and types to appoint."""
    gs = _game(ctx)
    return await _logged(
        ctx, "get_governors", {}, lambda: _narrate(gs.get_governors, nr.narrate_governors)
    )


@mcp.tool()
async def appoint_governor(ctx: Context, governor_type: str) -> str:
    """Appoint a new governor (requires a governor point).

    Args:
        governor_type: e.g. GOVERNOR_THE_EDUCATOR (Pingala), GOVERNOR_THE_DEFENDER (Victor)
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "appoint_governor",
        {"governor_type": governor_type},
        lambda: gs.appoint_governor(governor_type),
    )


@mcp.tool()
async def assign_governor(ctx: Context, governor_type: str, city_id: int) -> str:
    """Assign an appointed governor to a city (takes several turns to establish).

    Args:
        governor_type: The governor type (from get_governors output)
        city_id: The city ID (from get_cities output)
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "assign_governor",
        {"governor_type": governor_type, "city_id": city_id},
        lambda: gs.assign_governor(governor_type, city_id),
    )


@mcp.tool()
async def promote_governor(ctx: Context, governor_type: str, promotion_type: str) -> str:
    """Promote a governor with a new ability (requires a governor point).

    Args:
        governor_type: The governor type (from get_governors output)
        promotion_type: The promotion type (from get_governors output)
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "promote_governor",
        {"governor_type": governor_type, "promotion_type": promotion_type},
        lambda: gs.promote_governor(governor_type, promotion_type),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_unit_promotions(ctx: Context, unit_id: int) -> str:
    """List available promotions for a unit (only units with enough XP have any).

    Args:
        unit_id: The unit's composite ID (from get_units output)
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_unit_promotions",
        {"unit_id": unit_id},
        lambda: _narrate(lambda: gs.get_unit_promotions(unit_id), nr.narrate_unit_promotions),
    )


@mcp.tool()
async def promote_unit(ctx: Context, unit_id: int, promotion_type: str) -> str:
    """Apply a promotion to a unit. Use get_unit_promotions first to see the options.

    Args:
        unit_id: The unit's composite ID (from get_units output)
        promotion_type: e.g. PROMOTION_BATTLECRY, PROMOTION_TORTOISE
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "promote_unit",
        {"unit_id": unit_id, "promotion_type": promotion_type},
        lambda: gs.promote_unit(unit_id, promotion_type),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_city_states(ctx: Context) -> str:
    """Known city-states with their type, your envoys, the suzerain, and envoy tokens available."""
    gs = _game(ctx)
    return await _logged(
        ctx, "get_city_states", {}, lambda: _narrate(gs.get_city_states, nr.narrate_city_states)
    )


@mcp.tool()
async def send_envoy(ctx: Context, player_id: int) -> str:
    """Send an envoy to a city-state (requires an envoy token).

    Args:
        player_id: The city-state's player ID (from get_city_states)
    """
    gs = _game(ctx)
    return await _logged(
        ctx, "send_envoy", {"player_id": player_id}, lambda: gs.send_envoy(player_id)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pantheon_beliefs(ctx: Context) -> str:
    """Current pantheon (if any), faith balance, and available pantheon beliefs."""
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_pantheon_beliefs",
        {},
        lambda: _narrate(gs.get_pantheon_status, nr.narrate_pantheon_status),
    )


@mcp.tool()
async def choose_pantheon(ctx: Context, belief_type: str) -> str:
    """Found a pantheon with the specified belief (requires enough faith, no pantheon yet).

    Args:
        belief_type: e.g. BELIEF_GOD_OF_THE_FORGE, BELIEF_DIVINE_SPARK
    """
    gs = _game(ctx)
    return await _logged(
        ctx, "choose_pantheon", {"belief_type": belief_type}, lambda: gs.choose_pantheon(belief_type)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_religion_beliefs(ctx: Context) -> str:
    """Religion founding status, available religion types, and beliefs grouped by class."""
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_religion_beliefs",
        {},
        lambda: _narrate(
            gs.get_religion_founding_status, nr.narrate_religion_founding_status
        ),
    )


@mcp.tool()
async def found_religion(
    ctx: Context, religion_type: str, follower_belief: str, founder_belief: str
) -> str:
    """Found a religion after your Great Prophet has activated on a Holy Site.

    Args:
        religion_type: e.g. RELIGION_HINDUISM, RELIGION_BUDDHISM, RELIGION_ISLAM
        follower_belief: e.g. BELIEF_WORK_ETHIC, BELIEF_CHORAL_MUSIC
        founder_belief: e.g. BELIEF_STEWARDSHIP, BELIEF_CHURCH_PROPERTY
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "found_religion",
        {
            "religion_type": religion_type,
            "follower_belief": follower_belief,
            "founder_belief": founder_belief,
        },
        lambda: gs.found_religion(religion_type, follower_belief, founder_belief),
    )


@mcp.tool()
async def upgrade_unit(ctx: Context, unit_id: int) -> str:
    """Upgrade a unit to its next type (needs the tech, the gold, and moves left).

    Args:
        unit_id: The unit's composite ID (from get_units output)
    """
    gs = _game(ctx)
    return await _logged(
        ctx, "upgrade_unit", {"unit_id": unit_id}, lambda: gs.upgrade_unit(unit_id)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_dedications(ctx: Context) -> str:
    """Era score thresholds, current age, and available dedications."""
    gs = _game(ctx)
    return await _logged(
        ctx, "get_dedications", {}, lambda: _narrate(gs.get_dedications, nr.narrate_dedications)
    )


@mcp.tool()
async def choose_dedication(ctx: Context, dedication_index: int) -> str:
    """Choose a dedication for the current era (index from get_dedications)."""
    gs = _game(ctx)
    return await _logged(
        ctx,
        "choose_dedication",
        {"dedication_index": dedication_index},
        lambda: gs.choose_dedication(dedication_index),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_trade_options(ctx: Context, other_player_id: int) -> str:
    """What both sides can trade — like opening the trade screen with that player.

    Args:
        other_player_id: The player ID (from get_diplomacy output)
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_trade_options",
        {"other_player_id": other_player_id},
        lambda: _narrate(lambda: gs.get_deal_options(other_player_id), nr.narrate_deal_options),
    )


@mcp.tool()
async def respond_to_trade(ctx: Context, other_player_id: int, accept: bool) -> str:
    """Accept or reject a pending trade deal (see get_pending_trades).

    Args:
        other_player_id: The player ID of the civilization (from get_pending_trades)
        accept: True to accept the deal, False to reject it
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "respond_to_trade",
        {"other_player_id": other_player_id, "accept": accept},
        lambda: gs.respond_to_deal(other_player_id, accept),
    )


@mcp.tool()
async def propose_trade(
    ctx: Context,
    other_player_id: int,
    offer_gold: int = 0,
    offer_gold_per_turn: int = 0,
    offer_resources: str = "",
    offer_favor: int = 0,
    offer_open_borders: bool = False,
    request_gold: int = 0,
    request_gold_per_turn: int = 0,
    request_resources: str = "",
    request_favor: int = 0,
    request_open_borders: bool = False,
    joint_war_target: int = 0,
    mode: str = "send",
) -> str:
    """Propose a trade deal to another player. They answer on their own turn.

    Args:
        other_player_id: The player ID (from get_diplomacy output)
        offer_gold: Lump sum gold to give them
        offer_gold_per_turn: Gold per turn to give them (30-turn duration)
        offer_resources: Comma-separated resource types to offer, e.g. "RESOURCE_SILK,RESOURCE_TEA"
        offer_favor: Diplomatic favor to offer
        offer_open_borders: True to offer our open borders
        request_gold: Lump sum gold to request from them
        request_gold_per_turn: Gold per turn to request (30-turn duration)
        request_resources: Comma-separated resource types to request
        request_favor: Diplomatic favor to request from them
        request_open_borders: True to request their open borders
        joint_war_target: Player ID of a third civ to declare joint war against
        mode: "send" to queue the deal for the other player, "test" to ask the
            game's AI evaluator what it thinks — this says nothing about what a
            human will accept.
    """
    gs = _game(ctx)
    offer_items: list[dict] = []
    request_items: list[dict] = []
    if offer_gold > 0:
        offer_items.append({"type": "GOLD", "amount": offer_gold, "duration": 0})
    if offer_gold_per_turn > 0:
        offer_items.append({"type": "GOLD", "amount": offer_gold_per_turn, "duration": 30})
    for res in (r.strip() for r in offer_resources.split(",") if r.strip()):
        offer_items.append({"type": "RESOURCE", "name": res, "amount": 1, "duration": 30})
    if offer_favor > 0:
        offer_items.append({"type": "FAVOR", "amount": offer_favor})
    if offer_open_borders:
        offer_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})
    if request_gold > 0:
        request_items.append({"type": "GOLD", "amount": request_gold, "duration": 0})
    if request_gold_per_turn > 0:
        request_items.append({"type": "GOLD", "amount": request_gold_per_turn, "duration": 30})
    for res in (r.strip() for r in request_resources.split(",") if r.strip()):
        request_items.append({"type": "RESOURCE", "name": res, "amount": 1, "duration": 30})
    if request_favor > 0:
        request_items.append({"type": "FAVOR", "amount": request_favor})
    if request_open_borders:
        request_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})
    if joint_war_target > 0:  # joint war is mutual — both sides commit
        offer_items.append({"type": "AGREEMENT", "subtype": "JOINT_WAR"})
        request_items.append({"type": "AGREEMENT", "subtype": "JOINT_WAR"})
    if not offer_items and not request_items:
        return "Error: must specify at least one offer or request item"

    params = {
        "other_player_id": other_player_id,
        "offer_items": offer_items,
        "request_items": request_items,
    }
    if mode == "test":
        return await _logged(
            ctx, "test_trade", params, lambda: gs.test_trade(other_player_id, offer_items, request_items)
        )
    return await _logged(
        ctx, "propose_trade", params, lambda: gs.propose_trade(other_player_id, offer_items, request_items)
    )


@mcp.tool()
async def propose_peace(ctx: Context, other_player_id: int) -> str:
    """Propose white peace to a player you're at war with (10-turn war cooldown applies).

    Args:
        other_player_id: The player ID (from get_diplomacy output)
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "propose_peace",
        {"other_player_id": other_player_id},
        lambda: gs.propose_peace(other_player_id),
    )


@mcp.tool()
async def set_policies(ctx: Context, assignments: str) -> str:
    """Set policy cards in government slots.

    Args:
        assignments: Comma-separated slot assignments, e.g.
            "0=POLICY_AGOGE,1=POLICY_URBAN_PLANNING". Slots not listed keep
            their policy; "2=NONE" clears a slot. See get_policies for
            slot indices and the policies each slot type accepts.
    """
    gs = _game(ctx)

    async def _run() -> str:
        parsed: dict[int, str] = {}
        for pair in assignments.split(","):
            if "=" in pair:
                idx_str, policy = pair.split("=", 1)
                parsed[int(idx_str.strip())] = policy.strip()
        if not parsed:
            return "Error: no valid assignments. Format: '0=POLICY_AGOGE,1=POLICY_URBAN_PLANNING'"
        return await gs.set_policies(parsed)

    return await _logged(ctx, "set_policies", {"assignments": assignments}, _run)


@mcp.tool()
async def respond_to_diplomacy(ctx: Context, other_player_id: int, response: str) -> str:
    """Respond to a pending diplomacy encounter (see get_pending_diplomacy).

    Args:
        other_player_id: The player ID of the other civilization
        response: "POSITIVE" (friendly), "NEGATIVE" (dismissive), or "EXIT" to
            leave the conversation

    If SESSION_CONTINUES is returned, send another response for the next round.
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "respond_to_diplomacy",
        {"other_player_id": other_player_id, "response": response},
        lambda: gs.diplomacy_respond(other_player_id, response),
    )


@mcp.tool()
async def send_diplomatic_action(ctx: Context, other_player_id: int, action: str) -> str:
    """Send a proactive diplomatic action to another player.

    Args:
        other_player_id: The player ID (from get_diplomacy output)
        action: One of: DIPLOMATIC_DELEGATION, DECLARE_FRIENDSHIP, DENOUNCE,
                RESIDENT_EMBASSY, OPEN_BORDERS,
                DECLARE_SURPRISE_WAR, DECLARE_FORMAL_WAR, DECLARE_HOLY_WAR,
                DECLARE_LIBERATION_WAR, DECLARE_RECONQUEST_WAR,
                DECLARE_PROTECTORATE_WAR, DECLARE_COLONIAL_WAR,
                DECLARE_TERRITORIAL_WAR

    Delegations cost 25 gold; embassies require Writing. get_diplomacy shows
    which actions are available. Never declare war unless the player's note
    asks for it.
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "send_diplomatic_action",
        {"other_player_id": other_player_id, "action": action},
        lambda: gs.send_diplomatic_action(other_player_id, action),
    )


@mcp.tool()
async def form_alliance(ctx: Context, other_player_id: int, alliance_type: str = "MILITARY") -> str:
    """Form an alliance (requires declared friendship and the Diplomatic Service civic).

    Args:
        other_player_id: The player ID (from get_diplomacy output)
        alliance_type: One of: MILITARY, RESEARCH, CULTURAL, ECONOMIC, RELIGIOUS
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "form_alliance",
        {"other_player_id": other_player_id, "alliance_type": alliance_type},
        lambda: gs.form_alliance(other_player_id, alliance_type.upper()),
    )


@mcp.tool()
async def city_action(
    ctx: Context,
    city_id: int,
    action: str,
    target_x: Optional[int] = None,
    target_y: Optional[int] = None,
) -> str:
    """Issue a command to a city.

    Args:
        city_id: City ID (from get_cities output)
        action: 'attack' (city ranged attack; needs walls, range 2, target_x/y),
            or a captured/disloyal city decision (city_id ignored): 'keep',
            'reject' (free a disloyal city), 'raze', 'liberate_founder',
            'liberate_previous'
        target_x: Target X coordinate (attack)
        target_y: Target Y coordinate (attack)
    """
    gs = _game(ctx)
    match action:
        case "attack":
            if target_x is None or target_y is None:
                return "Error: attack requires target_x and target_y"
            return await _logged(
                ctx,
                "city_attack",
                {"city_id": city_id, "x": target_x, "y": target_y},
                lambda: gs.city_attack(city_id, target_x, target_y),
            )
        case "keep" | "reject" | "raze" | "liberate_founder" | "liberate_previous":
            return await _logged(
                ctx, "resolve_city_capture", {"action": action}, lambda: gs.resolve_city_capture(action)
            )
        case _:
            return (
                f"Error: Unknown city action '{action}'. Available: attack, keep, reject, "
                "raze, liberate_founder, liberate_previous"
            )


@mcp.tool()
async def unit_action(
    ctx: Context,
    unit_id: int,
    action: str,
    target_x: Optional[int] = None,
    target_y: Optional[int] = None,
    improvement: Optional[str] = None,
) -> str:
    """Issue a command to a unit.

    Args:
        unit_id: The unit's composite ID (from get_units output)
        action: One of: move, attack, fortify, skip, found_city, improve, repair,
            remove_improvement, remove_feature, build_route, automate, heal, alert,
            sleep, delete, trade_route, activate, sacrifice_charges, teleport,
            spread_religion
        target_x: Target X coordinate (move/attack/trade_route/teleport)
        target_y: Target Y coordinate (move/attack/trade_route/teleport)
        improvement: Improvement type for builders (improve), e.g. IMPROVEMENT_FARM,
            IMPROVEMENT_MINE, IMPROVEMENT_QUARRY, IMPROVEMENT_PLANTATION,
            IMPROVEMENT_CAMP, IMPROVEMENT_PASTURE, IMPROVEMENT_FISHING_BOATS,
            IMPROVEMENT_LUMBER_MILL

    improve: builder must be on the tile. repair: pillaged improvement on the
    builder's tile. remove_improvement: demolish an intact improvement (one
    charge). remove_feature: chop forest/jungle/marsh. build_route: road or
    railroad on the current tile (Military Engineers). trade_route/teleport:
    target_x/y of the destination city (Traders). activate: Great Person on
    its matching district. sacrifice_charges: Royal Society builder sacrifice
    on a district project. spread_religion: Missionaries/Apostles. heal =
    fortify until healed; alert = sleep and wake when an enemy appears;
    delete = disband permanently.
    """
    gs = _game(ctx)
    unit_index = unit_id % 65536
    params: dict[str, Any] = {"unit_id": unit_id, "action": action}
    if target_x is not None:
        params["target_x"] = target_x
    if target_y is not None:
        params["target_y"] = target_y
    if improvement:
        params["improvement"] = improvement

    async def _run() -> str:
        match action.lower():
            case "move":
                if target_x is None or target_y is None:
                    return "Error: move requires target_x and target_y"
                return await gs.move_unit(unit_index, target_x, target_y)
            case "attack":
                if target_x is None or target_y is None:
                    return "Error: attack requires target_x and target_y"
                return await gs.attack_unit(unit_index, target_x, target_y)
            case "fortify":
                return await gs.fortify_unit(unit_index)
            case "skip":
                return await gs.skip_unit(unit_index)
            case "found_city":
                return await gs.found_city(unit_index)
            case "improve":
                if not improvement:
                    return (
                        "Error: improve requires improvement name (e.g. IMPROVEMENT_FARM). "
                        "To repair a pillaged improvement, use action='repair' instead."
                    )
                return await gs.improve_tile(unit_index, improvement)
            case "repair":
                return await gs.repair_improvement(unit_index)
            case "remove_improvement":
                return await gs.remove_improvement(unit_index)
            case "remove_feature":
                return await gs.remove_feature(unit_index)
            case "build_route":
                return await gs.build_route(unit_index)
            case "automate":
                return await gs.automate_explore(unit_index)
            case "heal":
                return await gs.heal_unit(unit_index)
            case "alert":
                return await gs.alert_unit(unit_index)
            case "sleep":
                return await gs.sleep_unit(unit_index)
            case "delete":
                return await gs.delete_unit(unit_index)
            case "trade_route":
                if target_x is None or target_y is None:
                    return "Error: trade_route requires target_x and target_y of destination city"
                return await gs.make_trade_route(unit_index, target_x, target_y)
            case "activate":
                return await gs.activate_great_person(unit_index)
            case "sacrifice_charges":
                return await gs.sacrifice_builder_charges(unit_index)
            case "spread_religion":
                return await gs.spread_religion(unit_index)
            case "teleport":
                if target_x is None or target_y is None:
                    return "Error: teleport requires target_x and target_y of the destination city"
                return await gs.teleport_to_city(unit_index, target_x, target_y)
            case _:
                return (
                    f"Error: Unknown action '{action}'. Valid: move, attack, fortify, skip, "
                    "found_city, improve, repair, remove_improvement, remove_feature, "
                    "build_route, automate, heal, alert, sleep, delete, trade_route, "
                    "activate, sacrifice_charges, teleport, spread_religion"
                )

    return await _logged(ctx, "unit_action", params, _run)


@mcp.tool()
async def skip_remaining_units(ctx: Context) -> str:
    """Fortify or heal idle combat units, then skip every unit that still needs orders."""
    gs = _game(ctx)
    return await _logged(ctx, "skip_remaining_units", {}, gs.skip_remaining_units)


@mcp.tool()
async def set_city_production(
    ctx: Context,
    city_id: int,
    item_type: str,
    item_name: str,
    target_x: int | None = None,
    target_y: int | None = None,
) -> str:
    """Set what a city should produce.

    Args:
        city_id: City ID (from get_cities output)
        item_type: UNIT, BUILDING, DISTRICT, or PROJECT
        item_name: e.g. UNIT_WARRIOR, BUILDING_MONUMENT, DISTRICT_CAMPUS, PROJECT_LAUNCH_EARTH_SATELLITE
        target_x: X coordinate for district/wonder placement (use get_district_advisor / get_wonder_advisor)
        target_y: Y coordinate for district/wonder placement
    """
    gs = _game(ctx)
    params: dict = {"city_id": city_id, "item_type": item_type, "item_name": item_name}
    if target_x is not None:
        params["target_x"] = target_x
        params["target_y"] = target_y
    return await _logged(
        ctx,
        "set_city_production",
        params,
        lambda: gs.set_city_production(city_id, item_type, item_name, target_x, target_y),
    )


@mcp.tool()
async def purchase_item(
    ctx: Context, city_id: int, item_type: str, item_name: str, yield_type: str = "YIELD_GOLD"
) -> str:
    """Purchase a unit or building instantly with gold or faith (see get_city_production for costs).

    Args:
        city_id: City ID (from get_cities output)
        item_type: UNIT or BUILDING
        item_name: e.g. UNIT_WARRIOR, BUILDING_MONUMENT
        yield_type: YIELD_GOLD (default) or YIELD_FAITH
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "purchase_item",
        {"city_id": city_id, "item_type": item_type, "item_name": item_name, "yield_type": yield_type},
        lambda: gs.purchase_item(city_id, item_type, item_name, yield_type),
    )


@mcp.tool()
async def set_research(ctx: Context, tech_or_civic: str, category: str = "tech") -> str:
    """Choose a technology or civic to research (see get_tech_civics).

    Args:
        tech_or_civic: The type name, e.g. TECH_POTTERY or CIVIC_CRAFTSMANSHIP
        category: "tech" or "civic" (default: tech)
    """
    gs = _game(ctx)

    async def _run() -> str:
        if category.lower() == "civic":
            return await gs.set_civic(tech_or_civic)
        return await gs.set_research(tech_or_civic)

    return await _logged(
        ctx, "set_research", {"tech_or_civic": tech_or_civic, "category": category}, _run
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_trade_routes(ctx: Context) -> str:
    """Trade route capacity, active routes, and trader status (idle or on a route)."""
    gs = _game(ctx)
    return await _logged(
        ctx, "get_trade_routes", {}, lambda: _narrate(gs.get_trade_routes, nr.narrate_trade_routes)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_trade_destinations(ctx: Context, unit_id: int) -> str:
    """Valid trade route destinations for a trader, with yields.

    Args:
        unit_id: The trader's composite ID (from get_units output)

    Start a route with unit_action(action='trade_route', target_x, target_y).
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_trade_destinations",
        {"unit_id": unit_id},
        lambda: _narrate(
            lambda: gs.get_trade_destinations(unit_id % 65536), nr.narrate_trade_destinations
        ),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_district_advisor(ctx: Context, city_id: int, district_type: str) -> str:
    """Valid tiles for a district ranked by adjacency bonus.

    Args:
        city_id: City ID (from get_cities)
        district_type: e.g. DISTRICT_CAMPUS, DISTRICT_HOLY_SITE, DISTRICT_INDUSTRIAL_ZONE

    Then set_city_production with target_x/target_y.
    """
    gs = _game(ctx)

    async def _run() -> str:
        result = await gs.get_district_advisor(city_id, district_type)
        if isinstance(result, str):
            return f"Error: {result}"
        return nr.narrate_district_advisor(result, district_type)

    return await _logged(
        ctx, "get_district_advisor", {"city_id": city_id, "district_type": district_type}, _run
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_wonder_advisor(ctx: Context, city_id: int, wonder_name: str) -> str:
    """Valid tiles for a wonder ranked by displacement cost (lowest = best).

    Args:
        city_id: City ID (from get_cities output)
        wonder_name: Wonder building type, e.g. BUILDING_CHICHEN_ITZA

    Then set_city_production with target_x/target_y.
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_wonder_advisor",
        {"city_id": city_id, "wonder_name": wonder_name},
        lambda: _narrate(
            lambda: gs.get_wonder_advisor(city_id, wonder_name),
            lambda placements: nr.narrate_wonder_advisor(placements, wonder_name),
        ),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_purchasable_tiles(ctx: Context, city_id: int) -> str:
    """Tiles a city can buy with gold, with cost, terrain, and resources.

    Args:
        city_id: City ID (from get_cities)
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_purchasable_tiles",
        {"city_id": city_id},
        lambda: _narrate(lambda: gs.get_purchasable_tiles(city_id), nr.narrate_purchasable_tiles),
    )


@mcp.tool()
async def purchase_tile(ctx: Context, city_id: int, x: int, y: int) -> str:
    """Buy a tile for a city with gold (see get_purchasable_tiles).

    Args:
        city_id: City ID
        x: Tile X coordinate
        y: Tile Y coordinate
    """
    gs = _game(ctx)
    return await _logged(
        ctx, "purchase_tile", {"city_id": city_id, "x": x, "y": y}, lambda: gs.purchase_tile(city_id, x, y)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_map_pins(ctx: Context) -> str:
    """Every map pin (map tack) this player can see: their own, and pins others shared with them.

    Pins are the player's own notes on the map (planned cities, districts,
    improvements, threats). The turn briefing includes them; read them as
    the player's plan.
    """
    gs = _game(ctx)
    return await _logged(
        ctx, "get_map_pins", {}, lambda: _narrate(gs.get_map_pins, nr.narrate_map_pins)
    )


@mcp.tool()
async def place_map_pin(
    ctx: Context, x: int, y: int, name: str, icon: str = "ICON_MAP_PIN_CIRCLE"
) -> str:
    """Put a map pin (map tack) on a tile for this player, or rename the one already there.

    New pins are private to this player. Use them to mark plans the player
    or the next stand-in should follow (city sites, district and
    improvement spots); say in the diary what they mean.

    Args:
        x: Tile X coordinate
        y: Tile Y coordinate
        name: Label shown on the map, up to 20 characters (longer is cut)
        icon: ICON_MAP_PIN_CIRCLE (default), _STRENGTH, _RANGED, _BOMBARD,
            _DISTRICT, _CHARGES, _DEFENSE, _MOVEMENT, _NO, _PLUS, _TRIANGLE,
            _SUN, _SQUARE, _DIAMOND; or an improvement, district or building
            type such as IMPROVEMENT_CAMP or DISTRICT_CAMPUS for its icon
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "place_map_pin",
        {"x": x, "y": y, "name": name, "icon": icon},
        lambda: gs.place_map_pin(x, y, name, icon),
    )


@mcp.tool()
async def remove_map_pin(ctx: Context, x: int, y: int) -> str:
    """Delete this player's map pin on a tile (pins others shared stay).

    Args:
        x: Tile X coordinate
        y: Tile Y coordinate
    """
    gs = _game(ctx)
    return await _logged(
        ctx, "remove_map_pin", {"x": x, "y": y}, lambda: gs.remove_map_pin(x, y)
    )


@mcp.tool()
async def change_government(ctx: Context, government_type: str) -> str:
    """Switch to a different government (first switch after unlocking a tier is free).

    Args:
        government_type: e.g. GOVERNMENT_CLASSICAL_REPUBLIC, GOVERNMENT_OLIGARCHY
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "change_government",
        {"government_type": government_type},
        lambda: gs.change_government(government_type),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_great_people(ctx: Context) -> str:
    """Available Great People, recruitment progress and costs, and who is recruiting them."""
    gs = _game(ctx)
    return await _logged(
        ctx, "get_great_people", {}, lambda: _narrate(gs.get_great_people, nr.narrate_great_people)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_gp_advisor(ctx: Context, unit_index: int) -> str:
    """Best cities to activate a Great Person, ranked by suitability.

    Args:
        unit_index: The Great Person unit's index (from get_units output).
    """
    gs = _game(ctx)

    async def _run() -> str:
        result = await gs.get_gp_advisor(unit_index)
        if result is None:
            return "Could not get GP advisor info. Is this a Great Person unit?"
        return nr.narrate_gp_advisor(result)

    return await _logged(ctx, "get_gp_advisor", {"unit": unit_index}, _run)


@mcp.tool()
async def recruit_great_person(ctx: Context, individual_id: int) -> str:
    """Recruit a Great Person with accumulated points (check [CAN RECRUIT] in get_great_people).

    Args:
        individual_id: The individual's ID (from get_great_people output)
    """
    gs = _game(ctx)
    return await _logged(
        ctx, "recruit_great_person", {"id": individual_id}, lambda: gs.recruit_great_person(individual_id)
    )


@mcp.tool()
async def patronize_great_person(
    ctx: Context, individual_id: int, yield_type: str = "YIELD_GOLD"
) -> str:
    """Buy a Great Person instantly with gold or faith (costs in get_great_people).

    Args:
        individual_id: The individual's ID (from get_great_people output)
        yield_type: YIELD_GOLD (default) or YIELD_FAITH
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "patronize_great_person",
        {"id": individual_id, "yield": yield_type},
        lambda: gs.patronize_great_person(individual_id, yield_type),
    )


@mcp.tool()
async def reject_great_person(ctx: Context, individual_id: int) -> str:
    """Pass on a Great Person (costs faith; the next one in that class becomes available).

    Args:
        individual_id: The individual's ID (from get_great_people output)
    """
    gs = _game(ctx)
    return await _logged(
        ctx, "reject_great_person", {"id": individual_id}, lambda: gs.reject_great_person(individual_id)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_world_congress(ctx: Context) -> str:
    """World Congress status, resolutions and voting options, turns until the next session.

    If it says the congress has not convened or is disabled, there is nothing to do.
    """
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_world_congress",
        {},
        lambda: _narrate(gs.get_world_congress, nr.narrate_world_congress),
    )


@mcp.tool()
async def queue_wc_votes(ctx: Context, votes: str) -> str:
    """Register World Congress votes to cast when the session fires inside end_turn.

    Args:
        votes: JSON array of vote objects, e.g.
            '[{"hash": -513644209, "option": 1, "target": 2, "votes": 5}]'
            hash = resolution type hash (from get_world_congress);
            option = 1 for A, 2 for B; target = player ID for player-targeted
            resolutions (resolved to the list index at runtime); votes = max
            votes to spend.

    Call this before end_turn when get_world_congress shows 0 turns until the
    next session.
    """
    import json

    gs = _game(ctx)
    vote_list = json.loads(votes)
    return await _logged(
        ctx, "queue_wc_votes", {"votes": vote_list}, lambda: gs.queue_wc_votes(vote_list)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_victory_progress(ctx: Context) -> str:
    """Progress of every met civilization toward each victory type."""
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_victory_progress",
        {},
        lambda: _narrate(gs.get_victory_progress, nr.narrate_victory_progress),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_religion_spread(ctx: Context) -> str:
    """Per-city religion breakdown across visible cities and who is closest to religious victory."""
    gs = _game(ctx)
    return await _logged(
        ctx,
        "get_religion_spread",
        {},
        lambda: _narrate(gs.get_religion_status, nr.narrate_religion_status),
    )


@mcp.tool()
async def set_city_focus(ctx: Context, city_id: int, focus: str) -> str:
    """Set a city's citizen yield priority.

    Args:
        city_id: City ID
        focus: One of: food, production, gold, science, culture, faith, default
    """
    gs = _game(ctx)
    return await _logged(
        ctx, "set_city_focus", {"city_id": city_id, "focus": focus}, lambda: gs.set_city_focus(city_id, focus)
    )


@mcp.tool()
async def dismiss_popup(ctx: Context) -> str:
    """Dismiss a blocking popup in the game UI, or report one that needs the keyboard.

    Call this if a popup (historic moment, boost, world congress intro) seems
    to be blocking interaction. Popups with their own close button (natural
    wonder, wonder built, era complete, tutorial advisor) are reported; press
    ESC in the game for those.
    """
    gs = _game(ctx)
    return await _logged(ctx, "dismiss_popup", {}, gs.dismiss_popup)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")
