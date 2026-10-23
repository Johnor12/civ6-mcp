"""Slack message text and blocks."""

from __future__ import annotations

from typing import Any

from slackbot.config import Player
from slackbot.state import Request

HELP = (
    "Send me a DM with what Claude should do on your turn, for example "
    "`settle near the coast, research Bronze Working next, no wars`.\n"
    "I run it only when it is your turn in the channel and in the game.\n"
    "Other commands: `status`, `takeover <prompt>` (sets the prompt for an open takeover request)."
)


def safe(text: str) -> str:
    """Stop agent text from pinging people or the channel."""
    return text.replace("<!", "<​!").replace("<@", "<​@")


def mention(slack_id: str) -> str:
    return f"<@{slack_id}>"


def approval_blocks(
    req: Request, player: Player, approvers: set[str], needed: int
) -> list[dict[str, Any]]:
    who = ", ".join(mention(u) for u in sorted(approvers)) or "nobody yet"
    text = (
        f"{mention(player.slack_id)} the 2-hour limit is over. Claude can take your turn "
        f"({player.leader}).\n"
        f"*Prompt:* {safe(req.prompt)}\n"
        f"*Approvals:* {who}. Needs {needed} players, or {player.name} alone.\n"
        "Players can change the prompt with a DM: `takeover <prompt>`."
    )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": "civ_approve",
                    "value": req.id,
                }
            ],
        },
    ]


def closed_text(req: Request, player: Player, note: str) -> str:
    return f"Takeover request for {player.name} ({player.leader}): {note}"
