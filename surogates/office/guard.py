"""The editor is the only writer of a document open in it: other writes are refused, reads are told.

A Word document is office_edit's to change whether or not it is open: text writes to one are refused, and reads of one
are told where to change it."""

from __future__ import annotations

from typing import Any

from surogates.config import load_settings
from surogates.devices.binding import device_of
from surogates.office import sessions as office_sessions
from surogates.office.agent import location_of, relative_path, within_folder
from surogates.office.files import _folded
from surogates.office.kinds import office_kind
from surogates.sandbox.history import YOU
from surogates.tools.builtin.file_ops import patch_targets

WRITING_TOOLS = frozenset({"write_file", "patch"})


def _applies(session: Any, session_factory: Any) -> bool:
    # Only where office_read and office_edit are offered: every word here sends the model to them.
    return session_factory is not None and load_settings().office.agent_editing


async def _open_locations(session_factory: Any, session: Any, locations: list[str]) -> set[str]:
    """Those of *locations* whose file is open in the editor. On a computer without case or Unicode form: its folder
    may ignore them."""
    if device_of(session.config) is None:
        return await office_sessions.open_locations(session_factory, locations)
    found = {_folded(document.location) for document in await office_sessions.open_like(session_factory, locations)}
    return {location for location in locations if _folded(location) in found}


async def refused_open_document(tool_name: str, tool_args: Any, session: Any, *, session_factory: Any) -> str | None:
    """Why a write_file or patch to a document open in the editor, or to a Word document, is refused, or None."""
    if tool_name not in WRITING_TOOLS or not isinstance(tool_args, dict) or not _applies(session, session_factory):
        return None
    targets = [str(tool_args.get("path") or "")] if tool_name == "write_file" else patch_targets(tool_args)
    # A path holding a NUL names no file: the sandbox refuses it, and the open rows cannot be looked up by it.
    targets = [p for p in targets if p and office_kind(p) and "\0" not in p]
    if device_of(session.config) is not None:
        # The editor may have it open by its path in the folder, which this one cannot be matched to. The desktop
        # refuses a write outside the folder anyway, so nothing that could be written is lost.
        for p in targets:
            if not within_folder(relative_path(p, session)):
                return (
                    f"{p} names a file through the computer's home folder: name it by its path in the chat's folder, "
                    "like docs/report.docx."
                )
    paths = {location_of(session, relative_path(p, session)): relative_path(p, session) for p in targets}
    for location in sorted(await _open_locations(session_factory, session, list(paths))):
        return (
            f"{paths[location]} is open in the document editor, where people may be editing it: change it with "
            f"office_edit (read it with office_read), not with {tool_name}."
        )
    # Closed, a Word document is office_edit's too: a text write breaks it.
    for path in sorted(paths.values()):
        if office_kind(path).file_type == "docx":
            return f"{path} is a Word document: change it with office_edit (read it with office_read), not with {tool_name}."
    return None


async def open_document_note(tool_args: Any, session: Any, *, session_factory: Any) -> str | None:
    """The note read_file adds when the document it read is open in the editor or is a Word document, or None."""
    raw = str(tool_args.get("path") or "") if isinstance(tool_args, dict) else ""
    if not raw or not office_kind(raw) or "\0" in raw or not _applies(session, session_factory):
        return None
    path = relative_path(raw, session)
    if device_of(session.config) is not None and not within_folder(path):
        return None  # it cannot be matched to an open document
    if not await _open_locations(session_factory, session, [location_of(session, path)]):
        if office_kind(path).file_type != "docx":
            return None
        return f"{path} is a Word document: to change it, read it with office_read and edit it with office_edit."
    return (
        f"{path} is open in the document editor, which holds it now: read it with office_read and change it with "
        "office_edit. Your own files can lag the editor or not have the document yet (a project thread's copy gets it "
        "at its next turn): do not look for it there."
    )


async def hold_open_documents(session_factory: Any, session: Any, turn: dict[str, Any]) -> None:
    """Move *turn*'s changes to documents open in the editor to its overlapped files, in place.

    The landing then writes none of them, and the thread is told a file it changed was changed by you, and
    redoes the change where the document is: through office_edit.
    """
    if not load_settings().office.agent_editing:
        return
    candidates = {location_of(session, change["path"]): change for change in turn["changes"] if office_kind(change["path"])}
    if not candidates:
        return
    held = await office_sessions.open_locations(session_factory, list(candidates))
    if not held:
        return
    held_paths = {candidates[location]["path"] for location in held}
    turn["changes"] = [change for change in turn["changes"] if change["path"] not in held_paths]
    for location in sorted(held):
        change = candidates[location]
        turn["overlapped"].append({"path": change["path"], "reason": "changed", "before": change["before"], "after": change["after"], "by": YOU})
