"""Landing on the user's computer: a project thread's turn applied to its folder there, through its computer.

The cloud's landing saga (:mod:`surogates.harness.landing`), its steps
device operations of the thread's own kinds (:mod:`surogates.devices.history`):
the folder's history runs in the computer's guest, and the app's file helper
alone writes the folder.  A turn's end holds the folder first, and the
helper puts right what a landing cut short left there (``recover``).  Then
one saga: the files the thread's copy changed, a look at each in the folder,
your edits picked up on ``main``, the turn committed on the thread's branch,
an apply of each file over the real one only while that is the file the look
saw, the record on ``main``, and the forgetting of what the landing kept of
the files it replaced, which lets the folder go.  The look comes before the
pickup, and each apply carries what the look saw, so a save of yours after
the look is picked up, or found by its apply, and never written over.
Nothing on the computer checks that order: it is kept here.

What differs from the cloud is who holds what.  No lock of the server's is
held: a computer may be away for a day in the middle of a landing, and its
app holds the folder from the landing's first ``land`` operation to its
forgetting.  A step is asked once, and waits for its computer up to
:data:`STEP_WAIT`.  The steps are journaled under ``land:<turn>``, the saga
named after that name, so a turn taken up again after its worker was lost
gets each step's recorded outcome, and a saga's id names one landing ever.

The saga's durable record is its ``workstream_history`` row, with the
computer and the folder it is of.  The row is written, and seen written,
with every apply before the first is sent, and with the record before it is
sent: a row that cannot be written then ends the landing with no file
written, or with none recorded.  A step that fails puts the landing back,
newest first; where its record may have pushed, the folder's history is
asked first, and a landing ``main`` holds is complete, with nothing put
back.  A landing whose computer stops answering is left as its row says,
nothing put back and nothing forgotten, for whoever settles it.  A landing
put back whole is tried once more, at once, as a new saga.

A landing left running in a folder, its worker lost and its turn never taken
up again, is settled before anything else lands there: by the next landing
in that folder, any thread's of any project's, which holds the folder and
takes the lost landing's thread's lease first.  What the landing left open
on its computer is closed, so none of it runs when its computer is back; it
is complete where the folder's history holds its saga, and put back whole
where it does not.  A turn taken up again on another path than its first
run, or that worked on since its landing began, has its landing settled so
too, and lands again.  The row of a landing on a computer is written only
while its writer holds its thread's lease: a worker that finds its lease
another's stands down where it is, and writes and asks nothing more
(:class:`LeaseLost`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from functools import partial
from typing import Any
from uuid import UUID, uuid4, uuid5

from surogates.devices.history import (
    MAX_LOOKED,
    ComputerRefused,
    NotAnAnswer,
    Steps,
    ThreadCopy,
    answered,
    code_of,
    thread_copy,
)
from surogates.devices.operations import LeaseLost, OperationConflict
from surogates.devices.workspace import MAX_MESSAGE_CHARS, DeviceOperationError
from surogates.governance.saga import Saga, SagaState, SagaStep, StepState
from surogates.governance.saga.orchestrator import SagaTimeoutError
from surogates.harness.landing import (
    TURN_ENDS,
    LandingUnsettled,
    _orchestrator,
    _Row,
    _row_picked_up,
    _tell,
    _tell_escalated,
    _written,
    redo_files,
)
from surogates.harness.tool_exec import SAGA_EXCLUDED_TOOLS, _turn_now
from surogates.session.events import EventType
from surogates.workstreams.history import landing_row, running_in, saga_of, start_landing, unforgotten_in

logger = logging.getLogger(__name__)

#: How long a step of a landing waits for its computer, which may be away for days: the journal delivers each step once.
STEP_WAIT = 7 * 86_400
#: How long a landing waits before it asks the land kind again while another chat holds the folder, and how long it
#: asks for in all: a refusal of ``busy`` did nothing.
BUSY_WAIT = 15.0
BUSY_PATIENCE = 1_800.0
#: How long a turn waits for its computer to give the folder back: its app lets the folder go by itself once the
#: landing's helper has been idle.
RELEASE_WAIT = 30.0
#: How long the lease a settle takes on a lost landing's thread lasts, and how often it is renewed while the settle runs.
SETTLE_LEASE = 60
SETTLE_RENEW = 20.0
#: How many landings ended in a folder whose forgetting never let go of what they kept a landing asks it again for.
FORGET_AGAIN = 16
#: A landing put back whole is tried once more, as a new saga: its look and its pickup see what the first ran into.
#: What a turn taken up again did after its landing began lands after it, as a landing of its own (``land:<turn>:3``).
TRIES = 2
#: Why a landing leaves out a file that no landing will ever write: a name that runs code, and a link or a file with a
#: second name.  The thread's version stays in its copy, and is no work a turn's end failed to save.
NEVER_LANDS = ("protected", "linked")
#: How much one landing carries, in characters of its files' names and of what is said of each: one answer of its
#: computer's, and each request of the server's that names its files, is one frame of the link, which this leaves
#: room in.  A turn that changed more lands none of it, and says so, before anything is committed.
CARRIES = MAX_MESSAGE_CHARS
#: What a commit's answer says of one file beside its name, at its longest: its two versions, why it was left out,
#: and the thread that changed it, by its id and a title of some words.  A record's request says less of each.
_PER_FILE = 256
#: What a look's answer says of one file beside its name: a revision at its longest.
_PER_LOOK = 128
#: Why a landing put back is not tried again at once: the next try would meet the same.
_ONCE = frozenset({"stale", "too_large", "yours_too_large"})
#: A put-back its computer did not finish, though it answered: it may be asked again, and was not refused.
_UNFINISHED = frozenset({"cancelled", "interrupted", "unavailable", "busy"})


class _Unsettled(Exception):
    """A landing its computer stopped answering, or the journal stopped: left as its row says, for whoever settles it."""


class _Unwritten(Exception):
    """A landing's row could not be written where it must be: before its first apply, or before its record."""


class _Stale(Exception):
    """The thread's copy changed after the landing looked at the folder: something still writes there."""


class _TooLarge(Exception):
    """A turn changed more files than one landing carries."""


class _YoursTooLarge(Exception):
    """You changed more files in the folder since its last landing than one landing's pickup records."""


class _Waited(Exception):
    """A landing left running in the folder is its own worker's still, for longer than a landing waits for the folder."""


def carries(paths: list[str]) -> bool:
    """Whether one landing carries the files *paths*: what its computer answers of them, and what is asked, fits the link.

    Each name is counted twice, as a record names a file it leaves out that
    no landing writes, and as JSON spells it, every character past the
    basic ones as its escape, which is never less than the app counts.
    """
    return sum(2 * len(json.dumps(path)) + _PER_FILE for path in paths) <= CARRIES


