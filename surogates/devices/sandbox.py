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
import logging
import weakref
from contextvars import Token
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from surogates.devices.binding import device_of
from surogates.devices.binding import device_owners as _owners
from surogates.devices.operations import DeviceOperations, JournalRunner
from surogates.devices.workspace import DeviceOperationError, DeviceWorkspaceIO
from surogates.sandbox.pool import sandbox_session_key
from surogates.tools.builtin.file_ops import forget_read_tracker, patch_targets
from surogates.tools.utils.tool_result_storage import HARNESS_FOLDER_REFUSAL, in_harness_folder
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError

if TYPE_CHECKING:
    from surogates.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

NOT_AVAILABLE = "not available for sessions on a local folder"

# Switched off for sessions on the user's computer in the first release.
UNAVAILABLE_TOOLS = frozenset({"run_coding_agent", "idea_tree", "dispatch_experiments", "merge_experiment"})

DEVICE_SANDBOX_ID = "device"

# The result of a resumed call that cannot be resumed safely: it asked the
# computer for other operations than its first run, or it is a harness tool,
# whose effects off the computer are not in the journal.  What happened cannot
# be told from the journal.  What its first run left open is cancelled, so
# nothing happens later.
INTERRUPTED = json.dumps({"error": (
    "interrupted: this call was resumed after its worker stopped, and could not be "
    "resumed safely. Some of its effects may have happened. Check the folder before "
    "repeating it."
)})
# A browser tool's: its effects are on the page in the user's browser, not in the folder.
INTERRUPTED_IN_BROWSER = json.dumps({"error": (
    "interrupted: this call was resumed after its worker stopped, and could not be "
    "resumed safely. Some of its effects may have happened on the page. Read it with "
    "browser_get_state before repeating it."
)})


def interrupted(tool_name: str) -> str:
    """The result of *tool_name*'s call that could not be resumed safely, in its own words."""
    return INTERRUPTED_IN_BROWSER if tool_name.startswith("browser_") else INTERRUPTED


def refusal(name: str) -> str:
    """The tool error for a feature switched off for sessions on a local folder."""
    error = f"This is {NOT_AVAILABLE}" if name.startswith("_") else f"{name} is {NOT_AVAILABLE}"
    if name == "_checkpoint":
        # The saga compensator reads ``success``.
        return json.dumps({"success": False, "error": error})
    return json.dumps({"error": error})


async def harness_folder_refusal(workspace_io: DeviceWorkspaceIO, name: str, args: Any) -> str | None:
    """The model's own write_file or patch into the harness's folder, refused; else None.

    Each target is judged as the computer resolves it, as Ask every time
    judges a write.  One the computer will not resolve is the handler's to
    refuse, in the computer's words.  A JSON string is judged as the
    arguments it encodes, which ``ToolRegistry.dispatch`` parses and runs.
    """
    if name not in ("write_file", "patch"):
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return None  # the registry refuses it
    if not isinstance(args, dict):
        return None
    for path in [args.get("path")] if name == "write_file" else patch_targets(args):
        if not isinstance(path, str) or not path:
            continue
        try:
            key = await workspace_io.resolve(path)
        except (OSError, ValueError, WorkspaceSandboxError, DeviceOperationError):
            continue
        if in_harness_folder(key):
            return json.dumps({"error": HARNESS_FOLDER_REFUSAL})
    return None


