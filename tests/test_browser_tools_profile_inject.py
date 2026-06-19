import uuid

from surogates.browser.base import BrowserEndpoint
from surogates.tools.builtin.browser import _resolve_session_browser


class _Tenant:
    def __init__(self, org_id, user_id=None, service_account_id=None):
        self.org_id = org_id
        self.user_id = user_id
        self.service_account_id = service_account_id


class _Pool:
    def __init__(self):
        self.spec = None

    async def ensure(self, *, session_id, org_id, user_id, spec):
        self.spec = spec
        from surogates.browser.pool import EnsureResult

        return EnsureResult(
            "bid", BrowserEndpoint("http://b", "ws://c", "ws://l"), True, {}
        )


class _Store:
    def __init__(self, state):
        self._state = state
        self.touched = False

    async def storage_state_for(
        self, profile_id, org_id, *, user_id, service_account_id
    ):
        return self._state

    async def touch_last_used(self, *a, **k):
        self.touched = True


async def test_profile_state_is_set_on_spec():
    org, sa, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pool = _Pool()
    store = _Store({"cookies": [{"name": "SID"}], "origins": []})
    result = await _resolve_session_browser(
        tenant=_Tenant(org, service_account_id=sa),
        session_id="s1",
        browser_pool=pool,
        browser_control=None,
        browser_profile_store=store,
        session_config={
            "browser": {"profile_id": str(pid)},
            "service_account_id": str(sa),
        },
    )
    assert not isinstance(result, str)
    assert pool.spec.storage_state == {"cookies": [{"name": "SID"}], "origins": []}
    assert store.touched is True


async def test_profile_state_read_from_pool_when_no_explicit_store():
    # Production path: handlers don't thread the store; it rides on the pool.
    org, sa, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pool = _Pool()
    pool.browser_profile_store = _Store({"cookies": [{"name": "T"}], "origins": []})
    result = await _resolve_session_browser(
        tenant=_Tenant(org, service_account_id=sa),
        session_id="s1",
        browser_pool=pool,
        browser_control=None,
        session_config={
            "browser": {"profile_id": str(pid)},
            "service_account_id": str(sa),
        },
    )
    assert not isinstance(result, str)
    assert pool.spec.storage_state == {"cookies": [{"name": "T"}], "origins": []}
    assert pool.browser_profile_store.touched is True