def looks(paths: list[str]) -> Iterator[list[str]]:
    """*paths* in the looks that ask for them: at most a look's files each, and an answer that fits the link."""
    look: list[str] = []
    size = 0
    for path in paths:
        more = len(json.dumps(path)) + _PER_LOOK
        if look and (len(look) == MAX_LOOKED or size + more > CARRIES):
            yield look
            look, size = [], 0
        look.append(path)
        size += more
    if look:
        yield look


async def lands(store: Any, session: Any, turn: int, calls: list) -> bool:
    """Whether a thread's turn end has anything to land in its folder: asked before its computer is.

    It has when a step of the turn could change the copy, which is any tool
    call but one that only reads.  It has when the copy may still hold a
    turn's work from before: the thread's last turn end did not land it, its
    landing put back or not finished, or that turn failed or was stopped.
    And it has when a helper of the thread, which works in its copy,
    reported since.  A turn that only talked asks its computer nothing more,
    and waits for none that is away.
    """
    if any((call.data or {}).get("name") not in SAGA_EXCLUDED_TOOLS for call in calls):
        return True
    ended = await store.last_event(session.id, *TURN_ENDS)
    if ended is not None and not (ended.type == EventType.SESSION_COMPLETE.value and ended.data.get("saved", True)):
        return True
    for report in (EventType.WORKER_COMPLETE, EventType.WORKER_FAILED):
        if await store.has_event(session.id, report, after=turn):
            return True
    return False


async def land_local_turn(
    *, store: Any, session_factory: Any, redis: Any, session: Any, lease_token: str | None,
    saga_settings: Any, tool_saga_id: str | None,
) -> dict | None:
    """Land *session*'s turn in its folder on its user's computer; its outcome, as ``landing.land_turn`` gives one, or None.

    None when the turn has nothing to land.  The outcome's *state* is
    ``completed``, ``compensated`` (put back whole, or never begun),
    ``escalated`` (a file could not be put back) or ``unsettled`` (its
    computer stopped answering in the middle of it, and the landing is as
    its row says).  A landing that did not land says why, ``reason``: the
    folder changed while it landed (``changed``), the copy was still being
    written (``stale``), the turn changed more than one landing carries
    (``too_large``), you changed more files in the folder since its last
    landing than one landing records (``yours_too_large``, told apart by
    its computer's answer to the pickup), its row could not be written
    (``unwritten``), another chat held the folder (``busy``), its computer
    did not answer (``unanswered``), or refused it (``refused``, with its
    word, ``code``), or a landing left in its folder had recorded, and its
    row could not say so (``settling``).
    ``recovery`` is what the folder's helper found there, left by a landing
    cut short, where it found any.

    The folder is held first, and every landing left running in it is
    settled (:func:`settle_folder`); then it is landed in.  Where the turn's
    last landing did not let the folder go by a forgetting of its own, the
    turn gives it back by its hold's own name, which drops nothing a landing
    kept.  Taken up again after its worker was lost, a turn whose landing
    began goes on with it from the journal; and lands what it did since, as
    a landing of its own.

    Raises :class:`LeaseLost` where another worker took the thread's turn:
    this one stands down, and has written nothing more.
    """
    turn = await _turn_now(store, session)
    calls = await store.get_events(session.id, after=turn, types=[EventType.TOOL_CALL])
    if not await lands(store, session, turn, calls):
        return None
    copy = thread_copy(session, session_factory=session_factory, redis=redis, lease_token=lease_token)
    redoing = await redo_files(store, session.id)
    held = (session.id, lease_token) if lease_token is not None else None
    hold = copy.steps(f"land:{turn}:hold")
    try:
        recovery = await _held(copy, hold)
        # Before anything of this turn's is looked at there.
        await settle_folder(store, session_factory, copy, session, turn, hold, held, redis=redis)
    except LeaseLost:
        raise
    except Exception as refused:
        logger.warning("The folder of thread %s was not held and settled for its landing, so its turn lands nothing", session.id, exc_info=True)
        outcome = _not_begun(refused)
        if outcome["reason"] in ("refused", "settling"):
            await _release(copy, session, turn)
        return outcome
    land = partial(
        _land, copy, session, turn, session_factory=session_factory, saga_settings=saga_settings, tool_saga_id=tool_saga_id,
        held=held,
    )
    outcome: dict[str, Any] = {}
    for attempt in range(1, TRIES + 1):
        outcome = await land(attempt, calls=calls)
        # Not where a try at once would meet the same, nor after a cancel, which is what a Stop's pause sends.
        if outcome["state"] != "compensated" or outcome.get("reason") in _ONCE or outcome.get("code") == "cancelled":
            break
    since = [call for call in calls if call.id > outcome["through"]]
    if outcome["state"] == "completed" and since:
        # Taken up again, the turn worked on after its landing began, which
        # had recorded: what it did since lands now, as a landing of its own.
        outcome = _both(outcome, await land(TRIES + 1, calls=since))
    if not outcome.pop("forgot") and outcome["state"] != "unsettled":
        await _release(copy, session, turn)
    if recovery:
        outcome["recovery"] = recovery
    _tell(outcome, redoing)
    if outcome["state"] == "completed":
        # A file no landing writes is not work this turn's end failed to save.
        left = {o["path"] for o in outcome["overlapped"] if o["reason"] not in NEVER_LANDS}
        outcome["saved"] = left <= {redo["path"] for redo in outcome["redo"]}
    return outcome


def waiting_on_you_here(session: Any, paths: list[str], *, escalated: bool) -> dict:
    """The ``inbox.action_required`` a thread on a computer waits on you with, over its files: as the cloud's, in words of its own.

    Nothing opens a version of a computer's file from here yet, so its words
    say where each thing is, and its target is the thread: the files in the
    folder, the thread's versions in its copy or the folder's history, and
    the earlier ones in that history, on that computer.  Over a landing that
    could not be put back whole, or over files that changed again while the
    thread redid its change, as ``landing.waiting_on_you`` says them.
    """
    config = session.config or {}
    computer = (config.get("execution") or {}).get("device_name") or "your computer"
    folder = config.get("workspace_path") or "its folder"
    named = ", ".join(paths[:20]) + (f" and {len(paths) - 20} more" if len(paths) > 20 else "")
    if escalated:
        title = "Couldn't finish landing my changes"
        instructions = (
            f"A landing of this thread's changes in {folder} on {computer} could not be put back whole: {named or 'its files'}. "
            "Open each of them there and check it: it holds this thread's version, the file as it was, or a change you made since. "
            "This thread's versions are in its own copy on that computer, and what each file held before is in the folder's "
            "history on that computer, which cannot be opened from here yet."
        )
    else:
        others = len(paths) - 1
        title = f"Couldn't merge my changes to {paths[0]}" + (f" and {others} other file{'s' if others > 1 else ''}" if others else "")
        instructions = (
            f"{named} changed again while this thread redid its change. The newer file was kept; this thread's version "
            f"is in the folder's history on {computer}, which cannot be opened from here yet."
        )
    return {
        "title": title, "instructions": instructions, "context": "", "action_type": "files", "target": "session", "reason": "files",
        # For the thread, which reads the wait as news, and for a later landing, which ends a wait whose files landed.
        "files": list(paths), "escalated": escalated,
    }


