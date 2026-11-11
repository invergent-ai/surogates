"""Inside a run the team members its plan names are swapped for stand-ins on its account."""

from __future__ import annotations

import uuid

import pytest

from surogates.db.models import FollowupSettingsRow, User
from surogates.followup import store as followup_store
from surogates.simulation import store
from surogates.simulation.team import in_scope, run_settings
from tests.test_simulation_models import rsf  # noqa: F401
from tests.test_simulation_store import FakeVault, line, make_org, provision_into


@pytest.mark.asyncio
async def test_the_team_is_swapped_for_stand_ins_inside_the_run_only(rsf):  # noqa: F811
    org_id = await make_org(rsf)
    doctor, patient = uuid.uuid4(), uuid.uuid4()
    async with rsf() as db:
        db.add(User(id=doctor, org_id=org_id, email="d@x", display_name="Dr Ana"))
        db.add(User(id=patient, org_id=org_id, email="p@x", display_name="Real patient"))
        db.add(FollowupSettingsRow(
            org_id=org_id, agent_id="a1", alert_recipient_user_ids=[str(doctor)],
            senders={"whatsapp": "100"}, team_permissions={str(doctor): {"receives": "all"}},
        ))
        await db.commit()
    run = await store.create_run(
        rsf, FakeVault(), org_id=org_id, agent_id="a1", created_by="u1", routing_config={},
        people=[line("Vasile"), line("Dr Ana", member_id=doctor)], provision=provision_into(rsf),
    )
    customer, stand_in = await store.people(rsf, run.id)
    assert (customer.stands_for, stand_in.stands_for, stand_in.name) == (None, doctor, "Dr Ana")

    real = await followup_store.get_settings(rsf, org_id=org_id, agent_id="a1")
    assert await run_settings(rsf, real, patient) is real
    inside = await run_settings(rsf, real, customer.user_id)
    assert followup_store.is_team_member(inside, stand_in.user_id)
    assert not followup_store.is_team_member(inside, doctor)
    assert followup_store.senders_of(inside) == {"whatsapp": run.phone_number_id}
    assert followup_store.member_permissions(inside, stand_in.user_id)["receives"]
    everyone = {patient, customer.user_id}
    assert await in_scope(rsf, inside, everyone) == {customer.user_id}
    assert await in_scope(rsf, real, everyone) == {patient}

    told = await followup_store.queue_outreach(
        rsf, org_id=org_id, agent_id="a1", user_id=stand_in.user_id, platform="whatsapp",
        channel_identifier=run.phone_number_id, kind="alert", text="Bruise",
        template_name=None, template_language=None, subject_user_id=customer.user_id,
    )
    assert told.state == "queued"

    summary = (await store.list_runs(rsf, org_id=org_id, agent_id="a1"))[0]
    assert summary.id == run.id

    await followup_store.upsert_enrollment(
        rsf, org_id=org_id, agent_id="a1", user_id=customer.user_id,
        protocol="p", start_date=None, phase=None, enrolled_by="agent",
    )
    assert await followup_store.enrollments(
        rsf, org_id=org_id, agent_id="a1", user_id=customer.user_id,
    )

    # What a person on the team is spared, their stand-in is spared too.
    as_seen = await followup_store.settings_for(
        rsf, org_id=org_id, agent_id="a1", user_id=stand_in.user_id,
    )
    assert followup_store.is_team_member(as_seen, stand_in.user_id)
    with pytest.raises(followup_store.TeamMember):
        await followup_store.upsert_enrollment(
            rsf, org_id=org_id, agent_id="a1", user_id=stand_in.user_id,
            protocol="p", start_date=None, phase=None, enrolled_by="agent",
        )


