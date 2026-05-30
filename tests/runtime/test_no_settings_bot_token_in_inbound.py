"""Plan 6 / Task 11 source-level regression.

The shared adapter pod must NOT read ``settings.slack.bot_token``
or ``settings.telegram.bot_token`` in the inbound resolution path
-- the process-wide token is the helm-mode artifact.  Tokens come
from the per-tenant credential vault via Task 6's
``resolve_channel_token``.

Scoped to the inbound handler classes (``SharedSlackInbound`` /
``SharedTelegramInbound``): a future refactor that re-introduces
``settings.<channel>.bot_token`` into the inbound path would let
a single tenant's token serve every workspace -- the exact
multi-tenant blast radius this plan eliminates.

The check is class-scoped via ``inspect.getsource`` because the
broader ``slack.py`` / ``telegram.py`` modules still contain the
legacy ``SlackAdapter`` / ``TelegramAdapter`` classes that read
process-wide settings (helm-mode path); those must keep working
until Plan 9 retires helm mode entirely.
"""

from __future__ import annotations

import inspect
import re

from surogates.channels.slack import SharedSlackInbound
from surogates.channels.telegram import SharedTelegramInbound


def test_shared_slack_inbound_does_not_read_settings_token():
    src = inspect.getsource(SharedSlackInbound)
    assert "settings.slack.bot_token" not in src
    # Catches re-introductions through any settings attribute
    # whose name contains 'bot_token'.
    assert re.search(r"settings.*bot_token", src) is None


def test_shared_telegram_inbound_does_not_read_settings_token():
    src = inspect.getsource(SharedTelegramInbound)
    assert "settings.telegram.bot_token" not in src
    assert re.search(r"settings.*bot_token", src) is None


def test_shared_slack_inbound_uses_resolve_channel_token():
    """Positive regression: the shared handler MUST go through
    resolve_channel_token (which routes to the canonical
    CredentialVault.resolve_ref entry point per Plan 2 Task 16)."""
    src = inspect.getsource(SharedSlackInbound)
    assert "resolve_channel_token" in src


def test_shared_telegram_inbound_uses_resolve_channel_token():
    src = inspect.getsource(SharedTelegramInbound)
    assert "resolve_channel_token" in src
