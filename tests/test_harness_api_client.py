"""Tests for surogates.harness.api_client.HarnessAPIClient."""

from __future__ import annotations

import json

import httpx
import pytest

from surogates.harness.api_client import HarnessAPIClient


@pytest.fixture()
def mock_transport():
    """Build a list of (request, response) handlers for httpx mock transport."""
    handlers: list[tuple] = []

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            for method, path, status, body in handlers:
                if request.method == method and request.url.path == path:
                    return httpx.Response(status, json=body)
            return httpx.Response(404, json={"detail": "not found"})

    return handlers, MockTransport()


@pytest.fixture()
def client_with_transport(mock_transport):
    """Create a HarnessAPIClient using the mock transport."""
    handlers, transport = mock_transport
    client = HarnessAPIClient(base_url="http://test", token="test-token")
    # Replace the internal httpx client with one using our mock transport.
    client._client = httpx.AsyncClient(
        base_url="http://test",
        transport=transport,
        headers={"Authorization": "Bearer test-token"},
    )
    return client, handlers


class TestListSkills:
    async def test_returns_json(self, client_with_transport):
        client, handlers = client_with_transport
        handlers.append((
            "GET", "/v1/skills", 200,
            {"skills": [{"name": "s1", "description": "d1", "category": None, "trigger": "/s1"}], "total": 1},
        ))
        result = json.loads(await client.list_skills())
        assert result["success"] is True
        assert result["count"] == 1
        assert result["skills"][0]["name"] == "s1"

    async def test_empty_list(self, client_with_transport):
        client, handlers = client_with_transport
        handlers.append(("GET", "/v1/skills", 200, {"skills": [], "total": 0}))
        result = json.loads(await client.list_skills())
        assert result["count"] == 0


class TestViewSkill:
    async def test_view_skill(self, client_with_transport):
        client, handlers = client_with_transport
        handlers.append((
            "GET", "/v1/skills/my-skill", 200,
            {"name": "my-skill", "description": "desc", "content": "# Skill"},
        ))
        result = json.loads(await client.view_skill("my-skill"))
        assert result["success"] is True
        assert result["name"] == "my-skill"

    async def test_view_skill_forwards_session_id(self, mock_transport):
        """When constructed with a session_id, it is sent as a query parameter."""
        handlers, transport = mock_transport
        captured: list[httpx.Request] = []

        class CapturingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                captured.append(request)
                return httpx.Response(
                    200, json={"name": "s", "content": "c", "staged_at": "/workspace/.skills/s/"},
                )

        client = HarnessAPIClient(
            base_url="http://test",
            token="t",
            session_id="abc-123",
        )
        client._client = httpx.AsyncClient(
            base_url="http://test",
            transport=CapturingTransport(),
            headers={"Authorization": "Bearer t"},
        )

        result = json.loads(await client.view_skill("s"))
        assert result["staged_at"] == "/workspace/.skills/s/"
        assert captured, "expected the client to make a request"
        assert captured[0].url.params.get("session_id") == "abc-123"

    async def test_view_skill_file_forwards_session_id(self, mock_transport):
        """``file_path`` requests also forward ``session_id``."""
        captured: list[httpx.Request] = []

        class CapturingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                captured.append(request)
                return httpx.Response(
                    200,
                    json={
                        "file_path": "assets/t.pptx",
                        "binary": True,
                        "staged_at": "/workspace/.skills/s/",
                        "staged_file_path": "/workspace/.skills/s/assets/t.pptx",
                    },
                )

        client = HarnessAPIClient(
            base_url="http://test", token="t", session_id="sess-9",
        )
        client._client = httpx.AsyncClient(
            base_url="http://test",
            transport=CapturingTransport(),
            headers={"Authorization": "Bearer t"},
        )

        result = json.loads(await client.view_skill("s", file_path="assets/t.pptx"))
        assert result["staged_file_path"] == "/workspace/.skills/s/assets/t.pptx"
        assert captured[0].url.params.get("session_id") == "sess-9"
        assert captured[0].url.params.get("path") == "assets/t.pptx"

    async def test_view_skill_omits_session_id_when_unset(self, mock_transport):
        """Without a session_id, no query param is sent (legacy behaviour)."""
        captured: list[httpx.Request] = []

        class CapturingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                captured.append(request)
                return httpx.Response(200, json={"name": "s", "content": "c"})

        client = HarnessAPIClient(base_url="http://test", token="t")
        client._client = httpx.AsyncClient(
            base_url="http://test",
            transport=CapturingTransport(),
            headers={"Authorization": "Bearer t"},
        )

        await client.view_skill("s")
        assert "session_id" not in captured[0].url.params


class TestCreateSkill:
    async def test_create(self, client_with_transport):
        client, handlers = client_with_transport
        handlers.append((
            "POST", "/v1/skills", 201,
            {"success": True, "message": "Skill 'new' created.", "path": "/skills/new"},
        ))
        result = json.loads(await client.create_skill("new", "---\nname: new\ndescription: d\n---\n\n# Body"))
        assert result["success"] is True


class TestMutateMemory:
    async def test_add(self, client_with_transport):
        client, handlers = client_with_transport
        handlers.append((
            "POST", "/v1/memory", 200,
            {"success": True, "message": "Entry added.", "entries": ["hello"], "usage": "10%", "entry_count": 1},
        ))
        result = json.loads(await client.mutate_memory("add", "memory", content="hello"))
        assert result["success"] is True


class TestErrorHandling:
    async def test_http_error_returns_json(self, client_with_transport):
        client, handlers = client_with_transport
        handlers.append(("POST", "/v1/skills", 422, {"detail": "name is required"}))
        result = json.loads(await client.create_skill("", ""))
        assert result["success"] is False
        assert "name is required" in result["error"]
