import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, update

from surogates.browser.profiles import BrowserProfileStore
from surogates.db.models import BrowserProfile

from .conftest import create_org, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")

_KEY = Fernet.generate_key()


def _store(session_factory):
    return BrowserProfileStore(session_factory, encryption_key=_KEY)


async def test_browser_profile_persists_for_service_account_principal(session_factory):
    org_id = await create_org(session_factory)
    sa = await issue_service_account_token(session_factory, org_id)

    async with session_factory() as s:
        async with s.begin():
            s.add(
                BrowserProfile(
                    org_id=org_id,
                    service_account_id=sa.id,
                    name="Personal",
                )
            )

    # Read back in a fresh session so server defaults are loaded from the DB.
    async with session_factory() as s:
        row = (
            await s.execute(
                select(BrowserProfile).where(BrowserProfile.org_id == org_id)
            )
        ).scalar_one()

    assert row.name == "Personal"
    assert row.user_id is None
    assert row.service_account_id == sa.id
    assert row.source == "manual_vnc"
    assert row.cookie_domains == []
    assert row.storage_state_enc is None


async def test_create_list_scoped_to_principal(session_factory):
    store = _store(session_factory)
    org = await create_org(session_factory)
    sa_a = (await issue_service_account_token(session_factory, org, name="a")).id
    sa_b = (await issue_service_account_token(session_factory, org, name="b")).id

    a = await store.create(org, user_id=None, service_account_id=sa_a, name="A")
    await store.create(org, user_id=None, service_account_id=sa_b, name="B")

    rows = await store.list(org, user_id=None, service_account_id=sa_a)
    assert [r.name for r in rows] == ["A"]
    assert rows[0].id == a.id
    assert rows[0].has_state is False


async def test_capture_roundtrip_and_cookie_domains(session_factory):
    store = _store(session_factory)
    org = await create_org(session_factory)
    sa = (await issue_service_account_token(session_factory, org)).id

    p = await store.create(org, user_id=None, service_account_id=sa, name="P")
    state = {
        "cookies": [
            {"name": "SID", "domain": ".google.com", "value": "x"},
            {"name": "h", "domain": "github.com", "value": "y"},
        ],
        "origins": [],
    }
    row = await store.save_capture(
        p.id, org, user_id=None, service_account_id=sa, storage_state=state
    )
    assert sorted(row.cookie_domains) == ["github.com", "google.com"]
    assert row.has_state is True

    got = await store.storage_state_for(
        p.id, org, user_id=None, service_account_id=sa
    )
    assert got == state


async def test_storage_state_for_denies_foreign_principal(session_factory):
    store = _store(session_factory)
    org = await create_org(session_factory)
    sa = (await issue_service_account_token(session_factory, org, name="owner")).id
    other = (await issue_service_account_token(session_factory, org, name="other")).id

    p = await store.create(org, user_id=None, service_account_id=sa, name="P")
    await store.save_capture(
        p.id,
        org,
        user_id=None,
        service_account_id=sa,
        storage_state={"cookies": [], "origins": []},
    )

    assert (
        await store.storage_state_for(
            p.id, org, user_id=None, service_account_id=other
        )
        is None
    )


async def test_an_agent_profile_is_used_only_on_the_channels_it_was_granted(session_factory):
    store = _store(session_factory)
    org = await create_org(session_factory)
    agent_sa = (await issue_service_account_token(session_factory, org, name="agent")).id
    other_sa = (await issue_service_account_token(session_factory, org, name="other")).id

    empty = await store.create(org, user_id=None, service_account_id=agent_sa, name="Empty")
    x = await store.create(org, user_id=None, service_account_id=agent_sa, name="X")
    await store.save_capture(
        x.id, org, user_id=None, service_account_id=agent_sa,
        storage_state={"cookies": [{"name": "auth", "domain": ".x.com"}], "origins": []},
    )
    for profile in (empty, x):
        assert await store.set_use_in_channels(
            profile.id, org, service_account_id=agent_sa, channels=["slack", "scheduled", "slack"],
        )
    assert not await store.set_use_in_channels(
        x.id, org, service_account_id=other_sa, channels=["web"],
    )

    assert await store.used_in_channel(org, service_account_id=agent_sa, channel="slack") == x.id
    assert await store.used_in_channel(org, service_account_id=agent_sa, channel="web") is None
    assert await store.used_in_channel(org, service_account_id=other_sa, channel="slack") is None
    [row] = [r for r in await store.list(org, user_id=None, service_account_id=agent_sa) if r.id == x.id]
    assert row.use_in_channels == ["scheduled", "slack"]


