"""Inside a WhatsApp run the agent's team is swapped for stand-ins on the run's number."""

from __future__ import annotations

import uuid

import pytest

from surogates.db.models import FollowupSettingsRow, User
from surogates.followup import store as followup_store
from surogates.simulation import store
from surogates.simulation.team import in_scope, run_settings
from tests.test_simulation_models import rsf  # noqa: F401
from tests.test_simulation_store import FakeVault, make_org, provision_into


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
        person_name="Vasile", provision=provision_into(rsf),
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


@pytest.mark.asyncio
async def test_an_autopilot_run_plays_its_team_too_and_the_team_never_writes_first(rsf):  # noqa: F811
    from surogates.simulation import actor

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
        person_name="Vasile", provision=provision_into(rsf), persona="You are Vasile.",
    )
    _, stand_in = await store.people(rsf, run.id)
    assert (stand_in.driver, stand_in.ai_state) == ("ai", "talking")
    assert "You handle: medical questions." in stand_in.persona

    class Boom:
        session_factory, settings = rsf, None

    # No message from the agent yet: the stand-in waits, and never reaches the model.
    await actor.Actor(Boom)._move(run, stand_in)


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
        person_name="Vasile", provision=provision_into(rsf), persona="You are Vasile.",
        others=[{"name": "Maria", "persona": "You are Maria."}],
        team_tasks={str(nurse): "Enrol Maria in the knee programme"}, via="copilot",
    )
    detail = await _detail(rsf, run)
    assert detail["via"] == "copilot"
    by_name = {p["name"]: p for p in detail["people"]}
    assert [n for n, p in by_name.items() if not p["team"]] == ["Vasile", "Maria"]
    assert by_name["Maria"]["driver"] == "ai"
    assert by_name["Ioana"]["task"] == "Enrol Maria in the knee programme"
    assert by_name["Ioana"]["permissions"]["can"] == ["see_people"]
    assert "enroll" in by_name["Dr Ana"]["permissions"]["can"]
    assert by_name["Dr Ana"]["task"] is None
    nurse_person = next(p for p in await store.people(rsf, run.id) if p.task)
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
        person_name="Vasile", provision=provision_into(rsf), persona="You are Vasile.",
    )
    customer, stand_in = await store.people(rsf, run.id)
    for person in (customer, stand_in):
        await store.update_person(rsf, person.id, ai_state="done", ai_note="ok")
        await store.log_message(rsf, run_id=run.id, person_id=person.id, direction="out",
                                kind="text", body={}, wamid=f"w{person.id}")
    customer, stand_in = await store.people(rsf, run.id)
    assert (customer.ai_state, stand_in.ai_state) == ("done", "talking")
