"""The care team hears when someone stops answering."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from surogates.db.models import (
    OutreachMessageRow,
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    ProgramScheduleRow,
    User,
)
from surogates.followup import store
from surogates.followup.missed import (
    DEFAULT_MISSED_TEXT,
    alert_missed_checkins,
    paused_by_silence,
    render_missed_text,
)

ORG = uuid.uuid4()
AGENT = "a1"
NOW = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)


async def _setup(sf, *, threshold=3, text=None, recipients=True):
    program_id, patient, doctor = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sf() as db:
        db.add(User(id=patient, org_id=ORG, email="p@x", display_name="Ion Pop"))
        db.add(ProgramScheduleRow(
            program_id=program_id, org_id=ORG, agent_id=AGENT, active=True,
            config={
                "name": "Prima lună", "channel_identifier": "pn1",
                "alert_after_missed": threshold, "missed_alert_text": text,
            },
        ))
        await db.commit()
    if recipients:
        await store.update_settings(
            sf, org_id=ORG, agent_id=AGENT, escalation_service_account_id=None,
            channel_identifier="pn1", alert_recipient_user_ids=[doctor],
            alert_template_name="alerta", alert_template_language="ro",
            message_template_name=None, message_template_language=None,
        )
    return program_id, patient, doctor


async def _invitation(sf, program_id, patient, *, days_ago, response="no_reply_by_deadline",
                      delivery="delivered"):
    created = NOW - timedelta(days=days_ago)
    async with sf() as db:
        occ = ProgramOccurrenceRow(
            program_id=program_id, org_id=ORG, agent_id=AGENT,
            scheduled_for=created, skill_ref="s", status="fired",
        )
        db.add(occ)
        await db.flush()
        row = ProgramInvitationRow(
            occurrence_id=occ.id, program_id=program_id, org_id=ORG,
            agent_id=AGENT, user_id=patient, platform="whatsapp",
            platform_user_id="40700000001", delivery_state=delivery,
            response_state=response, escalation_state="none",
            deadline_at=created, created_at=created,
        )
        db.add(row)
        await db.commit()
        return row.id


async def _alerts(sf):
    async with sf() as db:
        return (await db.execute(sa.select(OutreachMessageRow))).scalars().all()


async def _state(sf, inv_id):
    async with sf() as db:
        return (await db.get(ProgramInvitationRow, inv_id)).escalation_state


@pytest.mark.asyncio
async def test_three_unanswered_in_a_row_alert_the_care_team(sf):
    program_id, patient, doctor = await _setup(sf)
    for days in (0.20, 0.14):
        await _invitation(sf, program_id, patient, days_ago=days)
    assert await alert_missed_checkins(sf, now=NOW) == 0
    third = await _invitation(sf, program_id, patient, days_ago=0.10)

    assert await alert_missed_checkins(sf, now=NOW) == 1
    [alert] = await _alerts(sf)
    assert alert.user_id == doctor
    assert alert.subject_user_id == patient
    assert alert.text == (
        "Ion Pop (+40 700 000 001) has not answered the last 3 messages of "
        "“Prima lună”."
    )
    assert await _state(sf, third) == "raised"
    # Re-running the pass never alerts twice.
    assert await alert_missed_checkins(sf, now=NOW) == 0
    assert len(await _alerts(sf)) == 1


@pytest.mark.asyncio
async def test_an_answer_breaks_the_streak(sf):
    program_id, patient, _ = await _setup(sf)
    await _invitation(sf, program_id, patient, days_ago=0.18)
    await _invitation(sf, program_id, patient, days_ago=0.14, response="completed")
    await _invitation(sf, program_id, patient, days_ago=0.10)
    assert await alert_missed_checkins(sf, now=NOW) == 0


@pytest.mark.asyncio
async def test_a_skipped_opener_is_not_silence(sf):
    program_id, patient, _ = await _setup(sf, threshold=2)
    await _invitation(sf, program_id, patient, days_ago=0.18)
    await _invitation(sf, program_id, patient, days_ago=0.14, delivery="skipped",
                      response="not_started")
    assert await alert_missed_checkins(sf, now=NOW) == 0


@pytest.mark.asyncio
async def test_the_next_alert_needs_a_whole_new_streak(sf):
    program_id, patient, _ = await _setup(sf, threshold=2)
    await _invitation(sf, program_id, patient, days_ago=0.18)
    await _invitation(sf, program_id, patient, days_ago=0.16)
    assert await alert_missed_checkins(sf, now=NOW) == 1
    await _invitation(sf, program_id, patient, days_ago=0.14)
    assert await alert_missed_checkins(sf, now=NOW) == 0  # one more is not two
    await _invitation(sf, program_id, patient, days_ago=0.10)
    assert await alert_missed_checkins(sf, now=NOW) == 1


@pytest.mark.asyncio
async def test_nobody_to_tell_is_recorded_not_silent(sf):
    program_id, patient, _ = await _setup(sf, threshold=1, recipients=False)
    inv = await _invitation(sf, program_id, patient, days_ago=0.10)
    assert await alert_missed_checkins(sf, now=NOW) == 0
    assert await _state(sf, inv) == "failed"


@pytest.mark.asyncio
async def test_programs_without_the_option_are_left_alone(sf):
    program_id, patient, _ = await _setup(sf, threshold=None)
    inv = await _invitation(sf, program_id, patient, days_ago=0.10)
    assert await alert_missed_checkins(sf, now=NOW) == 0
    assert await _state(sf, inv) == "none"


@pytest.mark.asyncio
async def test_the_operators_wording_is_used(sf):
    program_id, patient, _ = await _setup(
        sf, threshold=1, text="{name} ({phone}) nu a răspuns de {count} ori.",
    )
    await _invitation(sf, program_id, patient, days_ago=0.10)
    await alert_missed_checkins(sf, now=NOW)
    [alert] = await _alerts(sf)
    assert alert.text == "Ion Pop (+40 700 000 001) nu a răspuns de 1 ori."


def test_a_broken_template_falls_back_to_the_default():
    values = dict(name="A", phone="+1", count=2, program="P")
    assert render_missed_text("{nume} {", **values) == DEFAULT_MISSED_TEXT.format(**values)
    assert render_missed_text(None, **values) == DEFAULT_MISSED_TEXT.format(**values)


@pytest.mark.asyncio
async def test_a_program_pauses_after_unanswered_openers_until_they_write(sf):
    program_id, patient, _ = await _setup(sf)
    for days_ago in (3, 2, 1):
        await _invitation(sf, program_id, patient, days_ago=days_ago)

    async def paused(last_inbound_at):
        return await paused_by_silence(
            sf, program_id=program_id, user_id=patient, count=3,
            last_inbound_at=last_inbound_at,
        )

    assert await paused(None)
    assert await paused(NOW - timedelta(days=5))  # wrote before the streak
    assert not await paused(NOW)  # wrote since: the pause lifts

    # After they write, one new miss is not a streak: it takes three again.
    wrote = NOW - timedelta(hours=12)
    await _invitation(sf, program_id, patient, days_ago=0.25)
    assert not await paused(wrote)


@pytest.mark.asyncio
async def test_no_pause_without_a_full_streak(sf):
    program_id, patient, _ = await _setup(sf)
    await _invitation(sf, program_id, patient, days_ago=3)
    await _invitation(sf, program_id, patient, days_ago=2, response="completed")
    await _invitation(sf, program_id, patient, days_ago=1)
    assert not await paused_by_silence(
        sf, program_id=program_id, user_id=patient, count=2, last_inbound_at=None,
    )
    # A pause's own skipped runs are not counted, so it holds until they write.
    await _invitation(sf, program_id, patient, days_ago=0.5)
    await _invitation(sf, program_id, patient, days_ago=0.1, delivery="skipped",
                      response="not_started")
    assert await paused_by_silence(
        sf, program_id=program_id, user_id=patient, count=2, last_inbound_at=None,
    )


@pytest.mark.asyncio
async def test_a_streak_is_judged_once_not_every_pass(sf, monkeypatch):
    import surogates.followup.missed as missed

    program_id, patient, _ = await _setup(sf)
    await _invitation(sf, program_id, patient, days_ago=0.1)  # not a streak yet
    calls = []
    real = missed._alert_if_streak

    async def counting(*args, **kwargs):
        calls.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(missed, "_alert_if_streak", counting)
    await alert_missed_checkins(sf, now=NOW)
    await alert_missed_checkins(sf, now=NOW + timedelta(minutes=1))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_slack_programs_alert_goes_out_on_slack_never_whatsapp(sf):
    # The Slack team id is not a WhatsApp sender: queuing it as one sent the
    # alert nowhere.  The Program's own channel and sender carry it instead.
    from surogates.db.models import ChannelIdentity

    program_id, patient, doctor = await _setup(sf, threshold=1, recipients=False)
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, alert_recipient_user_ids=[doctor],
    )
    async with sf() as db:
        sched = (await db.execute(
            sa.select(ProgramScheduleRow).where(ProgramScheduleRow.program_id == program_id)
        )).scalar_one()
        sched.config = {**sched.config, "channel": "slack", "channel_identifier": "T0SLACK"}
        db.add(ChannelIdentity(
            org_id=ORG, user_id=doctor, platform="slack", platform_user_id="U9",
            platform_meta={"contact_permission": {"status": "granted"}},
        ))
        await db.commit()
    await _invitation(sf, program_id, patient, days_ago=0.01)

    assert await alert_missed_checkins(sf, now=NOW) == 1
    [alert] = await _alerts(sf)
    assert (alert.platform, alert.channel_identifier) == ("slack", "T0SLACK")
    assert alert.user_id == doctor and alert.template_name is None


@pytest.mark.asyncio
async def test_each_team_member_is_told_on_their_own_channel(sf):
    from surogates.db.models import ChannelIdentity

    a, b = uuid.uuid4(), uuid.uuid4()
    await store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, alert_recipient_user_ids=[a, b],
        channel_identifier="pn1", senders={"slack": "T1"},
        team_channels={str(b): ["slack", "whatsapp"]},
        alert_template_name="alerta", alert_template_language="ro",
    )
    async with sf() as db:
        for user, platform, pid in ((a, "whatsapp", "4070"), (b, "whatsapp", "4071"), (b, "slack", "U1")):
            db.add(ChannelIdentity(
                org_id=ORG, user_id=user, platform=platform, platform_user_id=pid,
                platform_meta={"contact_permission": {"status": "granted"}},
            ))
        await db.commit()
    settings = await store.get_settings(sf, org_id=ORG, agent_id=AGENT)
    queued, _ = await store.notify_team(
        sf, settings, org_id=ORG, agent_id=AGENT, kind="alert", text="x", created_by="t",
    )
    assert queued == 2
    by_user = {r.user_id: r for r in await _alerts(sf)}
    assert (by_user[a].platform, by_user[a].channel_identifier) == ("whatsapp", "pn1")
    assert by_user[a].template_name == "alerta"
    assert (by_user[b].platform, by_user[b].channel_identifier) == ("slack", "T1")
    assert by_user[b].template_name is None
    with pytest.raises(ValueError):
        await store.update_settings(sf, org_id=ORG, agent_id=AGENT, senders={"web": "x"})


@pytest.mark.asyncio
async def test_a_missed_streak_is_an_escalation_the_team_can_take(sf):
    from surogates.db.models import EscalationRow

    program_id, patient, _ = await _setup(sf, threshold=1)
    inv = await _invitation(sf, program_id, patient, days_ago=0.10)
    assert await alert_missed_checkins(sf, now=NOW) == 1
    async with sf() as db:
        [esc] = (await db.execute(sa.select(EscalationRow))).scalars().all()
    assert (esc.id, esc.kind, esc.state, esc.user_id) == (inv, "missed_replies", "raised", patient)
    assert (esc.invitation_id, esc.program_id, esc.platform) == (inv, program_id, "whatsapp")
    assert esc.reason.startswith("Ion Pop")


@pytest.mark.asyncio
async def test_a_missed_streak_nobody_hears_is_a_failed_escalation(sf):
    from surogates.db.models import EscalationRow

    program_id, patient, _ = await _setup(sf, threshold=1, recipients=False)
    await _invitation(sf, program_id, patient, days_ago=0.10)
    await alert_missed_checkins(sf, now=NOW)
    async with sf() as db:
        [esc] = (await db.execute(sa.select(EscalationRow))).scalars().all()
    assert esc.state == "failed"


@pytest.mark.asyncio
async def test_a_telegram_chat_id_is_never_shown_as_a_phone(sf):
    program_id, patient, _ = await _setup(sf, threshold=1)
    inv = await _invitation(sf, program_id, patient, days_ago=0.10)
    async with sf() as db:
        row = await db.get(ProgramInvitationRow, inv)
        row.platform, row.platform_user_id = "telegram", "123456789"
        await db.commit()
    await alert_missed_checkins(sf, now=NOW)
    [alert] = await _alerts(sf)
    assert alert.text.startswith("Ion Pop (?) has not answered")