async def _agent(session_factory, org):
    from surogates.tenant.auth.service_account import KIND_AGENT_PRINCIPAL, ServiceAccountStore

    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    sa = await ServiceAccountStore(session_factory).create(
        org_id=org, name=agent_id, agent_id=agent_id, kind=KIND_AGENT_PRINCIPAL,
    )
    return sa.id, agent_id


_STATE = {"cookies": [{"name": "SID", "domain": ".x.com"}], "origins": []}


async def _personal_login(store, session_factory, org):
    person = (await issue_service_account_token(session_factory, org, name="ops-chat-me")).id
    p = await store.create(org, user_id=None, service_account_id=person, name="Mine")
    await store.save_capture(p.id, org, user_id=None, service_account_id=person, storage_state=_STATE)
    return p.id, person


async def test_a_personal_login_is_not_shared_by_default(session_factory):
    store = _store(session_factory)
    org = await create_org(session_factory)
    pid, person = await _personal_login(store, session_factory, org)
    agent_sa, _ = await _agent(session_factory, org)

    [row] = await store.list(org, user_id=None, service_account_id=person)
    assert row.shared_with_agents == []
    assert await store.shared_state_for(pid, org, agent_sa=agent_sa) is None


async def test_a_shared_login_loads_for_a_listed_agent_only(session_factory):
    store = _store(session_factory)
    org = await create_org(session_factory)
    pid, person = await _personal_login(store, session_factory, org)
    listed_sa, listed = await _agent(session_factory, org)
    unlisted_sa, _ = await _agent(session_factory, org)
    stranger = (await issue_service_account_token(session_factory, org, name="other")).id

    assert await store.set_shared_with_agents(
        pid, org, user_id=None, service_account_id=person, agent_ids=[listed, listed],
    )
    # Only the owner sets it.
    assert not await store.set_shared_with_agents(
        pid, org, user_id=None, service_account_id=stranger, agent_ids=["x"],
    )

    assert await store.shared_state_for(pid, org, agent_sa=listed_sa) == _STATE
    assert await store.shared_state_for(pid, org, agent_sa=unlisted_sa) is None
    [row] = await store.list(org, user_id=None, service_account_id=person)
    assert row.shared_with_agents == [listed]
    assert row.last_used_by == listed and row.last_used_at is not None
    # Its owner using it again is no agent's use.
    await store.touch_last_used(pid, org, user_id=None, service_account_id=person)
    [row] = await store.list(org, user_id=None, service_account_id=person)
    assert row.last_used_by is None


async def test_a_shared_login_never_loads_for_an_agent_of_another_org(session_factory):
    store = _store(session_factory)
    org, elsewhere = await create_org(session_factory), await create_org(session_factory)
    pid, person = await _personal_login(store, session_factory, org)
    foreign_sa, foreign = await _agent(session_factory, elsewhere)
    await store.set_shared_with_agents(pid, org, user_id=None, service_account_id=person, agent_ids=[foreign])

    assert await store.shared_state_for(pid, org, agent_sa=foreign_sa) is None
    assert await store.shared_state_for(pid, elsewhere, agent_sa=foreign_sa) is None


async def test_an_agents_own_login_cannot_be_shared(session_factory):
    store = _store(session_factory)
    org = await create_org(session_factory)
    agent_sa, _ = await _agent(session_factory, org)
    other_sa, other = await _agent(session_factory, org)
    p = await store.create(org, user_id=None, service_account_id=agent_sa, name="Agent's")
    await store.save_capture(p.id, org, user_id=None, service_account_id=agent_sa, storage_state=_STATE)

    assert not await store.set_shared_with_agents(
        p.id, org, user_id=None, service_account_id=agent_sa, agent_ids=[other],
    )
    # Even written behind the store's back, it is never loaded for another agent.
    async with session_factory() as s:
        async with s.begin():
            await s.execute(
                update(BrowserProfile).where(BrowserProfile.id == p.id).values(shared_with_agents=[other])
            )
    assert await store.shared_state_for(p.id, org, agent_sa=other_sa) is None


