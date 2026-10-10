"""A project thread's copy on its user's computer: what the worker asks of it, and what it takes for an answer.

A thread the server bound with a copy of its own (``DeviceOperations.bind``,
``surogates.devices.binding.copy_of``) works in that copy, never in the
folder.  Its computer's app has three kinds for it beside the file and
process kinds: ``checkpoint``, a snapshot of the copy or the copy put back
to one; ``history``, a step of the folder's history, which git runs in the
app's guest (``surogates.sandbox.local_history``); and ``land``, the file
helper's look at the real folder and its writes into it.

None is a tool's.  Each is the worker's own, journaled under an invocation
of its own, so a worker lost part-way gets each step's recorded outcome when
its turn resumes: ``open:<turn>`` for a turn's open, ``checkpoint:<turn>:…``
for a snapshot and for a put-back, ``land:<turn>…`` for a landing.  The
journal records each kind under those names alone, and a step of the history
or of a landing from the thread itself alone, as the app takes them.

The computer is its user's, and its answers are still data.  The app checks
what its guest says (``desktop/src/vm/history.ts``), and what reaches the
server is checked again here, wherever it is read: by the step that asked,
and by whoever reads an outcome out of the journal later.

- An answer is built again from its own fields, one shape an action; every
  other field is dropped.  An id is forty lowercase hex digits.  A path is a
  file inside the folder: at most 4,096 characters of text, no NUL, not
  absolute, no empty, ``.`` or ``..`` part.  A word is one of the few its
  answer may say.  A count is a whole number from 0 to 2**53 - 1.  A list
  holds at most 50,000 entries; a look at most 2,000 files; an open at most
  64 names of what was set aside, each the asking thread's own.  Who changed
  a file is one of three shapes, each word text of a path's length.  So an
  answer holds nothing that names a session, a project, a computer or a
  folder: those are the session's own, which the server stamped.
- A refusal is read by its type and its code, each taken only from the
  server's own list of them, never by its words.  Its words are for a
  person: at most 2,000 characters are kept, and nothing is looked up by
  them.
- Anything else is :class:`NotAnAnswer`, in the server's words alone.

One frame of the link carries an outcome, so none is larger than the link
takes (``surogates.devices.link.MAX_FRAME_CHARS``, 2 MiB), and none of these
kinds is answered with a transfer.  The bounds are the app's own
(``desktop/src/vm/history.ts``): what its check passes on, this one takes.

A turn of the thread's opens its copy at its start, before anything else
of the turn reaches the computer (:meth:`ThreadCopy.opened`).  A thread
whose folder or computer is no place for it, or whose copy cannot be opened
there, has nowhere to work: its turn sends nothing more, and says why in the
words of :data:`NOWHERE`.
"""

from __future__ import annotations

import errno
import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

from surogates.devices.binding import copy_of, device_of
from surogates.devices.operations import DeviceOperations, JournalRunner
from surogates.devices.workspace import DeviceOperationError, OperationRunner
from surogates.sandbox.pool import sandbox_session_key
from surogates.workstreams import is_project_thread

__all__ = [
    "ACTIONS", "HISTORY_CODES", "NO_COPY", "NOWHERE", "OPEN_TRIES", "REFUSALS", "ComputerRefused", "NotAnAnswer",
    "NowhereToWork", "Steps", "ThreadCopy", "answered", "checked", "code_of", "opens_its_copy", "refused", "thread_copy",
]

#: The most entries one answer may list, the most files one look answers for, and the most
#: names an open gives of what was set aside whole.
MAX_LISTED = 50_000
MAX_LOOKED = 2_000
MAX_ASIDE = 64
#: The longest path, or word of who changed a file, and what is kept of a refusal's words.
MAX_PATH = 4_096
MAX_WORDS = 2_000
#: The largest count: a whole number the app's own check takes for one.
_MAX_COUNT = 2**53 - 1