@pytest.mark.asyncio
async def test_an_autopilot_stand_in_plays_the_member_with_what_they_handle(rsf):  # noqa: F811
    org_id = await make_org(rsf)
    doctor = uuid.uuid4()
    async with rsf() as db:
        db.add(User(id=doctor, org_id=org_id, email="d@x", display_name="Dr Ana"))
        db.add(FollowupSettingsRow(
            org_id=org_id, agent_id="a1", alert_recipient_user_ids=[str(doctor)],
            team_permissions={str(doctor): {"handles": "medical questions"}},
        ))
        await db.commit()
    run = await store.create_run(
        rsf, FakeVault(), org_id=org_id, agent_id="a1", created_by="u1", routing_config={},
        people=[line("Vasile", autopilot=True), line("Dr Ana", member_id=doctor, autopilot=True)],
        provision=provision_into(rsf),
    )
    _, stand_in = await store.people(rsf, run.id)
    assert (stand_in.driver, stand_in.ai_state) == ("ai", "talking")
    assert "You handle: medical questions." in stand_in.persona


@pytest.mark.asyncio
async def test_a_whole_cast_run_with_a_team_member_who_asks_first(rsf):  # noqa: F811
    from surogates.simulation.api import _detail

    org_id = await make_org(rsf)
    doctor, nurse = uuid.uuid4(), uuid.uuid4()
    async with rsf() as db:
        db.add(User(id=doctor, org_id=org_id, email="d@x", display_name="Dr Ana"))
        db.add(User(id=nurse, org_id=org_id, email="n@x", display_name="Ioana"))
        db.add(FollowupSettingsRow(
            org_id=org_id, agent_id="a1", alert_recipient_user_ids=[str(doctor), str(nurse)],
            team_permissions={str(nurse): {"receives": ["attention"], "can": ["see_people"]}},
        ))
        await db.commit()
    run = await store.create_run(
        rsf, FakeVault(), org_id=org_id, agent_id="a1", created_by="u1", routing_config={},
        provision=provision_into(rsf), via="copilot",
        people=[line("Vasile", autopilot=True), line("Maria", autopilot=True),
                line("Ioana", about="Enrol Maria in the knee programme", member_id=nurse,
                     autopilot=True),
                line("Dr Ana", member_id=doctor)],
    )
    detail = await _detail(rsf, run)
    assert detail["via"] == "copilot"
    by_name = {p["name"]: p for p in detail["people"]}
    assert [n for n, p in by_name.items() if not p["team"]] == ["Vasile", "Maria"]
    assert by_name["Maria"]["driver"] == "ai"
    assert by_name["Ioana"]["task"] == "Enrol Maria in the knee programme"
    assert by_name["Ioana"]["permissions"]["can"] == ["see_people"]
    assert "enroll" in by_name["Dr Ana"]["permissions"]["can"]
    assert by_name["Dr Ana"]["driver"] == "human"
    nurse_person = next(p for p in await store.people(rsf, run.id) if p.name == "Ioana")
    assert "you write first" in nurse_person.persona


@pytest.mark.asyncio
async def test_a_team_stand_in_who_was_done_answers_the_agent_again(rsf):  # noqa: F811
    org_id = await make_org(rsf)
    doctor = uuid.uuid4()
    async with rsf() as db:
        db.add(User(id=doctor, org_id=org_id, email="d@x", display_name="Dr Ana"))
        db.add(FollowupSettingsRow(org_id=org_id, agent_id="a1", alert_recipient_user_ids=[str(doctor)]))
        await db.commit()
    run = await store.create_run(
        rsf, FakeVault(), org_id=org_id, agent_id="a1", created_by="u1", routing_config={},
        people=[line("Vasile", autopilot=True), line("Dr Ana", member_id=doctor, autopilot=True)],
        provision=provision_into(rsf),
    )
    customer, stand_in = await store.people(rsf, run.id)
    for person in (customer, stand_in):
        await store.update_person(rsf, person.id, ai_state="done", ai_note="ok")
        await store.log_message(rsf, run_id=run.id, person_id=person.id, direction="out",
                                kind="text", body={}, wamid=f"w{person.id}")
    customer, stand_in = await store.people(rsf, run.id)
    assert (customer.ai_state, stand_in.ai_state) == ("done", "talking")


