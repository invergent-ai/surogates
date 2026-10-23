"""Closing editing sessions nobody uses: never opened, a case's nobody is in any more, a case's the document server
no longer has, or a chat's whose chats are gone."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from surogates.config import OfficeSettings
from surogates.office import sessions as office_sessions
from surogates.office.chats import live_session
from surogates.office.commands import UNKNOWN_KEY, document_info, drop_editors
from surogates.office.runtime_office import close_pages

logger = logging.getLogger(__name__)

NEVER_OPENED_AFTER = timedelta(minutes=10)


async def reap(*, session_factory: Any, session_store: Any, office: OfficeSettings, storage: Any,
               now: datetime | None = None) -> int:
    closed = await office_sessions.close_never_opened(session_factory, older_than=NEVER_OPENED_AFTER, now=now)
    closed += await office_sessions.close_idle_cases(session_factory, older_than=NEVER_OPENED_AFTER, now=now)
    listed = await office_sessions.idle_cases_with_editors(session_factory, older_than=NEVER_OPENED_AFTER, now=now)
    for document in listed:
        # Only the document server knows whether anyone is still in it. A key it has no document by closes here; for
        # one it has, it calls back: with who is in it, or that it closed.
        answer = await document_info(office, document.doc_key)
        if answer == UNKNOWN_KEY:
            # Counted only when closed here: a save that came meanwhile closes it once it lands.
            if await office_sessions.close_when_saved(session_factory, document.id):
                closed += 1
        elif answer != 0:
            logger.warning("the document server could not say whether case document %s is open (%s): asked again "
                           "next sweep", document.id, "no answer" if answer is None else f"error {answer}")
    for document in await office_sessions.open_documents(session_factory):
        # A case's document is the case's: its chats going away leaves it open.
        if document.case_id is not None or await live_session(session_store, document.session_ids) is not None:
            continue
        # A deleted chat's document must not save back into its removed workspace.
        await drop_editors(office, document.doc_key, list(document.editors))
        await close_pages(office, document.doc_key, document.session_ids)
        dropped = await office_sessions.close(session_factory, document.id)
        closed += 1
        if dropped is not None:
            try:
                await storage.delete(document.pending_save["bucket"], dropped)
            except Exception:
                logger.warning("could not delete the dropped save %s of %s", dropped, document.path, exc_info=True)
    return closed
