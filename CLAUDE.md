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
   (the game's autosave at turn change, or a save from that screen). Saves
   tagged `MCP` were written by the tooling on that screen; `MCP MIDTURN`
   means that player had already started playing when it was taken.
2. `begin_standin_turn(leader, note)` — takes that player's seat (presses
   Start Turn for them) and arms stand-in mode. It refuses if the screen
   shows someone else: never play a different player than the one named.
   Its reply is the **turn briefing**: overview, the last diary entry,
   notifications, pending deals, cities, units and the world snapshot.
3. Read the briefing and the civilization's BBG kit (see Better Balanced
   Game), then run the turn loop below. `get_turn_briefing`
   returns a fresh briefing mid-turn; `get_diary` has older entries.
4. `end_turn` exactly once when the turn is done. It refuses while a
   diplomacy encounter or incoming deal is open, while a blocker remains,
   or while any unit still needs orders; then it ends the turn for real,
   waits for the next human's turn-change screen and saves there. After
   it reports TURN ENDED every game tool is locked again: stop. Never load
   other saves or "continue". If it reports the turn was **interrupted**
   (an automated scout walked into a village, a wonder or a stranger while
   the turn ended), the seat is still yours: read the notifications it
   lists, give any new orders, and call `end_turn` once more.

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

## Better Balanced Game

The games run the **Better Balanced Game** mod (BBG 7.5.1, Steam Workshop
2865001760) with Better Balanced Maps. BBG rewrites most leader and civ
abilities, unique units, improvements and buildings, policies, pantheons
and beliefs, so what you know about vanilla Civ 6 is often wrong here.

Before planning a player's turn, read their civilization's BBG entry: civ
ability, leader ability, unique unit, unique infrastructure, start bias.
Let it drive the turn: build order, research and civic path, where to
settle, what to improve, which districts to place where. On the first
stand-in for a player, write the kit into the diary's strategic field so
later stand-ins have it without the lookup.

- Leaders and civs: https://civ6bbg.github.io/en_US/leaders_7.5.html. The
  other pages follow the same pattern: `units_`, `buildings_`,
  `improvements_`, `policies_`, `religion_`, `governor_`, `great_people_`,
  `city_states_`, `tech_tree_`, `civic_tree_`, `world_wonder_`,
  `natural_wonder_`, `changelog_7.5.html`.
- The leaders page is about 1 MB. WebFetch reads 100k characters per call
  and most civs are far down the page, so download it with curl into the
  scratchpad and extract the one entry. It starts at a comment such as
  `<!--LOC_CIVILIZATION_NUBIA_NAME LOC_LEADER_AMANITORE_NAME-->`; the BBG
  text of each ability is the element with class `actual-text`. The
  `base-game-text` block after it repeats that text beside the vanilla
  version for comparison.

The game tools read units, districts and buildings from the running game's
tables, so their numbers already include BBG. When they disagree with the
docs, trust the tools.

## Coordinate system

Hex grid (X, Y) where **higher Y is visually south**. X increases east.
Moving from (9,24) to (9,26) is south.

## The world snapshot

The briefing's World section is everything this player can see, the way a
human sees the map: an ASCII hex grid of every revealed tile (terrain,
feature, river/coast; legend printed with it; odd rows sit half a tile
east) and, grouped by name, every resource, improvement, district, wonder,
foreign unit and city on revealed tiles, with the distance from your
nearest unit for things outside your borders. Tribal villages show up as
`GOODY_HUT` under Improvements: walk any unit onto one for a free bonus.
Barbarian camps are `BARBARIAN_CAMP`. A `~` after a coordinate means the
tile is in fog, so what was there may have changed.

## Turn loop

1. The briefing — turn, yields, research, notifications, pending deals,
   map pins, cities, units, world. Check it against the diary's plan and
   the map pins.
2. Anything **Action Required** in Notifications is resolved first.
3. `get_units` after moving when you need fresh positions and threats.
4. `get_map_area` for tile yields and movement costs when choosing a
   destination; the snapshot grid shows terrain but not yields.
5. Move or order each unit (`unit_action`); `get_builder_tasks` for builders.
6. `get_cities` — queues, growth, pillaged districts; `set_city_production`
   where a queue is empty, `get_district_advisor` before placing a district.
7. `set_research` if research or a civic completed.
8. Spend what the note allows: `purchase_item`, `purchase_tile`,
   `send_envoy`, `set_policies`, `promote_unit`, governors, Great People.
9. `end_turn` with the five reflections.

## Map pins

The briefing's Map pins section lists every pin (map tack) this player can
see: their own, and pins other players shared with them (`get_map_pins`
reads them mid-turn). A human's pins are their plan, so follow them: a pin
on a tile usually means "settle here", "district here" or "build this
improvement here". `place_map_pin(x, y, name, icon)` marks a plan of your
own (private to this player; `icon` takes an improvement or district type
such as `IMPROVEMENT_CAMP`), and `remove_map_pin` clears one that is done or
stale. Say in the diary what any pin you placed means.

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
| `move` | Move to tile | target_x, target_y required; the reply shows where the unit actually stopped, and what a tribal village gave (gold, faith, a new unit with its id) |
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
BBG content shows up with its modded values. Trust the numbers the tools
print.
