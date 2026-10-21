# Hotseat stand-in mode

Use civ6-mcp to play one absent player's turn in a local hotseat game, without
seeing anything that player could not see.

## The constraint: FireTuner is off in hotseat

civ6-mcp talks to the game only through FireTuner (TCP 4318). Civ 6 shuts that
listener down the moment a multiplayer session starts, and hotseat counts as
multiplayer. Verified on 2026-10-21 (Windows, build 1.0.12.68): the port is open
at the main menu and in single-player games, and closed as soon as a hotseat
save is opened — already in the staging room, before the first turn. There is no
`AppOptions.txt` switch for it, and the save-file loaders in the game's Lua
(`Network.LoadGame(..., ServerType.SERVER_TYPE_HOTSEAT)`) go through the same
session code.

So the server cannot drive a hotseat game directly. Instead it plays the absent
player's turn in a **single-player copy of the hotseat save** and the finished
turn is converted back.

## How a stand-in turn works

```
hotseat game (humans)                         single-player detour (agent)
─────────────────────                         ───────────────────────────
1. the game reaches the absent player's
   turn (PlayerChange screen shows their
   name). Click SAVE on that screen, or
   OK then Menu > Save Game.            ──►   2. hotseat_standin.py to-single
                                                 flips every other civ to AI,
                                                 writes Saves/Single/MCP_STANDIN_IN
                                                 and ~/.civ6-mcp/standin.json
                                              3. load MCP_STANDIN_IN as a single-
                                                 player game (Single Player > Load
                                                 Game, or load_game_save)
                                              4. agent plays the turn with the
                                                 normal tools; end_turn FINISHES
                                                 the turn (no End Turn is sent)
                                                 and saves MCP_STANDIN_OUT
6. load it via Multiplayer > Hotseat >  ◄──   5. hotseat_standin.py to-hotseat
   Load Game, Start Game, confirm the            restores the human flags and
   absent player's name on the                   writes Saves/Hotseat/<LEADER> <turn> MCP
   PlayerChange screen, press End Turn
```

