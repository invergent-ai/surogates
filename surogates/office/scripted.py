"""Office documents a script leaves behind: the user can neither watch nor co-edit a document made that way.

A terminal command that makes or changes a .docx, .xlsx, .pptx or .pdf names a script, not the document, so what it
did is seen in the workspace after it ran: one ``find`` in the session's sandbox for the documents changed since it
began. A command that names a document open in the editor is told what a read of it is told: the editor holds it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import shlex
from typing import Any

from surogates.devices.binding import device_of
from surogates.office.agent import AGENT_KINDS
from surogates.office.guard import OPEN_NOTE, open_documents
from surogates.office.kinds import office_kind
from surogates.sandbox.pool import sandbox_session_key
from surogates.tools.skill_visibility import offered_fallback_tools

logger = logging.getLogger(__name__)

SCRIPTED_NOTE = (
    "{paths} changed by a script: the user can neither watch nor co-edit a document made that way. "
    "Make and change documents with office_edit (read them with office_read)."
)
WORKSPACE = "/workspace"
#: A mounted workspace keeps whole seconds, on its storage's clock: a change up to this long before the call is the
#: call's.
MOUNT_SLACK_S = 2
LOOK_TIMEOUT_S = 5
#: The documents one note names; the rest are counted.
NAMED_AT_MOST = 10
#: Folders not looked in: the skills staged for the chat, and the trees a clone or an install brings, whose sample
#: documents are not a script's and whose walk could take the whole look.
NOT_LOOKED_IN = ("node_modules", ".git", ".skills", ".venv", "site-packages", "__pycache__")
#: What ends a name in a command: the shell's spaces, quotes and operators, and an option's ``=``.
_SEPARATORS = re.compile(r"[\s'\"`<>|;&(){}\[\]=]+")


def named_documents(command: str) -> set[str]:
    """The office documents *command* names, as it writes them: each word, a quoted name with spaces in it whole, and
    each name inside a word (a script's string, an option's value). A pattern (``*.pdf``) names none."""
    words = set(_SEPARATORS.split(command))
    with contextlib.suppress(ValueError):
        words.update(word for word in shlex.split(command) if any(c.isspace() for c in word))
    return {word for word in words if office_kind(word) and not any(c in word for c in "*?")}


async def scripted_documents(session: Any, since: float, *, sandbox_pool: Any) -> list[str]:
    """The office documents changed in *session*'s sandbox since *since* (``time.time()``), relative to the workspace
    and sorted; none for a session on the user's computer, whose tools run there, or when the look fails."""
    if sandbox_pool is None or device_of(session.config) is not None:
        return []
    owner = sandbox_session_key(session)
    skipped = " -o ".join(f"-name {folder}" for folder in NOT_LOOKED_IN)
    kinds = " -o ".join(f"-iname '*.{kind}'" for kind in AGENT_KINDS)
    try:
        # A thread's copy is on its pod's own disk, its times exact, and is made when the pod starts, just before the
        # call: a slack would count its making as the script's.
        after = since if sandbox_pool.holds_copy(owner) else since - MOUNT_SLACK_S
        command = (
            f"find {WORKSPACE} -maxdepth 6 \\( {skipped} \\) -prune -o -type f \\( {kinds} \\) -newermt @{after:.3f} "
            "-print 2>/dev/null"
        )
        answer = json.loads(await sandbox_pool.execute(
            owner, "terminal", json.dumps({"command": command, "timeout": LOOK_TIMEOUT_S}), timeout=LOOK_TIMEOUT_S,
        ))
        listed = str(answer.get("output") or "")
    except Exception as exc:
        logger.warning("Could not look for the documents a command left in sandbox %s: %r", owner, exc)
        return []
    return sorted(line[len(WORKSPACE) + 1:] for line in listed.splitlines() if line.startswith(f"{WORKSPACE}/"))


def _listed(paths: list[str]) -> str:
    more = len(paths) - NAMED_AT_MOST
    return ", ".join(paths[:NAMED_AT_MOST]) + (f" and {more} more" if more > 0 else "")


async def terminal_notes(
    tool_args: Any, session: Any, started: float | None, *, sandbox_pool: Any, session_factory: Any,
) -> list[str]:
    """What a terminal call's result is told: of each document it named or changed that is open in the editor, that
    the editor holds it; of the others it changed, in a chat with the office tools, that a script made them.

    *started* is when the command began in the sandbox; None where it ran elsewhere, which is not looked at.
    """
    command = str(tool_args.get("command") or "") if isinstance(tool_args, dict) else ""
    changed: list[str] = []
    if started is not None and offered_fallback_tools(getattr(session, "channel", None), session.config):
        changed = await scripted_documents(session, started, sandbox_pool=sandbox_pool)
    try:
        held = await open_documents([*named_documents(command), *changed], session, session_factory=session_factory)
    except Exception:
        # The command ran: its result goes back without the note.
        logger.warning("Could not tell which documents a command named are open in the editor", exc_info=True)
        held = []
    notes = [OPEN_NOTE.format(path=path) for path in held]
    made = [path for path in changed if path not in held]
    if made:
        notes.append(SCRIPTED_NOTE.format(paths=_listed(made)))
    return notes
