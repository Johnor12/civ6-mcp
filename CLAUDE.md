# Civ 6 hotseat stand-in — agent reference

This MCP server plays one absent player's turn in a local Civilization VI
hotseat game, inside the live session, through the MCP Bridge UI mod. The
operator says something like "take Poundmaker's turn" and passes on what
that player asked for. Every other civilization is a human being who plays
their own turns later.

**You only know what you explicitly query.** A human player absorbs the map,
unit health bars and notifications passively; you have none of that, so
query what you need before deciding.

## The stand-in workflow

1. `hotseat_status` — where the game is and whose turn-change screen is up.
   Every game tool is locked until step 2. If the game is at the main menu,
   `list_saves` then `load_hotseat_save(name)` opens the save and ends on the
   turn-change screen. Load a save made during the absent player's turn
   (the game's autosave at turn change, or a save from that screen).
2. `begin_standin_turn(leader, note)` — takes that player's seat (presses
   Start Turn for them) and arms stand-in mode. It refuses if the screen
   shows someone else: never play a different player than the one named.
3. `get_diary` — this player's memory from earlier stand-ins — then
   `get_game_overview` and the turn loop below.
4. `end_turn` exactly once when the turn is done. It refuses while a
   diplomacy encounter or incoming deal is open, while a blocker remains,
   or while any unit still needs orders; then it ends the turn for real,
   waits for the next human's turn-change screen and saves there. After
   it reports TURN ENDED every game tool is locked again: stop. Never load
   other saves or "continue".

While the **STAND-IN MODE** paragraph shows in `get_game_overview`:

- You control exactly one civilization. Treat the others as humans: trade
  and diplomacy proposals get answered on their turn; `propose_trade(mode="test")`
  asks the game's AI evaluator and says nothing about what a human accepts;
  leader "agendas" and AI relationship modifiers are meaningless.
- The player's note is the instruction. Play the civilization the way its
  owner would: steady, no gambits they did not ask for, no wars or peace
  deals unless the note says so. When in doubt, leave a clear diary entry
  about what you did and why.
- You only see what this player can see. Do not try to work around that.

## Coordinate system

Hex grid (X, Y) where **higher Y is visually south**. X increases east.
Moving from (9,24) to (9,26) is south.

## Turn loop

1. `get_game_overview` — turn, yields, research, score, the stand-in note.
2. `get_notifications` and `get_pending_trades` — anything waiting for this player.
3. `get_units` — positions, HP, moves, charges, nearby threats.
4. `get_map_area` around cities and units when deciding where to move.
5. Move or order each unit (`unit_action`); `get_builder_tasks` for builders.
6. `get_cities` — queues, growth, pillaged districts; `set_city_production`
   where a queue is empty, `get_district_advisor` before placing a district.
7. `set_research` if research or a civic completed.
8. Spend what the note allows: `purchase_item`, `purchase_tile`,
   `send_envoy`, `set_policies`, `promote_unit`, governors, Great People.
9. `end_turn` with the five reflections.

## Diary

The diary is this player's memory across stand-ins. Entries with specific
details (unit names, coordinates, yields, reasoning) are far more useful to
the next stand-in than summaries. Reflections are recorded before the turn
ends; anything that happens afterwards belongs in the next entry.

Five fields, all required: **tactical** (what happened), **strategic**
(standing vs rivals, with numbers), **tooling** (tool issues, or "No
issues"), **planning** (concrete actions for the next few turns),
**hypothesis** (predictions and risks).

## Unit actions

| Action | Effect | Notes |
|--------|--------|-------|
| `move` | Move to tile | target_x, target_y required; the reply shows where the unit actually stopped |
| `attack` | Attack enemy | shows a damage estimate; melee/ranged auto-detected |
| `fortify` / `heal` / `alert` / `sleep` / `skip` | Settle a unit for the turn | `heal` wakes at full HP, `alert` wakes when an enemy appears |
| `automate` | Auto-explore | scouts |
| `found_city` | Settle | settlers; `get_settle_advisor` first |
| `improve` | Build improvement | builders; `improvement=IMPROVEMENT_FARM` etc.; `valid_improvements` in `get_units` lists what the tile allows |
| `remove_feature` / `repair` / `remove_improvement` | Builder tile work | forest, jungle and marsh block most improvements: chop first |
| `build_route` | Road/railroad on the current tile | Military Engineers |
| `trade_route` / `teleport` | Traders | target_x/y of the destination city; `get_trade_destinations` first |
| `activate` | Use a Great Person | on its matching completed district; `get_gp_advisor` |
| `spread_religion` | Missionaries/Apostles | |
| `delete` | Disband | |

`skip_remaining_units` fortifies or heals idle combat units and skips the
rest; use it only after every unit that matters has its orders.

Hills, forests and jungles cost 2 movement each and stack. Civilians have no
combat strength: check `get_map_area` (radius 2) around a destination before
sending a builder or settler there. `get_pathing_estimate` gives turns to a
destination using the game's own pathfinder.

## End-turn blockers

`end_turn` resolves what it can and names what it cannot:

- **Units** — unmoved units need orders.
- **Production** — a city queue is empty: `set_city_production`.
- **Research / Civic** — choose the next one: `get_tech_civics` → `set_research`.
- **Governor** — `get_governors` → `appoint_governor` / `assign_governor` / `promote_governor`.
- **Promotion** — `get_unit_promotions` → `promote_unit`.
- **Policy slot** — `get_policies` → `set_policies`.
- **Pantheon / Religion** — `get_pantheon_beliefs` → `choose_pantheon`; `get_religion_beliefs` → `found_religion`.
- **Envoys** — `get_city_states` → `send_envoy`.
- **Dedication** — `get_dedications` → `choose_dedication`.
- **City capture** — `city_action(city_id, "keep" | "raze" | "liberate_founder" | "liberate_previous")`.
- **Diplomacy** — `get_pending_diplomacy` → `respond_to_diplomacy`; `get_pending_trades` → `respond_to_trade`.

If a popup seems to block the game, `dismiss_popup`. Popups with their own
close button (natural wonder, wonder built, era complete) are only reported:
tell the operator to press ESC in the game.

## Diplomacy and trade with human players

Proposals (`propose_trade`, delegations, friendship, alliances, peace) are
queued and resolved by the other player on their own turn. Incoming deals
from humans show up in `get_pending_trades` and must be answered before the
turn can end. War declarations take effect immediately for diplomacy but
units cannot attack the new enemy until the following turn.

`get_city_states` → `send_envoy` works as in single player. `get_world_congress`
and `queue_wc_votes` only matter if a congress is in session; this game has
the World Congress disabled.

## Production and districts

`get_city_production` lists what a city can build or buy with costs.
`get_district_advisor(city_id, district_type)` ranks tiles by adjacency, then
`set_city_production(..., target_x, target_y)`. `get_wonder_advisor` does the
same for wonders. `get_tech_civics` flags cheap boosted items with `!! GRAB THIS`.

All unit, district and building data comes from the game's own tables, so
modded content (this game runs Better Balanced Game) shows up with its
modded values. Trust the numbers the tools print.
