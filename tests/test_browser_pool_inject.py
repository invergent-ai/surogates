from surogates.browser.base import BrowserEndpoint, BrowserSpec, BrowserStatus
from surogates.browser.pool import BrowserPool


def _make_recording_emitter(order):
    async def _emit(session_id, event_type, data):
        order.append("event")

    return _emit


class _FakeBackend:
    async def provision(self, spec, *, session_id, org_id, user_id):
        return "bid-1", BrowserEndpoint(
            rest_url="http://b:10001",
            cdp_url="ws://b:9222",
            live_view_url="ws://b:8080",
        )

    async def status(self, browser_id):
        return BrowserStatus.RUNNING


async def test_inject_applies_state_before_registry_publish(monkeypatch):
    order = []

    class _Registry:
        async def set(self, entry):
            order.append("registry")

        async def get(self, session_id):
            return None

    applied = {}

    class _FakeClient:
        def __init__(self, rest_url, **kw):
            applied["rest_url"] = rest_url

        async def apply_storage_state(self, state):
            order.append("apply")
            applied["state"] = state

        async def close(self):
            pass

    monkeypatch.setattr("surogates.browser.pool.KernelBrowserClient", _FakeClient)

    pool = BrowserPool(
        backend=_FakeBackend(),
        registry=_Registry(),
        event_emitter=_make_recording_emitter(order),
    )
    spec = BrowserSpec(storage_state={"cookies": [{"name": "SID"}], "origins": []})
    await pool.ensure(session_id="s1", org_id="o1", user_id="u1", spec=spec)

    assert order.index("apply") < order.index("registry")
    assert applied["state"]["cookies"][0]["name"] == "SID"
    assert applied["rest_url"] == "http://b:10001"
