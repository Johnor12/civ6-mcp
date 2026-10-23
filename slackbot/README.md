# slackbot

Lets a player who is away DM a prompt that Claude Code uses to play their civ turn.
Runs on the civ machine, in the logged-in Windows desktop session (screen capture needs it).

## What it does

- Reads `#proj-diplomatic-relations` and finds the current player: the last short message
  that @-mentions exactly one player (`turn_tracker.py`). The order is `players.toml`.
- A DM starts a run only if the sender owns the turn in Slack **and** the game shows that
  player's turn-change screen (`rules.authorize`).
- A run is `claude -p` in the repo root, limited to the `mcp__civ6` tools. The prompt goes in
  on stdin. The marker file `~/.civ6-mcp/standin.json` proves whether the turn really ended.
- After a run: DM the player Claude's answer, @ the next player, post saves, screenshots
  and the recording in a thread.
- Timer: ping at 1 hour. At 2 hours a takeover request with an Approve button opens. The
  current player alone, or 3 turn-order players, can approve. `takeover <prompt>` in a DM
  changes the prompt and clears approvals. A request expires after 30 minutes; after that,
  `takeover <prompt>` opens a new one.
- During a run the bot does not probe the game: a second bridge connection makes the mod
  skip the agent's commands.
- `begin_standin_turn` saves `<LEADER> <turn> PRE MCP` before Start Turn.

## Setup

1. Create the Slack app (Socket Mode on). Scopes: `channels:history`, `chat:write`,
   `im:history`, `im:write`, `files:write`, `users:read`. Subscribe to `message.im`.
   Enable Interactivity. Invite the bot to the channel.
2. Install `ffmpeg` and put it on `PATH` (recording).
3. Set `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`.
4. Check the table and the game: `uv run --group slackbot python -m slackbot --check`
5. Start: `uv run --group slackbot python -m slackbot`

Optional env: `CIV_CHANNEL_ID`, `CIV_HISTORY_START_TS`, `CLAUDE_BIN`, `CIV_RUN_TIMEOUT_S`,
`CIV_PING_AFTER_S`, `CIV_WARN_AFTER_S`, `CIV_APPROVALS_NEEDED`, `CIV_APPROVAL_TTL_S`.

## Tests

`uv run --group slackbot pytest slackbot` (no game, no Slack).
