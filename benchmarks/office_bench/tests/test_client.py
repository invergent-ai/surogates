"""Client transport against a mocked harness API."""
import json

import httpx
import pytest
import respx

from obench.client import HarnessClient, HarnessError

BASE = "http://harness.test"


@pytest.fixture
def client():
    return HarnessClient(base_url=BASE, token="tok", agent_id="agent-1", retry_window_s=0.0)


@respx.mock
async def test_create_session_declares_studio_channel_and_config(client):
    route = respx.post(f"{BASE}/v1/api/sessions").mock(return_value=httpx.Response(201, json={"id": "sess-1"}))
    async with client:
        assert await client.create_session("studio", {"ops": {"user_id": "u1"}}) == "sess-1"
    request = route.calls.last.request
    assert request.url.params["agent_id"] == "agent-1"
    assert request.headers["authorization"] == "Bearer tok"
    assert json.loads(request.content) == {"system": None, "config": {"ops": {"user_id": "u1"}}, "channel": "studio"}


@respx.mock
async def test_refused_create_keeps_its_status(client):
    respx.post(f"{BASE}/v1/api/sessions").mock(
        return_value=httpx.Response(403, json={"detail": "channel='studio' is reserved"})
    )
    async with client:
        with pytest.raises(HarnessError) as err:
            await client.create_session("studio", {})
    assert err.value.status == 403


@respx.mock
async def test_a_reused_conversation_is_an_error():
    """The web route answers 200 with the agent's one kept session when multi-session is off."""
    respx.post(f"{BASE}/v1/sessions").mock(return_value=httpx.Response(200, json={"id": "old"}))
    async with HarnessClient(base_url=BASE, token="t", agent_id="a", api_prefix="/v1", retry_window_s=0.0) as c:
        with pytest.raises(HarnessError, match="existing conversation"):
            await c.create_session("studio", {})


@respx.mock
async def test_send_message_raises_on_non_2xx(client):
    respx.post(f"{BASE}/v1/api/sessions/s1/messages").mock(return_value=httpx.Response(422, text="bad"))
    async with client:
        with pytest.raises(HarnessError, match="HTTP 422"):
            await client.send_message("s1", "hi")


@respx.mock
async def test_upload_tree_download(client, tmp_path):
    local = tmp_path / "a.docx"
    local.write_bytes(b"PK")
    up = respx.post(f"{BASE}/v1/api/sessions/s1/workspace/upload").mock(
        return_value=httpx.Response(201, json={"path": "a.docx", "size": 2})
    )
    respx.get(f"{BASE}/v1/api/sessions/s1/workspace/tree").mock(return_value=httpx.Response(200, json={
        "root": "b", "truncated": False,
        "entries": [{"name": "d", "path": "d", "kind": "dir", "children": [
            {"name": "x.xlsx", "path": "d/x.xlsx", "kind": "file", "size": 0, "children": None}]},
            {"name": "a.docx", "path": "a.docx", "kind": "file", "size": 2, "children": None}],
    }))
    respx.get(f"{BASE}/v1/api/sessions/s1/workspace/download").mock(return_value=httpx.Response(200, content=b"PKdata"))
    async with client:
        assert await client.upload_file("s1", str(local), "a.docx") == "a.docx"
        assert "path" not in up.calls.last.request.url.params  # the workspace root
        assert await client.get_workspace_tree("s1") == [{"path": "d/x.xlsx", "size": 0}, {"path": "a.docx", "size": 2}]
        assert await client.download_file("s1", "a.docx") == b"PKdata"


@respx.mock
async def test_stream_skips_markers_and_ends_on_done(client):
    body = (
        "id: 5\nevent: user.message\ndata: {\"content\": \"hi\"}\n\n"
        ": keepalive\n\n"
        "id: 6\nevent: llm.response\ndata: {\"model\": \"m\"}\n\n"
        "id: 6\nevent: stream.live\ndata: {}\n\n"
        "event: session.done\ndata: {\"reason\": \"completed\"}\n\n"
        "id: 7\nevent: never.read\ndata: {}\n\n"
    )
    respx.get(f"{BASE}/v1/api/sessions/s1/events").mock(
        return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    )
    async with client:
        events = [e async for e in client.stream_events("s1")]
    assert [(e.id, e.type) for e in events] == [(5, "user.message"), (6, "llm.response")]


@respx.mock
async def test_transient_503_is_retried():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(201, json={"id": "s2"})

    respx.post(f"{BASE}/v1/api/sessions").mock(side_effect=flaky)
    async with HarnessClient(base_url=BASE, token="t", agent_id="a", retry_window_s=10.0) as c:
        assert await c.create_session("studio", {}) == "s2"
    assert calls["n"] == 2


@respx.mock
async def test_delete_session(client):
    route = respx.delete(f"{BASE}/v1/api/sessions/s1").mock(return_value=httpx.Response(204))
    async with client:
        await client.delete_session("s1")
    assert route.called


@respx.mock
async def test_answer_question_answers_each_question_of_the_call(client):
    route = respx.post(f"{BASE}/v1/api/sessions/s1/ask_user_question/call-1/respond").mock(
        return_value=httpx.Response(201, json={"event_id": 42})
    )
    responses = [{"question": "What should the first week slide show?", "answer": "Do what you can.", "is_other": True}]
    async with client:
        assert await client.answer_question("s1", "call-1", responses) == 42
    request = route.calls.last.request
    assert request.url.params["agent_id"] == "agent-1"
    assert json.loads(request.content) == {"responses": responses}


@respx.mock
async def test_a_refused_answer_keeps_its_status(client):
    respx.post(f"{BASE}/v1/api/sessions/s1/ask_user_question/call-1/respond").mock(
        return_value=httpx.Response(404, json={"detail": "Session s1 not found."})
    )
    async with client:
        with pytest.raises(HarnessError) as err:
            await client.answer_question("s1", "call-1", [{"question": "Q?", "answer": "A", "is_other": True}])
    assert err.value.status == 404
