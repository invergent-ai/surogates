"""A session in its own image: the governance containment check does not judge the image's paths by this host's folder.

The session works in the image's container (its folder ``sandbox_workdir``, e.g. ``/app``); the container is its boundary and
has no storage mount. Judged against the host folder, ``/app/x`` "resolves outside the session workspace" and was refused,
although the file tools run in the container. A session without an image is checked exactly as before; the argument
checks (protected system folders) apply to both.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.tool_exec import execute_single_tool
from surogates.tools.registry import ToolRegistry, ToolSchema

pytestmark = pytest.mark.asyncio

TASK_IMAGE = "registry.example/tasks/repo-fix:2026-11-20"
_ids = iter(range(1, 1_000_000))


def registry_with(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(
            name,
            ToolSchema(
                name=name,
                description=name,
                parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
            ),
            handler=AsyncMock(return_value='{"ok": true}'),
        )
    return registry


def session(tmp_path, **config) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), parent_id=None, agent_id="test-agent", model="test-model",
        config={"workspace_path": str(tmp_path), **config},
    )


async def run(registry, sess, name, args) -> dict:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=lambda *a, **k: next(_ids))
    store.advance_harness_cursor = AsyncMock()
    return await execute_single_tool(
        {"id": "call_1", "function": {"name": name, "arguments": json.dumps(args)}},
        session=sess,
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
        sandbox_pool=None,
        session_factory=MagicMock(),
        redis=MagicMock(),
    )


@pytest.mark.parametrize(("name", "path"), [
    ("read_file", "/app/eigen.py"),
    ("list_files", "/app"),
    ("write_file", "/app/solution.py"),
    ("write_file", "/usr/local/bin/serve.sh"),
])
async def test_a_path_of_the_image_reaches_the_tool(tmp_path, name, path):
    registry = registry_with(name)
    result = await run(registry, session(tmp_path, sandbox_image=TASK_IMAGE, sandbox_workdir="/app"), name,
                       {"path": path, "content": "x"})
    assert "Workspace sandbox violation" not in result["content"]
    registry.get(name).handler.assert_called_once()


async def test_the_protected_system_folders_stay_refused_in_an_image(tmp_path):
    # Only the containment check is left to the container; the argument checks (/etc/, /sys/, /proc/, /dev/) still apply.
    registry = registry_with("write_file")
    result = await run(registry, session(tmp_path, sandbox_image=TASK_IMAGE, sandbox_workdir="/app"), "write_file",
                       {"path": "/etc/nginx/conf.d/site.conf", "content": "x"})
    assert "protected directory" in result["content"]
    registry.get("write_file").handler.assert_not_called()


async def test_without_an_image_the_host_folder_still_bounds_the_paths(tmp_path):
    registry = registry_with("read_file")
    result = await run(registry, session(tmp_path), "read_file", {"path": "/app/eigen.py"})
    assert "Workspace sandbox violation" in result["content"]
    registry.get("read_file").handler.assert_not_called()


async def test_an_empty_image_name_is_no_image(tmp_path):
    registry = registry_with("read_file")
    result = await run(registry, session(tmp_path, sandbox_image=""), "read_file", {"path": "/app/eigen.py"})
    assert "Workspace sandbox violation" in result["content"]
    registry.get("read_file").handler.assert_not_called()
