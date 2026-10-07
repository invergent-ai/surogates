"""Thread pods for tests: the real executor daemon, in process, over temporary folders.

Each pod has its own copy and home.  Every pod mounts one folder as the
project's real files, as every thread's pod mounts the project's
workspace prefix at ``/project``.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import httpx

from surogates.sandbox import executor_server
from surogates.sandbox.base import SandboxSpec, SandboxStatus
from surogates.sandbox.history import History
from surogates.tools.utils.checkpoint_manager import _shadow_repo_path


class ThreadPods:
    """A sandbox backend whose pods are thread pods."""

    def __init__(self, root: Path) -> None:
        if executor_server._REGISTRY is None:
            executor_server.init_registry()
        self.root = root
        self.project = root / "project"
        self.project.mkdir(parents=True, exist_ok=True)
        self.pods: dict[str, httpx.AsyncClient] = {}
        #: Each thread's latest copy, kept after its pod goes.
        self.copies: dict[str, Path] = {}

    async def provision(self, spec: SandboxSpec) -> str:
        sandbox_id = uuid4().hex
        if "HISTORY_THREAD" not in spec.env:
            # A pod with the plain layout: the real files at its workspace.
            app = executor_server.create_app(token="t", workspace=str(self.project), require_fuse=False)
            self.pods[sandbox_id] = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pod")
            return sandbox_id
        copy = self.root / sandbox_id / "workspace"
        copy.mkdir(parents=True)
        thread = spec.env["HISTORY_THREAD"]
        history = History(
            repo=_shadow_repo_path(str(self.project), base=self.root / sandbox_id / "home" / ".surogates" / "history"),
            project=self.project, copy=copy, thread=thread, user=spec.env.get("USER_ID", ""),
        )
        app = executor_server.create_app(token="t", workspace=str(copy), require_fuse=False, history=history)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pod")
        ready = await client.get("/healthz")
        assert ready.status_code == 200, ready.text
        self.pods[sandbox_id] = client
        self.copies[thread] = copy
        return sandbox_id

    async def execute(self, sandbox_id: str, name: str, input: str) -> str:
        response = await self.pods[sandbox_id].post(
            "/execute",
            json={"name": name, "args": json.loads(input or "{}"), "timeout": 60},
            headers={"Authorization": "Bearer t"},
        )
        return response.text

    async def destroy(self, sandbox_id: str) -> None:
        # A pod already gone is destroyed, as a delete answered 404 is.
        client = self.pods.pop(sandbox_id, None)
        if client is not None:
            await client.aclose()

    async def status(self, sandbox_id: str) -> SandboxStatus:
        return SandboxStatus.RUNNING if sandbox_id in self.pods else SandboxStatus.TERMINATED
