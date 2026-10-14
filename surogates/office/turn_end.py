"""The turn's save: every document a turn edited through the editor saves once, as the turn ends."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from uuid import uuid4

from surogates.config import OfficeSettings
from surogates.office import sessions as office_sessions
from surogates.office.agent import location_of, relative_path, within_folder
from surogates.office.commands import force_save
from surogates.office.runtime_office import PageGone, RuntimeOffice, RuntimeOfficeError, page_id
from surogates.runtime.turn_slots import turn_waiting
from surogates.session.events import EventType

logger = logging.getLogger(__name__)

# How long a turn's end waits for the document server's save of one document.
SAVE_WAIT_S = 30.0
SAVE_POLL_S = 0.5


async def edited_paths(store: Any, session_id: Any, after: int) -> list[str]:
    """The paths the turn's office_edit calls named, after event *after*, each once."""
    paths: list[str] = []
    for call in await store.get_events(session_id, after=after, types=[EventType.TOOL_CALL]):
        data = call.data or {}
        arguments = data.get("arguments")
        if data.get("name") != "office_edit" or not isinstance(arguments, dict):
            continue
        path = arguments.get("path")
        if isinstance(path, str) and path and path not in paths:
            paths.append(path)
    return paths


async def save_turn(*, store: Any, session: Any, after: int, office: OfficeSettings, session_factory: Any) -> None:
    """Save every document this turn edited: flush the agent's editor, force a save, wait for it to come."""
    paths = await edited_paths(store, session.id, after)
    if not paths:
        return
    # Waiting on the document server holds none of the worker's turn slots; a turn that edited nothing gives none back.
    async with turn_waiting():
        # As office_edit named them: stripped, relative to the workspace, one name however the file is reached. A path
        # outside the folder, which office_edit refused, is skipped: on a computer it could name another chat's.
        relative = [relative_path(path.strip(), session) for path in paths]
        locations = list(dict.fromkeys(location_of(session, path) for path in relative if within_folder(path)))
        runtime = RuntimeOffice(office)
        for location in locations:
            document = await office_sessions.open_at(session_factory, location)
            if document is not None:
                await _save(runtime, office, session_factory, document, str(session.id))


async def _save(runtime: RuntimeOffice, office: OfficeSettings, session_factory: Any,
                document: office_sessions.OpenDocument, session_id: str) -> None:
    # A forced save goes only once the agent's editor has no unsent changes: else it would save without them.
    try:
        if not await runtime.flush(page_id(document.doc_key, session_id)):
            logger.warning("the agent's editor of %s still had unsent changes as the turn ended; "
                           "autosave or the editor closing saves them", document.path)
            return
    except PageGone:
        pass  # Its page closed; what it sent is on the document server, and the save below takes it.
    except RuntimeOfficeError:
        logger.warning("could not flush the agent's editor of %s; autosave or the editor closing saves it",
                       document.path, exc_info=True)
        return
    userdata = json.dumps({"turn": uuid4().hex})
    # After the flush: 0, a save is coming; 4, nothing is unsaved, so every edit is saved; None, could not ask (logged).
    if await force_save(office, document.doc_key, userdata) != 0:
        return
    deadline = time.monotonic() + SAVE_WAIT_S
    while time.monotonic() < deadline:
        current = await office_sessions.get_open(session_factory, document.id)
        if current is None or current.base_version != document.base_version:
            return  # landed, or the document closed after its last save
        if userdata in (current.pending_save or {}).get("marks", []):
            return  # the save is in; the save job lands it within seconds
        await asyncio.sleep(SAVE_POLL_S)
    logger.warning("the save of %s did not come within %ss of the turn's end", document.path, SAVE_WAIT_S)