#: The refusal of a computer whose app keeps no copy for the thread: it bound the thread to
#: the folder itself, as an app older than copies binds every chat.  Asking again does not
#: change it.  Such a thread has nowhere to work: it is never run in the folder.
NO_COPY = "unsupported"
#: What a refusal's type may be: the app's own for a thread's kinds and its file helper's,
#: its guest's, and the journal's for an operation the server closed itself.  Any other
#: reads as ``other``.
REFUSALS = frozenset({
    "history", "busy", "history_off", NO_COPY, "refused", "value", "conflict", "stale", "sandbox", "os", "too_large",
    "cancelled", "interrupted", "unavailable", "folder_unavailable", "revoked", "other",
})
#: The code a refusal of the type ``history`` always has: the folder's history's own ten
#: (``surogates.sandbox.history`` and ``local_history``), the guest's for a request that
#: ended without an answer, and the app's for what its guest sent in an answer's place.
HISTORY_CODES = frozenset({
    "failed", "history_refused", "conflict", "no_whole_copy", "name_not_utf8", "not_a_request", "record_unfinished",
    "move_unfinished", "landing_unsettled", "not_on_base", "no_answer", "not_an_answer",
})
#: A refusal of the type ``os`` may carry the system's name for the error.
_ERRNOS = frozenset(errno.errorcode.values())
_OTHER = "other"

#: How many times a turn asks its computer to open the thread's copy: a refusal the next asking
#: may pass is asked again at once, and one at the last asking stands for the turn.
OPEN_TRIES = 3
#: The refusals of a turn's open that the next asking may pass, each of which may have done part
#: of the work: the history's for a copy that is not whole, and for a move or a record of the
#: thread's that was cut, which its next open makes or finishes; its guest's for a request it gave
#: up on; and one that ended without its answer, cut off, or before the guest was there.
_PASSING_CODES = frozenset({"no_whole_copy", "move_unfinished", "record_unfinished", "no_answer"})
_PASSING_KINDS = frozenset({"interrupted", "unavailable"})
#: The refusal of an open its turn's Stop closed: no reason of the folder's, and nothing more of
#: the turn is asked.
_STOPPED = "cancelled"
#: Why a thread has nowhere to work where its copy could not be opened: no reason of its folder's.
_REFUSED = "refused"
#: The refusal of a computer whose app gives the thread no copy of a folder too large to copy in
#: the time a copy may take, its making cut short by that bound twice: until the app starts again.
_TOO_LARGE = "history_off"
#: Why a thread has nowhere to work, by its open's refusal: its type, else its code.
_NOWHERE_BY = {NO_COPY: NO_COPY, _TOO_LARGE: _TOO_LARGE, "name_not_utf8": "names"}
#: What a person reads where a thread has nowhere to work on its computer, by why: its folder has
#: more files than its history keeps, or a name its history cannot keep, or is too large for its
#: computer's app to copy; its app keeps no copy of the folder for it; or its copy could not be
#: opened there.  Said as its turn's failure.  The owner's words, and these alone.
NOWHERE: dict[str, str] = {
    "cap": "This folder has more files than its history can keep, so this thread cannot work there. Choose a folder inside it.",
    "names": "A file in this folder has a name its history cannot keep, so this thread cannot work there. Choose a folder inside it.",
    _TOO_LARGE: (
        "This folder is too large for this thread to have a copy of its own on this computer, so it cannot work there. "
        "Choose a folder inside it that holds less."
    ),
    NO_COPY: (
        "Surogate Desktop on this computer keeps no copy of the folder for this thread, so it cannot work there. "
        "Update Surogate Desktop."
    ),
    _REFUSED: (
        "Surogate Desktop could not open this thread's copy of the folder, so it cannot work there now. "
        "Try again, or update Surogate Desktop."
    ),
}

