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
  and screenshots (before, up to 2 during, after) in a thread.
- Timer: ping at 1 office hour. At 2 office hours a takeover request with an Approve button opens. The
  current player alone, or 3 turn-order players, can approve. `takeover <prompt>` in a DM
  changes the prompt and clears approvals. A request expires after 30 minutes; after that,
  `takeover <prompt>` opens a new one.
- During a run the bot does not probe the game: a second bridge connection makes the mod
  skip the agent's commands.
- `begin_standin_turn` saves `<LEADER> <turn> PRE MCP` before Start Turn.

Office hours count for the timer and for request expiry: Monday to Thursday 10:00-16:30,
Friday 10:00-15:00, New York time (`slackbot/hours.py`). Nights and weekends are skipped,
so a turn left at 16:00 Thursday has 30 office minutes used at 10:00 Friday. Holidays are not
handled. `CIV_TZ` changes the zone and `CIV_OFFICE_HOURS=off` turns the limit off.

## Setup

The Slack app is `civ6` (App ID `A0CBNQLDTP0`), created by John. `manifest.yml` holds its
settings: Socket Mode, Interactivity, the `message.im` event, the Messages tab and these bot
scopes: `channels:history`, `chat:write`, `im:history`, `im:write`, `files:write`, `users:read`.

1. Compare the app with `manifest.yml` (App Manifest page). Fix any difference.
2. Get the app installed in the workspace (needs approval). Then copy the Bot User OAuth
   Token (`xoxb-`) from OAuth & Permissions. Invite the bot to `#proj-diplomatic-relations`.
3. On the civ machine, set two user environment variables (Windows `setx`), then open a new
   terminal. Never commit them or paste them in Slack.
   ```
   setx SLACK_BOT_TOKEN "xoxb-..."
   setx SLACK_APP_TOKEN "xapp-..."
   ```
4. Check the table, the game and Slack: `uv run --group slackbot python -m slackbot --check`
   The Slack part reports bad tokens, missing scopes and a channel the bot cannot read.
5. Start: `uv run --group slackbot python -m slackbot` (it runs the same Slack check and
   stops with a clear message if something is wrong).

Optional env: `CIV_CHANNEL_ID`, `CIV_HISTORY_START_TS`, `CLAUDE_BIN`, `CIV_RUN_TIMEOUT_S`,
`CIV_PING_AFTER_S`, `CIV_WARN_AFTER_S`, `CIV_APPROVALS_NEEDED`, `CIV_APPROVAL_TTL_S`.

## Tests

`uv run --group slackbot pytest slackbot` (no game, no Slack).