async def test_a_revoked_agent_account_loses_the_share(session_factory):
    from surogates.tenant.auth.service_account import ServiceAccountStore

    store = _store(session_factory)
    org = await create_org(session_factory)
    pid, person = await _personal_login(store, session_factory, org)
    agent_sa, agent = await _agent(session_factory, org)
    await store.set_shared_with_agents(pid, org, user_id=None, service_account_id=person, agent_ids=[agent])

    assert await ServiceAccountStore(session_factory).revoke(service_account_id=agent_sa, org_id=org)
    assert await store.shared_state_for(pid, org, agent_sa=agent_sa) is None


async def test_an_agent_can_use_its_own_and_shared_logins_and_nothing_of_another_agent(session_factory):
    store = _store(session_factory)
    org = await create_org(session_factory)
    pid, person = await _personal_login(store, session_factory, org)
    agent_sa, agent = await _agent(session_factory, org)
    other_sa, other = await _agent(session_factory, org)
    own = await store.create(org, user_id=None, service_account_id=agent_sa, name="Own")
    await store.create(org, user_id=None, service_account_id=other_sa, name="Other's")
    unshared = await store.create(org, user_id=None, service_account_id=person, name="Unshared")
    await store.set_shared_with_agents(pid, org, user_id=None, service_account_id=person, agent_ids=[agent])

    usable = await store.usable_by_agent(org, agent_sa=agent_sa, agent_id=agent)
    assert {(r.id, kind) for r, kind in usable} == {(own.id, "agent"), (pid, "shared")}
    assert unshared.id not in {r.id for r, _ in usable}
    assert await store.usable_by_agent(org, agent_sa=other_sa, agent_id=other) != []
    assert pid not in {r.id for r, _ in await store.usable_by_agent(org, agent_sa=other_sa, agent_id=other)}


async def test_the_orgs_shared_logins_are_listed_with_their_owner(session_factory):
    store = _store(session_factory)
    org = await create_org(session_factory)
    pid, person = await _personal_login(store, session_factory, org)
    await store.create(org, user_id=None, service_account_id=person, name="Unshared")
    _, agent = await _agent(session_factory, org)
    await store.set_shared_with_agents(pid, org, user_id=None, service_account_id=person, agent_ids=[agent])

    [(row, user_id, sa_id)] = await store.shared_in_org(org)
    assert (row.id, row.shared_with_agents, user_id, sa_id) == (pid, [agent], None, person)


async def test_a_shared_login_that_cannot_be_decrypted_is_not_recorded_as_used(session_factory):
    store = _store(session_factory)
    org = await create_org(session_factory)
    pid, person = await _personal_login(store, session_factory, org)
    agent_sa, agent = await _agent(session_factory, org)
    await store.set_shared_with_agents(pid, org, user_id=None, service_account_id=person, agent_ids=[agent])
    async with session_factory() as s:
        async with s.begin():
            await s.execute(update(BrowserProfile).where(BrowserProfile.id == pid).values(storage_state_enc=b"junk"))

    with pytest.raises(ValueError):
        await store.shared_state_for(pid, org, agent_sa=agent_sa)
    [row] = await store.list(org, user_id=None, service_account_id=person)
    assert (row.last_used_at, row.last_used_by) == (None, None)


async def test_an_update_whose_new_name_is_taken_changes_nothing(session_factory):
    from sqlalchemy.exc import IntegrityError

    store = _store(session_factory)
    org = await create_org(session_factory)
    pid, person = await _personal_login(store, session_factory, org)
    await store.create(org, user_id=None, service_account_id=person, name="Taken")

    with pytest.raises(IntegrityError):
        await store.update(
            pid, org, user_id=None, service_account_id=person, name="Taken", shared_with_agents=["agent-1"],
        )
    [row] = [r for r in await store.list(org, user_id=None, service_account_id=person) if r.id == pid]
    assert (row.name, row.shared_with_agents) == ("Mine", [])
