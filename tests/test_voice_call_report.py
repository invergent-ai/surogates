"""The voice worker tells ops about each call when it ends (recorded now, charged later)."""
import json
import logging

import httpx

from surogates.runtime.platform_client import PlatformClient

CALL = {"call_id": "SCL_1", "agent_id": "a-1", "number": "+40300000001", "caller": "40722000111",
        "session_id": None, "started_at": "2026-10-05T14:00:00", "ended_at": "2026-10-05T14:00:42",
        "seconds": 42, "outcome": "completed"}


async def test_a_finished_call_is_reported_with_the_runtime_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(path=request.url.path, auth=request.headers["authorization"], body=json.loads(request.content))
        return httpx.Response(204)

    client = PlatformClient(base_url="http://ops", token="rt-token", transport=httpx.MockTransport(handler))
    await client.report_voice_call(**CALL)
    assert seen == {"path": "/api/channels/voice/calls", "auth": "Bearer rt-token", "body": CALL}


async def test_ops_trouble_is_logged_never_raised(caplog):
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    for handler in (down, unreachable):
        client = PlatformClient(base_url="http://ops", token="t", transport=httpx.MockTransport(handler))
        with caplog.at_level(logging.WARNING):
            await client.report_voice_call(**CALL)  # must not raise: a hang-up never fails on reporting
    assert sum("SCL_1" in r.getMessage() for r in caplog.records) == 2
