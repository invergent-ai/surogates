"""Starting a run provisions its world: credentials, a person, a stand-in user."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from surogates.channels.identity import ResolvedIdentity
from surogates.db.models import Org, User
from surogates.rehearsal import store
from surogates.rehearsal.ids import is_rehearsal_id
from tests.test_rehearsal_models import rsf  # noqa: F401


class FakeVault:
    def __init__(self):
        self.items: dict[tuple, str] = {}

    async def store(self, org_id, name, value, user_id=None, *, service_account_id=None):
        self.items[(org_id, name)] = value
        return uuid.uuid4(), True

    async def delete(self, org_id, name, user_id=None, *, service_account_id=None):
        return self.items.pop((org_id, name), None) is not None


def provision_into(sf):
    async def provision(session_factory, *, platform, platform_user_id, org_id, display_name=""):
        user = User(org_id=org_id, email=f"{platform}-{platform_user_id}@x", display_name=display_name,
                    auth_provider=platform, external_id=platform_user_id)
        async with sf() as db:
            db.add(user)
            await db.commit()
        return ResolvedIdentity(user_id=user.id, org_id=org_id, platform=platform,
                                platform_user_id=platform_user_id)
    return provision


async def make_org(sf):
    org_id = uuid.uuid4()
    async with sf() as db:
        db.add(Org(id=org_id, name="Org"))
        await db.commit()
    return org_id


async def make_run(sf, vault=None, routing_config=None):
    org_id = await make_org(sf)
    return await store.create_run(
        sf, vault or FakeVault(), org_id=org_id, agent_id="a1", created_by="u1",
        routing_config=routing_config or {}, person_name="Ana", provision=provision_into(sf),
    )


@pytest.mark.asyncio
async def test_create_run_provisions_world(rsf):  # noqa: F811
    vault = FakeVault()
    run = await make_run(rsf, vault, {"identity_policy": "shadow"})
    assert is_rehearsal_id(run.phone_number_id)
    names = {name for (_, name) in vault.items}
    assert names == {f"whatsapp_{c}_{run.phone_number_id}" for c in store.VAULT_CREDENTIALS}
    [person] = await store.people(rsf, run.id)
    assert person.name == "Ana" and is_rehearsal_id(person.address)
    async with rsf() as db:
        user = await db.get(User, person.user_id)
    assert user.rehearsal_run_id == run.id

    routing = await store.routing_for(rsf, "whatsapp", run.phone_number_id)
    assert routing == {"org_id": str(run.org_id), "agent_id": "a1",
                       "config": {"identity_policy": "shadow", "rehearsal_run_id": str(run.id)}}

    await store.end_run(rsf, vault, run.id)
    assert vault.items == {}
    assert await store.routing_for(rsf, "whatsapp", run.phone_number_id) is None


@pytest.mark.asyncio
async def test_routing_ignores_other_kinds_and_real_ids(rsf):  # noqa: F811
    assert await store.routing_for(rsf, "slack", "9990000000000001") is None
    assert await store.routing_for(rsf, "whatsapp", "100000000000001") is None


@pytest.mark.asyncio
async def test_messages_and_window(rsf):  # noqa: F811
    run = await make_run(rsf)
    [person] = await store.people(rsf, run.id)
    now = datetime.now(timezone.utc)
    assert not store.window_open([], now)
    await store.log_message(rsf, run_id=run.id, person_id=person.id, direction="in", kind="text",
                            body={"text": {"body": "hi"}}, wamid="wamid.1", status="delivered")
    await store.log_message(rsf, run_id=run.id, person_id=person.id, direction="out",
                            kind="text", body={"text": {"body": "hello"}}, wamid="wamid.2",
                            status="sent")
    await store.set_status(rsf, run.id, "wamid.2", "read")
    await store.mark_read_up_to(rsf, run.id, "wamid.1")
    thread = await store.thread(rsf, person.id)
    assert [(m.wamid, m.status) for m in thread] == [("wamid.1", "read"), ("wamid.2", "read")]
    assert store.window_open(thread, now)
    assert not store.window_open(thread, now + timedelta(hours=25))


@pytest.mark.asyncio
async def test_a_template_send_carries_its_definition(rsf):  # noqa: F811
    org_id = await make_org(rsf)
    components = [{"type": "BODY", "text": "Bună {{1}}, ne vedem mâine."}]
    run = await store.create_run(
        rsf, FakeVault(), org_id=org_id, agent_id="a1", created_by="u1", routing_config={},
        person_name="Ana", provision=provision_into(rsf),
        templates=[{"name": "reminder", "language": "ro", "components": components}],
    )
    sent = {"type": "template", "template": {"name": "reminder", "language": {"code": "ro"}}}
    assert store.with_template_definition(run, sent)["template"]["definition"] == components
    other = {"type": "template", "template": {"name": "other", "language": {"code": "ro"}}}
    assert store.with_template_definition(run, other) == other
