"""The follow-up store: enrollments, settings, contacts and outreach rows."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from surogates.db.models import ProgramScheduleRow
from surogates.followup import store

ORG = uuid.uuid4()
AGENT = "agent-1"


async def _enrol(sf, user, protocol="post-op", **kw):
    return await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user, protocol=protocol,
        start_date=kw.get("start_date"), phase=kw.get("phase"),
        enrolled_by=kw.get("enrolled_by", "agent"),
    )


@pytest.mark.asyncio
async def test_enrol_then_list_active(sf):
    a, b = uuid.uuid4(), uuid.uuid4()
    await _enrol(sf, a, start_date=date(2026, 10, 1))
    await _enrol(sf, b)
    await _enrol(sf, uuid.uuid4(), protocol="other")
    rows = await store.enrollments(
        sf, org_id=ORG, agent_id=AGENT, protocol="post-op", status="active",
    )
    assert [r.user_id for r in rows] == [a, b]
    assert rows[0].start_date == date(2026, 10, 1)


@pytest.mark.asyncio
async def test_enrolling_again_reactivates_and_keeps_what_was_known(sf):
    user = uuid.uuid4()
    await _enrol(sf, user, start_date=date(2026, 10, 1), phase="acute")
    await store.update_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user, protocol="post-op",
        status="ended", status_reason="moved away",
    )
    again = await _enrol(sf, user)
    assert again.status == "active"
    assert again.status_reason is None
    assert again.start_date == date(2026, 10, 1)
    assert again.phase == "acute"


@pytest.mark.asyncio
async def test_update_touches_only_what_is_passed(sf):
    user = uuid.uuid4()
    await _enrol(sf, user, start_date=date(2026, 10, 1), phase="acute")
    row = await store.update_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=user, protocol="post-op",
        phase=None,
    )
    assert row.phase is None
    assert row.start_date == date(2026, 10, 1)


@pytest.mark.asyncio
async def test_update_refuses_an_unknown_status(sf):
    user = uuid.uuid4()
    await _enrol(sf, user)
    with pytest.raises(ValueError):
        await store.update_enrollment(
            sf, org_id=ORG, agent_id=AGENT, user_id=user, protocol="post-op",
            status="finished",
        )


@pytest.mark.asyncio
async def test_update_of_a_missing_enrollment_returns_none(sf):
    assert await store.update_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=uuid.uuid4(),
        protocol="post-op", phase="x",
    ) is None


@pytest.mark.asyncio
async def test_protocols_come_from_active_audience_programs(sf):
    async with sf() as db:
        for active, audience in [
            (True, {"protocol": "post-op", "day_to": 30}),
            (True, {"protocol": "post-op", "day_from": 31}),
            (True, {"protocol": "risk"}),
            (False, {"protocol": "paused-one"}),
            (True, None),
            (True, {"protocol": "", "day_to": 3}),
        ]:
            db.add(ProgramScheduleRow(
                program_id=uuid.uuid4(), org_id=ORG, agent_id=AGENT,
                active=active, config={"audience": audience},
            ))
        db.add(ProgramScheduleRow(
            program_id=uuid.uuid4(), org_id=ORG, agent_id="another-agent",
            active=True, config={"audience": {"protocol": "theirs"}},
        ))
        await db.commit()
    assert set(await store.protocol_timezones(
        sf, org_id=ORG, agent_id=AGENT,
    )) == {"post-op", "risk"}


@pytest.mark.asyncio
async def test_settings_round_trip_dedupes_recipients(sf):
    sa_id, doc = uuid.uuid4(), uuid.uuid4()
    assert await store.get_settings(sf, org_id=ORG, agent_id=AGENT) is None
    saved = await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, escalation_service_account_id=sa_id,
        channel_identifier="pn1", alert_recipient_user_ids=[doc, str(doc)],
        alert_template_name="alert", alert_template_language="ro",
        message_template_name="", message_template_language=None,
    )
    assert saved.alert_recipient_user_ids == [str(doc)]
    assert saved.message_template_name is None
    loaded = await store.get_settings(sf, org_id=ORG, agent_id=AGENT)
    assert loaded.escalation_service_account_id == sa_id
    assert loaded.channel_identifier == "pn1"


@pytest.mark.asyncio
async def test_record_inbound_upserts_per_sender(sf):
    first = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
    key = dict(org_id=ORG, platform="whatsapp", platform_user_id="40700000001")
    await store.record_inbound(sf, **key, channel_identifier="pn1", at=first)
    await store.record_inbound(
        sf, **key, channel_identifier="pn1", at=first + timedelta(hours=1),
    )
    await store.record_inbound(sf, **key, channel_identifier="pn2", at=first)
    assert await store.last_inbound(
        sf, **key, channel_identifier="pn1",
    ) == first + timedelta(hours=1)
    by_user = await store.last_inbound_by_user(
        sf, org_id=ORG, platform="whatsapp", channel_identifier="pn2",
        platform_user_ids=["40700000001", "40700000002"],
    )
    assert by_user == {"40700000001": first}


@pytest.mark.asyncio
async def test_queue_outreach_validates(sf):
    base = dict(
        org_id=ORG, agent_id=AGENT, user_id=uuid.uuid4(), platform="whatsapp",
        channel_identifier="pn1", template_name=None, template_language=None,
    )
    with pytest.raises(ValueError):
        await store.queue_outreach(sf, **base, kind="spam", text="hi")
    with pytest.raises(ValueError):
        await store.queue_outreach(sf, **base, kind="alert", text="  ")
    row = await store.queue_outreach(sf, **base, kind="alert", text=" fever ")
    assert row.state == "queued"
    assert row.text == "fever"


@pytest.mark.asyncio
async def test_queueing_the_same_cause_twice_writes_once(sf):
    base = dict(
        org_id=ORG, agent_id=AGENT, user_id=uuid.uuid4(), platform="whatsapp",
        channel_identifier="pn1", kind="welcome", text="Bun venit",
        template_name="welcome", template_language="ro",
        dedupe_key="welcome:cs_1",
    )
    first = await store.queue_outreach(sf, **base)
    again = await store.queue_outreach(sf, **base)
    assert again.id == first.id
    other = await store.queue_outreach(sf, **{**base, "dedupe_key": "welcome:cs_2"})
    assert other.id != first.id


@pytest.mark.asyncio
async def test_a_purchase_records_payer_and_source(sf):
    patient, payer = uuid.uuid4(), uuid.uuid4()
    row = await _enrol(sf, patient, enrolled_by="purchase")
    assert row.payer_user_id is None
    row = await store.upsert_enrollment(
        sf, org_id=ORG, agent_id=AGENT, user_id=patient, protocol="post-op",
        start_date=None, phase=None, enrolled_by="purchase",
        payer_user_id=payer, source_ref="checkout:cs_1",
    )
    assert row.payer_user_id == payer
    assert row.source_ref == "checkout:cs_1"
    with pytest.raises(ValueError):
        await _enrol(sf, uuid.uuid4(), enrolled_by="robot")


@pytest.mark.asyncio
async def test_welcome_settings_and_package_protocols(sf):
    saved = await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, escalation_service_account_id=None,
        channel_identifier="pn1", alert_recipient_user_ids=[],
        alert_template_name=None, alert_template_language=None,
        message_template_name=None, message_template_language=None,
        welcome_template_name="bun_venit", welcome_template_language="ro",
        welcome_text="  Vă mulțumim.  ",
        offer_protocols={"offer-1": " post-op ", "offer-2": ""},
        texts={"checkout_phone": " Nr. WhatsApp ", "purchase_notice": ""},
    )
    assert saved.welcome_text == "Vă mulțumim."
    assert saved.texts == {"checkout_phone": "Nr. WhatsApp"}
    assert saved.offer_protocols == {"offer-1": "post-op"}


@pytest.mark.asyncio
async def test_permission_records_how_it_was_given(sf):
    import sqlalchemy as sa

    from surogates.db.models import ChannelIdentity

    user = uuid.uuid4()
    async with sf() as db:
        db.add(ChannelIdentity(
            org_id=ORG, user_id=user, platform="whatsapp",
            platform_user_id="40700000009", platform_meta={},
        ))
        await db.commit()
    assert await store.grant_contact_permission(
        sf, org_id=ORG, platform="whatsapp", platform_user_id="40700000009",
        recorded_by="checkout", method="purchase",
    )
    async with sf() as db:
        ident = (await db.execute(sa.select(ChannelIdentity))).scalar_one()
    assert ident.platform_meta["contact_permission"]["method"] == "purchase"


@pytest.mark.asyncio
async def test_the_care_team_is_never_enrolled_by_any_path(sf):
    doctor = uuid.uuid4()
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, alert_recipient_user_ids=[doctor],
    )
    with pytest.raises(store.CareTeamMember):
        await _enrol(sf, doctor, enrolled_by="purchase")


@pytest.mark.asyncio
async def test_settings_change_only_the_fields_given(sf):
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, channel_identifier="pn1",
        welcome_text="Bun venit", offer_protocols={"off_1": "post-op"},
    )
    saved = await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, alert_template_name="alerta",
    )
    assert saved.channel_identifier == "pn1"
    assert saved.welcome_text == "Bun venit"
    assert saved.offer_protocols == {"off_1": "post-op"}
    assert saved.alert_template_name == "alerta"
    with pytest.raises(ValueError):
        await store.update_settings(sf, org_id=ORG, agent_id=AGENT, colour="red")