async def _held(copy: ThreadCopy, hold: Steps) -> dict[str, list]:
    """Hold the folder for the turn's landing, its helper first putting right what a landing cut short left there: what it found.

    Of that, what a person may have to look at: a file kept beside a newer
    one of its name, a file gone with its folder, a record that could not
    be read.  A file that took its name again is as it was.  Asked under
    the turn's hold, ``land:<turn>:hold``.
    """
    found = await _within(_land_asked(hold, "recover"))
    told = {key: found[key] for key in ("beside", "lost", "unread") if found[key]}
    if told:
        logger.warning("The folder of a landing in %s held what a landing cut short left there: %s", copy.folder, sorted(told))
    return told


def _hold_of(session: Any, turn: int) -> str:
    """The name a turn holds its folder under, which no landing has: forgotten, it drops nothing a landing kept."""
    return f"hold:{uuid5(session.id, f'land:{turn}:hold')}"


async def _release(copy: ThreadCopy, session: Any, turn: int) -> None:
    """Give the folder back for a turn whose landing let it go by no forgetting of its own.

    Its landing ended before, as one taken up again finds it, was left
    escalated, or its forgetting was refused.  By the turn's hold's own name,
    under an invocation of its own, so a turn's end taken up again asks it
    anew.
    """
    await _given_back(copy.steps(f"land:{turn}:release:{uuid4()}"), session, turn)


async def _given_back(steps: Steps, session: Any, turn: int) -> None:
    """Forget the turn's hold of its folder, under *steps*: as best it can, for a while at most.

    Its app lets the folder go by itself once the landing's helper has been idle.
    """
    try:
        await asyncio.wait_for(steps.land("forget", saga=_hold_of(session, turn), applied=[]), RELEASE_WAIT)
    except LeaseLost:
        raise
    except Exception:
        logger.warning("The folder of thread %s was not given back: its app lets it go once idle", session.id, exc_info=True)


async def _within(asked: Any) -> Any:
    """One ask of a landing's computer, waited for as a step is."""
    return await asyncio.wait_for(asked, STEP_WAIT)


async def _land_asked(steps: Steps, action: str, **arguments: Any) -> dict:
    """One ``land`` action, asked again while another chat holds the folder, for :data:`BUSY_PATIENCE` at most."""
    patience = time.monotonic() + BUSY_PATIENCE
    while True:
        try:
            return await steps.land(action, **arguments)
        except ComputerRefused as refused:
            if refused.kind != "busy" or time.monotonic() >= patience:
                raise
        await asyncio.sleep(BUSY_WAIT)


def _away(exc: BaseException | None) -> bool:
    """Whether *exc* says nothing more of a landing can be asked now: its computer's wait reached its bound, or the
    journal refused the worker (another holds the turn, the thread was deleted, the turn took another path)."""
    return isinstance(exc, (TimeoutError, SagaTimeoutError)) or _journals(exc)


def _journals(exc: BaseException | None) -> bool:
    """Whether *exc* is the journal's own refusal of an operation, which its computer never heard of."""
    if isinstance(exc, OperationConflict):
        return True
    return isinstance(exc, DeviceOperationError) and not isinstance(exc, (ComputerRefused, NotAnAnswer))


def _unfinished(exc: BaseException) -> bool:
    """Whether a put-back *exc* ended is one its computer did not finish: asked again, it may be done."""
    if _away(exc):
        return True
    return isinstance(exc, ComputerRefused) and (exc.kind in _UNFINISHED or exc.code == "no_answer")


def _why(exc: BaseException) -> dict[str, str]:
    """Why a landing *exc* ended did not land, as its report says it."""
    if isinstance(exc, _Unwritten):
        return {"reason": "unwritten"}
    if isinstance(exc, _Stale):
        return {"reason": "stale"}
    if isinstance(exc, ComputerRefused):
        if exc.kind in ("conflict", "stale", "too_large"):
            return {"reason": "changed" if exc.kind == "conflict" else exc.kind}
        return {"reason": "refused", "code": code_of(exc)}
    if isinstance(exc, NotAnAnswer):
        return {"reason": "refused", "code": code_of(exc)}
    return {}


def _not_begun(refused: BaseException) -> dict:
    """The outcome of a turn's end whose landing did not begin: nothing of the turn reached the folder, and it is in the copy.

    A settle its computer did not finish says why by what ended it.
    """
    if isinstance(refused, _Unsettled) and refused.__cause__ is not None:
        refused = refused.__cause__
    if isinstance(refused, LandingUnsettled):
        why = {"reason": "settling"}
    elif isinstance(refused, _Waited) or (isinstance(refused, ComputerRefused) and refused.kind == "busy"):
        why = {"reason": "busy"}
    elif _away(refused):
        why = {"reason": "unanswered"}
    else:
        why = {"reason": "refused", "code": code_of(refused) if isinstance(refused, DeviceOperationError) else "other"}
    return {
        "saga": None, "state": "compensated", "commit": None, "landed": [], "overlapped": [], "excluded": [], "repositories": [],
        "not_taken": [], "files": [], "picked_up": [], "saved": False, "packs": 0, **why,
    }


def _of_row(row: Any) -> dict:
    """The outcome of a landing whose row says it ended, as its own run gave it: what it landed, what it left out, and why.

    Read from its steps, as :func:`_row_files` is: the commit's one answer,
    the files no landing writes that its record left in the copy, and the
    files its record landed.  So a turn's end taken up again after its
    first run was lost before telling tells the same, the files to redo
    among it.
    """
    saga = saga_of(row)
    commit = next((s.execute_result for s in saga.steps if s.tool_name == "history.commit" and s.execute_result), None) or {}
    record = next((s for s in saga.steps if s.tool_name == "history.record"), None)
    left = set(record.arguments.get("left", [])) if record is not None else set()
    changes = commit.get("changes", [])
    held = {o["path"] for o in commit.get("overlapped", [])}
    overlapped = [*commit.get("overlapped", []), *({**c, "reason": "linked"} for c in changes if c["path"] in left and c["path"] not in held)]
    landed = record.arguments["applied"] if record is not None and row.saga_state == "completed" else []
    return {
        "saga": row.saga_id, "state": row.saga_state, "commit": row.commit, "landed": landed, "overlapped": overlapped,
        "excluded": commit.get("excluded", []), "repositories": commit.get("repositories", []), "not_taken": commit.get("not_taken", []),
        "files": _told(changes, landed, overlapped), "picked_up": row.picked_up or [], "saved": False, "packs": 0, "forgot": False,
        "through": _through(row),
    }


def _events_of(row: Any) -> tuple[int, int] | None:
    """The first and the last tool call the landing *row* was begun for, as its first run named them; None for none."""
    if row.events is None:
        return None
    return row.events.lower, row.events.upper - (0 if row.events.upper_inc else 1)


