# civ6-mcp — hotseat stand-in

An MCP server that lets Claude Code play one absent player's turn in a local
Civilization VI hotseat game, inside the live session, without seeing
anything that player could not see.

When the turn-change screen shows the absent player's name, you tell Claude
Code "take Poundmaker's turn — he said: finish the campus, no wars". The
agent takes the seat, plays the turn with the normal game tools, ends the
turn for real, hands the game to the next player's turn-change screen and
saves there. The whole stand-in leaves no trace except the save and the
player's diary.

This is a heavily trimmed fork of [lmwilki/civ6-mcp](https://github.com/lmwilki/civ6-mcp)
(MIT). The single-player, benchmark, dashboard and FireTuner machinery of
the original is gone; what remains is the hotseat stand-in and the game
tools it needs. Windows only.

## How it works

Civ 6 shuts its FireTuner debug port down inside any multiplayer session,
including local hotseat, so the server drives the game through a tiny UI
mod instead, **MCP Bridge** (`mods/MCPBridge`):

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

`include()` re-reads the file from disk on every call, `Lua.log` is written
as the game prints, and that is the whole protocol: about 0.2 s per call.
The mod is pure UI (`AffectsSavedGames=0`); it changes no rules and no
saves. It runs in two contexts: `InGame` (any loaded game) and the front end
(main menu and staging room, where it rides on the base game's `Test.lua`
placeholder so the server can load a hotseat save from the menu).

## Setup (once)

Requirements: Windows, Civ 6 (Steam), [uv](https://docs.astral.sh/uv/), Claude Code.

```bash
git clone <this repo> && cd civ6-mcp
uv sync
uv run python scripts/install_bridge_mod.py   # copies mods/MCPBridge into Documents\My Games\...\Mods
```

Start Civ 6. It discovers the mod on launch and enables it; confirm under
Additional Content > Mods that **MCP Bridge** is enabled. In the game
options turn **Auto End Turn** off. `PlayIntroVideo 0` in `AppOptions.txt`
saves time on every restart.

The repo's `.mcp.json` registers the server for Claude Code; start Claude
Code in this directory and check `/mcp` shows `civ6` connected. After
changing the server code, restart it from `/mcp`.

## Playing an absent player's turn

The humans play as usual. When the turn-change screen shows the absent
player's name ("RAY — Save Game / Start Turn"), leave the game there and
tell Claude Code, in this repo:

> take Poundmaker's turn — he said: finish the campus in Mistawasis, no wars

Tool by tool:

1. `hotseat_status` — confirms a hotseat session on Poundmaker's turn with
   the turn-change screen up. Every other game tool is **locked** until a
   stand-in is armed.
2. `begin_standin_turn("Poundmaker", note="finish the campus …")` — checks
   the seat, presses Start Turn (dismisses the screen, unpauses), writes the
   marker `~/.civ6-mcp/standin.json` and shows the note. A hotseat password
   on that player is bypassed.
3. `get_diary` — the diary for this game and player persists across
   sessions, so the agent knows what it, or the human, did before.
4. The normal turn: `get_game_overview`, `get_units`, `get_cities`, … every
   tool acts for Poundmaker only; the guard refuses anything else.
5. `end_turn` — runs the checks a real end turn runs (open diplomacy,
   incoming deals, hard blockers, units that still need orders), then ends
   the turn. The engine hands the seat to the next living human (or, after
   the last one, runs the AI civs and city-states and starts the next turn);
   the call returns once that player's turn-change screen is up and the
   game has been saved there as `<LEADER> <turn> <year> MCP` in the hotseat
   save folder. The marker stays active with `finished_turn` set, so every
   game tool is locked again until the next `begin_standin_turn`.

The next human sits down, sees their own turn-change screen, and plays.

If the game is at the main menu instead (someone quit, or the PC was
restarted), `list_saves` shows the hotseat saves and
`load_hotseat_save("POUNDMAKER 63 150 AD")` goes through Multiplayer >
Hotseat > Load Game > Start Game by itself and ends on the turn-change
screen (about 30 s for a 10-civ save). It also works from inside another
game (that game is left without saving). Load a save made **during the
absent player's turn, before anything was done in it**: the game's
autosave at turn change, or a save from the turn-change screen's own Save
Game button. A save made by the previous player still has that player's
turn active and `begin_standin_turn` refuses it.

## Better Balanced Game

The games run the [Better Balanced Game](https://civ6bbg.github.io/en_US/leaders_7.5.html)
mod (BBG 7.5.1, Steam Workshop 2865001760) with Better Balanced Maps. BBG
changes most leader and civ abilities, unique units and improvements,
policies and beliefs, so the agent cannot rely on vanilla Civ 6 knowledge.
`CLAUDE.md` tells it to look up the seated civilization's BBG entry before
planning a turn, let that kit drive its choices, and write the kit into the
player's diary on the first stand-in. The game tools read units, districts
and buildings from the running game's own tables, so their numbers already
include the mod. When BBG updates, change the version in the links in
`CLAUDE.md` and here.

## Guarantees

- **One seat.** The marker names one leader; before every game tool the
  server checks that the game's local player is that leader and that their
  turn is active (one Lua round trip, cached 20 s). Without an armed
  stand-in every game tool is refused. After `end_turn` the local player is
  someone else, so everything is refused until the next `begin_standin_turn`.
- **Information isolation.** Everything the tools return is scoped to the
  current player the way the game scopes it for a human: tiles through
  `PlayersVisibility`, other civilizations through `HasMet`, enemy units
  only on visible tiles. The diary query reads and stores only the local
  player's own figures. There is no arbitrary-Lua tool.
- **No process management.** The server never launches, kills or restarts
  the game; a silent bridge just means the game is loading or at a screen
  without the mod.
- **Always saved, always on the turn-change screen.** `end_turn` returns
  only after the save confirmed. If the save fails the message says so and
  the game is still on the next player's screen for a manual Save Game.

## Diplomacy with human players

Other civilizations are humans. Proposals (`propose_trade`, delegations,
friendship, alliances, peace) are queued and resolved by the other player on
their own turn; `propose_trade(mode="test")` asks the game's AI evaluator
and says nothing about what a human will accept. Incoming deals from humans
show up through `get_pending_trades` and must be answered before `end_turn`
lets the turn go.

## Known limitations

- Windows only: the front-end half of the bridge rides on a `Test.lua`
  context that exists only in the Windows build, and the game folder
  lookup uses the Windows shell. `CIV_MCP_MODS_DIR` and `CIV_MCP_LUA_LOG`
  override the mod and log locations.
- Popups that reference an engine event until their own close button runs
  (natural wonder, wonder built, era complete, tutorial advisor) cannot be
  closed from the bridge's UI context; `dismiss_popup` reports them and
  someone presses ESC in the game.
- A turn in which the stand-in is the last human of the round (the AI civs
  and city-states run before the next turn-change screen; `end_turn` waits
  up to four minutes) and a World Congress session have not been exercised
  live.
- A player's hotseat password is bypassed by `begin_standin_turn`.

## Development

```bash
uv run pytest -q          # unit tests; no game needed
```

Layout: `bridge.py` (transport), `hotseat.py` (load save, accept turn, end
turn, save), `standin.py` (marker and guard), `end_turn.py` (blocker
resolution and the end-turn sequence), `game_state.py` (typed game API),
`lua/` (Lua builders and parsers per domain), `narrate.py` (text for the
agent), `diary.py`, `server.py` (the MCP tools). `CLAUDE.md` is the agent's
playbook.

Engine facts the bridge depends on: `include()` re-reads files from disk on
every call; `Lua.log` cuts lines at about 2 KB (the mod splits long lines);
mod UI contexts start hidden and get no update ticks until shown; the front
end ignores a mod's `AddUserInterfaces`, hence the `Test.lua` shadow; the
game discovers new mods only at launch.

## License

MIT, see `LICENSE`.