Only the uncompressed save header changes in steps 2 and 5: each player slot
has an `ACTOR_AI_HUMAN` word (3 = human, 1 = AI) and an `IS_CURRENT_TURN` flag.
The compressed game state is copied byte for byte. The markers come from the
[pydt/civ6-save-parser](https://github.com/pydt/civ6-save-parser) project.

The AI never gets to act for the other civilizations: in single-player mode
the other players are AI only on paper, and the agent never ends the turn.
Nothing about their empires changes during the detour.

### Step by step

```bash
# 1. In the hotseat game, when it is e.g. Poundmaker's turn: save (any name).
# 2. Convert (names are looked up in Saves/Hotseat and Saves/Single):
uv run python scripts/hotseat_standin.py inspect "POUNDMAKER 63 150 AD"
uv run python scripts/hotseat_standin.py to-single "POUNDMAKER 63 150 AD" \
    --leader LEADER_POUNDMAKER --note "finish the campus in Mistawasis, no wars"

# 3. Start Claude Code in this repo (the MCP server reads the marker at start):
claude
#    > load_game_save("MCP_STANDIN_IN") — or load it from the game's menu —
#    > "take Poundmaker's turn"
#    The agent reads get_diary (its memory for this player), plays, and calls
#    end_turn, which saves MCP_STANDIN_OUT and reports "TURN N FINISHED".

# 4. Convert back (defaults come from the marker written in step 2):
uv run python scripts/hotseat_standin.py to-hotseat
#    writes Saves/Hotseat/POUNDMAKER 63 MCP.Civ6Save and deactivates the marker

# 5. Multiplayer > Hotseat > Load Game > "POUNDMAKER 63 MCP" > Start Game.
#    The PlayerChange screen must name the absent player; press OK, then End
#    Turn. The next human takes over as usual.
```

`inspect` prints every full civilization in a save with its human/AI flag and
which player's turn it is; run it on both the input and the output if anything
looks off. `status` shows the marker, `clear` removes it.

### Which save to convert

Convert a save made **during the absent player's turn, before anything was
done in it** — the PlayerChange screen has its own Save button for exactly
this. A save made by the previous player (the usual "LEADER TURN YEAR" saves)
still has that player's turn active; loading it would put the agent in the
wrong seat, and the stand-in guard refuses to act.

## What the server does in stand-in mode

The marker `~/.civ6-mcp/standin.json` (written by `to-single`, deactivated by
`to-hotseat`) switches the server into stand-in mode at startup and is re-read
on every tool call:

- **`end_turn` finishes instead of ending.** It runs the same checks as a real
  end turn (open diplomacy sessions, incoming deals, hard blockers such as
  empty production or an unchosen research), refuses while any unit still has
  moves, then saves the game as `MCP_STANDIN_OUT` (`--out-save` changes the
  name). `ACTION_ENDTURN` is never sent. The diary entry is written as usual.
- **Player guard.** Before every game tool runs, the server checks that the
  game's local player is the leader named in the marker and that the game is
  not a multiplayer session. Any mismatch is refused with a message that says
  which save to load. Lifecycle tools (`list_saves`, `load_game_save`, ...)
  stay available so the right save can be loaded.
- **Diary isolation and persistence.** Only the agent's own player row and its
  cities are written, never the other players' figures. The run id is fixed to
  `standin`, so `get_diary` finds the previous sessions' entries for this game
  and player (`~/.civ6-mcp/diary_<civ>_<seed>_standin.jsonl`). Every player who
  uses a stand-in gets their own file.
- **No `run_lua`.** The escape hatch is removed in stand-in mode (set
  `CIV_MCP_STANDIN_ALLOW_LUA=1` to keep it for debugging).
- **No automatic game restarts.** The connection-loss and AI-hang recovery
  paths that kill and relaunch the game are disabled.
- The web dashboard API binds to `127.0.0.1` (in every mode; set
  `CIV_MCP_WEB_HOST` to expose it).

`get_game_overview` prints a STAND-IN MODE paragraph with the player's note and
whether the turn was already finished, so an agent resuming after a context
reset knows where it stands.

## Information isolation

Everything the tools return is scoped to the current player the way the game
scopes it for a human: tiles through `PlayersVisibility`, other civilizations
through `HasMet`, enemy units only on visible tiles. Figures that the game shows
for met civilizations (score, yields, military strength, city counts) are
shown; this game also runs the Extended Diplomacy Ribbon mod, which shows the
same numbers in the UI. Victory demographics now cover met civilizations only.
In a team game, teammates share visibility, so the agent sees what a teammate
would see — the same as the human in that seat.

Things that are not the agent's business are not fetched: the omniscient map
dump for the web dashboard only runs after a real turn advance, which never
happens in stand-in mode, and the per-turn diary snapshot keeps only the
agent's row. Leader "agendas" and AI relationship modifiers still appear in
`get_diplomacy`; in a hotseat game those belong to human players and mean
nothing.

## Diplomacy with human players

Other civilizations are humans. Proposals (`propose_trade`, delegations,
friendship, alliances, peace) are queued and resolved by the other player on
their own turn; `propose_trade(mode="test")` asks the game's AI evaluator and
says nothing about what a human will accept. Incoming deals from humans show
up through `get_pending_trades` at the start of the turn. World Congress: if the
congress is in session during the stand-in turn, use `get_world_congress` and
`queue_wc_votes`; end-of-turn blockers from the congress are reported like
any other blocker. (The congress had not convened in the test game — the
narration now says so instead of "fires this turn".)

## Mods

The test game runs Better Balanced Game 7.4.6, Better Balanced Map 1.39.5 and
several UI mods. All tools read unit, district and building data from the
game's `GameInfo` tables, so modded units (Okihtcitaw, Skirmisher at 30 CS, the
Preserve district, ...) show up with their modded values. The combat table in
AGENTS.md is base-game reference material; trust the numbers the tools print.
Loading a converted save activates the mods the save requires (the game checks
them before loading).

## Verified on 2026-10-21

- Tuner port: open at the main menu and in single-player; closed in the
  hotseat staging room; reopened after returning to the main menu with ESC.
- A 10-civ hotseat save (turn 63, two teams, BBG) converted with `to-single`
  loaded as a single-player game with Poundmaker as the only human and the
  turn active; `get_game_overview`, `get_units`, `get_cities`,
  `get_tech_civics`, `get_diplomacy`, `get_notifications`,
  `get_victory_progress` and `get_world_congress` all worked.
- `Network.SaveGame` from that game wrote a single-player save; `to-hotseat`
  restored all nine other human flags and the current-turn flag, and `inspect`
  shows the result identical in layout to the original.
- Not verified: loading the converted-back save through the hotseat menu and
  pressing End Turn there. Mouse clicks sent by the OCR helper are ignored by
  the staging room on this machine (keyboard ESC works), so that step was
  left to a person. Do it once with a copy before relying on the workflow.

## Known limitations

- The detour needs a person at the machine for the two save conversions and
  the hotseat load. The conversion commands are one-liners; the OCR launcher
  could be taught the hotseat menus later.
- A turn in which the World Congress session itself must be resolved has not
  been exercised.
- If the absent player has a hotseat password, nothing here needs it — the
  single-player copy has no password prompt. Players should know that.
- Scores and "RANK" lines compare the agent's civilization only with met
  civilizations, same as the in-game ribbon.
