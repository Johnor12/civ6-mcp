# civ6-mcp

An MCP server that lets LLM agents play full games of Civilization VI.

Connect any MCP-compatible client — Claude Code, Codex, Gemini CLI, or your own — to a running Civ 6 game. The agent reads game state, moves units, manages cities, conducts diplomacy, and ends turns, all through the game's own rule-enforcing APIs. No cheats, no vision model required.

<!-- TODO: Add screenshot or GIF of agent playing -->

## Capabilities

76 tools covering the full gameplay loop:

- **Units** — list, move, attack, fortify, found cities, build improvements, promote, upgrade
- **Cities** — inspect, set production, purchase units/buildings with gold, manage focus
- **Map** — explore terrain, resources, fog of war; get settle and district placement advice
- **Research** — browse tech and civic trees, set research targets
- **Diplomacy** — relationships, modifiers, delegations, embassies, alliances, peace deals
- **Trade** — propose and respond to deals, manage trade routes and destinations
- **Government** — swap policy cards, change governments, choose era dedications
- **Governors** — appoint, assign to cities, promote
- **Religion** — found pantheons and religions, select beliefs, track spread
- **Great People** — recruit, patronize, reject
- **World Congress** — vote on resolutions, manage diplomatic favor
- **Victory** — track progress across all victory conditions
- **Game lifecycle** — save, load, launch, restart, kill

Every turn, `end_turn` takes before/after snapshots and reports what happened: units damaged, cities grew, production completed, threats spotted near your cities.

## Hotseat stand-in (this fork)

This fork adds a **stand-in mode** for local hotseat games: when one of the human players is away, the agent plays their turn for them, inside the live hotseat session. Civ 6 switches FireTuner off in hotseat, so the fork ships a tiny UI mod, **MCP Bridge**, through which the server drives the game instead (a command file the mod polls, replies through `Lua.log`; ~0.2 s per call, works everywhere the tuner does and in hotseat too):

```bash
uv run python scripts/install_bridge_mod.py   # once; then (re)start Civ 6 and the MCP server
```

When the turn-change screen shows the absent player's name, tell the agent "take Poundmaker's turn" (plus whatever that player asked for). It calls `begin_standin_turn`, plays with the normal tools, and `end_turn` ends the turn for real, hands the game to the next player's turn-change screen and saves there. From the main menu, `load_hotseat_save` opens a hotseat save by itself.

Every game tool is locked in a hotseat session until a stand-in is armed, refuses to act for any other player afterwards, the diary persists per game and player, and `run_lua` is removed. Full workflow, guarantees and limitations: [docs/hotseat-standin.md](docs/hotseat-standin.md). The older single-player-copy detour (`scripts/hotseat_standin.py`) still works where the mod cannot be used.

## Quick start

### 1. Configure Civ 6

Enable the FireTuner debug interface and configure recommended settings:

| Setting | Value | Why |
|---------|-------|-----|
| **Tuner** | Enabled | Required — opens the TCP debug port the MCP server connects to. Disables achievements. |
| **Auto End Turn** | Disabled | The agent controls when turns end. Auto-end interferes with the blocker resolution flow. |
| **Windowed mode** | Recommended | Lets you watch the game while the agent plays. Required for OCR-based save loading. |

**Windows:** All three settings are available in the in-game Options menu. The Tuner setting appears as "Tuner (disables achievements)" under gameplay options.

**macOS:** The Tuner setting is not exposed in the menu. Edit `AppOptions.txt` directly and set `EnableTuner 1`:

```
~/Library/Application Support/Sid Meier's Civilization VI/Firaxis Games/Sid Meier's Civilization VI/AppOptions.txt
```

**Linux:** Same as macOS — edit `AppOptions.txt` directly and set `EnableTuner 1`:

```
~/.local/share/aspyr-media/Sid Meier's Civilization VI/AppOptions.txt
```

<details>
<summary><strong>Windows: additional setup</strong></summary>

**The Civ 6 SDK is not required** — the tuner server is built into the game; `EnableTuner 1` (the in-game "Tuner" option) is enough. Install the SDK only if you want its FireTuner GUI.

