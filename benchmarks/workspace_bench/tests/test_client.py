"""Client transport against a mocked harness API."""
import httpx
import pytest
import respx

from wsbench.client import HarnessClient, HarnessError

BASE = "http://harness.test"


@pytest.fixture
def client():
    return HarnessClient(base_url=BASE, token="tok", agent_id="agent-1")


@respx.mock
async def test_create_session_sends_agent_id(client):
    route = respx.post(f"{BASE}/v1/api/sessions").mock(
        return_value=httpx.Response(200, json={"id": "sess-1"})
    )
    async with client:
        assert await client.create_session() == "sess-1"
    assert route.calls.last.request.url.params["agent_id"] == "agent-1"


@respx.mock
async def test_upload_file_nested_subdir(client, tmp_path):
    local = tmp_path / "a.md"
    local.write_text("hello")
    route = respx.post(f"{BASE}/v1/api/sessions/s1/workspace/upload").mock(
        return_value=httpx.Response(
            201, json={"path": "workdir/sub/a.md", "size": 5}
        )
    )
    async with client:
        key = await client.upload_file(
            "s1", str(local), "a.md", subdir="workdir/sub"
        )
    assert key == "workdir/sub/a.md"
    request = route.calls.last.request
    assert request.url.params["path"] == "workdir/sub"
    assert b"hello" in request.read()


@respx.mock
async def test_workspace_tree_flattens_nested_entries(client):
    respx.get(f"{BASE}/v1/api/sessions/s1/workspace/tree").mock(
        return_value=httpx.Response(200, json={
            "root": "bucket",
            "entries": [
                {"name": "workdir", "path": "workdir", "kind": "dir",
                 "children": [
                     {"name": "a.md", "path": "workdir/a.md", "kind": "file",
                      "size": 5, "children": None},
                 ]},
                {"name": "out.md", "path": "out.md", "kind": "file",
                 "size": 7, "children": None},
            ],
            "truncated": False,
        })
    )
    async with client:
        files = await client.get_workspace_tree("s1")
    assert files == [
        {"path": "workdir/a.md", "size": 5},
        {"path": "out.md", "size": 7},
    ]


@respx.mock
async def test_download_file(client):
    respx.get(f"{BASE}/v1/api/sessions/s1/workspace/download").mock(
        return_value=httpx.Response(200, content=b"\x00bin")
    )
    async with client:
        blob = await client.download_file("s1", "out.md")
    assert blob == b"\x00bin"


@respx.mock
async def test_non_success_raises(client):
    respx.post(f"{BASE}/v1/api/sessions").mock(
        return_value=httpx.Response(400, text="nope")
    )
    async with client:
        with pytest.raises(HarnessError, match="HTTP 400"):
            await client.create_session()


@respx.mock
async def test_transient_503_is_retried():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="paused")
        return httpx.Response(200, json={"id": "sess-2"})

    respx.post(f"{BASE}/v1/api/sessions").mock(side_effect=flaky)
    async with HarnessClient(
        base_url=BASE, token="tok", agent_id="a", retry_window_s=10.0
    ) as client:
        assert await client.create_session() == "sess-2"
    assert calls["n"] == 2
