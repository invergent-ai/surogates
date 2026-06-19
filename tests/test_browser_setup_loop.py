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


def _session(status, **browser):
    return types.SimpleNamespace(
        id="s1",
        org_id="o1",
        status=status,
        config={"browser": {"setup_owner_user_id": "u1", **browser}},
    )


async def test_browser_setup_provisions_and_grants_control():
    pool, control = _Pool(), _Control()
    me = types.SimpleNamespace(_browser_pool=pool, _browser_control=control)
    await AgentHarness._run_browser_setup(me, _session("active", setup_ttl_seconds=900))
    assert pool.calls["ensure"]["session_id"] == "s1"
    assert pool.calls["ensure"]["user_id"] == "u1"
    assert pool.calls["ensure"]["spec"].active_deadline_seconds == 900
    assert control.acquired == ("s1", "u1")
    assert "destroy" not in pool.calls


async def test_browser_setup_destroys_on_terminal_status():
    pool, control = _Pool(), _Control()
    me = types.SimpleNamespace(_browser_pool=pool, _browser_control=control)
    await AgentHarness._run_browser_setup(me, _session("completed"))
    assert pool.calls["destroy"] == "s1"
    assert "ensure" not in pool.calls
    assert control.acquired is None


async def test_browser_setup_noop_without_owner():
    pool = _Pool()
    me = types.SimpleNamespace(_browser_pool=pool, _browser_control=None)
    session = types.SimpleNamespace(
        id="s1", org_id="o1", status="active", config={"browser": {}}
    )
    await AgentHarness._run_browser_setup(me, session)
    assert pool.calls == {}
