"""Where sandbox requests of a session on the user's computer go: the design's DesktopSandbox.

Such a session never gets a cloud sandbox.  Each of its tool calls has a
DeviceCall, which runs sandbox tools' Python handlers here in the worker on
the computer, through one journal runner, so every operation of the call is
numbered in order.  Anything else that asks the sandbox pool for the session
while it is awake is refused: it has no invocation to journal its work under.
Nothing is provisioned, so there is no sandbox to find dead: an offline
computer makes the operation wait, never the pool replace it.
"""

from __future__ import annotations

import json
from contextvars import Token
from typing import TYPE_CHECKING, Any
from uuid import UUID

from surogates.devices.binding import device_of
from surogates.devices.binding import device_owners as _owners
from surogates.devices.operations import DeviceOperations, JournalRunner
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.sandbox.pool import sandbox_session_key

if TYPE_CHECKING:
    from surogates.tools.registry import ToolRegistry

NOT_AVAILABLE = "not available for sessions on a local folder"

# Switched off for sessions on the user's computer in the first release.
UNAVAILABLE_TOOLS = frozenset({"run_coding_agent", "idea_tree", "dispatch_experiments", "merge_experiment"})

DEVICE_SANDBOX_ID = "device"

# The result of a resumed call that cannot be resumed safely: it asked the
# computer for other operations than its first run, or it is a harness tool,
# whose effects off the computer are not in the journal.  What happened cannot
# be told from the journal, and an operation still waiting for the computer
# may happen when it reconnects.
INTERRUPTED = json.dumps({"error": (
    "interrupted: this call was resumed after its worker stopped, and could not be "
    "resumed safely. Some of its effects may have happened, and one still waiting for "
    "the computer may happen when it reconnects. Check the folder before repeating it."
)})


def refusal(name: str) -> str:
    """The tool error for a feature switched off for sessions on a local folder."""
    error = f"This is {NOT_AVAILABLE}" if name.startswith("_") else f"{name} is {NOT_AVAILABLE}"
    if name == "_checkpoint":
        # The saga compensator reads ``success``.
        return json.dumps({"success": False, "error": error})
    return json.dumps({"error": error})


class DeviceCall:
    """One tool call of a session on the user's computer.

    It has the SandboxPool methods tool code calls, so a harness tool handed
    it as its sandbox pool (the expert tool loop, the citation check) reaches
    the computer through the same call.
    """

    def __init__(
        self, *, tools: ToolRegistry, workspace_io: DeviceWorkspaceIO, task_id: str,
        runner: JournalRunner | None = None,
    ) -> None:
        self._tools = tools
        self._workspace_io = workspace_io
        self._task_id = task_id
        self._runner = runner

    @property
    def workspace_io(self) -> DeviceWorkspaceIO:
        return self._workspace_io

    async def diverged(self) -> bool:
        """Whether this call, resumed, took another path than its first run."""
        return self._runner is not None and await self._runner.diverged()

    async def dispatch(self, name: str, args: dict[str, Any]) -> str:
        """Run a sandbox tool's handler on the computer."""
        return await self._tools.dispatch(
            name,
            args,
            workspace_io=self._workspace_io,
            # The folder is the computer's to resolve; no handler may treat
            # it as a path here.
            workspace_path=None,
            task_id=self._task_id,
            tools=self._tools,
        )

    async def ensure(self, session_id: str, spec: Any) -> str:
        return DEVICE_SANDBOX_ID

    async def execute(self, session_id: str, name: str, input: str) -> str:
        if name.startswith("_"):
            # _code and _checkpoint: coding agents and checkpoints are switched off.
            return refusal(name)
        try:
            args = json.loads(input) if input else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"Invalid JSON arguments: {exc}"})
        if not isinstance(args, dict):
            return json.dumps({"error": "Tool arguments must be a JSON object"})
        args.pop("_trace_context", None)
        return await self.dispatch(name, args)


def device_call_for(
    session: Any,
    *,
    tools: ToolRegistry,
    invocation_id: str,
    lease_token: str | None,
    session_factory: Any,
    redis: Any,
) -> DeviceCall:
    """The DeviceCall for one tool call of *session*, journaled under *invocation_id*."""
    # Imported here: both reach surogates.session, whose store imports the
    # harness, which imports this module.
    from surogates.devices.waits import DeviceWaitNotice
    from surogates.session.store import SessionStore

    root = sandbox_session_key(session)
    runner = JournalRunner(
        DeviceOperations(
            session_factory, redis,
            notice=DeviceWaitNotice(SessionStore(session_factory, redis), session_factory),
        ),
        # From the session the server stamped, never from tool input.
        device_id=device_of(session.config),
        root_session_id=UUID(root),
        calling_session_id=session.id,
        invocation_id=invocation_id,
        lease_token=lease_token,
    )
    return DeviceCall(
        tools=tools,
        workspace_io=DeviceWorkspaceIO(runner, root=session.config["workspace_path"]),
        task_id=root,
        runner=runner,
    )


def enter_device_session(session: Any) -> Token[frozenset[str]] | None:
    """Mark this task as working for *session* when it is on the user's computer.

    The sandbox pool then refuses every request for it: inside a tool call
    requests go through that call's DeviceCall instead.
    """
    if device_of(session.config) is None:
        return None
    return _owners.set(frozenset({sandbox_session_key(session), str(session.id)}))


def leave_device_session(token: Token[frozenset[str]] | None) -> None:
    if token is not None:
        _owners.reset(token)


def is_device_owner(session_id: str) -> bool:
    return session_id in _owners.get()
