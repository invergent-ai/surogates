"""Guards on the per-channel constant lists.

Each of these lists is a hardcoded enumeration that a new platform must
join.  Omission fails silently at runtime, so the guards live here.
"""

from __future__ import annotations

from types import SimpleNamespace

import surogates.channels.platforms  # noqa: F401  (registers every platform)
from surogates.channels.constants import (
    ADAPTER_CHANNELS,
    API_CHANNEL,
    DIRECT_UI_CHANNELS,
    END_USER_CHANNELS,
    INBOX_NOTIFY_CHANNELS,
    INTERACTIVE_PROMPT_CHANNELS,
    SERVICE_ACCOUNT_CHANNELS,
    STUDIO_CHANNEL,
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


def test_whatsapp_is_enabled_without_any_config():
    # WhatsApp needs no per-kind flag: the routing table is the only gate
    # that matters, so a deployment must not have to hand-apply a
    # ConfigMap edit to turn the channel on.
    from surogates.channels.registry import ChannelRegistry
    from surogates.config import ChannelsSettings
    from surogates.channels.platforms.whatsapp import WhatsAppPlatform

    reg = ChannelRegistry()
    reg.register(WhatsAppPlatform())

    settings = SimpleNamespace(channels=ChannelsSettings())
    kinds = {p.kind for p in reg.enabled_platforms(settings)}
    assert "whatsapp" in kinds


def test_whatsapp_can_still_be_switched_off():
    # The escape hatch survives: enabled-by-default is not un-disableable.
    from surogates.channels.registry import ChannelRegistry
    from surogates.config import ChannelsSettings, WhatsAppChannelSettings
    from surogates.channels.platforms.whatsapp import WhatsAppPlatform

    reg = ChannelRegistry()
    reg.register(WhatsAppPlatform())

    settings = SimpleNamespace(
        channels=ChannelsSettings(whatsapp=WhatsAppChannelSettings(enabled=False)),
    )
    kinds = {p.kind for p in reg.enabled_platforms(settings)}
    assert "whatsapp" not in kinds


def test_studio_renders_its_own_conversation():
    # Outside DIRECT_UI_CHANNELS a scheduled run's result is dropped
    # instead of delivered to its parent conversation
    # (_resolve_loop_result_parent), and long code runs start posting a
    # channel heartbeat into a UI that already streams progress.
    assert STUDIO_CHANNEL in DIRECT_UI_CHANNELS
    assert API_CHANNEL in DIRECT_UI_CHANNELS
    assert "web" in DIRECT_UI_CHANNELS


def test_studio_authenticates_as_a_service_account():
    # Drives the prompt-injection detector's source profile: a studio
    # session carries no user_id, so it must not be screened as if a
    # logged-in end-user typed it.
    assert STUDIO_CHANNEL in SERVICE_ACCOUNT_CHANNELS
    assert API_CHANNEL in SERVICE_ACCOUNT_CHANNELS


def test_studio_is_not_an_end_user_channel():
    # The operator talking to their own agent is not a customer of it.
    # Inclusion would mint agent_users bindings and inflate every
    # Users-page engagement number with the operator's own testing.
    assert STUDIO_CHANNEL not in END_USER_CHANNELS
    assert STUDIO_CHANNEL not in INBOX_NOTIFY_CHANNELS
    assert STUDIO_CHANNEL not in ADAPTER_CHANNELS
    assert STUDIO_CHANNEL not in INTERACTIVE_PROMPT_CHANNELS


def test_studio_behaves_exactly_like_api_everywhere_but_naming():
    # The split exists to report operator chats separately, NOT to change
    # how they run. Any set that disagrees about the two is a behaviour
    # change smuggled in under a naming change.
    sets = {
        "ADAPTER_CHANNELS": ADAPTER_CHANNELS,
        "END_USER_CHANNELS": END_USER_CHANNELS,
        "INBOX_NOTIFY_CHANNELS": INBOX_NOTIFY_CHANNELS,
        "INTERACTIVE_PROMPT_CHANNELS": INTERACTIVE_PROMPT_CHANNELS,
        "SERVICE_ACCOUNT_CHANNELS": SERVICE_ACCOUNT_CHANNELS,
        "DIRECT_UI_CHANNELS": DIRECT_UI_CHANNELS,
    }
    disagree = [
        name
        for name, members in sets.items()
        if (API_CHANNEL in members) != (STUDIO_CHANNEL in members)
    ]
    assert not disagree, f"studio diverges from api in: {disagree}"


def test_other_kinds_still_default_off():
    # Only WhatsApp changes; Slack/Telegram/Website keep opt-in semantics.
    from surogates.config import ChannelsSettings

    cfg = ChannelsSettings()
    assert cfg.slack.enabled is False
    assert cfg.telegram.enabled is False
    assert cfg.website.enabled is False
    assert cfg.whatsapp.enabled is True
