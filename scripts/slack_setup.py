"""Which Slack bot scopes the bot needs, and a check against the ones it has."""

from __future__ import annotations

REQUIRED_SCOPES = frozenset({"app_mentions:read", "im:history", "im:write", "chat:write"})
# Without these a feature is switched off, not the whole bot.
OPTIONAL_SCOPES = {"files:write": "screenshots are not uploaded"}


def parse_scopes(header: str | None) -> set[str]:
    return {s.strip() for s in (header or "").split(",") if s.strip()}


def scope_problems(granted: set[str]) -> tuple[list[str], list[str]]:
    """(fatal, warnings): scopes the bot cannot run without, and ones it can."""
    fatal = [f"missing bot scope {s}" for s in sorted(REQUIRED_SCOPES - granted)]
    warnings = [f"missing bot scope {s}: {what}" for s, what in OPTIONAL_SCOPES.items() if s not in granted]
    return fatal, warnings
