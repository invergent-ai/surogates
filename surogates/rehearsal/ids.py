"""Identifiers a rehearsal world hands out.

Every id starts with ``999``: E.164 leaves country code 999 unassigned,
Meta's object ids never start with it and a Telegram username must start with
a letter, so a rehearsal id can never be a real phone number, phone-number id
or bot, and the runtime tells one apart without a lookup.  A rehearsal bot's
token starts with its 13-digit id, longer than any id Telegram has issued; a
Slack one is ``xoxb-`` and 16 digits where Slack puts a team's id (Slack's
are shorter), and a Slack app id starts with ``A``.  A Discord id is a 64-bit snowflake, so a 20-digit one
starting with 999 (over 2^64) cannot exist, and a real bot token starts with
the base64 of its id, never with a digit.  A Teams App ID is a GUID
Entra issues as version 4; a rehearsal one is version 0, which Entra never
issues.  A website key is ``surg_wk_`` and 44 random characters; a
rehearsal one is ``surg_wk_999r`` and 48 hex digits, a length no real key has.
"""

from __future__ import annotations

import re
import secrets

__all__ = [
    "REHEARSAL_ID_PREFIX", "is_rehearsal_id", "new_bot_token", "new_bot_username",
    "new_phone_number_id", "new_slack_app_id", "new_slack_bot_token", "new_slack_user_id",
    "new_teams_app_id", "new_teams_user_id", "new_telegram_user_id", "new_wa_id",
    "is_rehearsal_bot_token", "is_rehearsal_discord_token", "new_discord_bot_token",
    "new_discord_id", "is_rehearsal_slack_token", "is_rehearsal_teams_app",
    "new_website_key", "is_rehearsal_website_key",
]

REHEARSAL_ID_PREFIX = "999"


def _digits(count: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(count))


def new_phone_number_id() -> str:
    return REHEARSAL_ID_PREFIX + _digits(13)


def new_wa_id() -> str:
    return REHEARSAL_ID_PREFIX + _digits(9)


def new_bot_username() -> str:
    return f"{REHEARSAL_ID_PREFIX}{_digits(7)}_bot"


def new_bot_token() -> str:
    return f"{REHEARSAL_ID_PREFIX}{_digits(10)}:{secrets.token_urlsafe(26)}"


def new_telegram_user_id() -> str:
    return REHEARSAL_ID_PREFIX + _digits(9)


def new_slack_app_id() -> str:
    return REHEARSAL_ID_PREFIX + _digits(8)


def new_slack_bot_token() -> str:
    return f"xoxb-{REHEARSAL_ID_PREFIX}{_digits(13)}-{secrets.token_hex(12)}"


def new_slack_user_id() -> str:
    return f"U{REHEARSAL_ID_PREFIX}{_digits(9)}"


def new_discord_id() -> str:
    return REHEARSAL_ID_PREFIX + _digits(17)


def new_discord_bot_token(app_id: str) -> str:
    return f"{app_id}.{secrets.token_urlsafe(27)}"


def new_website_key() -> str:
    return f"surg_wk_999r{secrets.token_hex(24)}"


def new_teams_app_id() -> str:
    h = secrets.token_hex(16)
    return f"999{h[:5]}-{h[5:9]}-0{h[9:12]}-8{h[12:15]}-{h[15:27]}"


def new_teams_user_id() -> str:
    return f"29:{REHEARSAL_ID_PREFIX}{secrets.token_hex(12)}"


_BOT_TOKEN = re.compile(r"999\d{10}:.+")
_DISCORD_TOKEN = re.compile(r"999\d{17}\.[A-Za-z0-9_-]+")
_WEBSITE_KEY = re.compile(r"surg_wk_999r[0-9a-f]{48}")
_SLACK_TOKEN = re.compile(r"xoxb-999\d{13}-[0-9a-f]{24}")
_TEAMS_APP = re.compile(r"999[0-9a-f]{5}-[0-9a-f]{4}-0[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}")


def is_rehearsal_bot_token(token: str) -> bool:
    return _BOT_TOKEN.fullmatch(str(token or "")) is not None


def is_rehearsal_discord_token(token: str) -> bool:
    return _DISCORD_TOKEN.fullmatch(str(token or "")) is not None


def is_rehearsal_website_key(key: str) -> bool:
    return _WEBSITE_KEY.fullmatch(str(key or "")) is not None


def is_rehearsal_slack_token(token: str) -> bool:
    return _SLACK_TOKEN.fullmatch(str(token or "")) is not None


def is_rehearsal_teams_app(app_id: str) -> bool:
    return _TEAMS_APP.fullmatch(str(app_id or "")) is not None


def is_rehearsal_id(value: str) -> bool:
    """Whether *value* may name a rehearsal account, so routing asks the runtime DB first."""
    value = str(value or "")
    return value.startswith(REHEARSAL_ID_PREFIX) or is_rehearsal_website_key(value)
