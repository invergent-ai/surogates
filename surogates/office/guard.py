"""The editor is the only writer of a document open in it: other writes are refused, reads are told."""

from __future__ import annotations

from typing import Any

from surogates.config import load_settings
from surogates.devices.binding import device_of
from surogates.office import sessions as office_sessions
from surogates.office.agent import location_of, relative_path
from surogates.office.kinds import office_kind
from surogates.sandbox.history import YOU
from surogates.tools.builtin.file_ops import patch_targets

WRITING_TOOLS = frozenset({"write_file", "patch"})


def _applies(session: Any, session_factory: Any) -> bool:
    # Only where office_read and office_edit are offered: every word here sends the model to them.
    return session_factory is not None and device_of(session.config) is None and load_settings().office.agent_editing


async def refused_open_document(tool_name: str, tool_args: Any, session: Any, *, session_factory: Any) -> str | None:
    """Why a write_file or patch to a document open in the editor is refused, or None."""
    if tool_name not in WRITING_TOOLS or not isinstance(tool_args, dict) or not _applies(session, session_factory):
        return None
    targets = [str(tool_args.get("path") or "")] if tool_name == "write_file" else patch_targets(tool_args)
    paths = {location_of(session, relative_path(p, session)): relative_path(p, session) for p in targets if p and office_kind(p)}
    for location in sorted(await office_sessions.open_locations(session_factory, list(paths))):
        return (
            f"{paths[location]} is open in the document editor, where people may be editing it: change it with "
            f"office_edit (read it with office_read), not with {tool_name}."
        )
    return None


async def open_document_note(tool_args: Any, session: Any, *, session_factory: Any) -> str | None:
    """The note read_file adds when the document it read is open in the editor, or None."""
    raw = str(tool_args.get("path") or "") if isinstance(tool_args, dict) else ""
    if not raw or not office_kind(raw) or not _applies(session, session_factory):
        return None
    path = relative_path(raw, session)
    if not await office_sessions.open_locations(session_factory, [location_of(session, path)]):
        return None
    return (
        f"{path} is open in the document editor: this read can lag what people are writing there. Use office_read "
        "for what is in it now, and office_edit to change it."
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