_ID = re.compile(r"[0-9a-f]{40}")
_THREAD = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: A file's revision as the file helper spells one: its device, inode, size and two times.
_REVISION = re.compile(r"[0-9]{1,20}:[0-9]{1,20}:[0-9]{1,20}:-?[0-9]{1,20}:-?[0-9]{1,20}")
#: A saga's id, and a file of the helper's own beside a real one, as the land kind names them.
_SAGA = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:.-]{0,127}")
_OWN_FILE = re.compile(r"\.surogate-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\.tmp")
#: When a copy or a repository was set aside, and which in its place's order: what its name starts with.
_ASIDE = re.compile(r"[0-9]{8}-[0-9]{8}T[0-9]{6}Z-")
_COPIES = ("made", "moved", "kept")
_OFF = ("cap", "names")
_REASONS = ("changed", "shape", "with", "protected")


class ComputerRefused(DeviceOperationError):
    """The computer answered one of a thread's kinds with an error.

    *kind* is the error's type and *code* the stable code it carries, each
    one of the server's own list (:data:`REFUSALS`, :data:`HISTORY_CODES`,
    the system's error names for ``os``) or not taken.  Whoever acts on a
    refusal reads those two.  Its words are the computer's, for a person.
    """

    def __init__(self, kind: str, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code


class NotAnAnswer(DeviceOperationError):
    """The computer answered one of a thread's kinds with what is no answer to it."""


class NowhereToWork(Exception):
    """A thread has nowhere to work on its computer this turn: no step of the turn runs.

    *why* is a key of :data:`NOWHERE`, whose words it says; *code* the word
    its computer's refusal is named by (:func:`code_of`), where it refused.
    """

    def __init__(self, why: str, *, code: str | None = None) -> None:
        super().__init__(NOWHERE[why])
        self.why = why
        self.code = code
        #: Whether a later turn may find otherwise: only one whose copy could not be opened.
        self.retryable = why == _REFUSED


def opens_its_copy(session: Any) -> bool:
    """Whether *session*'s own turn opens a copy of the folder of its own: a project's thread the server bound with one.

    A session under the thread works in the thread's copy and opens none,
    as the journal takes a thread's open from the thread alone.
    """
    return copy_of(session.config) == session.id and is_project_thread(session.config)


class _No(Exception):
    """A part of an answer is not what its shape has there."""


def code_of(refusal: DeviceOperationError) -> str:
    """The word a report names a refusal by: its code, or its type where it carries none.

    ``not_an_answer`` for what was no answer, and ``not_asked`` for the
    journal's own refusal, which its computer never heard of.
    """
    if isinstance(refusal, ComputerRefused):
        return refusal.code or refusal.kind
    return "not_an_answer" if isinstance(refusal, NotAnAnswer) else "not_asked"


def _need(ok: bool) -> None:
    if not ok:
        raise _No


def _has(answer: dict[str, Any], field: str) -> Any:
    """A field that may be null, and is there all the same."""
    _need(field in answer)
    return answer[field]


def _well_formed(text: str) -> bool:
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True


def _text(value: Any, most: int) -> str:
    _need(isinstance(value, str) and len(value) <= most and "\0" not in value and _well_formed(value))
    return value


def _word(value: Any, words: tuple[str, ...]) -> str:
    _need(isinstance(value, str) and value in words)
    return value


def _path(value: Any) -> str:
    """A file from the folder's top, each part a name: an empty part is a path from the root, or one that ends in a slash."""
    path = _text(value, MAX_PATH)
    _need(all(part not in ("", ".", "..") for part in path.split("/")))
    return path


def _name(value: Any) -> str:
    """A file or a folder history leaves out, as git lists it: a folder's name ends in a slash."""
    _need(isinstance(value, str))
    _path(value.removesuffix("/"))
    return value


def _id(value: Any) -> str:
    _need(isinstance(value, str) and _ID.fullmatch(value) is not None)
    return value


def _id_or_none(value: Any) -> str | None:
    return None if value is None else _id(value)


def _count(value: Any) -> int:
    _need(type(value) is int and 0 <= value <= _MAX_COUNT)
    return value


def _flag(value: Any) -> bool:
    _need(type(value) is bool)
    return value


def _listed(value: Any, item: Callable[[Any], Any], most: int = MAX_LISTED) -> list:
    """A list, counted before any of it is looked at."""
    _need(isinstance(value, list) and len(value) <= most)
    return [item(one) for one in value]


def _version(value: Any) -> dict[str, Any]:
    """A file's two versions, each a blob's id or none: a file that was not there, or is gone."""
    _need(isinstance(value, dict))
    return {"path": _path(value.get("path")), "before": _id_or_none(_has(value, "before")), "after": _id_or_none(_has(value, "after"))}


def _by(value: Any) -> dict[str, str]:
    """Who changed a file: you, a thread by its id and title, or a routine by its name.  The computer's text, each."""
    _need(isinstance(value, dict))
    kind = _word(value.get("kind"), ("you", "thread", "routine"))
    words = () if kind == "you" else ("id", "title") if kind == "thread" else ("name",)
    return {"kind": kind, **{word: _text(value.get(word), MAX_PATH) for word in words}}


def _held(value: Any) -> dict[str, Any]:
    """A file a landing leaves out, and why."""
    entry = {**_version(value), "reason": _word(value.get("reason"), _REASONS)}
    if "by" in value:
        entry["by"] = _by(value["by"])
    return entry


def _aside(value: Any, thread: str | None) -> str:
    """A copy or a repository the place keeps set aside whole, by its folder's name there: the asking thread's own, or none."""
    _need(isinstance(value, str) and thread is not None and _THREAD.fullmatch(thread) is not None)
    _need(_ASIDE.fullmatch(value[:26]) is not None and value[26:] in (f"{thread}.copy", f"{thread}.repository"))
    return value


def _opened(answer: dict[str, Any], thread: str | None) -> dict[str, Any]:
    """A turn's open: the thread's copy, made, moved to the folder as it is or kept as it was; or why the folder has no history.

    On either, what the place keeps set aside whole of the thread's own,
    and what of it went at the place's bound.  The first form that holds,
    as the app takes them.
    """
    for form in (_with_copy, _with_no_history):
        try:
            opened = form(answer)
            for names in ("set_aside_folders", "set_aside_gone"):
                if names in answer:
                    opened[names] = _listed(answer[names], lambda name: _aside(name, thread), MAX_ASIDE)
            return opened
        except _No:
            continue
    raise _No


def _with_copy(answer: dict[str, Any]) -> dict[str, Any]:
    opened: dict[str, Any] = {"copy": _word(answer.get("copy"), _COPIES)}
    if "set_asides" in answer:
        opened["set_asides"] = _listed(answer["set_asides"], _id)
    return opened


def _with_no_history(answer: dict[str, Any]) -> dict[str, Any]:
    return {"history": _word(answer.get("history"), ("off",)), "reason": _word(answer.get("reason"), _OFF)}


def _changed(answer: dict[str, Any]) -> dict[str, Any]:
    return {"paths": _listed(answer.get("paths"), _path)}


def _fetched(answer: dict[str, Any]) -> dict[str, Any]:
    return {
        "main": _id_or_none(_has(answer, "main")), "landing": _id_or_none(_has(answer, "landing")),
        "hidden": _flag(answer.get("hidden")), "packs": _count(answer.get("packs")), "missing": _listed(answer.get("missing"), _id),
    }


def _picked_up(answer: dict[str, Any]) -> dict[str, Any]:
    return {
        "main": _id_or_none(_has(answer, "main")), "commit": _id_or_none(_has(answer, "commit")),
        "picked_up": _listed(answer.get("picked_up"), _version), "packs": _count(answer.get("packs")),
    }


def _committed(answer: dict[str, Any]) -> dict[str, Any]:
    return {
        "commit": _id_or_none(_has(answer, "commit")), "base": _id(answer.get("base")),
        "changes": _listed(answer.get("changes"), _version), "overlapped": _listed(answer.get("overlapped"), _held),
        "excluded": _listed(answer.get("excluded"), _name), "repositories": _listed(answer.get("repositories"), _name),
        "not_taken": _listed(answer.get("not_taken"), _path),
    }


def _recorded(answer: dict[str, Any]) -> dict[str, Any]:
    return {"commit": _id(answer.get("commit")), "set_aside": _id_or_none(_has(answer, "set_aside"))}


def _kept(answer: dict[str, Any]) -> dict[str, Any]:
    return {"commit": _id(answer.get("commit")), "not_taken": _listed(answer.get("not_taken"), _path)}


def _forgettable(answer: dict[str, Any]) -> dict[str, Any]:
    """Whether what a landing kept may be forgotten: its landing, recorded, or none.  This field and no other, as the app takes it."""
    _need(answer.keys() == {"landing"})
    return {"landing": _id_or_none(answer["landing"])}


def _snapshot(answer: dict[str, Any]) -> dict[str, Any]:
    return {"hash": _id(answer.get("hash"))}


def _done(answer: dict[str, Any]) -> dict[str, Any]:
    return {}


def _pair(second: Callable[[Any], Any]) -> Callable[[Any], list]:
    def pair(value: Any) -> list:
        _need(isinstance(value, list) and len(value) == 2)
        return [_path(value[0]), second(value[1])]

    return pair


def _own_file(value: Any) -> str | None:
    _need(value is None or (isinstance(value, str) and _OWN_FILE.fullmatch(value) is not None))
    return value


def _unread(value: Any) -> list:
    _need(isinstance(value, list) and len(value) == 3)
    saga, step, path = value
    _need(isinstance(saga, str) and _SAGA.fullmatch(saga) is not None)
    return [saga, _count(step), None if path is None else _path(path)]


def _recovered(answer: dict[str, Any]) -> dict[str, Any]:
    """What a landing's helper put right of steps an earlier one was cut short in.

    A file that took its name again; one whose name another holds since,
    and where it is now; one that went with its folder, by the helper's
    name for it; and a step's record that cannot be read, by its landing,
    its number and the file it names if it still says.
    """
    return {
        "restored": _listed(answer.get("restored"), _path), "beside": _listed(answer.get("beside"), _pair(_path)),
        "lost": _listed(answer.get("lost"), _pair(_own_file)), "unread": _listed(answer.get("unread"), _unread),
    }


def _look(value: Any) -> list:
    _need(isinstance(value, list) and len(value) == 2)
    path, token = value
    _need(isinstance(token, str) and (token in ("absent", "other") or _REVISION.fullmatch(token) is not None))
    return [_path(path), token]


def _looked(answer: dict[str, Any]) -> dict[str, Any]:
    return {"revisions": _listed(answer.get("revisions"), _look, MAX_LOOKED)}


def _applied(answer: dict[str, Any]) -> dict[str, Any]:
    """An apply's answer: the file and its two versions as it was asked, and the folders it made for it."""
    return {**_version(answer), "made": _listed(answer.get("made"), _path)}


def _unapplied(answer: dict[str, Any]) -> dict[str, Any]:
    return {"path": _path(answer.get("path")), "put_back": _flag(answer.get("put_back"))}


def _forgotten(answer: dict[str, Any]) -> dict[str, Any]:
    """A forgetting answers nothing: an answer that holds anything is another action's."""
    _need(not answer)
    return {}


#: Each action a thread's kinds have, and the shape of its answer.
_ANSWERS: dict[str, dict[str, Callable[..., dict[str, Any]]]] = {
    "checkpoint": {"take": _snapshot, "restore": _done},
    "history": {
        "open": _opened, "changed": _changed, "fetch": _fetched, "pickup": _picked_up, "commit": _committed,
        "record": _recorded, "keep": _kept, "forget": _forgettable,
    },
    "land": {"recover": _recovered, "revisions": _looked, "apply": _applied, "unapply": _unapplied, "forget": _forgotten},
}
ACTIONS = {kind: frozenset(actions) for kind, actions in _ANSWERS.items()}


def checked(kind: str, action: str, answer: Any, *, thread: str | None = None) -> dict[str, Any]:
    """*answer* to *action* of *kind*, built again from its own fields; :class:`NotAnAnswer` when it is none.

    *thread* is the thread whose copy was asked about, where the caller
    knows it: what an open names as set aside is taken only as that
    thread's own.
    """
    shape = _ANSWERS.get(kind, {}).get(action)
    if shape is None:
        raise NotAnAnswer(f"This computer was asked nothing called '{action}'")
    try:
        _need(isinstance(answer, dict))
        return _opened(answer, None if thread is None else str(thread)) if shape is _opened else shape(answer)
    except _No:
        raise NotAnAnswer(f"This computer's answer to '{action}' was not one") from None


def answered(kind: str, action: str, outcome: Any, *, thread: str | None = None) -> dict[str, Any] | None:
    """What the journal holds as a computer's answer to *action*, checked as one given now; None for a refusal, or for no answer.

    Whatever reads an outcome out of the journal, and not as the step that
    asked it, reads it through here: a recorded answer is its computer's
    word still.
    """
    if not isinstance(outcome, dict) or "error" in outcome or "ok" not in outcome:
        return None
    try:
        return checked(kind, action, outcome["ok"], thread=thread)
    except NotAnAnswer:
        return None


def refused(outcome: Any) -> ComputerRefused | None:
    """The refusal the journal holds as a computer's outcome, read as one given now; None where it is none."""
    if not isinstance(outcome, dict) or "error" not in outcome:
        return None
    return _refusal(outcome["error"])


def _refusal(error: Any) -> ComputerRefused:
    """What a computer refused with, by its type and its code: each one of the server's own list, or not taken."""
    error = error if isinstance(error, dict) else {}
    kind, code, words = (error.get(part) for part in ("type", "code", "message"))
    kind = kind if isinstance(kind, str) and kind in REFUSALS else _OTHER
    codes = HISTORY_CODES if kind == "history" else _ERRNOS if kind == "os" else frozenset()
    code = code if isinstance(code, str) and code in codes else None
    if kind == "history" and code is None:
        # A refusal of the history's always has one of its codes: this is none of them, whatever its words say.
        kind = _OTHER
    words = words[:MAX_WORDS] if isinstance(words, str) else ""
    return ComputerRefused(kind, (words.replace("\0", "") if _well_formed(words) else "") or "This computer refused it", code)


class Steps:
    """The steps of one invocation of a thread's, in the order asked, each answer checked.

    Asked again under the same invocation, a step that was recorded is
    answered as it was, and is not run again: the journal's rule for every
    operation.  One that names other arguments than its first run raises
    ``OperationConflict``.  A step its computer refused raises
    :class:`ComputerRefused`, and one answered with anything else
    :class:`NotAnAnswer`.
    """

    def __init__(self, runner: OperationRunner, *, thread: UUID | None = None) -> None:
        self._runner = runner
        self._thread = None if thread is None else str(thread)

    async def ask(self, kind: str, action: str, **arguments: Any) -> dict[str, Any]:
        outcome = await self._runner.run(kind, {"action": action, **arguments})
        if isinstance(outcome, dict) and "error" in outcome:
            raise _refusal(outcome["error"])
        if not isinstance(outcome, dict) or "ok" not in outcome:
            raise NotAnAnswer(f"This computer's answer to '{action}' was not one")
        return checked(kind, action, outcome["ok"], thread=self._thread)

    async def history(self, action: str, **arguments: Any) -> dict[str, Any]:
        return await self.ask("history", action, **arguments)

    async def land(self, action: str, **arguments: Any) -> dict[str, Any]:
        return await self.ask("land", action, **arguments)


class ThreadCopy:
    """What a worker asks of the computer about one thread's copy, as the session that asks."""

    def __init__(self, operations: DeviceOperations, session: Any, *, lease_token: str | None) -> None:
        thread = copy_of(session.config)
        if thread is None or str(thread) != sandbox_session_key(session):
            # From the session the server stamped, never from an answer or a tool's input.
            raise ValueError("Only a session of a thread that works in a copy of its own has one to ask about")
        self.operations = operations
        self._session = session
        self._lease_token = lease_token
        #: The thread whose copy it is: the root every operation is asked for.
        self.thread: UUID = thread
        self.device_id: UUID = device_of(session.config)
        #: The folder as its computer names it: with the computer, the place a record is of.
        self.folder: str = session.config["workspace_path"]

    def steps(self, invocation: str) -> Steps:
        return Steps(JournalRunner(
            self.operations, device_id=self.device_id, root_session_id=self.thread,
            calling_session_id=self._session.id, invocation_id=invocation, lease_token=self._lease_token,
        ), thread=self.thread)

    async def open(self, turn: int, *, again: int = 0) -> dict[str, Any]:
        """Bring the copy to the turn *turn*: ``{copy, …}``, or ``{history: "off", reason, …}`` for a folder with no history.

        Journaled under the turn, so an answer is the turn's: asked again,
        it is given as it was, and the computer hears of it once.  A refusal
        is recorded the same way, so whoever asks again after one asks under
        the turn's next name: *again* counts the refusals before this
        asking.

        A thread with nowhere to work never works in the folder.  A folder
        with no history answers so, with its reason.  A computer that keeps
        no copy for the thread refuses with :data:`NO_COPY`, which asking
        again does not change.  Every other refusal may have done part of
        the work, and says nothing of where the thread works.
        """
        return await self.steps(f"open:{turn}:{again}" if again else f"open:{turn}").history("open")

    async def opened(self, turn: int) -> dict[str, Any]:
        """The copy brought to the turn *turn*: the open's answer, which every step of the turn works from.

        The journal holds each asking of the turn's.  An answer is the
        turn's: asked again, it is read there, checked as one given now, and
        the computer hears nothing.  An asking still open is waited for.  A
        refusal the next asking may pass is asked again at once, under the
        turn's next name, up to :data:`OPEN_TRIES` askings.

        Raises :class:`NowhereToWork` where the thread has nowhere to work:
        its folder has no history, its computer's app keeps no copy for it
        or gives it none of a folder too large to copy, or it refused at the
        turn's last asking or in a way asking again would not pass.  Raises the
        :class:`ComputerRefused` of an open its turn's Stop closed: nothing
        more is asked.  Asked again, it raises the same, and the computer
        hears nothing.  The journal's own refusals pass through as they are.
        """
        number = max(await self.operations.opens(self._session.id, turn) - 1, 0)
        while True:
            try:
                answer = await self.open(turn, again=number)
            except ComputerRefused as refusal:
                if refusal.kind == _STOPPED:
                    raise
                if number + 1 < OPEN_TRIES and (refusal.code in _PASSING_CODES or refusal.kind in _PASSING_KINDS):
                    number += 1
                    continue
                why = _NOWHERE_BY.get(refusal.kind) or _NOWHERE_BY.get(refusal.code) or _REFUSED
                raise NowhereToWork(why, code=code_of(refusal)) from None
            except NotAnAnswer as refusal:
                raise NowhereToWork(_REFUSED, code=code_of(refusal)) from None
            if "history" in answer:
                raise NowhereToWork(answer["reason"])
            return answer

    async def take(self, turn: int, step: int, call: str, reason: str) -> str:
        """A snapshot of the copy before the turn's step *step*, the tool call *call*; its commit."""
        taken = await self.steps(f"checkpoint:{turn}:{step}:{call}").ask("checkpoint", "take", reason=reason[:200])
        return taken["hash"]

    async def restore(self, turn: int, commit: str) -> None:
        """Put the copy back to *commit*, a snapshot of this turn's."""
        if _ID.fullmatch(commit) is None:
            raise ValueError("A snapshot is named by its commit")
        await self.steps(f"checkpoint:{turn}:restore:{commit}").ask("checkpoint", "restore", hash=commit)


def thread_copy(session: Any, *, session_factory: Any, redis: Any, lease_token: str | None) -> ThreadCopy:
    """The copy *session* works in on its computer, asked as that session."""
    # Imported here: both reach surogates.session, whose store imports the harness.
    from surogates.devices.waits import DeviceWaitNotice
    from surogates.session.store import SessionStore

    notice = DeviceWaitNotice(SessionStore(session_factory, redis), session_factory)
    return ThreadCopy(DeviceOperations(session_factory, redis, notice=notice), session, lease_token=lease_token)
