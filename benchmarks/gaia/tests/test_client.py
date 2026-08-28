import httpx
import pytest
import respx

from gaia_bench.client import HarnessClient, HarnessError

BASE = "http://localhost:8000"
SID = "11111111-2222-3333-4444-555555555555"


def make_client() -> HarnessClient:
    return HarnessClient(base_url=BASE, token="tok", agent_id="agent-1",
                         retry_window_s=0)


@respx.mock
async def test_create_session_posts_agent_id_and_returns_id():
    route = respx.post(f"{BASE}/v1/api/sessions").mock(
        return_value=httpx.Response(201, json={"id": SID})
    )
    async with make_client() as c:
        assert await c.create_session() == SID
    assert route.calls.last.request.url.params["agent_id"] == "agent-1"
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
async def test_create_session_raises_on_error():
    respx.post(f"{BASE}/v1/api/sessions").mock(
        return_value=httpx.Response(503, text="agent stopped")
    )
    async with make_client() as c:
        with pytest.raises(HarnessError, match="503"):
            await c.create_session()


@respx.mock
async def test_send_message_returns_event_id():
    respx.post(f"{BASE}/v1/api/sessions/{SID}/messages").mock(
        return_value=httpx.Response(202, json={"event_id": 7, "status": "processing"})
    )
    async with make_client() as c:
        assert await c.send_message(SID, "hello") == 7


@respx.mock
async def test_send_message_carries_agent_id():
    # POST /messages resolves the agent context too, not just create.
    # Omitting agent_id here yields 400 "no agent_id in request".
    route = respx.post(f"{BASE}/v1/api/sessions/{SID}/messages").mock(
        return_value=httpx.Response(202, json={"event_id": 7})
    )
    async with make_client() as c:
        await c.send_message(SID, "hello")
    assert route.calls.last.request.url.params["agent_id"] == "agent-1"


@respx.mock
async def test_get_session_status():
    respx.get(f"{BASE}/v1/api/sessions/{SID}").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    async with make_client() as c:
        assert await c.get_session_status(SID) == "completed"


@respx.mock
async def test_get_session_status_carries_agent_id():
    route = respx.get(f"{BASE}/v1/api/sessions/{SID}").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    async with make_client() as c:
        await c.get_session_status(SID)
    assert route.calls.last.request.url.params["agent_id"] == "agent-1"


@respx.mock
async def test_upload_carries_agent_id():
    route = respx.post(f"{BASE}/v1/api/sessions/{SID}/workspace/upload").mock(
        return_value=httpx.Response(201, json={"path": "f.txt", "size": 3})
    )
    async with make_client() as c:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".txt") as fh:
            fh.write(b"abc")
            fh.flush()
            await c.upload_file(SID, fh.name, "f.txt")
    assert route.calls.last.request.url.params["agent_id"] == "agent-1"


@respx.mock
async def test_stream_events_parses_frames_and_stops_on_done():
    body = (
        "id: 1\nevent: user.message\ndata: {\"content\": \"hi\"}\n\n"
        "id: 2\nevent: llm.response\ndata: {\"message\": {\"content\": \"x\"}}\n\n"
        "event: session.done\ndata: {\"reason\": \"completed\"}\n\n"
    )
    respx.get(f"{BASE}/v1/api/sessions/{SID}/events").mock(
        return_value=httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )
    )
    async with make_client() as c:
        events = [e async for e in c.stream_events(SID)]
    assert [e.id for e in events] == [1, 2]
    assert [e.type for e in events] == ["user.message", "llm.response"]
    assert events[1].data["message"]["content"] == "x"


@respx.mock
async def test_stream_events_stops_on_stream_timeout():
    body = (
        "id: 5\nevent: tool.call\ndata: {\"name\": \"web_search\"}\n\n"
        "event: stream.timeout\ndata: {\"reason\": \"max_duration_exceeded\"}\n\n"
    )
    respx.get(f"{BASE}/v1/api/sessions/{SID}/events").mock(
        return_value=httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )
    )
    async with make_client() as c:
        events = [e async for e in c.stream_events(SID, after=4)]
    assert [e.id for e in events] == [5]


@respx.mock
async def test_stream_events_sends_after_cursor():
    route = respx.get(f"{BASE}/v1/api/sessions/{SID}/events").mock(
        return_value=httpx.Response(
            200,
            text="event: session.done\ndata: {}\n\n",
            headers={"content-type": "text/event-stream"},
        )
    )
    async with make_client() as c:
        _ = [e async for e in c.stream_events(SID, after=42)]
    assert route.calls.last.request.url.params["after"] == "42"


@respx.mock
async def test_retries_a_transient_503_then_succeeds():
    # A paused ops server makes the harness 503 instantly, so without this
    # a whole split burns through at full speed on one breakpoint.
    route = respx.post(f"{BASE}/v1/api/sessions").mock(
        side_effect=[
            httpx.Response(503, text="runtime configuration unavailable"),
            httpx.Response(201, json={"id": SID}),
        ]
    )
    c = HarnessClient(base_url=BASE, token="tok", agent_id="agent-1",
                      retry_window_s=30)
    async with c:
        assert await c.create_session() == SID
    assert route.call_count == 2


@respx.mock
async def test_gives_up_on_a_non_transient_error():
    respx.post(f"{BASE}/v1/api/sessions").mock(
        return_value=httpx.Response(403, text="forbidden")
    )
    c = HarnessClient(base_url=BASE, token="tok", agent_id="agent-1",
                      retry_window_s=30)
    async with c:
        with pytest.raises(HarnessError, match="403"):
            await c.create_session()