def _through(row: Any) -> int:
    """The last tool call the landing *row* was begun for: 0 where its turn had made none."""
    events = _events_of(row)
    return events[1] if events is not None else 0


def _told(changes: list[dict], landed: list[dict], overlapped: list[dict]) -> list[dict]:
    """A landing's files as its turn's report names them: each landed, or left out, and why."""
    applied = {c["path"]: c for c in landed}
    reasons = {o["path"]: o["reason"] for o in overlapped}
    return [
        {
            "kind": "file", "label": path, "ref": path, "landing": "landed" if path in applied else "not_merged",
            # A landed deletion is no file to open: the report names it apart.
            **({"change": "deleted"} if path in applied and applied[path]["after"] is None else {}),
            **({"reason": reasons[path]} if path in reasons else {}),
        }
        for path in sorted({c["path"] for c in changes} | set(reasons))
    ]


def _row_files(saga: Saga) -> list[dict]:
    """A completed landing's files on a computer, each with its two versions: those its record landed, and those it left out.

    Read from its steps alone, so whoever completes it from its row writes
    the same: its record names what it landed, a file already as the turn
    left it among them, and its commit what it left out, with what no
    landing writes that its look found a link or a file with a second name.
    """
    commit = next(s.execute_result for s in saga.steps if s.tool_name == "history.commit")
    landed = next(s for s in saga.steps if s.tool_name == "history.record").arguments["applied"]
    paths = {c["path"] for c in landed}
    return [
        *({"path": c["path"], "before": c["before"], "after": c["after"], "merged": True} for c in landed),
        *({"path": o["path"], "before": o["before"], "after": o["after"], "merged": False} for o in commit["overlapped"]),
        *({"path": c["path"], "before": c["before"], "after": c["after"], "merged": False} for c in commit["changes"] if c["path"] not in paths),
    ]