**Important notes:**
- The tuner is **disabled in multiplayer, including local hotseat**. Install the MCP Bridge mod (`uv run python scripts/install_bridge_mod.py`) and the server drives the game through it instead; see `docs/hotseat-standin.md`.
- A second monitor is fine: OCR captures the game window itself (not the primary screen) and clicks are mapped over the whole virtual desktop. Keep the game on one monitor and don't move it mid-session.
- Close `FireTuner.exe` (the SDK's GUI tool) before running civ6-mcp — the game only allows **one** tuner connection at a time
- Do **not** run from WSL — the network bridging between WSL2 and Windows is unreliable and the tuner server locks up after failed connections
- If the connection fails, **restart the game** — the tuner often hangs after a bad handshake and won't recover until the process is recycled
</details>

<details>
<summary><strong>Linux: additional notes</strong></summary>

- The **native Linux port** is required — the FireTuner debug interface is built into the native binary. Proton/Wine builds do not expose it.
- The game runs as a single `Civ6` process launched via Steam Linux Runtime (scout-on-soldier).
- GUI automation (OCR-based menu navigation) requires **X11**. On Wayland, the game typically runs under XWayland which should work, but a native X11 session is most reliable.
</details>

Restart Civ 6. The game will listen on TCP port 4318 for connections.

### 2. Install

```bash
git clone https://github.com/lmwilki/civ6-mcp.git
cd civ6-mcp
uv sync
```

For GUI automation features (screenshot, OCR-based menu navigation):

```bash
# macOS
uv pip install 'civ6-mcp[launcher-macos]'

# Windows (uses built-in Windows OCR — no external binaries needed)
uv pip install 'civ6-mcp[launcher-windows]'

# Linux (Ubuntu/Debian)
sudo apt install xdotool tesseract-ocr
uv pip install 'civ6-mcp[launcher-linux]'
```

### 3. Test the connection

With Civ 6 running and a game loaded:

```bash
uv run python scripts/test_connection.py
```

You should see a successful handshake and a list of Lua states (GameCore_Tuner, InGame, etc.).

### 4. Configure your MCP client

The server runs over stdio. Point your client at it:

<details>
<summary><strong>Claude Code</strong></summary>

The repo includes `.mcp.json` — detected automatically:

```bash
cd civ6-mcp
claude
```
</details>

<details>
<summary><strong>Claude Desktop</strong></summary>

Add to your config file:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "civ6": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/civ6-mcp", "civ-mcp"]
    }
  }
}
```
</details>

<details>
<summary><strong>Codex</strong></summary>

Add to `.codex/config.toml` in the project root:

```toml
[mcp_servers.civ6]
command = "uv"
args = ["run", "--directory", "/path/to/civ6-mcp", "civ-mcp"]
```
</details>

<details>
<summary><strong>Gemini CLI</strong></summary>

Add to `.gemini/settings.json` in the project root:

```json
{
  "mcpServers": {
    "civ6": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/civ6-mcp", "civ-mcp"]
    }
  }
}
```
</details>

<details>
<summary><strong>Other MCP clients</strong></summary>

The server speaks stdio JSON-RPC:

```bash
uv run civ-mcp
```
</details>

### 5. Play

Load a game in Civ 6, connect your client, and try:

```
Play my Civ 6 game. Start by getting an overview, then check units and
cities, and play through the turn.
```

The agent will orient with `get_game_overview`, scan the map for threats, move units, set production and research, handle diplomacy, and end the turn.

## As a benchmark

Civilization VI is a compelling environment for evaluating LLM strategic reasoning. Games run 300+ turns with compounding decisions, incomplete information, and multiple competing objectives — a significant step up from single-turn or short-horizon tasks.

- **Multi-turn planning** — decisions compound over hundreds of turns with delayed payoffs
- **Incomplete information** — fog of war, hidden AI intentions, unexplored map
- **Resource management** — balancing gold, production, science, culture, faith, and military
- **Opponent modeling** — reading diplomatic signals, anticipating AI behavior
- **Strategic adaptation** — responding to threats, shifting priorities mid-game

The MCP interface provides a clean abstraction: the model receives narrated game state as text and responds with tool calls. All game rules are enforced by the engine. A companion web app lets you replay sessions turn by turn.

## How it works

```
Claude / Any MCP Client
    |  stdio (JSON-RPC)
    v
MCP Server (Python)    <- 70+ tools
    |
    |  Generates Lua code at runtime
    |  TCP :4318
    v
Civilization VI        <- Game is the TCP server
```

The server maintains a persistent TCP connection to Civ 6 via the FireTuner debug protocol. It generates Lua code, executes it inside the game's two Lua VMs (GameCore for reading state, InGame for issuing commands), parses the output, and returns narrated text to the LLM.

The repo includes an [AGENTS.md](AGENTS.md) playbook (symlinked as `CLAUDE.md` for Claude Code) with detailed instructions for agents: turn loop, combat, diplomacy, common pitfalls. See the [devlog](docs/devlog/) for the full development story, including reverse-engineering the FireTuner protocol and the many API quirks discovered along the way.

## Requirements

- **macOS, Windows, or Linux** with Civilization VI (Steam version, Gathering Storm DLC)
- **Python 3.12+** with [uv](https://docs.astral.sh/uv/)
- An **MCP client** (Claude Code, Codex, Gemini CLI, or any MCP-compatible client)

## License

MIT
