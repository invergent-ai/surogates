"""The desktop app's OAuth client against the agent's real OAuth routes: the sign-in both sides speak."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
import uvicorn
from jose import jwt as jose_jwt

from .test_desktop_link_client import DESKTOP, built_client  # noqa: F401  (fixtures)
from .test_devices import api  # noqa: F401  (fixtures)

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]

CLIENT = DESKTOP / "dist" / "testing" / "sign-in.js"


@pytest_asyncio.fixture(loop_scope="session")
async def origin(api):
    """The agent, served by a real uvicorn server: its /api prefix is the app's own to strip."""
    server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=0, lifespan="off", log_config=None))
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started, "uvicorn did not start"
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    await task


async def test_the_desktop_signs_in_refreshes_and_signs_out_against_the_agent(built_client, api, origin):
    app = await asyncio.create_subprocess_exec("node", str(CLIENT), "--origin", origin, stdout=asyncio.subprocess.PIPE)
    try:
        assert app.stdout is not None
        opened = json.loads(await asyncio.wait_for(app.stdout.readline(), 30))
        asked = urlsplit(opened["url"])
        assert (asked.path, opened["event"]) == ("/oauth/authorize", "open")
        query = {key: values[0] for key, values in parse_qs(asked.query).items()}
        # The browser: the user is signed in there, and allows the desktop on the consent page.
        allowed = await api.client.post("/v1/auth/oauth/authorize", headers=api.auth(), json={
            key: query[key] for key in ("response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method")
        } | {"decision": "allow"})
        assert allowed.status_code == 200, allowed.text
        async with httpx.AsyncClient() as browser:
            tab = await browser.get(allowed.json()["redirect_to"])
        assert tab.status_code == 200
        assert "Signed in" in tab.text
        events = [json.loads(await asyncio.wait_for(app.stdout.readline(), 30)) for _ in range(3)]
        signed_in_at = jose_jwt.get_unverified_claims(api.token)["auth_time"]
        assert events[0]["authTime"] == signed_in_at
        assert jose_jwt.get_unverified_claims(events[0]["accessToken"])["client_id"] == "surogate-desktop"
        assert events[1] == {"event": "refreshed", "rotated": True, "authTime": signed_in_at}
        assert events[2] == {"event": "signed-out", "refresh": "invalid_grant"}
        assert await asyncio.wait_for(app.wait(), 30) == 0
    finally:
        if app.returncode is None:
            app.kill()
            await app.wait()