async def _land(
    copy: ThreadCopy, session: Any, turn: int, attempt: int, *,
    session_factory: Any, saga_settings: Any, tool_saga_id: str | None, calls: list, held: tuple[UUID, str] | None,
) -> dict:
    """One try of a turn's landing, as one saga, of the turn's tool calls *calls*; its outcome.

    With ``forgot``, whether its own forgetting let the folder go, and
    ``through``, the last tool call it was begun for.  Taken up again, it
    goes on as its first run began it, its audit among it, so each step it
    asks again is answered from the journal; but where the turn took
    another path than that run, or worked on since that run began it, or a
    settle began on it, it is settled as a landing left running is, from
    what its computer was asked, and the turn lands again as a new saga.
    """
    invocation = f"land:{turn}" if attempt == 1 else f"land:{turn}:{attempt}"
    # Named after its invocation: a turn taken up again finds its landing, and no other landing ever has its name.
    saga_id = f"saga:{uuid5(session.id, invocation)}"
    orchestrator = _orchestrator(saga_settings)
    saga = orchestrator.adopt(Saga(saga_id=saga_id, session_id=session.id, kind="landing"))
    events = (calls[0].id, calls[-1].id) if calls else None
    outcome: dict[str, Any] = {
        "saga": saga_id, "state": "completed", "commit": None,
        "landed": [], "overlapped": [], "excluded": [], "repositories": [], "not_taken": [], "files": [], "picked_up": [],
        "saved": False, "packs": 0, "forgot": False, "through": events[1] if events is not None else 0,
    }
    settled = partial(_settled, copy, session, turn, session_factory=session_factory, held=held)
    try:
        found = await landing_row(session_factory, saga_id)
    except Exception:
        logger.warning("The landing of thread %s on its computer has no row, so it did not begin", session.id, exc_info=True)
        return {**outcome, "state": "compensated", "reason": "unwritten"}
    if found is not None and found.saga_state != "running":
        # Ended already, by a run of this turn's end that was lost before it was told: as its row says.
        return _of_row(found)
    if found is not None:
        tool_saga_id, events, outcome["through"] = found.tool_saga_id, _events_of(found), _through(found)
        if calls and calls[-1].id > outcome["through"]:
            # Gone on with, it would record the copy as it is now over the
            # turn it committed, and set the work done since aside.
            logger.warning("Thread %s worked on since its landing %s began: it is settled, and the turn lands again", session.id, saga_id)
            return await settled(found)
        if await copy.operations.settling(copy.device_id, saga_id):
            # Another landing began putting it back, and stopped: gone on
            # with, it would record files the folder may no longer hold.
            logger.warning("A settle began on the landing %s of thread %s: it is settled, and the turn lands again", saga_id, session.id)
            return await settled(found)
    try:
        row_id = found.id if found is not None else await start_landing(
            session_factory, saga, workstream_id=session.config["workstream_id"], thread_id=session.id,
            agent_id=str(session.agent_id), user_id=session.user_id, tool_saga_id=tool_saga_id,
            events=events, device_id=copy.device_id, folder=copy.folder, held=held,
        )
    except Exception:
        logger.warning("The landing of thread %s on its computer has no row, so it did not begin", session.id, exc_info=True)
        return {**outcome, "state": "compensated", "reason": "unwritten"}
    if row_id is None:
        raise LeaseLost(session.id)
    row = _Row(session_factory, row_id, saga, held=held)
    steps = copy.steps(invocation)
    thread = {"name": session.title or "Thread", "email": f"thread:{session.id}@surogate"}
    you = {"name": str(session.user_id), "email": f"user:{session.user_id}@surogate"}
    audit = [
        ["Surogate-Project", str(session.config["workstream_id"])],
        ["Surogate-Thread", str(session.id)],
        ["Surogate-Agent", str(session.agent_id)],
        ["Surogate-User", str(session.user_id)],
        ["Surogate-Saga", saga_id],
        *([["Surogate-Tool-Saga", tool_saga_id]] if tool_saga_id else []),
        *([["Surogate-Events", f"{events[0]}-{events[1]}"]] if events is not None else []),
    ]

    def step(name: str, **arguments: Any) -> SagaStep:
        return orchestrator.add_step(
            saga_id, tool_name=f"history.{name}", tool_call_id="", arguments=arguments, timeout_seconds=STEP_WAIT, max_retries=0,
        )

    async def execute(it: SagaStep, ask: Any) -> dict:
        async def once() -> dict:
            await _written(row.alive)
            return await ask()

        return await orchestrator.execute_step(saga_id, it.step_id, once)

    async def fixed() -> None:
        # At a turning point the row must hold: seen written, or the landing goes no further.
        try:
            await row.write()
        except LeaseLost:
            raise
        except Exception as unwritten:
            raise _Unwritten("the landing's row could not be written") from unwritten

    # The applies whose file the computer's helper said it holds nothing of, and never wrote: by their step's number.
    nothing: set[int] = set()
    changes: list[dict] = []
    try:
        # The files the copy changed, and a look at each in the folder, before
        # anything is committed: a save of yours after the look is either
        # picked up, or found by its apply.
        paths = (await _within(steps.history("changed")))["paths"]
        if paths and not carries(paths):
            raise _TooLarge(f"the turn changed {len(paths)} files")
        revisions: dict[str, str] = {}
        for look in looks(paths):
            revisions.update(dict((await _within(_land_asked(steps, "revisions", paths=look)))["revisions"]))
        turned: dict[str, Any] = {"commit": None, "changes": [], "overlapped": [], "excluded": [], "repositories": [], "not_taken": []}
        if paths:
            pickup = step("pickup", author=you, trailers=[*audit, ["Surogate-Kind", "pickup"]])
            picked = await _your_edits(execute, pickup, steps)
            outcome["packs"] = picked["packs"]
            commit = step("commit", author=thread, trailers=[*audit, ["Surogate-Kind", "turn"]], pickup=picked["commit"])
            # The one answer the saga keeps: each apply's step is its file's place among its changes.
            turned = await execute(commit, partial(steps.history, "commit", **commit.arguments))
        changes = turned["changes"]
        # A file already as the turn left it, or gone on both sides, needs no write.
        written = [(number, c) for number, c in enumerate(changes) if c["before"] != c["after"]]
        if any(c["path"] not in revisions for _, c in written):
            raise _Stale("the turn names a file the landing's look did not see")
        # A link, or a file with a second name, is never replaced: the thread's version stays in its copy.
        linked = {c["path"] for _, c in written if revisions[c["path"]] == "other"}
        overlapped = [*turned["overlapped"], *({**c, "reason": "linked"} for c in changes if c["path"] in linked)]
        outcome.update(
            overlapped=overlapped, excluded=turned["excluded"], repositories=turned["repositories"], not_taken=turned["not_taken"],
        )
        if turned["commit"] is None:
            # Nothing changed, nothing lands: no change to the folder's files to record, and the folder is let go.
            await _written(row.drop)
            outcome["forgot"] = await _forgotten(saga, orchestrator, steps, None, nothing, "completed")
            return outcome
        applies = [
            step("apply", saga=saga_id, step=number, **c, expected=revisions[c["path"]])
            for number, c in written if c["path"] not in linked
        ]
        # The steps, fixed: all a put-back by whoever settles this landing needs.
        await fixed()
        for it in applies:
            await execute(it, partial(_applied, steps, **it.arguments))
        landed = [c for c in changes if c["path"] not in linked]
        record = step(
            "record", turn=turned["commit"], applied=landed, author=thread, main=picked["main"], pickup=picked["commit"],
            trailers=[*audit, ["Surogate-Kind", "landing"], *(["Surogate-Not-Merged", o["path"]] for o in overlapped)],
            # What no landing writes stays in the copy as the thread left it: the commit's own changes, and none else.
            left=sorted(o["path"] for o in overlapped if o["reason"] in NEVER_LANDS),
        )
        # Before it is sent: a landing whose row has no record step never pushed.
        await fixed()
        recorded = await execute(record, partial(steps.history, "record", **record.arguments))
        saga.transition(SagaState.COMPLETED)
        outcome.update(commit=recorded["commit"], landed=landed, picked_up=picked["picked_up"])
        await row.write(state="completed", commit=recorded["commit"], files=_row_files(saga), picked_up=_row_picked_up(saga))
    except LeaseLost:
        # Another worker runs the thread now, and goes on with this landing from the journal.
        raise
    except OperationConflict:
        # Taken up again, the turn asked a step otherwise than the run it takes up.
        logger.warning("The landing %s of thread %s was taken up again on another path: it is settled, and the turn lands again", saga_id, session.id)
        return await settled(await landing_row(session_factory, saga_id))
    except _TooLarge as large:
        logger.warning("The landing of thread %s on its computer carries no turn so large: %s", session.id, large)
        outcome.update(state="compensated", reason="too_large", saved=False)
        await _written(row.write, tries=2, state="compensated")
    except _YoursTooLarge:
        logger.warning("The landing of thread %s on its computer cannot record your edits there in one go", session.id, exc_info=True)
        # Nothing of it was done: no ref moved, no file was written, and no row is left of it.
        outcome.update(state="compensated", reason="yours_too_large", saved=False)
        await _written(row.drop)
        outcome["forgot"] = await _forgotten(saga, orchestrator, steps, None, nothing, "compensated")
        return outcome
    except Exception as failed:
        logger.warning("The landing of thread %s on its computer did not finish", session.id, exc_info=True)
        outcome.update(saved=False, **_why(failed))
        try:
            if _away(failed):
                raise _Unsettled("its computer did not answer, or the journal took no more of it") from failed
            state, pushed = await _ended(saga, orchestrator, steps, row, nothing)
        except _Unsettled as unsettled:
            logger.warning("The landing %s of thread %s is left unsettled, as its row says", saga_id, session.id)
            if not _journals(unsettled.__cause__):
                # Its computer did not answer: the row says how far it got.  What the journal refused is another's to write.
                await _written(row.write)
            return {**outcome, "state": "unsettled", "commit": None, "landed": []}
        outcome["state"] = state
        if pushed is not None:
            # The push happened though its answer was lost: the landing counts, and nothing went back.
            outcome.pop("reason", None)
            outcome.pop("code", None)
            landed = next(s for s in saga.steps if s.tool_name == "history.record").arguments["applied"]
            outcome.update(commit=pushed, landed=landed, picked_up=_row_picked_up(saga))
    if outcome["state"] != "escalated":
        outcome["forgot"] = await _forgotten(saga, orchestrator, steps, row, nothing, outcome["state"])
    outcome["files"] = _told(changes, outcome["landed"], outcome["overlapped"])
    return outcome


async def _your_edits(execute: Any, pickup: SagaStep, steps: Steps) -> dict:
    """Your edits in the folder since its last landing, committed on ``main`` for the landing's record to push: the pickup's answer.

    Read here alone.  It names every file you changed there since, which
    no bound of the turn's limits: one too many for one answer of the link
    is answered ``too_large`` by its computer once it ran.  On a computer a
    pickup moves no ref, so nothing of it is left, and the landing goes no
    further (:class:`_YoursTooLarge`).  Your edits stay as they are in the
    folder, and are asked for again by the next landing there.
    """
    try:
        return await execute(pickup, partial(steps.history, "pickup", **pickup.arguments))
    except ComputerRefused as refused:
        if refused.kind == "too_large":
            raise _YoursTooLarge("your edits in the folder do not fit one answer of its computer's") from refused
        raise


async def _applied(steps: Steps, **asked: Any) -> dict:
    """One apply; what it wrote, as it was asked to.

    A landing's row holds the files the server asked to apply, and its
    computer's answer is checked against that: one that names another
    file, or other versions, is no answer to this step.
    """
    did = await _land_asked(steps, "apply", **asked)
    if (did["path"], did["before"], did["after"]) != (asked["path"], asked["before"], asked["after"]):
        raise NotAnAnswer("This computer's answer to 'apply' names another file than the one it was asked to write")
    return {"path": asked["path"], "before": asked["before"], "after": asked["after"], "made": did["made"]}


