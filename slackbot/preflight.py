"""Start-up checks: tokens are set and the Slack app has the scopes the bot needs."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

REQUIRED_SCOPES = frozenset(
    {"channels:history", "chat:write", "im:history", "im:write", "files:write", "users:read"}
)


def env_problems(env: Mapping[str, str]) -> list[str]:
    """Missing or swapped tokens."""
    problems = []
    for name, prefix in (("SLACK_BOT_TOKEN", "xoxb-"), ("SLACK_APP_TOKEN", "xapp-")):
        value = env.get(name, "").strip()
        if not value:
            problems.append(f"{name} is not set.")
        elif not value.startswith(prefix):
            problems.append(f"{name} should start with '{prefix}'.")
    return problems


def parse_scopes(header: str | None) -> set[str]:
    return {s.strip() for s in (header or "").split(",") if s.strip()}


def missing_scopes(granted: Iterable[str]) -> list[str]:
    return sorted(REQUIRED_SCOPES - set(granted))


def _header(headers: Any, name: str) -> str | None:
    for key, value in dict(headers or {}).items():
        if str(key).lower() == name:
            return str(value)
    return None


async def slack_problems(client: Any, channel_id: str | None = None) -> list[str]:
    """Ask Slack who the token is, which scopes it has and if it can read the channel.

    An empty list means ready.
    """
    try:
        resp = await client.auth_test()
    except Exception as e:  # noqa: BLE001 - any failure here means the bot cannot run
        return [f"Slack rejected the bot token (auth.test: {e})."]
    header = _header(getattr(resp, "headers", None), "x-oauth-scopes")
    if header is None:
        return ["Slack did not report the token's scopes, so they could not be checked."]
    missing = missing_scopes(parse_scopes(header))
    if missing:
        return [
            "The Slack app is missing bot scopes: "
            + ", ".join(missing)
            + ". Add them under OAuth & Permissions and reinstall the app."
        ]
    if channel_id:
        try:
            await client.conversations_history(channel=channel_id, limit=1)
        except Exception as e:  # noqa: BLE001
            return [
                f"The bot cannot read channel {channel_id} ({e}). Invite it to the channel. "
                "A private channel also needs the groups:history scope."
            ]
    return []
