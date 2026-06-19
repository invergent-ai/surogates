import types

from surogates.harness.loop import AgentHarness


class _Pool:
    def __init__(self):
        self.calls = {}

    async def ensure(self, **kw):
        self.calls["ensure"] = kw

    async def destroy_for_session(self, sid):
        self.calls["destroy"] = sid


class _Control:
    def __init__(self):
        self.acquired = None

    async def acquire(self, sid, uid):
        self.acquired = (sid, uid)


class _Lease:
    lease_token = "tok"


class _Store:
    def __init__(self, acquire=True):
        self._acquire = acquire
        self.released = None

    async def try_acquire_lease(self, sid, worker_id, ttl_seconds=None):
        return _Lease() if self._acquire else None

    async def release_lease(self, sid, token):
        self.released = token


def _me(pool, control, store=None):
    return types.SimpleNamespace(
        _browser_pool=pool,
        _browser_control=control,
        _store=store or _Store(),
        _worker_id="w1",
    )


def _session(status, **browser):
    return types.SimpleNamespace(
        id="s1",
        org_id="o1",
        status=status,
        config={"browser": {"setup_owner_user_id": "u1", **browser}},
    )


async def test_browser_setup_provisions_and_grants_control():
    pool, control, store = _Pool(), _Control(), _Store()
    await AgentHarness._run_browser_setup(
        _me(pool, control, store), _session("active", setup_ttl_seconds=900)
    )
    assert pool.calls["ensure"]["session_id"] == "s1"
    assert pool.calls["ensure"]["user_id"] == "u1"
    assert pool.calls["ensure"]["spec"].active_deadline_seconds == 900
    assert control.acquired == ("s1", "u1")
    assert "destroy" not in pool.calls
    assert store.released == "tok"  # lease released in finally


async def test_browser_setup_destroys_on_terminal_status():
    pool, control, store = _Pool(), _Control(), _Store()
    await AgentHarness._run_browser_setup(_me(pool, control, store), _session("completed"))
    assert pool.calls["destroy"] == "s1"
    assert "ensure" not in pool.calls
    assert control.acquired is None
    assert store.released == "tok"


async def test_browser_setup_skips_when_lease_held_elsewhere():
    pool, control = _Pool(), _Control()
    await AgentHarness._run_browser_setup(
        _me(pool, control, _Store(acquire=False)), _session("active")
    )
    assert pool.calls == {}  # another worker holds the lease
    assert control.acquired is None


async def test_browser_setup_noop_without_owner():
    pool = _Pool()
    session = types.SimpleNamespace(
        id="s1", org_id="o1", status="active", config={"browser": {}}
    )
    await AgentHarness._run_browser_setup(_me(pool, None), session)
    assert pool.calls == {}