async def _ended(saga: Saga, orchestrator: Any, steps: Steps, row: _Row, nothing: set[int]) -> tuple[str, str | None]:
    """End a landing that did not finish: ``completed`` with its commit where ``main`` holds it, else put back.

    As the cloud's settle (``landing._settle``): before anything goes back,
    a landing with a record step, asked or not, has the folder's history
    asked whether its saga is in ``main`` back to where it began.  One that
    is pushed is never put back.  Where a pruning's cut hides the answer it
    is given up, ``escalated``, its files left as they are.  A look its
    computer does not answer leaves the landing unsettled.
    """
    # Where it stopped, before anything goes back.
    await _written(row.write)
    record = next((s for s in saga.steps if s.tool_name == "history.record"), None)
    if record is not None:
        looked = await _looked(steps, saga, record)
        if looked["landing"] is not None:
            if saga.state is SagaState.RUNNING:
                saga.transition(SagaState.COMPLETED)
            await _written(
                row.write, tries=2, state="completed", commit=looked["landing"], files=_row_files(saga), picked_up=_row_picked_up(saga),
            )
            return "completed", looked["landing"]
        if looked["hidden"]:
            _given_up(saga, every=False)
            await _written(row.write, tries=2, state="escalated")
            return "escalated", None
    failed = await _put_back(saga, steps, row, nothing)
    state = "escalated" if failed else "compensated"
    await _written(row.write, tries=2, state=state)
    return state, None


async def _looked(steps: Steps, saga: Saga, record: SagaStep) -> dict:
    """Whether ``main`` in the folder's history holds the landing *saga*, back to the ``main`` its *record* names.

    A look its computer does not answer leaves the landing unsettled.
    """
    try:
        return await _within(steps.history("fetch", saga=saga.saga_id, since=record.arguments["main"]))
    except LeaseLost:
        raise
    except Exception as unseen:
        raise _Unsettled("the folder's history did not say whether the landing pushed") from unseen


def _given_up(saga: Saga, *, every: bool) -> list[str]:
    """A landing a pruning's cut hides the push of, given up: each apply that may have written its file, which stays as it is.

    With *every*, a settle's, every apply it holds: its row may show one
    pending that ran.  Their files, for whoever checks them.
    """
    logger.error("Landing %s cannot be put back: the folder's history is cut above where it began", saga.saga_id)
    applies = [it for it in saga.steps if it.tool_name == "history.apply" and (every or it.state is not StepState.PENDING)]
    for it in applies:
        it.state, it.error = StepState.COMPENSATION_FAILED, "The folder's history no longer says whether this landing pushed"
    return [it.arguments["path"] for it in applies]


async def _put_back(saga: Saga, steps: Steps, row: _Row, nothing: set[int], *, every: bool = False) -> list[SagaStep]:
    """Put back what a landing applied, newest first; the steps that could not be put back.

    The app's helper wrote down what each apply was about to do before it
    did it, so a put-back asks for a step by its number alone, and is safe
    for one that never ran, or was cut off: an apply that failed, whose
    answer may have been lost, is put back too.  A file changed since the
    landing wrote it is left as it is, and what the landing kept of it stays
    kept: the saga escalates.  A put-back its computer did not finish ends
    the put-back there: the landing is unsettled, and nothing more is asked.

    With *every*, a settle's: every apply the landing holds is asked, in
    the same order however often the settle begins again, whatever state
    its row gives it, since the row may show one pending that ran; and
    each ends ``compensated``, or ``compensation_failed`` where it could not
    be put back.
    """
    if saga.state is SagaState.RUNNING:
        saga.transition(SagaState.COMPENSATING)
    failed: list[SagaStep] = []
    applies = [it for it in saga.steps if it.tool_name == "history.apply" and (every or it.state is not StepState.PENDING)]
    for it in sorted(applies, key=lambda it: it.arguments["step"], reverse=True):
        committed = it.state is StepState.COMMITTED
        if committed:
            it.transition(StepState.COMPENSATING)
        await _written(row.alive)
        try:
            did = await _within(_land_asked(steps, "unapply", saga=saga.saga_id, step=it.arguments["step"], path=it.arguments["path"]))
            if did["path"] != it.arguments["path"]:
                raise NotAnAnswer("This computer's answer to 'unapply' names another file than the one it was asked to put back")
        except LeaseLost:
            raise
        except Exception as exc:
            logger.warning("Could not put back %s", it.arguments["path"], exc_info=True)
            if committed:
                it.error = f"Compensation failed: {exc}"
                it.transition(StepState.COMPENSATION_FAILED)
            elif every:
                it.state, it.error = StepState.COMPENSATION_FAILED, f"Compensation failed: {exc}"
            failed.append(it)
            if _unfinished(exc):
                raise _Unsettled("its computer did not finish putting the landing back") from exc
            continue
        if not did["put_back"]:
            nothing.add(it.arguments["step"])
        if committed:
            it.compensation_result = did
            it.transition(StepState.COMPENSATED)
        elif every:
            it.state, it.compensation_result = StepState.COMPENSATED, did
    if saga.state is SagaState.COMPENSATING:
        saga.transition(SagaState.ESCALATED if failed else SagaState.COMPLETED)
    return failed


async def _forgotten(saga: Saga, orchestrator: Any, steps: Steps, row: _Row | None, nothing: set[int], state: str) -> bool:
    """Forget what a landing recorded, or put back whole, kept of the files it replaced, which lets the folder go; whether it did.

    A step of its saga, in its row, so that one not forgotten is asked
    again by the next landing in the folder.  ``applied`` is every apply
    that was sent, but those the app's helper said it holds nothing of: the
    folder's history lets the landing go only where ``main`` holds it, or
    where each of them is its ``before`` again, and the app's helper forgets
    only where the history says it may.  Never asked of a landing left
    escalated: what it kept stays kept until a person settles it.
    """
    applied = [
        {"path": s.arguments["path"], "before": s.arguments["before"], "after": s.arguments["after"], "step": s.arguments["step"]}
        for s in saga.steps
        if s.tool_name == "history.apply" and s.state is not StepState.PENDING and s.arguments["step"] not in nothing
    ]
    it = orchestrator.add_step(
        saga.saga_id, tool_name="history.forget", tool_call_id="", arguments={"saga": saga.saga_id, "applied": applied},
        timeout_seconds=STEP_WAIT, max_retries=0,
    )
    try:
        await orchestrator.execute_step(saga.saga_id, it.step_id, partial(_land_asked, steps, "forget", **it.arguments))
    except LeaseLost:
        raise
    except Exception:
        logger.warning("The landing %s was not forgotten on its computer: the next landing in its folder asks again", saga.saga_id, exc_info=True)
    if row is not None:
        await _written(row.write, state=state)
    return it.state is StepState.COMMITTED


