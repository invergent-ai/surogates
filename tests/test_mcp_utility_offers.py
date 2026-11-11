"""An MCP server's resources / prompts tools are offered to the model only when it has resources / prompts.

Every client session has ``read_resource`` and ``get_prompt``, so the
``mcp__<server>__read_resource`` / ``get_prompt`` tools used to be offered for
every server: on a benchmark world server that declares neither, a third of
the model's MCP calls went to them, with guessed names.  A kind is offered now
when the server's initialize result declares its capability and its list is
not empty.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from surogates.tools.mcp.client import MCPServerTask, _select_utility_schemas

pytestmark = pytest.mark.asyncio

ALL = ["list_resources", "read_resource", "list_prompts", "get_prompt"]


class MethodNotFound(Exception):
    """As mcp's McpError for JSON-RPC -32601."""

    error = SimpleNamespace(code=-32601, message="Method not found")


class Session:
    """A client session's list calls, with what the server has."""

    def __init__(self, resources=(), templates=(), prompts=(), fails=(), unknown=()):
        self._lists = {"resources": list(resources), "resourceTemplates": list(templates), "prompts": list(prompts)}
        self._fails = set(fails)
        self._unknown = set(unknown)

    async def _list(self, field):
        if field in self._fails:
            raise RuntimeError(f"cannot list {field}")
        if field in self._unknown:  # the server implements no such list
            raise MethodNotFound()
        return SimpleNamespace(**{field: self._lists[field]})

    async def list_resources(self):
        return await self._list("resources")

    async def list_resource_templates(self):
        return await self._list("resourceTemplates")

    async def list_prompts(self):
        return await self._list("prompts")

    async def read_resource(self, uri):  # pragma: no cover - present as on every ClientSession
        raise NotImplementedError

    async def get_prompt(self, name, arguments=None):  # pragma: no cover
        raise NotImplementedError


async def _offered(capabilities, session) -> list[str]:
    server = MCPServerTask("srv")
    server.session = session
    server._server_capabilities = capabilities
    await server._discover_offers()
    return [entry["handler_key"] for entry in _select_utility_schemas("srv", server, {})]


def caps(*, resources=False, prompts=False):
    return SimpleNamespace(resources=SimpleNamespace() if resources else None,
                           prompts=SimpleNamespace() if prompts else None)


async def test_a_server_that_declares_neither_is_offered_none():
    assert await _offered(caps(), Session(resources=["r"], prompts=["p"])) == []


async def test_a_server_with_resources_only_is_offered_the_resource_tools():
    assert await _offered(caps(resources=True), Session(resources=["r"])) == ["list_resources", "read_resource"]


async def test_a_server_with_prompts_only_is_offered_the_prompt_tools():
    assert await _offered(caps(prompts=True), Session(prompts=["p"])) == ["list_prompts", "get_prompt"]


async def test_a_server_with_both_is_offered_all_four_as_before():
    assert await _offered(caps(resources=True, prompts=True), Session(resources=["r"], prompts=["p"])) == ALL


async def test_a_declared_kind_with_nothing_in_it_is_not_offered():
    assert await _offered(caps(resources=True, prompts=True), Session()) == []


async def test_resource_templates_count_as_resources():
    assert await _offered(caps(resources=True), Session(templates=["file://{path}"])) == ["list_resources", "read_resource"]


async def test_a_server_without_a_templates_list_and_no_resources_is_offered_none():
    """A low-level server that implements resources/list only: an empty list, and no templates method."""
    assert await _offered(caps(resources=True), Session(unknown={"resourceTemplates"})) == []
    assert await _offered(caps(resources=True), Session(resources=["r"], unknown={"resourceTemplates"})) == [
        "list_resources", "read_resource"]


async def test_a_list_that_cannot_be_read_keeps_its_tools():
    offered = await _offered(caps(resources=True, prompts=True), Session(fails={"resources", "prompts"}))
    assert offered == ALL


async def test_a_server_not_connected_yet_is_offered_none():
    server = MCPServerTask("srv")
    server.session = Session(resources=["r"])
    assert [e["handler_key"] for e in _select_utility_schemas("srv", server, {})] == []


async def test_the_config_can_still_turn_a_kind_off():
    server = MCPServerTask("srv")
    server.session = Session(resources=["r"], prompts=["p"])
    server._server_capabilities = caps(resources=True, prompts=True)
    await server._discover_offers()
    keys = [e["handler_key"] for e in _select_utility_schemas("srv", server, {"tools": {"prompts": False}})]
    assert keys == ["list_resources", "read_resource"]


@pytest.mark.parametrize("with_resource, with_prompt, expected", [
    (False, False, []),
    (True, False, ["list_resources", "read_resource"]),
    (False, True, ["list_prompts", "get_prompt"]),
    (True, True, ALL),
])
async def test_a_real_fastmcp_server(with_resource, with_prompt, expected):
    """FastMCP declares resources and prompts on every server, empty or not: the list decides."""
    app = FastMCP("world")

    @app.tool()
    def lookup(key: str) -> str:
        return key

    if with_resource:
        @app.resource("rules://world")
        def rules() -> str:
            return "the rules"

    if with_prompt:
        @app.prompt()
        def brief() -> str:
            return "brief me"

    async with create_connected_server_and_client_session(app._mcp_server) as session:
        server = MCPServerTask("world")
        server._server_capabilities = session.get_server_capabilities()  # what its initialize result declared
        server.session = session
        await server._discover_tools()
        keys = [e["handler_key"] for e in _select_utility_schemas("world", server, {})]
    assert keys == expected
    assert [t.name for t in server._tools] == ["lookup"]
