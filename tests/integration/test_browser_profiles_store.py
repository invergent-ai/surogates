import pytest
from sqlalchemy import select

from surogates.db.models import BrowserProfile

from .conftest import create_org, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")


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
