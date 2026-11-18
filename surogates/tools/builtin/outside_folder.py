"""Files outside a desktop chat's folder: ask for a folder, copy files, move them to the trash, put a copy back.

Each tool is one operation on the user's computer (surogates.devices.workspace,
the outside.* kinds), and each shows its user a window there: the answer is
what the computer did, or why it did not, as the tool's result.  Reading there
is the file tools' own (DeviceWorkspaceIO sends an absolute path outside the
folder to the outside.* reads).  Offered only in a chat on a computer, and
refused in a project's thread (surogates.harness.tool_exec).
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Callable
from typing import Any

from surogates.devices.binding import inside_folder, normal_path, tools_path
from surogates.devices.workspace import MAX_MESSAGE_CHARS, TOO_LARGE, DeviceWorkspaceIO
from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.utils.workspace_sandbox import NUL_REFUSED, holds_nul

OUTSIDE_FOLDER_TOOLS: frozenset[str] = frozenset({"request_folder_access", "copy_files", "move_to_trash", "put_back"})
IN_A_THREAD = "Files outside the chat's folder can be used from the main chat, not from a project's thread."
_NOT_ON_A_COMPUTER = "Files outside the chat's folder are reached only in a chat on a folder of the user's computer."


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _absolute(path: str, root: str) -> str:
    """*path*, relative to the chat's folder *root* (``.`` is the folder) or absolute, as the computer takes it.

    Raises ValueError for a relative path that leads out of the folder, and for one through ``~``: the chat's own
    home is a private scratch area, not the user's.  A file outside the folder is named by its absolute path, on a
    Windows computer as its user does too (``C:\\a`` is ``/mnt/c/a``).
    """
    path = tools_path(path, root)
    if path.startswith("/"):
        return normal_path(path)
    if path == "~" or path.startswith("~/"):
        raise ValueError(f"'{path}' is in this chat's private scratch area, not the user's: give an absolute path.")
    root = normal_path(root)
    joined = posixpath.normpath(posixpath.join(root, path))
    if not inside_folder(joined, root):
        raise ValueError(f"'{path}' leads out of the chat's folder: give the file outside it by its absolute path.")
    return joined


def _paths(value: Any) -> list[str] | None:
    """*value* as a list of paths, stripped, or None when it is not one."""
    if not isinstance(value, list) or not value or not all(isinstance(p, str) and p.strip() for p in value):
        return None
    return [p.strip() for p in value]


async def _ask(kind: str, args: dict[str, Any], workspace_io: Any) -> str:
    """Run *kind* on the computer and answer its outcome: its ok value as JSON, or its refusal's words."""
    # Before the user is asked about what can never run, as the file tools refuse it.
    if holds_nul(*(v for value in args.values() for v in (value if isinstance(value, list) else [value]))):
        return _error(NUL_REFUSED)
    if len(json.dumps(args)) > MAX_MESSAGE_CHARS:
        return _error(TOO_LARGE)
    outcome = await workspace_io.runner.run(kind, args)
    error = outcome.get("error")
    if isinstance(error, dict):
        # An app older than these kinds is answered by the link, in OUTSIDE_OLD_APP's words (NO_OUTSIDE_OUTCOME); one
        # that knows them says in its own why it did not, a kind it does not know among them.
        return _error(str(error.get("message") or f"The computer refused {kind}"))
    if "ok" not in outcome:
        return _error(f"The computer returned no result for {kind}")
    return json.dumps(outcome["ok"], ensure_ascii=False)


def _computer(kwargs: dict[str, Any]) -> DeviceWorkspaceIO | None:
    workspace_io = kwargs.get("workspace_io")
    return workspace_io if isinstance(workspace_io, DeviceWorkspaceIO) else None


async def _on_computer(kind: str, kwargs: dict[str, Any], build: Callable[[str], dict[str, Any]]) -> str:
    """Run *kind* with the args *build* makes from the chat's folder, in a chat on a computer; a ValueError's words otherwise."""
    computer = _computer(kwargs)
    if computer is None:
        return _error(_NOT_ON_A_COMPUTER)
    try:
        args = build(computer.root)
    except ValueError as exc:
        return _error(str(exc))
    return await _ask(kind, args, computer)


