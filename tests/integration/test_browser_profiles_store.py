import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

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