class DeviceCall:
    """One tool call of a session on the user's computer.

    It has the SandboxPool methods tool code calls, so a harness tool handed
    it as its sandbox pool (the expert tool loop, the citation check) reaches
    the computer through the same call.  Their tool calls are another
    conversation, so they keep their own reads, for as long as the call.
    """

    def __init__(
        self, *, tools: ToolRegistry, workspace_io: DeviceWorkspaceIO, task_id: str, read_tracker_id: str,
        runner: JournalRunner | None = None,
    ) -> None:
        self._tools = tools
        self._workspace_io = workspace_io
        self._task_id = task_id
        self._read_tracker_id = read_tracker_id
        self._harness_tool_tracker_id = f"{read_tracker_id}:{uuid4()}"
        # Dropped once the call is collected, however it ends: a cancel or a
        # lost lease skips the call's last steps.
        weakref.finalize(self, forget_read_tracker, self._harness_tool_tracker_id)
        self._runner = runner

    @property
    def workspace_io(self) -> DeviceWorkspaceIO:
        return self._workspace_io

    async def diverged(self) -> bool:
        """Whether this call, resumed, took another path than its first run."""
        return self._runner is not None and await self._runner.diverged()

    async def close_open(self) -> None:
        """Cancel what this call's first run left open on the computer."""
        if self._runner is not None:
            await self._runner.close_open()

    async def consumed(self) -> None:
        """This call's result is committed: what it read in transfers may go, a day later.

        Best effort: one left unmarked goes as an orphan instead.
        """
        if self._runner is None:
            return
        try:
            await self._runner.consumed()
        except Exception:
            logger.warning("could not mark what a tool call read as consumed", exc_info=True)

    async def dispatch(self, name: str, args: dict[str, Any] | str, *, read_tracker_id: str | None = None) -> str:
        """Run a model's sandbox tool call on the computer, reading as the session unless *read_tracker_id* says.

        The model is the session's own, or an expert's through :meth:`execute`:
        its write into the harness's own folder is refused, whichever loop
        sent it.  The harness writes there through :meth:`spill`.
        """
        refused = await harness_folder_refusal(self._workspace_io, name, args)
        if refused is not None:
            return refused
        return await self._run(name, args, read_tracker_id or self._read_tracker_id)

    async def spill(self, path: str, content: str) -> str:
        """The harness's own write_file of a result too long to keep in context, into its own folder."""
        return await self._run("write_file", {"path": path, "content": content}, self._harness_tool_tracker_id)

    async def _run(self, name: str, args: dict[str, Any] | str, read_tracker_id: str) -> str:
        return await self._tools.dispatch(
            name,
            args,
            workspace_io=self._workspace_io,
            # The folder is the computer's to resolve; no handler may treat
            # it as a path here.
            workspace_path=None,
            task_id=self._task_id,
            # What this session read, apart from its root and its sub-agents:
            # a result in one's conversation is not in the others'.
            read_tracker_id=read_tracker_id,
            tools=self._tools,
        )

    async def ensure(self, session_id: str, spec: Any) -> str:
        return DEVICE_SANDBOX_ID

    async def execute(self, session_id: str, name: str, input: str) -> str:
        """A sandbox tool call a harness tool makes through this call.

        An expert's tool loop makes its model's here, so they are refused as
        the session's own are (see :meth:`dispatch`).
        """
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
        return await self.dispatch(name, args, read_tracker_id=self._harness_tool_tracker_id)


def device_call_for(
    session: Any,
    *,
    tools: ToolRegistry,
    invocation_id: str,
    lease_token: str | None,
    session_factory: Any,
    redis: Any,
    resumed: bool = False,
) -> DeviceCall:
    """The DeviceCall for one tool call of *session*, journaled under *invocation_id*.

    A *resumed* call neither finds nor keeps a document in the cache: a hit
    would skip a read its first run asked for, and a call that asks for less
    reads as interrupted.
    """
    # Imported here: both reach surogates.session, whose store imports the
    # harness, which imports this module.
    from surogates.devices.waits import DeviceWaitNotice
    from surogates.session.store import SessionStore

    root = sandbox_session_key(session)
    # From the session the server stamped, never from tool input.
    device_id = device_of(session.config)
    runner = JournalRunner(
        DeviceOperations(
            session_factory, redis,
            notice=DeviceWaitNotice(SessionStore(session_factory, redis), session_factory),
        ),
        device_id=device_id,
        root_session_id=UUID(root),
        calling_session_id=session.id,
        invocation_id=invocation_id,
        lease_token=lease_token,
    )
    return DeviceCall(
        tools=tools,
        workspace_io=DeviceWorkspaceIO(
            runner, root=session.config["workspace_path"], identity=f"device:{device_id}",
            caches_documents=not resumed,
        ),
        task_id=root,
        read_tracker_id=str(session.id),
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
