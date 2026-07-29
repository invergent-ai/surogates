"""Guards on the per-channel constant lists.

Each of these lists is a hardcoded enumeration that a new platform must
join.  Omission fails silently at runtime, so the guards live here.
"""

from __future__ import annotations

import surogates.channels.platforms  # noqa: F401  (registers every platform)
from surogates.channels.constants import (
    ADAPTER_CHANNELS,
    END_USER_CHANNELS,
    INTERACTIVE_PROMPT_CHANNELS,
)
from surogates.channels.memory_boundary import MANAGED_CHANNELS
from surogates.channels.registry import registry


def test_every_registered_platform_has_a_delivery_adapter():
    # An outbox row for a channel outside ADAPTER_CHANNELS is never claimed
    # and sits pending forever (store.py:1148).
    missing = {p.kind for p in registry.all_platforms()} - set(ADAPTER_CHANNELS)
    assert not missing, f"registered but not in ADAPTER_CHANNELS: {sorted(missing)}"


def test_whatsapp_can_render_input_prompts():
    # Outside INTERACTIVE_PROMPT_CHANNELS, _build_channel_payload leaves the
    # payload empty and store.py writes NO outbox row at all — the user sees
    # nothing and the session parks for 30 minutes.
    assert "whatsapp" in INTERACTIVE_PROMPT_CHANNELS


def test_whatsapp_is_an_end_user_channel():
    assert "whatsapp" in END_USER_CHANNELS


def test_whatsapp_has_a_memory_boundary():
    # Fail-open otherwise: session_memory_boundary returns None and WhatsApp
    # conversations share the per-user memory partition with web sessions.
    assert "whatsapp" in MANAGED_CHANNELS
