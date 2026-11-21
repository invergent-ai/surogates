"""A session in its own image: the host-side containment check is left out only for calls that run in its container.

The session works in the image's container (its folder ``sandbox_workdir``, e.g. ``/app``); the container is its
boundary and has no storage mount. Judged against the host folder, ``/app/x`` "resolves outside the session workspace"
and was refused although the call runs in the container. The check is now left out exactly when the call goes to the
container of an image the operator's list admits, on a backend that runs session images; everywhere else (no image,
an image the list refuses, another backend, no sandbox pool so the harness-local handler would read this host, a
picture that the worker reads for the vision model) it applies as before. The argument checks (protected system
folders) apply in every case.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import surogates.config as config_module
import surogates.harness.tool_exec as tool_exec
from surogates.harness.tool_exec import _runs_in_its_image, execute_single_tool
from surogates.sandbox.base import SandboxSpec
from surogates.tools.registry import ToolRegistry, ToolSchema

pytestmark = pytest.mark.asyncio

TASK_IMAGE = "registry.example/tasks/repo-fix:2026-11-20"
_ids = iter(range(1, 1_000_000))
VIOLATION = "Workspace sandbox violation"


@pytest.fixture(autouse=True)
def operator_settings(monkeypatch):
    """The operator admits registry.example/tasks/* on Docker (each test may change it)."""
    real = config_module.load_settings()
    sandbox = real.sandbox.model_copy(
        update={"backend": "docker", "allowed_images": ["registry.example/tasks/*"], "require_image_digest": False},
    )
    settings = real.model_copy(update={"sandbox": sandbox})
    monkeypatch.setattr(config_module, "load_settings", lambda: settings)
    monkeypatch.setattr(tool_exec, "_build_session_sandbox_spec", AsyncMock(return_value=SandboxSpec()))
    return sandbox


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
            handler=AsyncMock(return_value='{"ran": "harness"}'),
        )
    return registry


def container_pool() -> MagicMock:
    pool = MagicMock()
    pool.ensure = AsyncMock()
    pool.execute = AsyncMock(return_value='{"ran": "container"}')
    return pool


def session(tmp_path, **config) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), parent_id=None, agent_id="test-agent", model="test-model",
        config={"workspace_path": str(tmp_path), **config},
    )


def image_session(tmp_path, **extra) -> SimpleNamespace:
    return session(tmp_path, sandbox_image=TASK_IMAGE, sandbox_workdir="/app", **extra)


async def run(registry, sess, name, args, *, sandbox_pool) -> dict:
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
        sandbox_pool=sandbox_pool,
        session_factory=MagicMock(),
        redis=MagicMock(),
    )


# ── the call runs in the image's container: its paths reach it ──────────────


@pytest.mark.parametrize(("name", "path"), [
    ("read_file", "/app/eigen.py"),
    ("list_files", "/app"),
    ("search_files", "/app/src"),
    ("write_file", "/app/solution.py"),
    ("patch", "/app/solution.py"),
    ("write_file", "/usr/local/bin/serve.sh"),
])
async def test_a_path_of_the_image_reaches_its_container(tmp_path, name, path):
    registry, pool = registry_with(name), container_pool()
    result = await run(registry, image_session(tmp_path), name, {"path": path, "content": "x"}, sandbox_pool=pool)
    assert VIOLATION not in result["content"]
    pool.execute.assert_awaited_once()
    assert pool.execute.await_args.args[1] == name
    registry.get(name).handler.assert_not_called()


async def test_the_protected_system_folders_stay_refused_in_an_image(tmp_path):
    registry, pool = registry_with("write_file"), container_pool()
    result = await run(registry, image_session(tmp_path), "write_file",
                       {"path": "/etc/nginx/conf.d/site.conf", "content": "x"}, sandbox_pool=pool)
    assert "protected directory" in result["content"]
    pool.execute.assert_not_called()


# ── anything that would not run in the container keeps the host check ──────


async def test_without_an_image_the_host_folder_still_bounds_the_paths(tmp_path):
    registry, pool = registry_with("read_file"), container_pool()
    result = await run(registry, session(tmp_path), "read_file", {"path": "/app/eigen.py"}, sandbox_pool=pool)
    assert VIOLATION in result["content"]
    pool.execute.assert_not_called()


async def test_an_empty_image_name_is_no_image(tmp_path):
    registry, pool = registry_with("read_file"), container_pool()
    result = await run(registry, session(tmp_path, sandbox_image=""), "read_file", {"path": "/app/eigen.py"},
                       sandbox_pool=pool)
    assert VIOLATION in result["content"]
    pool.execute.assert_not_called()


@pytest.mark.parametrize("path", ["/app/eigen.py", "/etc/hostname", "/var/lib/secret.txt", "../../outside.txt"])
async def test_an_image_session_without_a_container_cannot_reach_this_hosts_files(tmp_path, path):
    # No sandbox pool: the harness-local handler would read this host's filesystem, so the host check applies.
    registry = registry_with("read_file")
    result = await run(registry, image_session(tmp_path), "read_file", {"path": path}, sandbox_pool=None)
    assert VIOLATION in result["content"] or "protected directory" in result["content"]
    registry.get("read_file").handler.assert_not_called()


async def test_an_image_the_operator_does_not_admit_keeps_the_host_check(tmp_path, operator_settings):
    operator_settings.allowed_images = ["registry.example/other/*"]
    registry, pool = registry_with("read_file"), container_pool()
    result = await run(registry, image_session(tmp_path), "read_file", {"path": "/app/eigen.py"}, sandbox_pool=pool)
    assert VIOLATION in result["content"]
    pool.execute.assert_not_called()


async def test_a_backend_without_session_images_keeps_the_host_check(tmp_path, operator_settings):
    operator_settings.backend = "process"
    registry, pool = registry_with("read_file"), container_pool()
    result = await run(registry, image_session(tmp_path), "read_file", {"path": "/app/eigen.py"}, sandbox_pool=pool)
    assert VIOLATION in result["content"]
    pool.execute.assert_not_called()


async def test_a_picture_read_by_the_worker_keeps_the_host_check(tmp_path):
    # read_file of a picture is read in the worker for the vision model, not in the container.
    registry, pool = registry_with("read_file"), container_pool()
    result = await run(registry, image_session(tmp_path), "read_file", {"path": "/app/code.png"}, sandbox_pool=pool)
    assert VIOLATION in result["content"]
    pool.execute.assert_not_called()


def test_only_calls_into_the_container_leave_the_host_check_out(tmp_path):
    pool = container_pool()
    img = image_session(tmp_path)
    assert _runs_in_its_image(img, "read_file", {"path": "/app/x.py"}, sandbox_pool=pool)
    assert _runs_in_its_image(img, "terminal", {"command": "ls", "workdir": "/app"}, sandbox_pool=pool)
    assert not _runs_in_its_image(img, "memory", {"path": "/app/x"}, sandbox_pool=pool)  # a harness tool
    assert not _runs_in_its_image(img, "mcp__fs__read", {"path": "/app/x"}, sandbox_pool=pool)
    assert not _runs_in_its_image(img, "read_file", {"path": "/app/x.py"}, sandbox_pool=None)
    assert not _runs_in_its_image(session(tmp_path), "read_file", {"path": "/app/x.py"}, sandbox_pool=pool)