async def _request_folder_access(arguments: dict[str, Any], **kwargs: Any) -> str:
    def build(root: str) -> dict[str, Any]:
        folder = arguments.get("folder")
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError("'folder' is Desktop, Downloads, Documents, Pictures, or an absolute path.")
        folder = tools_path(folder.strip(), root)
        # A known folder's name goes as it is, for the computer to find; a path, normalised.
        return {"folder": normal_path(folder) if folder.startswith("/") else folder}

    return await _on_computer("outside.grant", kwargs, build)


async def _copy_files(arguments: dict[str, Any], **kwargs: Any) -> str:
    def build(root: str) -> dict[str, Any]:
        files, to = _paths(arguments.get("files")), arguments.get("to")
        if files is None or not isinstance(to, str) or not to.strip():
            raise ValueError("'files' lists the files to copy, and 'to' is the folder to copy them into ('.' is the chat's).")
        return {"files": [_absolute(f, root) for f in files], "to": _absolute(to.strip(), root)}

    return await _on_computer("outside.copy", kwargs, build)


async def _move_to_trash(arguments: dict[str, Any], **kwargs: Any) -> str:
    def build(root: str) -> dict[str, Any]:
        files = _paths(arguments.get("files"))
        if files is None:
            raise ValueError("'files' lists the files to move to the trash.")
        return {"files": [_absolute(f, root) for f in files]}

    return await _on_computer("outside.trash", kwargs, build)


async def _put_back(arguments: dict[str, Any], **kwargs: Any) -> str:
    def build(root: str) -> dict[str, Any]:
        copy, original = arguments.get("working_copy"), arguments.get("original")
        if not isinstance(copy, str) or not copy.strip() or not isinstance(original, str) or not original.strip():
            raise ValueError("'working_copy' is the changed copy in the chat's folder, and 'original' the file it replaces.")
        return {"copy": _absolute(copy.strip(), root), "original": _absolute(original.strip(), root)}

    return await _on_computer("outside.put_back", kwargs, build)


_FILES = {"type": "array", "items": {"type": "string"}, "minItems": 1}


def register(registry: ToolRegistry) -> None:
    for name, handler, description, properties, required in (
        (
            "request_folder_access", _request_folder_access,
            "Ask the user to let this chat read a folder outside its own: Desktop, Downloads, Documents, Pictures, "
            "or an absolute path the user named. The user answers in a window on their computer; once allowed, "
            "list_files and read_file work there with absolute paths, read-only. Returns the folder granted.",
            {"folder": {"type": "string", "description": "Desktop, Downloads, Documents, Pictures, or an absolute path."}},
            ["folder"],
        ),
        (
            "copy_files", _copy_files,
            "Copy files into the chat's folder, out of it, or straight between two folders outside it; the "
            "destination must be the chat's folder or inside a folder already granted. The user confirms in a window "
            "and may untick files. A copy never overwrites: a taken name gets ' (1)'. Returns what was copied where, "
            "and what was skipped (an unticked file among them).",
            {
                "files": {**_FILES, "description": "Absolute paths, or paths relative to the chat's folder."},
                "to": {"type": "string", "description": "The folder to copy into: '.' for the chat's folder, or an absolute path."},
            },
            ["files", "to"],
        ),
        (
            "move_to_trash", _move_to_trash,
            "Move files outside the chat's folder to the computer's trash (the user can restore them from there); "
            "the user confirms in a window. After copy_files, trash only the originals listed in its 'copied'. "
            "Returns what was trashed and what was kept.",
            {"files": {**_FILES, "description": "Absolute paths of the files to move to the trash."}},
            ["files"],
        ),
        (
            "put_back", _put_back,
            "Put a changed copy back in place of its original outside the chat's folder; the user confirms in a "
            "window. Refused when the original changed since it was copied. The original goes to the trash.",
            {
                "working_copy": {"type": "string", "description": "The changed copy, by its path in the chat's folder."},
                "original": {"type": "string", "description": "The original's absolute path."},
            },
            ["working_copy", "original"],
        ),
    ):
        registry.register(
            name=name,
            schema=ToolSchema(
                name=name,
                description=description,
                parameters={"type": "object", "properties": properties, "required": required},
            ),
            handler=handler,
            toolset="file",
        )