def _both(first: dict, more: dict) -> dict:
    """The outcome of a turn that landed twice, as one: what its first landing landed, and what the turn did since."""
    files = {f["ref"]: f for f in (*first["files"], *more["files"])}
    return {
        **more, "commit": more["commit"] or first["commit"],
        **{key: [*first[key], *more[key]] for key in ("landed", "overlapped", "not_taken", "picked_up")},
        **{key: sorted({*first[key], *more[key]}) for key in ("excluded", "repositories")},
        "files": [files[ref] for ref in sorted(files)],
    }


def _own(session: Any, turn: int) -> set[str]:
    """The sagas of this turn's own landings: its own to go on with, never settled as another's."""
    return {f"saga:{uuid5(session.id, f'land:{turn}' if n == 1 else f'land:{turn}:{n}')}" for n in range(1, TRIES + 2)}


@contextlib.asynccontextmanager
async def _lease_of(store: Any, thread_id: UUID, owner: str) -> AsyncIterator[str | None]:
    """Take *thread_id*'s lease for as long as the block runs; its token, or None while a worker holds it.

    Whoever settles a thread's landing holds that thread's turn meanwhile:
    a worker of its own that still ran finds its lease taken at its next
    step, and stands down.  Renewed while the settle runs, a renewal that
    failed asked again at the next; and given back.  Taken by another all
    the same, it is renewed no more: the settle stops at its next step,
    whose write of the row finds the lease another's (:class:`LeaseLost`).
    """
    # Imported here: the session's store imports the harness.
    from surogates.session.store import LeaseNotHeldError

    lease = await store.try_acquire_lease(thread_id, owner, ttl_seconds=SETTLE_LEASE)
    if lease is None:
        yield None
        return

    async def renewing() -> None:
        while True:
            await asyncio.sleep(SETTLE_RENEW)
            try:
                await store.renew_lease(thread_id, lease.lease_token, ttl_seconds=SETTLE_LEASE)
            except LeaseNotHeldError:
                logger.warning("The lease of thread %s, which a settle held, is another's now", thread_id)
                return
            except Exception:
                logger.warning("The lease of thread %s, which a settle holds, was not renewed: asked again", thread_id, exc_info=True)

    renewed = asyncio.ensure_future(renewing())
    try:
        yield str(lease.lease_token)
    finally:
        renewed.cancel()
        await asyncio.gather(renewed, return_exceptions=True)
        with contextlib.suppress(Exception):
            await store.release_lease(thread_id, lease.lease_token)


async def settle_folder(
    store: Any, session_factory: Any, copy: ThreadCopy, session: Any, turn: int, hold: Steps, held: tuple[UUID, str] | None,
    *, redis: Any,
) -> None:
    """Settle every landing left running in this thread's folder, before anything of this turn's is looked at there.

    A landing whose worker was lost, and whose turn was never taken up
    again, left the folder as far as it got and the app keeping what it
    replaced.  The next landing in the folder, any thread's of any
    project's, ends it first, holding the folder (*hold*): complete where
    it recorded, put back whole where it did not (:func:`settle`).  Whoever
    settles one takes its thread's lease first, and holds it meanwhile.
    One whose lease a worker holds is that worker's to finish: this landing
    gives the folder back, waits, and takes it again, for
    :data:`BUSY_PATIENCE` at most, then lands nothing (``_Waited``).  So it
    never looks at a folder that holds half of another's landing, and no
    pickup takes that landing's files for yours.  This turn's own landings
    are its own to go on with.  An earlier turn's of this thread is settled
    under this worker's own lease, *held*; and a deleted thread's, which
    nobody holds, under none.

    A landing settled ``escalated`` is told to its thread's master, and its
    thread waits on you over it, once a landing.  Raises
    :class:`LandingUnsettled` where one recorded and its row could not say
    so: nothing lands in the folder until it can.  Then each landing ended
    there whose forgetting never let go of what it kept is forgotten
    (:func:`_forget_owed`).
    """
    patience = time.monotonic() + BUSY_PATIENCE
    while True:
        waits = False
        for row in await running_in(session_factory, copy.device_id, copy.folder):
            if row.saga_id in _own(session, turn):
                continue
            if row.thread_id is None or row.thread_id == session.id:
                state, at_issue = await settle(session_factory, copy, turn, row, held if row.thread_id is not None else None)
            else:
                try:
                    async with _lease_of(store, row.thread_id, f"settle:{session.id}") as taken:
                        if taken is None:
                            waits = True
                            continue
                        state, at_issue = await settle(session_factory, copy, turn, row, (row.thread_id, taken))
                except LeaseLost as lost:
                    if lost.session_id != row.thread_id:
                        raise
                    # Its thread's turn is a worker's again: that worker's to finish.
                    waits = True
                    continue
            logger.warning("Settled landing %s of thread %s, left running in its folder: %s", row.saga_id, row.thread_id, state)
            if state == "escalated" and row.thread_id is not None:
                await _tell_escalated(
                    session_factory, row.thread_id, saga_of(row), at_issue, redis=redis,
                    waits=lambda thread, paths: waiting_on_you_here(thread, paths, escalated=True),
                )
        if not waits:
            await _forget_owed(store, session_factory, copy, session, turn, held)
            return
        if time.monotonic() >= patience:
            raise _Waited("a landing left running in the folder is its own worker's still")
        # Its own worker's to finish, which needs the folder: given back meanwhile, and taken again.
        await _given_back(hold, session, turn)
        await asyncio.sleep(BUSY_WAIT)
        await _held(copy, hold)


async def _forget_owed(
    store: Any, session_factory: Any, copy: ThreadCopy, session: Any, turn: int, held: tuple[UUID, str] | None,
) -> None:
    """Forget each landing ended in this folder whose forgetting never let go of what it kept, :data:`FORGET_AGAIN` at most.

    A landing recorded, or put back whole, is forgotten at its end, which
    lets go what the app kept of the files it replaced.  One whose
    forgetting never ran, its worker lost or its computer away, or was
    refused, is asked again by the next landing in its folder, once that
    holds the folder and has settled what was left running.  Each under an
    invocation of the settling's own, ``land:<turn>:settle:<saga>:forget``,
    its thread's lease held as a settle holds it, and written into its row
    as a step of its saga.  A landing put back whole keeps nothing, and its
    forgetting names no apply.  One left ``escalated`` is not among them:
    what it kept stays kept until a person settles it.  This turn's own are
    its own.  As best it can: one not forgotten now is asked again by the
    landing after.
    """
    try:
        rows = await unforgotten_in(session_factory, copy.device_id, copy.folder, besides=_own(session, turn), most=FORGET_AGAIN)
    except Exception:
        logger.warning("The landings in %s that owe a forgetting were not read", copy.folder, exc_info=True)
        return
    for row in rows:
        try:
            if row.thread_id is None or row.thread_id == session.id:
                await _forget_again(session_factory, copy, turn, row, held if row.thread_id is not None else None)
                continue
            async with _lease_of(store, row.thread_id, f"settle:{session.id}") as taken:
                # Where a worker holds its thread's turn, that worker's to ask.
                if taken is not None:
                    await _forget_again(session_factory, copy, turn, row, (row.thread_id, taken))
        except LeaseLost as lost:
            if lost.session_id == session.id:
                raise
            logger.warning("The thread of landing %s is a worker's again: its forgetting is that worker's", row.saga_id)


