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
    # Provider wording has its own column, so a late status can never
    # overwrite the agent's clinical note or a skip explanation in `reason`.
    assert "undeliverable" in (row.delivery_error or "").lower()
    assert row.reason is None


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


@pytest.mark.asyncio
async def test_a_late_delivered_status_cannot_resurrect_a_failed_send(
    sf, make_invitation, reload_invitation,
):
    # Meta batches statuses and retries un-acknowledged webhooks, so a stale
    # "delivered" can land after "failed". Letting it win would put a send we
    # know was lost back into the reached set, and the sweep would then mark
    # a patient we never reached as a non-responder.
    from surogates.programs.delivery import apply_status_callback

    inv = await make_invitation(
        delivery_state="failed",
        response_state="awaiting_reply",
        provider_message_id="wamid.LOST",
    )
    await apply_status_callback(
        sf, provider_message_id="wamid.LOST", status="delivered", reason=None,
    )
    assert (await reload_invitation(inv.id)).delivery_state == "failed"


@pytest.mark.asyncio
async def test_the_dispatcher_result_gives_the_invitation_its_provider_id(
    sf, make_invitation, reload_invitation,
):
    # The provider's id does not exist until the dispatcher has posted, so the
    # invitation is keyed on the outbox row and learns its wamid from the
    # dispatcher's report. Without this step no status webhook could ever
    # match, and a failed send would sit at awaiting_reply until swept.
    from surogates.programs.delivery import record_outbox_result

    inv = await make_invitation(
        delivery_state="queued", response_state="awaiting_reply", outbox_id=4242,
    )
    assert await record_outbox_result(
        sf, 4242, provider_message_id="wamid.NEW", error=None,
    ) is True
    row = await reload_invitation(inv.id)
    assert row.provider_message_id == "wamid.NEW"
    assert row.delivery_state == "accepted"


@pytest.mark.asyncio
async def test_a_dead_outbox_row_marks_the_invitation_failed(
    sf, make_invitation, reload_invitation,
):
    from surogates.programs.delivery import record_outbox_result

    inv = await make_invitation(
        delivery_state="queued", response_state="awaiting_reply", outbox_id=4243,
    )
    await record_outbox_result(
        sf, 4243, provider_message_id=None,
        error="graph error 132001 (HTTP 400): Template name does not exist",
    )
    row = await reload_invitation(inv.id)
    assert row.delivery_state == "failed"
    assert "132001" in row.delivery_error
    # Never asked, so the response axis is untouched — the sweep will not
    # count them as silent.
    assert row.response_state == "awaiting_reply"


@pytest.mark.asyncio
async def test_an_outbox_row_with_no_invitation_is_ignored(sf):
    # Almost every outbox row is an ordinary reply.
    from surogates.programs.delivery import record_outbox_result

    assert await record_outbox_result(
        sf, 999999, provider_message_id="wamid.X", error=None,
    ) is False


@pytest.mark.asyncio
async def test_a_template_missing_its_language_fails_permanently(monkeypatch):
    # A KeyError inside the send loop used to be recorded as the outbox error
    # "'language'" and retried sixty times over half an hour. The payload can
    # never become sendable, so it must fail in the permanent class.
    from surogates.channels.delivery import is_permanent_delivery_error

    called = False

    async def _fake_send(client, *, token, phone_number_id, payload, api_version):
        nonlocal called
        called = True
        return "wamid.NO", None

    monkeypatch.setattr(
        "surogates.channels.platforms.whatsapp.send_message", _fake_send,
    )
    result = await WhatsAppPlatform().send(
        _item({"template": {"name": "daily_checkin"}}), creds={"access_token": "T"},
    )
    assert result.success is False
    assert called is False
    assert is_permanent_delivery_error(result.error)


def test_template_graph_errors_are_permanent():
    # A renamed or paused template fails every opener for the roster; retrying
    # for thirty minutes cannot change that and only delays the failure the
    # operator needs to see.
    from surogates.channels.delivery import is_permanent_delivery_error

    for code in ("132000", "132001", "132005", "132007", "132012", "132015", "132016"):
        assert is_permanent_delivery_error(
            f"graph error {code} (HTTP 400): whatever Meta said"
        ), code


@pytest.mark.asyncio
async def test_a_sent_opener_is_keyed_on_its_outbox_row(
    sf, queued_invitation, identity_lookup, reload_invitation,
):
    # The dispatcher reports back through the outbox id, and the deadline
    # starts only once something was actually handed over.
    from surogates.programs.materialize import send_queued_openers

    async def _enqueue(inv):
        return 777

    await send_queued_openers(
        sf, identity_lookup=identity_lookup, now=_utcnow(), enqueue=_enqueue,
    )
    row = await reload_invitation(queued_invitation.id)
    assert row.outbox_id == 777
    assert row.response_state == "awaiting_reply"
    assert row.deadline_at is not None
    # Still "queued" on the delivery axis: the dispatcher has not posted yet,
    # and only its report moves this to accepted or failed.
    assert row.delivery_state == "queued"


@pytest.mark.asyncio
async def test_an_enqueue_that_hands_nothing_over_starts_no_deadline(
    sf, queued_invitation, identity_lookup, reload_invitation,
):
    from surogates.programs.materialize import send_queued_openers

    async def _enqueue(inv):
        return None

    await send_queued_openers(
        sf, identity_lookup=identity_lookup, now=_utcnow(), enqueue=_enqueue,
    )
    row = await reload_invitation(queued_invitation.id)
    assert row.outbox_id is None
    assert row.response_state == "not_started"
    assert row.deadline_at is None


@pytest.mark.asyncio
async def test_a_failure_mid_batch_does_not_resend_the_earlier_openers(
    sf, make_invitation, identity_lookup, reload_invitation,
):
    # One commit at the end meant a failure on the k-th enqueue rolled back
    # the first k-1, whose openers were already in the outbox — and the next
    # tick sent them all a second approved template.
    from surogates.programs.materialize import send_queued_openers

    first = await make_invitation(delivery_state="queued", response_state="not_started")
    second = await make_invitation(delivery_state="queued", response_state="not_started")
    calls = []

    async def _enqueue(inv):
        calls.append(inv.id)
        if len(calls) == 2:
            raise RuntimeError("provider hiccup")
        return 100 + len(calls)

    with pytest.raises(RuntimeError):
        await send_queued_openers(
            sf, identity_lookup=identity_lookup, now=_utcnow(), enqueue=_enqueue,
        )

    assert (await reload_invitation(first.id)).outbox_id == 101
    # The one that failed is still queued for the next tick, not lost.
    assert (await reload_invitation(second.id)).outbox_id is None


@pytest.mark.asyncio
async def test_an_already_handed_over_opener_is_not_enqueued_twice(
    sf, make_invitation, identity_lookup,
):
    # A second worker, or the next tick, must not pick up a row that already
    # has an outbox id even though its delivery_state is still "queued".
    from surogates.programs.materialize import send_queued_openers

    await make_invitation(
        delivery_state="queued", response_state="awaiting_reply", outbox_id=555,
    )
    calls = []

    async def _enqueue(inv):
        calls.append(inv.id)
        return 1

    await send_queued_openers(
        sf, identity_lookup=identity_lookup, now=_utcnow(), enqueue=_enqueue,
    )
    assert calls == []
