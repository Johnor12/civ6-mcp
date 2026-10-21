# Hotseat stand-in mode

Use civ6-mcp to play one absent player's turn in a local hotseat game, inside
the live hotseat session, without seeing anything that player could not see.

## The constraint, and the way around it

civ6-mcp originally talked to the game only through FireTuner (TCP 4318).
Civ 6 shuts that listener down the moment a multiplayer session starts, and
hotseat counts as multiplayer: the port is open at the main menu and in
single-player games and closes in the hotseat staging room, before the first
turn. There is no `AppOptions.txt` switch for it.

The engine still runs UI mods inside hotseat sessions, and a UI mod's Lua can
do everything the tuner's `InGame` state can. So this fork ships a small mod,
**MCP Bridge** (`mods/MCPBridge`), and a second transport, `bridge.py`:

```
civ6-mcp server                         Civ 6 (MCP Bridge mod)
───────────────                         ──────────────────────
writes Mods/MCPBridge/UI/mcp_bridge_cmd.lua
   MCP_CMD = { seq = 17, context = "InGame", code = [[ ... ]] }
                                        every 0.2 s: include("mcp_bridge_cmd")
                                        new seq? -> loadstring(code)() with
                                        print() captured
tails Logs/Lua.log                      MCPBridge_InGame: MCPB|17|O|<line>
                                        MCPBridge_InGame: MCPB|17|X
```

`include()` re-reads the file from disk on every call (verified), `Lua.log`
is written as the game prints, and that is the whole protocol. A round trip
takes about 0.2 s. Lines longer than ~2 KB are split by the mod and joined by
the server. The mod is pure UI (`AffectsSavedGames=0`): it changes no rules
and no saves, the humans' game is untouched.

Two contexts run the same core (`UI/MCPBridgeCore.lua`):

- `InGame` — a mod-added UI context, alive in every loaded game (single
  player, hotseat, any session type).
- `FrontEnd` — the main menu and the staging room. The front end does not
  load mod-added UI contexts, so the mod imports a `Test.lua` that shadows
  the base game's placeholder `Base/Assets/UI/Test.lua`, which `FrontEnd.xml`
  loads on Windows. (On macOS/Linux the front-end half of the bridge is not
  available; loading the hotseat save then has to be done by hand.)

The tuner is still used when the mod is not installed
(`CIV_MCP_TRANSPORT=tuner` forces it; `bridge` forces the mod; the default
`auto` picks the mod whenever it is installed). With the bridge there is no
GameCore Lua state: every query runs in the UI state, and the few
GameCore-only calls (`FinishMoves`, `CurrentlyBuilding`, …) have UI
equivalents behind a `pcall`.

## Setup (once)

```bash
uv sync --extra launcher-windows        # as for single player
uv run python scripts/install_bridge_mod.py
#   copies mods/MCPBridge into Documents\My Games\Sid Meier's Civilization VI\Mods
```

Start Civ 6. It discovers the mod on launch and enables it; confirm under
Additional Content > Mods that **MCP Bridge** is enabled. `EnableTuner 1` in
`AppOptions.txt` stays on (the launcher uses the port to tell "game is up"
from "game is loading"); `PlayIntroVideo 0` saves two minutes per restart.
Restart the MCP server (Claude Code `/mcp` → reconnect) after installing the
mod so it picks the bridge transport; `hotseat_status` must answer.

## Playing an absent player's turn

The humans play as usual. When the turn-change screen shows the absent
player's name ("RAY — Save Game / Start Turn"), leave the game there and tell
Claude Code, in this repo:

> take Poundmaker's turn — he said: finish the campus in Mistawasis, no wars

What happens then, tool by tool:

1. `hotseat_status` — confirms the game is in a hotseat session on
   Poundmaker's turn with the turn-change screen up. Every other game tool
   is **locked** in a hotseat session until a stand-in is armed.
2. `begin_standin_turn("Poundmaker", note="finish the campus …")` — checks
   the seat, presses Start Turn (dismisses the screen, unpauses the game),
   writes the marker `~/.civ6-mcp/standin.json` and reports the player's
   note. A hotseat password on that player is bypassed; players should know.
3. `get_diary` — the diary for this game + player persists across sessions
   (run id `standin`), so the agent knows what it, or the human, did before.
4. The normal turn: `get_game_overview`, `get_units`, … every tool acts for
   Poundmaker only; the guard refuses anything else.
