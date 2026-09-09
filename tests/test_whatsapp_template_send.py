"""Sending an approved template as a check-in opener, and the delivery axis."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa

from surogates.channels.platforms.whatsapp import WhatsAppPlatform
from surogates.db.models import DeliveryOutbox


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _item(payload):
    return type("Item", (), {
        "destination": {"wa_id": "40746148303", "phone_number_id": "127"},
        "payload": payload,
    })()


@pytest.mark.asyncio
async def test_a_template_payload_sends_a_template_not_text(monkeypatch):
    # Free-form text outside the 24h service window is rejected with 131047,
    # which is a permanent error — the opener would be dropped with nothing
    # surfaced to the operator.
    sent = {}

    async def _fake_send(client, *, token, phone_number_id, payload, api_version):
        sent.update(payload)
        return "wamid.1", None

    monkeypatch.setattr(
        "surogates.channels.platforms.whatsapp.send_message", _fake_send,
    )
    result = await WhatsAppPlatform().send(
        _item({"template": {"name": "daily_checkin", "language": "en_US"}}),
        creds={"access_token": "T", "api_version": "v23.0"},
    )
    assert result.success
    assert sent["type"] == "template"
    assert sent["template"]["name"] == "daily_checkin"
    assert sent["template"]["language"] == {"code": "en_US"}
    assert "text" not in sent


@pytest.mark.asyncio
async def test_an_ordinary_payload_still_sends_text(monkeypatch):
    sent = {}

    async def _fake_send(client, *, token, phone_number_id, payload, api_version):
        sent.update(payload)
        return "wamid.2", None

    monkeypatch.setattr(
        "surogates.channels.platforms.whatsapp.send_message", _fake_send,
    )
    await WhatsAppPlatform().send(
        _item({"content": "hello"}), creds={"access_token": "T"},
    )
    assert sent["type"] == "text"


@pytest.mark.asyncio
async def test_a_failed_template_send_is_reported_not_swallowed(monkeypatch):
    async def _fake_send(client, *, token, phone_number_id, payload, api_version):
        return None, "(#131047) Re-engagement message"

    monkeypatch.setattr(
        "surogates.channels.platforms.whatsapp.send_message", _fake_send,
    )
    result = await WhatsAppPlatform().send(
        _item({"template": {"name": "daily_checkin", "language": "en_US"}}),
        creds={"access_token": "T"},
    )
    assert result.success is False
    assert "131047" in (result.error or "")


class PermissionStub:
    """An identity lookup whose answer the test can change mid-run.

    The point under test is that ``send_queued_openers`` consults the lookup
    *again* at send time, so the stub must be mutable after materialisation.
    """

    def __init__(self, status="granted"):
        self.status = status

    async def __call__(self, org_id, platform, user_id):
        from types import SimpleNamespace

        return SimpleNamespace(
            platform_user_id=f"4074{str(user_id)[:7]}",
            platform_meta={"contact_permission": {"status": self.status}},
        )


@pytest.fixture
def identity_lookup():
    return PermissionStub()


@pytest_asyncio.fixture
async def queued_invitation(make_invitation):
    return await make_invitation(
        delivery_state="queued", response_state="not_started",
    )


@pytest_asyncio.fixture
async def accepted_invitation(make_invitation):
    return await make_invitation(
        delivery_state="accepted",
        response_state="awaiting_reply",
        provider_message_id="wamid.ACCEPTED",
        deadline_at=_utcnow() + timedelta(hours=24),
    )


@pytest.mark.asyncio
async def test_permission_is_rechecked_at_send_not_only_at_fire(
    sf, queued_invitation, identity_lookup, reload_invitation,
):
    # This is how a withdrawal reaches an already-queued opener without ops
    # calling into the runtime, and it closes the race where permission is
    # withdrawn between materialising and sending.
    from surogates.programs.materialize import send_queued_openers

    identity_lookup.status = "withdrawn"
    await send_queued_openers(sf, identity_lookup=identity_lookup, now=_utcnow())

    row = await reload_invitation(queued_invitation.id)
    assert row.delivery_state == "canceled"
    assert "permission" in row.reason.lower()

    # Cancelled means cancelled: nothing may have been handed to the outbox.
    async with sf() as db:
        assert (
            await db.execute(sa.select(sa.func.count()).select_from(DeliveryOutbox))
        ).scalar_one() == 0


@pytest.mark.asyncio
async def test_a_still_permitted_opener_is_left_for_delivery(
    sf, queued_invitation, identity_lookup, reload_invitation,
):
    # The negative control for the test above: the cancel path must not be
    # reachable for a patient who never withdrew.
    from surogates.programs.materialize import send_queued_openers

    await send_queued_openers(sf, identity_lookup=identity_lookup, now=_utcnow())
    row = await reload_invitation(queued_invitation.id)
    assert row.delivery_state != "canceled"


@pytest.mark.asyncio
async def test_a_delivery_status_callback_updates_the_invitation(
    sf, accepted_invitation, reload_invitation,
):
    # _log_statuses logs these and drops them, so an asynchronous rejection
    # would leave the patient looking like a non-responder.
    from surogates.programs.delivery import apply_status_callback

    applied = await apply_status_callback(
        sf,
        provider_message_id=accepted_invitation.provider_message_id,
        status="failed",
        reason="Message undeliverable",
    )
    assert applied is True

    row = await reload_invitation(accepted_invitation.id)
    assert row.delivery_state == "failed"
    # The response axis must not move: they were never reached.
    assert row.response_state == "awaiting_reply"
    assert "undeliverable" in (row.reason or "").lower()


@pytest.mark.asyncio
async def test_a_status_for_an_unknown_message_is_ignored(sf):
    # Meta sends statuses for every message the number sends, most of which
    # are ordinary replies with no invitation behind them.
    from surogates.programs.delivery import apply_status_callback

    assert await apply_status_callback(
        sf, provider_message_id="wamid.NOTOURS", status="failed", reason=None,
    ) is False


@pytest.mark.asyncio
async def test_a_delivered_status_does_not_downgrade_a_reply(
    sf, make_invitation, reload_invitation,
):
    # Statuses arrive out of order. A late "delivered" must not walk back a
    # patient who has already replied.
    from surogates.programs.delivery import apply_status_callback

    inv = await make_invitation(
        delivery_state="accepted",
        response_state="replied",
        provider_message_id="wamid.LATE",
    )
    await apply_status_callback(
        sf, provider_message_id="wamid.LATE", status="delivered", reason=None,
    )
    row = await reload_invitation(inv.id)
    assert row.delivery_state == "delivered"
    assert row.response_state == "replied"
