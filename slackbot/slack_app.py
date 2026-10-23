"""Slack Socket Mode wiring. Needs the `slackbot` dependency group."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from slackbot.bot import CivBot
from slackbot.capture import DesktopCapture
from slackbot.config import Settings, load_players, settings_from_env
from slackbot.game import BridgeGameChecker
from slackbot.preflight import env_problems, slack_problems
from slackbot.runner import AgentRunner
from slackbot.state import Store
from slackbot.turn_tracker import ChannelMessage

log = logging.getLogger(__name__)


class SlackClientPort:
    """SlackPort on top of slack_sdk's AsyncWebClient."""

    def __init__(self, client: Any, channel: str) -> None:
        self.client = client
        self.channel = channel

    async def history(self, oldest: str) -> list[ChannelMessage]:
        out: list[ChannelMessage] = []
        cursor = None
        while True:
            resp = await self.client.conversations_history(
                channel=self.channel, oldest=oldest, limit=200, cursor=cursor
            )
            out += [
                ChannelMessage(m["ts"], m.get("user"), m.get("text", ""))
                for m in resp.get("messages", [])
            ]
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return out

    async def post(self, text, *, thread_ts=None, blocks=None) -> str:
        resp = await self.client.chat_postMessage(
            channel=self.channel, text=text, thread_ts=thread_ts, blocks=blocks
        )
        return resp["ts"]

    async def update(self, ts, text, blocks=None) -> None:
        await self.client.chat_update(channel=self.channel, ts=ts, text=text, blocks=blocks or [])

    async def dm(self, user_id, text) -> None:
        im = await self.client.conversations_open(users=user_id)
        await self.client.chat_postMessage(channel=im["channel"]["id"], text=text)

    async def upload(self, path: Path, *, thread_ts, title) -> None:
        await self.client.files_upload_v2(
            channel=self.channel, file=str(path), title=title, thread_ts=thread_ts
        )


def build_app(settings: Settings) -> tuple[Any, CivBot]:
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])
    bot = CivBot(
        settings=settings,
        players=load_players(),
        store=Store(settings.db_path),
        slack=SlackClientPort(app.client, settings.channel_id),
        game=BridgeGameChecker(),
        runner=AgentRunner(settings),
        capture=DesktopCapture(settings.repo_dir / "slackbot" / "captures"),
    )
    tasks: set[asyncio.Task] = set()

    def spawn(coro) -> None:
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)
        t.add_done_callback(lambda t: t.cancelled() or t.exception() and log.error("task failed: %r", t.exception()))

    @app.event("message")
    async def on_message(event, say):  # noqa: ANN001
        if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"):
            return
        spawn(bot.handle_dm(event["user"], event.get("text", "")))

    @app.action("civ_approve")
    async def on_approve(ack, body, client):  # noqa: ANN001
        await ack()
        user = body["user"]["id"]
        outcome = await bot.handle_approve(user, body["actions"][0]["value"])
        await client.chat_postEphemeral(
            channel=settings.channel_id, user=user, text=outcome.reply
        )
        if outcome.job is not None:
            spawn(outcome.job)

    return app, bot


async def poll_loop(bot: CivBot, interval: float) -> None:
    while True:
        try:
            await bot.tick()
        except Exception:  # noqa: BLE001 - keep polling after a Slack hiccup
            log.exception("tick failed")
        await asyncio.sleep(interval)


async def serve(settings: Settings | None = None) -> None:
    from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler

    settings = settings or settings_from_env()
    problems = env_problems(os.environ)
    if problems:
        raise SystemExit("Cannot start:\n- " + "\n- ".join(problems))
    app, bot = build_app(settings)
    problems = await slack_problems(app.client, settings.channel_id)
    if problems:
        raise SystemExit("Cannot start:\n- " + "\n- ".join(problems))
    asyncio.create_task(poll_loop(bot, settings.poll_interval_s))
    await AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start_async()