async def _forget_again(session_factory: Any, copy: ThreadCopy, turn: int, row: Any, held: tuple[UUID, str] | None) -> None:
    """Ask the forgetting of the landing *row* records, which ended, once more; written into its row as a step of its saga."""
    saga = saga_of(row)
    orchestrator = _orchestrator(None)
    orchestrator.adopt(saga)
    # Put back whole, it keeps nothing: no apply is named, as none is kept.
    nothing = {s.arguments["step"] for s in saga.steps if s.tool_name == "history.apply"} if row.saga_state == "compensated" else set()
    steps = copy.steps(f"land:{turn}:settle:{row.saga_id}:forget")
    await _forgotten(saga, orchestrator, steps, _Row(session_factory, row.id, saga, held=held), nothing, row.saga_state)


async def settle(
    session_factory: Any, copy: ThreadCopy, turn: int, row: Any, held: tuple[UUID, str] | None,
) -> tuple[str, list[str]]:
    """End the landing *row* records, which no worker goes on with; its state, and the files a person has to check.

    Asked through this thread's own landing helper, under an invocation of
    the settling's own, ``land:<turn>:settle:<saga>``: a settle begun again
    asks the same steps in the same order, and the journal answers those
    already done.  What the landing left open on its computer is closed
    first, so none of it runs when its computer is back, and its steps are
    taken from the journal, which holds each its computer was asked though
    its row may have been written short of it (:func:`_as_asked`).  It may
    have recorded wherever the journal holds its record, asked or answered:
    the folder's history is asked whether ``main`` holds its saga, back to
    the ``main`` that record named.  Held, it is ``completed``, its files
    staying, and forgotten.  Where a pruning's cut hides that, it is given
    up, ``escalated``, its files left as they are.  Else every apply it may
    have sent is put back, newest first, and it is forgotten, or left
    ``escalated`` where a file could not be put back.  A thread deleted
    since has no journal: its row alone says.

    *held* is the landing's thread and the lease token its settler holds it
    by: the row is written only while that is its lease.  Raises
    :class:`LandingUnsettled` where it recorded and its row could not say
    so, and ``_Unsettled`` where its computer did not answer.
    """
    steps = copy.steps(f"land:{turn}:settle:{row.saga_id}")
    saga = saga_of(row)
    orchestrator = _orchestrator(None)
    orchestrator.adopt(saga)
    written = _Row(session_factory, row.id, saga, held=held)
    if row.thread_id is not None:
        record = _as_asked(saga, await copy.operations.close_landing(row.thread_id, row.saga_id))
    else:
        record = next((s for s in saga.steps if s.tool_name == "history.record"), None)
    if record is not None:
        looked = await _looked(steps, saga, record)
        if looked["landing"] is not None:
            for it in saga.steps:
                if it.tool_name in ("history.apply", "history.record"):
                    # Recorded, so each ran: the record pushed what every apply wrote.
                    it.state = StepState.COMMITTED
            saga.transition(SagaState.COMPLETED)
            await _written(
                written.write, tries=2, state="completed", commit=looked["landing"], files=_row_files(saga),
                picked_up=_row_picked_up(saga),
            )
            if written.state != "completed":
                raise LandingUnsettled(f"landing {row.saga_id} recorded, and its row could not be written")
            await _forgotten(saga, orchestrator, steps, written, set(), "completed")
            return "completed", []
        if looked["hidden"]:
            at_issue = _given_up(saga, every=True)
            await _written(written.write, tries=2, state="escalated")
            return "escalated", at_issue
    nothing: set[int] = set()
    try:
        failed = await _put_back(saga, steps, written, nothing, every=True)
    except _Unsettled:
        # As far as it got, for whoever settles it next.
        await _written(written.write)
        raise
    state = "escalated" if failed else "compensated"
    await _written(written.write, tries=2, state=state)
    if failed:
        return state, [it.arguments["path"] for it in failed]
    await _forgotten(saga, orchestrator, steps, written, nothing, state)
    return state, []


async def _settled(
    copy: ThreadCopy, session: Any, turn: int, row: Any, *, session_factory: Any, held: tuple[UUID, str] | None,
) -> dict:
    """A landing of this turn's own that the turn cannot go on with, settled as one left running is; its outcome, as its row says.

    Unsettled where its computer did not answer, or where it recorded and
    its row could not say so: the next landing in the folder settles it.
    """
    try:
        await settle(session_factory, copy, turn, row, held)
    except (_Unsettled, LandingUnsettled):
        logger.warning("The landing %s of thread %s is left unsettled, as its row says", row.saga_id, session.id, exc_info=True)
        return {**_of_row(row), "state": "unsettled", "landed": [], "commit": None}
    return _of_row(await landing_row(session_factory, row.saga_id))


#: The steps of a landing's row, by the operation of its computer's each one is.
_STEPS = {
    ("history", "pickup"): "history.pickup", ("history", "commit"): "history.commit",
    ("land", "apply"): "history.apply", ("history", "record"): "history.record",
}


def _as_asked(saga: Saga, asked: list[tuple[str, dict, dict | None]]) -> SagaStep | None:
    """*saga*'s steps as its computer was asked them, the journal's operations *asked*; its record step where the journal holds one.

    A landing's row is written whole at its turning points, and a turn taken
    up again writes its own steps as it asks them again: a row can lack a
    step its computer was asked, and only the journal says that it was.
    Each the journal holds is taken with what the row says of it, and with
    its computer's answer, checked as one given now, where the row has none;
    then those of the row's its computer was never asked.  One asked whose
    answer is lost, closed or a refusal may have run.
    """
    rows = {(s.tool_name, json.dumps(s.arguments, sort_keys=True)): s for s in saga.steps}
    steps: list[SagaStep] = []
    for kind, args, outcome in asked:
        name = _STEPS.get((kind, args.get("action")))
        if name is None:
            continue
        arguments = {key: value for key, value in args.items() if key != "action"}
        it = rows.pop((name, json.dumps(arguments, sort_keys=True)), None) or SagaStep(
            step_id=f"step:{uuid4()}", tool_name=name, tool_call_id="", arguments=arguments, timeout_seconds=STEP_WAIT,
        )
        answer = answered(kind, args["action"], outcome)
        if answer is not None:
            it.execute_result = it.execute_result or answer
            if it.state in (StepState.PENDING, StepState.EXECUTING):
                it.state = StepState.COMMITTED
        elif it.state is StepState.PENDING:
            it.state = StepState.EXECUTING
        steps.append(it)
    unasked = {id(it) for it in rows.values()}
    saga.steps = [*steps, *(it for it in saga.steps if id(it) in unasked)]
    return next((it for it in steps if it.tool_name == "history.record"), None)