@pytest.mark.asyncio
async def test_a_run_without_a_team_still_keeps_to_its_own_people(rsf):  # noqa: F811
    # Without the mark a simulated relative's claim was matched against the
    # real roster, and a real patient was asked to confirm them.
    org_id = await make_org(rsf)
    patient = uuid.uuid4()
    async with rsf() as db:
        db.add(User(id=patient, org_id=org_id, email="p@x", display_name="Real patient"))
        db.add(FollowupSettingsRow(org_id=org_id, agent_id="a1", alert_recipient_user_ids=[]))
        await db.commit()
    run = await store.create_run(
        rsf, FakeVault(), org_id=org_id, agent_id="a1", created_by="u1", routing_config={},
        people=[line("Maria")], provision=provision_into(rsf),
    )
    [customer] = await store.people(rsf, run.id)
    real = await followup_store.get_settings(rsf, org_id=org_id, agent_id="a1")
    inside = await run_settings(rsf, real, customer.user_id)
    assert inside.simulation_run_id == run.id
    assert inside.alert_recipient_user_ids == []
    assert await in_scope(rsf, inside, {patient, customer.user_id}) == {customer.user_id}


@pytest.mark.asyncio
async def test_plan_lines_on_telegram_bring_only_the_named_member_with_their_rights(rsf):  # noqa: F811
    from surogates.simulation.api import _detail
    from tests.test_simulation_store import line

    org_id = await make_org(rsf)
    doctor, nurse, batch = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with rsf() as db:
        db.add(User(id=doctor, org_id=org_id, email="d@x", display_name="Dr Ana"))
        db.add(User(id=nurse, org_id=org_id, email="n@x", display_name="Ioana"))
        db.add(FollowupSettingsRow(
            org_id=org_id, agent_id="a1", alert_recipient_user_ids=[str(doctor), str(nurse)],
            team_permissions={str(nurse): {"receives": ["attention"], "can": ["see_people"]}},
        ))
        await db.commit()
    run = await store.create_run(
        rsf, FakeVault(), org_id=org_id, agent_id="a1", created_by="u1", routing_config={},
        provision=provision_into(rsf), channel="telegram", batch_id=batch,
        people=[line("Vasile", autopilot=True), line("Maria"),
                line("Ioana", about="Asks for Maria's phone", test="Is refused the full number",
                     member_id=nurse, autopilot=True)],
    )
    assert run.batch_id == batch
    vasile, maria, ioana = await store.people(rsf, run.id)
    assert (vasile.driver, maria.driver, ioana.driver) == ("ai", "human", "ai")
    assert "Gets an answer" in vasile.persona and maria.persona
    assert (ioana.stands_for, ioana.channel, ioana.task) == (nurse, "telegram", "Asks for Maria's phone")
    assert ioana.address.isdigit() and "you write first" in ioana.persona

    inside = await run_settings(rsf, await followup_store.get_settings(rsf, org_id=org_id, agent_id="a1"),
                                vasile.user_id)
    assert followup_store.senders_of(inside) == {"telegram": run.phone_number_id}
    assert followup_store.member_permissions(inside, ioana.user_id)["can"] == {"see_people"}

    detail = await _detail(rsf, run)
    assert detail["batch_id"] == str(batch)
    by_name = {p["name"]: p for p in detail["people"]}
    assert set(by_name) == {"Vasile", "Maria", "Ioana"}
    assert by_name["Ioana"]["member_id"] == str(nurse) and by_name["Vasile"]["member_id"] is None
    assert (by_name["Ioana"]["about"], by_name["Ioana"]["test"]) == (
        "Asks for Maria's phone", "Is refused the full number")
    assert by_name["Ioana"]["permissions"]["can"] == ["see_people"]
    assert by_name["Vasile"]["enrolled"] is False

    await followup_store.upsert_enrollment(
        rsf, org_id=org_id, agent_id="a1", user_id=maria.user_id,
        protocol="p", start_date=None, phase=None, enrolled_by="agent",
    )
    by_name = {p["name"]: p for p in (await _detail(rsf, run))["people"]}
    assert by_name["Maria"]["enrolled"] is True and by_name["Vasile"]["enrolled"] is False
