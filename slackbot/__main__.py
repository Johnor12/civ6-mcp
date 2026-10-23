"""`uv run --group slackbot python -m slackbot [--check]`."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from slackbot.config import load_players, settings_from_env
from slackbot.preflight import env_problems, slack_problems


def check() -> None:
    """Print the player table and the game state. No Slack needed."""
    from slackbot.game import BridgeGameChecker

    for i, p in enumerate(load_players(), 1):
        print(f"{i:2}. {p.name:18} {p.slack_id}  {p.leader}")
    print(asyncio.run(BridgeGameChecker().status()).description or "game: no answer")
    problems = env_problems(os.environ)
    if not problems:
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=os.environ["SLACK_BOT_TOKEN"])
        problems = asyncio.run(slack_problems(client, settings_from_env().channel_id))
    print("Slack: ready." if not problems else "Slack problems:\n- " + "\n- ".join(problems))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if "--check" in sys.argv:
        check()
        return
    from slackbot.slack_app import serve

    asyncio.run(serve(settings_from_env()))


if __name__ == "__main__":
    main()