5. `end_turn` — runs the same checks as any end turn (open diplomacy,
   incoming deals, hard blockers, units that still need orders), then ends
   the turn for real. The engine hands the seat to the next living human
   (or, after the last one, runs the AI civs and city-states and starts the
   next turn); the call returns once that player's turn-change screen is up
   and the game has been saved there as `<LEADER> <turn> <year> MCP` in the
   hotseat save folder (the players' own naming, plus a tag). The marker
   stays active with `finished_turn` set, so every game tool is locked
   again until the next `begin_standin_turn`.

The next human sits down, sees their own turn-change screen, and plays. The
whole stand-in leaves no trace in the game except the save and the diary.

If the game is at the main menu instead (someone quit, or the PC was
restarted), `load_hotseat_save("POUNDMAKER 63 150 AD")` goes through
Multiplayer > Hotseat > Load Game > Start Game by itself and ends on the
turn-change screen (~30 s for a 10-civ save); `list_saves` shows hotseat
saves with a `[hotseat]` tag. It also works from inside another game (that
game is left without saving).

### Which save to load

Load a save made **during the absent player's turn, before anything was done
in it** — the turn-change screen has its own Save Game button for exactly
this, and that is what the game's autosaves at turn change are. A save made
by the previous player still has that player's turn active; `hotseat_status`
would show their name and `begin_standin_turn` refuses.

## Guarantees

- **One seat.** The marker names one leader; before every game tool the
  server checks that the game's local player is that leader and that their
  turn is active (one Lua round trip, cached 20 s). A hotseat session without
  an armed stand-in refuses every game tool. After `end_turn` the local
  player is someone else, so everything is refused until the next
  `begin_standin_turn`.
- **Information isolation.** Everything the tools return is scoped to the
  current player the way the game scopes it for a human: tiles through
  `PlayersVisibility`, other civilizations through `HasMet`, enemy units only
  on visible tiles. Figures the game shows for met civilizations (score,
  yields, military strength) are shown; this game also runs the Extended
  Diplomacy Ribbon mod, which shows the same numbers in the UI. The diary
  stores only the agent's own player and city rows. The omniscient map dump
  for the web dashboard only runs after a real turn advance in single
  player. `run_lua` is removed whenever the bridge is in use (set
  `CIV_MCP_STANDIN_ALLOW_LUA=1` to keep it for debugging).
- **No process management.** The connection-loss recovery that kills and
  relaunches the game is disabled on the bridge; a silent bridge means the
  game is loading or at a screen without the mod.
- **Always saved, always on the turn-change screen.** `end_turn` returns
  only after the save confirmed. If the save fails the message says so and
  the game is still on the next player's screen for a manual Save Game.

## Diplomacy with human players

Other civilizations are humans. Proposals (`propose_trade`, delegations,
friendship, alliances, peace) are queued and resolved by the other player on
their own turn; `propose_trade(mode="test")` asks the game's AI evaluator and
says nothing about what a human will accept. Incoming deals from humans show
up through `get_pending_trades` at the start of the turn and must be answered
before `end_turn` lets the turn go. Leader "agendas" and AI relationship
modifiers in `get_diplomacy` belong to human players and mean nothing.
This game has diplomatic victory and the World Congress disabled; if a
congress ever is in session, `get_world_congress` and `queue_wc_votes` work
as in single player.

## Mods

The game runs Better Balanced Game 7.4.6, Better Balanced Map 1.39.5 and
several UI mods. All tools read unit, district and building data from the
game's `GameInfo` tables, so modded units (Okihtcitaw, Skirmisher at 30 CS,
the Preserve district, …) show up with their modded values. The combat table
in AGENTS.md is base-game reference material; trust the numbers the tools
print.

## Verified on 2026-10-21

Windows 11, Steam build 1.0.12.68, 4K at 150 % with a second monitor, 10-civ
Better Balanced Game hotseat save at turn 63, driven entirely through the
bridge (the tuner port was closed throughout):

- main menu → `load_hotseat_save` → staging room with all ten slots human →
  Start Game → Poundmaker's turn-change screen, 29 s; also from inside a
  running game (leaves it, then the same path);
- `begin_standin_turn`: screen dismissed, game unpaused, turn active;
- every read tool (overview, units, cities, tech, diplomacy, notifications,
  victory, policies, governors, city-states, great people, religion, trade
  routes, strategic map, settle scan, map area, promotions, …) answered in
  0.2–0.8 s with the same output as over the tuner;
- `end_turn`: ACTION_ENDTURN → next human (Barbarossa) on the turn-change
  screen within a second → `FREDERICK BARBAROSSA 63 150 AD MCP.Civ6Save`
  written to `Saves/Hotseat` with Barbarossa's turn flagged current;
- Lua.log cuts lines at 2008 characters (handled by chunking); mod UI
  contexts start hidden and get no update ticks until shown (handled);
  the front end ignores `AddUserInterfaces` (hence the `Test.lua` shadow).

Not exercised: a turn in which the agent is the last human of the round
(the AI civs and city-states then run before the next turn-change screen;
`end_turn` waits up to four minutes for it), and a World Congress session.

## Legacy: the single-player detour

Before the bridge, the turn was played in a single-player copy of the
hotseat save: `scripts/hotseat_standin.py to-single` flips every other civ to
AI in the save header, the agent plays with `end_turn` *finishing* (saving)
instead of ending, and `to-hotseat` converts the result back for a person to
load and end by hand. The script and that mode of `end_turn` still work
(marker `mode: "single"`) for a machine where the mod cannot be used; the
old write-up is in the git history of this file. It needs a person at the
keyboard for two conversions and a menu trip, and about five minutes per
turn, against about one minute with the bridge.

## Known limitations

- The front-end bridge rides on the base game's `Test` context, which only
  exists on Windows. Elsewhere, load the hotseat save by hand and the
  in-game bridge takes it from there.
- A player's hotseat password is bypassed by `begin_standin_turn`.
- Scores and "RANK" lines compare the agent's civilization only with met
  civilizations, same as the in-game ribbon.
- The hotseat autosave folder is not read; name the save you want loaded.
