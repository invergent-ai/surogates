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
Nothing on the computer checks that order: it is kept here.  And it applies
only what its thread did: where the thread ran no command, started no
process, has no helper and wrote through no link, the files its own
operations named, as the journal has them (``DeviceOperations.named``), so a
copy that lacks a file of yours, or holds one cut short, changes none of them.

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

A Stop leaves a landing's steps to the landing (the pause route closes none
of them).  One that has written no file yet ends at the Stop: the step it
waits in is closed, nothing of it lands when its computer is back, and its
thread stays stopped.  One that has written a file is not interrupted: it
finishes, or is put back whole, and says it was stopped under way.  A thread
deleted while its landing has written a file has that landing put back whole
by its worker, within a bound; past it, the landing is left for the next one
in its folder.  A step whose computer does not answer within its bound ends
a landing that wrote no file, with nothing of it left for anyone.

A failed turn does not land, since its files may be half made, and nor does
one whose landing a Stop ended.  The copy outlives either on its computer,
and lands with the thread's next turn; meanwhile the turn is kept on the
thread's branch in the folder's history, its base and all, so its work is
in the history and not in the copy alone.  A keep touches nothing of the
folder, asks the app for no hold, and is recorded in no History: nothing of
it landed.

A turn whose landing recorded asks, last, for the folder's history to be
pruned on its computer, which does so at most once a day.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from functools import partial
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select

from surogates.db.models import Session as SessionRow
from surogates.devices.history import (
    MAX_ASIDE,
    MAX_LOOKED,
    ComputerRefused,
    NotAnAnswer,
    Steps,
    ThreadCopy,
    answered,
    code_of,
    thread_copy,
)
from surogates.devices.operations import DeviceOperations, LeaseLost, OperationConflict, as_named
from surogates.devices.workspace import MAX_MESSAGE_CHARS, DeviceOperationError
from surogates.governance.saga import Saga, SagaState, SagaStep, StepState
from surogates.governance.saga.orchestrator import SagaTimeoutError
from surogates.harness.landing import (
    _PUT_BACK_BOUND,
    LandingUnsettled,
    _kept_as,
    _orchestrator,
    _Row,
    _tell,
    _tell_escalated,
    _written,
    last_turn_end,
    redo_files,
)
from surogates.harness.tool_exec import SAGA_EXCLUDED_TOOLS, _turn_now
from surogates.sandbox.history import HISTORY_CAP
from surogates.session.events import EventType
from surogates.workstreams.derive import UNFINISHED, set_aside_said
from surogates.workstreams.history import kept_refs_in, landing_row, running_in, saga_of, start_landing, unforgotten_in

logger = logging.getLogger(__name__)

#: How long a step of a landing waits for its computer, which may be away for days: the journal delivers each step once.
STEP_WAIT = 7 * 86_400
#: How long a landing waits before it asks the land kind again while another chat holds the folder, and how long it
#: asks for in all: a refusal of ``busy`` did nothing.
BUSY_WAIT = 15.0
BUSY_PATIENCE = 1_800.0
#: How long a turn waits for its computer to give the folder back, and to forget its own landing at the landing's end:
#: its app lets the folder go by itself once the landing's helper has been idle, and a landing not forgotten is
#: forgotten by the next landing in its folder.
RELEASE_WAIT = 30.0
#: How often a landing whose thread may be stopped looks whether it was: one that has written no file ends there.
STOP_CHECK = 1.0
#: How long a turn whose landing a Stop ended waits for its computer to give the folder back: its app lets the
#: folder go by itself, later, once the landing's helper has been idle.
STOP_FORGET = 5.0
#: How often, at most, a landing whose thread was stopped reads whether the thread was deleted.
DELETED_READ = 15.0
#: How long the lease a settle takes on a lost landing's thread lasts, and how often it is renewed while the settle runs.
SETTLE_LEASE = 60
SETTLE_RENEW = 20.0
#: How many landings ended in a folder whose forgetting never let go of what they kept a landing asks it again for.
FORGET_AGAIN = 16
#: How long a turn's end waits for its folder's history to be pruned on its computer: a request of the history is cut
#: off at ten minutes there.  The landing stands either way, and the history is pruned on a later day.
PRUNE_WAIT = 660.0
#: A landing put back whole is tried once more, as a new saga: its look and its pickup see what the first ran into.
#: What a turn taken up again did after its landing began lands after it, as a landing of its own (``land:<turn>:3``).
TRIES = 2
#: Why a landing leaves out a file that no landing will ever write: a name that runs code, and a link or a file with a
#: second name.  The thread's version stays in its copy, and is no work a turn's end failed to save.
NEVER_LANDS = ("protected", "linked")
#: Why a landing leaves out a file its thread's copy changed that nothing the thread did names: one the copy lacks,
#: and one it holds otherwise.  Neither is the thread's work: each stays as the folder has it, and is the folder's in
#: the copy again once the landing is recorded.
NOT_ITS_OWN = ("gone", "unnamed")
#: How much one landing carries, in characters of its files' names and of what is said of each: one answer of its
#: computer's, and each request of the server's that names its files, is one frame of the link, which this leaves
#: room in.  A turn that changed more lands none of it, and says so, before anything is committed.
CARRIES = MAX_MESSAGE_CHARS
#: What a commit's answer says of one file beside its name, at its longest: its two versions, why it was left out,
#: and the thread that changed it, by its id and a title of some words.  A record's request says less of each.
_PER_FILE = 256
#: What a look's answer says of one file beside its name: a revision at its longest.
_PER_LOOK = 128
#: The most of your edits in a folder one landing records: as many as two folders the history keeps can differ by,
#: each file of one gone and each of the other new, as a rename of the whole folder is.
YOURS_MOST = 2 * HISTORY_CAP
#: The most answers one landing hears your edits in, the pickup's own among them: an answer but the last is at least
#: a part's worth of names, so your edits in all are at most this many parts of them, however long the names.
PARTS_MOST = 16
#: The most snapshots a turn's end records its computer keeping of a thread's: the latest sixteen in the thread's repository
#: and in each of the four it keeps set aside whole at most (``surogates.sandbox.local_history``), all an open names.
SNAPSHOTS_KEPT = 16 * (1 + 4)
#: Why a landing put back is not tried again at once: the next try would meet the same, or its thread was stopped or
#: deleted, or its computer did not answer a step for as long as a step waits.
_ONCE = frozenset({"stale", "too_large", "yours_too_large", "stopped", "deleted", "unanswered"})
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
    """You changed more files in the folder since its last landing than one landing records, or than its computer's answer carries."""


class _Waited(Exception):
    """A landing left running in the folder is its own worker's still, for longer than a landing waits for the folder."""


class Stopped(Exception):
    """Its thread was stopped before the landing wrote a file: the landing ends there, and nothing of it lands."""


class _Deleted(Exception):
    """Its thread was deleted once the landing had written a file: it is put back whole, within a bound."""


class _Ending:
    """What a landing hears of its thread being stopped or deleted while it runs.

    *stopped* is the harness's interrupt check, or None where nothing can
    stop the landing.  A deletion is read from the thread's own session once
    a stop is heard, at most every :data:`DELETED_READ` seconds, or where the
    journal refused a step: from the first read that finds it, the landing
    has until its ``deadline`` to be put back whole, or finished.  ``wrote``
    says whether a landing of the turn sent an apply.
    """

    def __init__(self, stopped: Callable[[], bool] | None, session_factory: Any, thread_id: UUID) -> None:
        self.hears = stopped is not None
        self._stopped = stopped
        self._session_factory, self._thread_id = session_factory, thread_id
        self.deadline: float | None = None
        self.wrote = False
        self._read_at: float | None = None

    def heard(self) -> bool:
        """Whether its thread was stopped, or deleted."""
        return self._stopped is not None and self._stopped()

    async def deleted(self, *, unheard: bool = False) -> bool:
        """Whether its thread was deleted, as far as the landing has read it; read *unheard* too, before a stop is."""
        now = time.monotonic()
        if self.deadline is None and (unheard or self.heard()) and (self._read_at is None or now >= self._read_at + DELETED_READ):
            self._read_at = now
            if await _archived(self._session_factory, self._thread_id):
                self.deadline = now + _PUT_BACK_BOUND
        return self.deadline is not None

    def left(self) -> float | None:
        """How long the landing has left once its thread was deleted; None while it was not."""
        return None if self.deadline is None else max(self.deadline - time.monotonic(), 0.0)


async def _archived(session_factory: Any, thread_id: UUID) -> bool:
    """Whether the thread *thread_id* was deleted: as best it can, a landing goes on as it would where it cannot tell."""
    try:
        async with session_factory() as db:
            return await db.scalar(select(SessionRow.status).where(SessionRow.id == thread_id)) == "archived"
    except Exception:
        logger.warning("Could not read whether thread %s was deleted", thread_id, exc_info=True)
        return False


async def _asked(ending: _Ending | None, ask: Callable[[], Awaitable[Any]], *, wrote: bool) -> Any:
    """*ask*, a step of a landing, as its thread's Stop or deletion leaves it.

    Before the landing has written a file a Stop ends it there
    (:class:`Stopped`): the step is cancelled where it waits, which closes
    its operation in the journal, so it does not run when its computer is
    back.  Once it has written one (*wrote*), its put-back among it, a Stop
    leaves the step to finish; its thread's deletion, heard or read where
    the journal refused a step, gives the step until the landing's
    deadline, then cancels it too (``TimeoutError``).
    """
    if ending is None or not ending.hears:
        return await ask()
    step = asyncio.ensure_future(ask())
    try:
        while True:
            left = ending.left()
            done, _ = await asyncio.wait({step}, timeout=STOP_CHECK if left is None else min(STOP_CHECK, left))
            if done:
                return step.result()
            if not wrote:
                if ending.heard():
                    raise Stopped("its thread was stopped before its landing wrote a file")
                continue
            if ending.heard():
                await ending.deleted()
            if ending.left() == 0:
                raise TimeoutError("its thread was deleted, and its computer did not answer within the landing's bound")
    finally:
        if not step.done():
            step.cancel()
            await asyncio.gather(step, return_exceptions=True)


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
    ended = await last_turn_end(store, session.id)
    if ended is not None and not (ended.type == EventType.SESSION_COMPLETE.value and ended.data.get("saved", True)):
        return True
    for report in (EventType.WORKER_COMPLETE, EventType.WORKER_FAILED):
        if await store.has_event(session.id, report, after=turn):
            return True
    return False


async def land_local_turn(
    *, store: Any, session_factory: Any, redis: Any, session: Any, lease_token: str | None,
    saga_settings: Any, tool_saga_id: str | None, stopped: Callable[[], bool] | None = None,
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
    landing than one landing records, more than the folder's history keeps
    or than its computer's answer to a part of the pickup carries
    (``yours_too_large``), its row could not be written
    (``unwritten``), another chat held the folder (``busy``), its computer
    did not answer (``unanswered``), or refused it (``refused``, with its
    word, ``code``), or a landing left in its folder had recorded, and its
    row could not say so (``settling``), or its thread was stopped before
    it wrote a file (``stopped``), or deleted before it finished
    (``deleted``).  ``stopped`` is true of a landing whose thread was stopped
    once it had written a file: it was not interrupted; and ``stop_heard``
    of any whose thread was stopped while it landed.
    ``recovery`` is what the folder's helper found there, left by a landing
    cut short, where it found any.

    The folder is held first, and every landing left running in it is
    settled (:func:`settle_folder`); then it is landed in.  Where the turn's
    last landing did not let the folder go by a forgetting of its own, the
    turn gives it back by its hold's own name, which drops nothing a landing
    kept.  Taken up again after its worker was lost, a turn whose landing
    began goes on with it from the journal; and lands what it did since, as
    a landing of its own.

    *stopped* is the harness's interrupt check.  A Stop ends the turn's
    landing while it has written no file, its holding of the folder and
    its settle of another's landing among it: what it waits in is closed,
    and the folder given back where its computer answers within
    :data:`STOP_FORGET`; then the turn is kept on its branch, as a failed
    turn is, within as long, its files each not merged.  From its first
    apply on, the landing finishes or is put back whole, as it would with
    no Stop, and is not tried again; but where the thread was deleted,
    heard so or read so where the journal refused a step, it is put back
    whole within ``landing._PUT_BACK_BOUND``, or left for the next landing
    in its folder.

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
    ending = _Ending(stopped, session_factory, session.id)
    hold = copy.steps(f"land:{turn}:hold")
    try:
        recovery = await _asked(ending, partial(_held, copy, hold), wrote=False)
        # Before anything of this turn's is looked at there.
        await _asked(ending, partial(settle_folder, store, session_factory, copy, session, turn, hold, held, redis=redis), wrote=False)
    except LeaseLost:
        raise
    except Exception as refused:
        if isinstance(refused, Stopped):
            logger.info("Thread %s was stopped before its landing began", session.id)
        else:
            logger.warning("The folder of thread %s was not held and settled for its landing, so its turn lands nothing", session.id, exc_info=True)
        outcome = _not_begun(refused)
        if outcome["reason"] in ("stopped", "refused") and await ending.deleted(unheard=True):
            # Its thread was deleted: the delete closes a landing's first steps, which it has no row to spare by.
            outcome.pop("code", None)
            outcome["reason"] = "deleted"
        if outcome["reason"] in ("refused", "settling", "stopped"):
            await _release(copy, session, turn, wait=STOP_FORGET if outcome["reason"] == "stopped" else None)
        if outcome["reason"] == "stopped":
            await _kept_at_a_stop(copy, session, turn, outcome)
        return _heard(outcome, ending)
    land = partial(
        _land, copy, session, turn, session_factory=session_factory, saga_settings=saga_settings, tool_saga_id=tool_saga_id,
        held=held, ending=ending,
    )
    outcome: dict[str, Any] = {}
    for attempt in range(1, TRIES + 1):
        outcome = await land(attempt, calls=calls)
        # Not where a try at once would meet the same, nor after a cancel: nothing asks for that landing any more.  Nor
        # once a Stop is heard: the landing that was under way at it is told as it ended.
        if outcome["state"] != "compensated" or outcome.get("reason") in _ONCE or outcome.get("code") == "cancelled" or ending.heard():
            break
    since = [call for call in calls if call.id > outcome["through"]]
    if outcome["state"] == "completed" and since and not ending.heard():
        # Taken up again, the turn worked on after its landing began, which
        # had recorded: what it did since lands now, as a landing of its own.
        outcome = _both(outcome, await land(TRIES + 1, calls=since))
    if not outcome.pop("forgot") and outcome["state"] != "unsettled" and outcome.get("reason") != "unanswered":
        await _release(copy, session, turn, wait=STOP_FORGET if outcome.get("reason") == "stopped" else None)
    if outcome.get("reason") == "stopped":
        await _kept_at_a_stop(copy, session, turn, outcome)
    if recovery:
        outcome["recovery"] = recovery
    _tell(outcome, redoing)
    if outcome["state"] == "completed":
        # A file no landing writes is not work this turn's end failed to save, nor one nothing the thread did changed.
        left = {o["path"] for o in outcome["overlapped"] if o["reason"] not in (*NEVER_LANDS, *NOT_ITS_OWN)}
        outcome["saved"] = left <= {redo["path"] for redo in outcome["redo"]}
    if ending.heard() and ending.wrote and outcome.get("reason") not in ("stopped", "deleted") and not await ending.deleted():
        # Stopped once its landing had written a file: it was not interrupted, and whoever stopped it is told so.
        outcome["stopped"] = True
    # The turn it was: a pruning after it is asked under its name, once the turn has ended (prune_local_history).
    outcome["turn"] = turn
    return _heard(outcome, ending)


def _heard(outcome: dict, ending: _Ending) -> dict:
    """*outcome*, saying whether its thread was stopped while it landed: the thread stays as the Stop left it."""
    if ending.heard():
        outcome["stop_heard"] = True
    return outcome


async def prune_local_history(*, session_factory: Any, redis: Any, session: Any, lease_token: str | None, landing: dict) -> None:
    """Prune the folder's history on *session*'s computer after *landing*, its turn's, where it recorded: its computer prunes at most once a day.

    Asked once the turn has ended and reported, so neither waits for a
    pruning, nor for a computer that went away before one; under the
    turn's own name, ``land:<turn>:prune``.  The day is the server's: the
    pruning is told this worker's time.  Told the refs to keep, the branch
    and the base of every thread that works in the folder, whichever
    project's; what each repository on that computer reads is the
    computer's to keep besides, which alone can read it.  As best it can,
    and for :data:`PRUNE_WAIT` at most: the landing stands, and the
    history is pruned on a later day.
    """
    if landing.get("state") != "completed" or not landing.get("commit") or landing.get("turn") is None:
        return
    copy = thread_copy(session, session_factory=session_factory, redis=redis, lease_token=lease_token)
    try:
        keep = await kept_refs_in(session_factory, copy.device_id, copy.folder)
        pruning = copy.steps(f"land:{landing['turn']}:prune").history("prune", keep=keep, now=int(time.time()))
        await asyncio.wait_for(pruning, PRUNE_WAIT)
    except Exception:
        logger.warning("The history of the folder of thread %s on its computer was not pruned", session.id, exc_info=True)


async def keep_local_turn(*, store: Any, session_factory: Any, redis: Any, session: Any, lease_token: str | None) -> list[str]:
    """Keep *session*'s failed turn on its branch in its folder's history on its user's computer: the files kept.

    Nothing where the copy holds nothing to keep: no turn since its last
    landing could have changed it, or nothing differs from its base.  A
    failed turn does not land, since its files may be half made; its copy
    outlives it on its computer, and lands with the thread's next turn,
    whatever that turn does (:func:`lands`).  Kept meanwhile, its work is in
    the folder's history and not in the copy alone.  Its end waits for its
    computer :data:`RELEASE_WAIT` at most: a keep not answered by then is
    closed, and what it would have kept lands with the next turn all the same.

    Raises :class:`LeaseLost` where another worker took the thread's turn:
    this one stands down, and has asked nothing more.
    """
    turn = await _turn_now(store, session)
    calls = await store.get_events(session.id, after=turn, types=[EventType.TOOL_CALL])
    if not await lands(store, session, turn, calls):
        return []
    copy = thread_copy(session, session_factory=session_factory, redis=redis, lease_token=lease_token)
    return await _kept(copy, session, turn, wait=RELEASE_WAIT)


async def set_aside_of(
    *, store: Any, session_factory: Any, redis: Any, session: Any, landing: dict | None,
) -> tuple[dict | None, dict | None]:
    """What *session*'s computer keeps set aside of the thread's work, as its turn's end records it, and what of that no earlier end told.

    Its computer names it at every open of the thread's copy: the copies and
    repositories of the thread's it kept whole where it made them again, and
    those of them that went at its bound, each by its folder's name there;
    and, where the open found a copy, the snapshots of what the copy held of
    the thread's own when it was made other files, in the thread's
    repository and in each it keeps set aside whole: one that went, at the
    thread's bound or with the repository that held it, is named no more,
    and counted no more.  A landing's record names its own snapshot at once
    (*landing*'s ``set_aside``); one whose answer was lost, or a turn's end
    taken up again, has the next open name it.  A place of the folder's
    its app set aside, its history and every thread's copy in it, is named
    by one open alone, the thread's that set it aside, and stays recorded:
    the latest as many as an open names.

    A turn's end records all of it by the computer's names,
    ``{copies, snapshots, gone}`` and ``places`` where it has any, and its
    report tells what no earlier end recorded, as a person is told it
    (``derive.set_aside_said``): so each is told once, and the master hears
    what each is and when, not its names.
    The open is read from the journal and checked as an answer given now:
    one that is none says nothing, and the last word stands.  None for
    either where it holds nothing: nothing was ever set aside, or nothing
    is new.
    """
    before = await store.last_event(session.id, EventType.SESSION_COMPLETE, EventType.SESSION_FAIL, with_key="set_aside")
    told = _set_aside_told(before.data["set_aside"] if before is not None else None)
    opened = answered(
        "history", "open", await DeviceOperations(session_factory, redis).last_opened(session.id),
        thread=str(session.id), folder=session.config.get("workspace_path"),
    )
    now = dict(told)
    if opened is not None:
        now.update(copies=opened.get("set_aside_folders", []), gone=opened.get("set_aside_gone", []))
        if "copy" in opened:
            # An open that found the folder's history off says nothing of the snapshots in the thread's repositories.
            now["snapshots"] = opened.get("set_asides", [])
        # Named by one open alone: what it names is added to what was told before.
        places = [name for name in dict.fromkeys(opened.get("set_aside_places", [])) if name not in told["places"]]
        now["places"] = [*told["places"], *places][-MAX_ASIDE:]
    recorded = [aside for aside in (landing or {}).get("set_aside", []) if aside not in now["snapshots"]]
    # A word of more than its computer keeps is kept to the latest it may.
    now["snapshots"] = [*now["snapshots"], *recorded][-SNAPSHOTS_KEPT:]
    new = {key: [name for name in now[key] if name not in told[key]] for key in ("copies", "snapshots", "gone", "places")}
    kept = {key: names for key, names in now.items() if key != "places" or names} if before is not None or any(now.values()) else None
    return kept, set_aside_said(new)


def _set_aside_told(told: Any) -> dict[str, list[str]]:
    """What a turn's end recorded its computer keeping set aside, as :func:`set_aside_of` gave it; nothing, where it recorded none."""
    told = told if isinstance(told, dict) else {}
    return {
        key: [name for name in told.get(key) or () if isinstance(name, str)] if isinstance(told.get(key), list) else []
        for key in ("copies", "snapshots", "gone", "places")
    }


async def _kept(copy: ThreadCopy, session: Any, turn: int, *, wait: float) -> list[str]:
    """The turn *turn*'s copy committed on its thread's branch, its base and all, within *wait*: the files kept.

    Asked under ``land:<turn>:keep``, a turn's end taken up again answered
    from the journal: the files the copy changed since its base, which a
    turn's report names, then the keep.  A copy that changed none keeps
    nothing, and its branch stays where it is.
    """
    steps = copy.steps(f"land:{turn}:keep")

    async def keeping() -> list[str]:
        paths = (await steps.history("changed"))["paths"]
        if paths:
            await steps.history("keep", **_kept_as(session), base=True)
        return paths

    return await asyncio.wait_for(keeping(), wait)


async def _kept_at_a_stop(copy: ThreadCopy, session: Any, turn: int, outcome: dict) -> None:
    """Keep a turn whose landing a Stop ended on its branch, as a failed turn is kept; its *outcome* names each file kept, not merged.

    Within :data:`STOP_FORGET`, as its folder is given back: as best it
    can, and what is not kept lands with the thread's next turn all the
    same.  A landing that ended before it named the turn's files names
    them so.
    """
    try:
        kept = await _kept(copy, session, turn, wait=STOP_FORGET)
    except LeaseLost:
        raise
    except TimeoutError:
        logger.warning("The turn of thread %s was not kept on its branch: its computer did not answer in time", session.id)
        return
    except Exception:
        logger.warning("The turn of thread %s, whose landing a Stop ended, was not kept on its branch", session.id, exc_info=True)
        return
    named = {f["ref"] for f in outcome["files"]}
    more = [{"kind": "file", "label": path, "ref": path, "landing": "not_merged"} for path in kept if path not in named]
    outcome["files"] = sorted([*outcome["files"], *more], key=lambda f: f["ref"])


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
        title = UNFINISHED
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


async def _release(copy: ThreadCopy, session: Any, turn: int, *, wait: float | None = None) -> None:
    """Give the folder back for a turn whose landing let it go by no forgetting of its own.

    Its landing ended before, as one taken up again finds it, was left
    escalated, or its forgetting was refused; or a Stop ended it.  By the
    turn's hold's own name, under an invocation of its own, so a turn's end
    taken up again asks it anew.  For :data:`RELEASE_WAIT` at most, or *wait*.
    """
    await _given_back(copy.steps(f"land:{turn}:release:{uuid4()}"), session, turn, wait=wait)


async def _given_back(steps: Steps, session: Any, turn: int, *, wait: float | None = None) -> None:
    """Forget the turn's hold of its folder, under *steps*: as best it can, for a while at most.

    Its app lets the folder go by itself once the landing's helper has been idle.
    """
    try:
        await asyncio.wait_for(steps.land("forget", saga=_hold_of(session, turn), applied=[]), RELEASE_WAIT if wait is None else wait)
    except LeaseLost:
        raise
    except Exception:
        logger.warning("The folder of thread %s was not given back: its app lets it go once idle", session.id, exc_info=True)


async def _within(asked: Any, wait: float | None = None) -> Any:
    """One ask of a landing's computer, waited for as a step is: :data:`STEP_WAIT`, or *wait*."""
    return await asyncio.wait_for(asked, STEP_WAIT if wait is None else wait)


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
    if isinstance(refused, Stopped):
        why = {"reason": "stopped"}
    elif isinstance(refused, LandingUnsettled):
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
    the files no landing writes that its record left in the copy, the files
    its record landed, and of the commit's changes those its record did
    neither with, which nothing the thread did named.  So a turn's end
    taken up again after its first run was lost before telling tells the
    same, the files to redo among it.
    """
    saga = saga_of(row)
    commit = next((s.execute_result for s in saga.steps if s.tool_name == "history.commit" and s.execute_result), None) or {}
    record = next((s for s in saga.steps if s.tool_name == "history.record"), None)
    left = set(record.arguments.get("left", [])) if record is not None else set()
    applied = {c["path"] for c in record.arguments["applied"]} if record is not None else None
    changes = commit.get("changes", [])
    held = {o["path"] for o in commit.get("overlapped", [])}
    overlapped = [
        *commit.get("overlapped", []), *({**c, "reason": "linked"} for c in changes if c["path"] in left and c["path"] not in held),
        *(_not_its_own(c) for c in changes if applied is not None and c["path"] not in applied | left),
    ]
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
    landing writes that its look found a link or a file with a second name,
    which its record left in the copy.  A file the copy changed that nothing
    the thread did named is none of the thread's files.
    """
    commit = next(s.execute_result for s in saga.steps if s.tool_name == "history.commit")
    record = next(s for s in saga.steps if s.tool_name == "history.record")
    landed, left = record.arguments["applied"], set(record.arguments.get("left", []))
    paths = {c["path"] for c in landed}
    return [
        *({"path": c["path"], "before": c["before"], "after": c["after"], "merged": True} for c in landed),
        *({"path": o["path"], "before": o["before"], "after": o["after"], "merged": False} for o in commit["overlapped"]),
        *(
            {"path": c["path"], "before": c["before"], "after": c["after"], "merged": False}
            for c in commit["changes"] if c["path"] not in paths and c["path"] in left
        ),
    ]


def _not_its_own(change: dict) -> dict:
    """*change*, a file the copy changed that nothing its thread did named, left out: one the copy lacks, or holds otherwise."""
    return {**change, "reason": "gone" if change["after"] is None else "unnamed"}


async def _land(
    copy: ThreadCopy, session: Any, turn: int, attempt: int, *,
    session_factory: Any, saga_settings: Any, tool_saga_id: str | None, calls: list, held: tuple[UUID, str] | None,
    ending: _Ending,
) -> dict:
    """One try of a turn's landing, as one saga, of the turn's tool calls *calls*; its outcome.

    With ``forgot``, whether its own forgetting let the folder go, and
    ``through``, the last tool call it was begun for.  Taken up again, it
    goes on as its first run began it, its audit among it, so each step it
    asks again is answered from the journal; but where the turn took
    another path than that run, or worked on since that run began it, or a
    settle began on it, it is settled as a landing left running is, from
    what its computer was asked, and the turn lands again as a new saga.

    *ending* says whether its thread was stopped or deleted: a Stop ends
    it until its first apply is sent, and a deletion from there on puts it
    back whole, all within the deletion's bound.  A step its computer does
    not answer within :data:`STEP_WAIT` ends it where it had sent no apply:
    nothing of it is in the folder, and nothing is asked of a computer that
    did not answer.
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
        # Taken up again: under way already where its first run sent an apply.
        ending.wrote = ending.wrote or _sent(saga_of(found))
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

    async def execute(it: SagaStep, ask: Any, *, wrote: bool = False) -> dict:
        async def once() -> dict:
            await _written(row.alive)
            return await _asked(ending, ask, wrote=wrote)

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
        paths = (await _within(_asked(ending, partial(steps.history, "changed"), wrote=False)))["paths"]
        # Its files until its commit says otherwise: a landing that ends before that names these, the turn's own, and
        # none of yours that its turn's open brought into the copy.
        changes = [{"path": path} for path in paths]
        if paths and not carries(paths):
            raise _TooLarge(f"the turn changed {len(paths)} files")
        revisions: dict[str, str] = {}
        for look in looks(paths):
            looked = await _within(_asked(ending, partial(_land_asked, steps, "revisions", paths=look), wrote=False))
            revisions.update(dict(looked["revisions"]))
        turned: dict[str, Any] = {"commit": None, "changes": [], "overlapped": [], "excluded": [], "repositories": [], "not_taken": []}
        if paths:
            pickup = step("pickup", author=you, trailers=[*audit, ["Surogate-Kind", "pickup"]])
            picked = await _your_edits(execute, step, pickup, steps)
            outcome["packs"] = picked["packs"]
            commit = step("commit", author=thread, trailers=[*audit, ["Surogate-Kind", "turn"]], pickup=picked["commit"])
            # The one answer the saga keeps: each apply's step is its file's place among its changes.
            turned = await execute(commit, partial(steps.history, "commit", **commit.arguments))
        changes = turned["changes"]
        # A file already as the turn left it, or gone on both sides, needs no write.
        written = [(number, c) for number, c in enumerate(changes) if c["before"] != c["after"]]
        if any(c["path"] not in revisions for _, c in written):
            raise _Stale("the turn names a file the landing's look did not see")
        # Where the thread ran no command, started no process, has no
        # helper and wrote through no link, only its own file operations
        # changed its copy, and the journal names each file they wrote or
        # deleted.  A file the copy changed that none of them names was
        # changed by nothing the thread did, whatever its computer says of
        # the copy: one the copy lacks, or holds cut short.  It stays as the
        # folder has it, and the landing's record gives the copy the folder's
        # file again, a link's place too: none of it is the thread's to
        # leave in its copy.
        named = await copy.operations.named(session.id, copy.folder, besides=invocation) if written else None
        unnamed = set() if named is None else {c["path"] for _, c in written if as_named(c["path"]) not in named}
        if unnamed:
            logger.warning("The copy of thread %s changed %d files that nothing the thread did names", session.id, len(unnamed))
        # A link, or a file with a second name, is never replaced: the thread's version stays in its copy.
        linked = {c["path"] for _, c in written if c["path"] not in unnamed and revisions[c["path"]] == "other"}
        held = linked | unnamed
        overlapped = [
            *turned["overlapped"], *({**c, "reason": "linked"} for c in changes if c["path"] in linked),
            *(_not_its_own(c) for c in changes if c["path"] in unnamed),
        ]
        outcome.update(
            overlapped=overlapped, excluded=turned["excluded"], repositories=turned["repositories"], not_taken=turned["not_taken"],
        )
        if turned["commit"] is None:
            # Nothing changed, nothing lands: no change to the folder's files to record, and the folder is let go.
            await _written(row.drop)
            outcome["forgot"] = await _forgotten(saga, orchestrator, steps, None, nothing, "completed", wait=RELEASE_WAIT)
            return outcome
        applies = [
            step("apply", saga=saga_id, step=number, **c, expected=revisions[c["path"]])
            for number, c in written if c["path"] not in held
        ]
        # The steps, fixed: all a put-back by whoever settles this landing needs.
        await fixed()
        if ending.heard():
            # The last moment a Stop ends it: from its first apply on, a landing finishes or is put back whole.
            raise Stopped("its thread was stopped before its landing wrote a file")
        ending.wrote = True
        for number, it in enumerate(applies):
            if number and await ending.deleted():
                raise _Deleted("its thread was deleted while its landing was under way")
            await execute(it, partial(_applied, steps, **it.arguments), wrote=True)
        if await ending.deleted():
            raise _Deleted("its thread was deleted while its landing was under way")
        landed = [c for c in changes if c["path"] not in held]
        record = step(
            "record", turn=turned["commit"], applied=landed, author=thread, main=picked["main"], pickup=picked["commit"],
            # What it left out of the thread's: never a file nothing the thread did named, which is none of its.
            trailers=[
                *audit, ["Surogate-Kind", "landing"],
                *(["Surogate-Not-Merged", o["path"]] for o in overlapped if o["reason"] not in NOT_ITS_OWN),
            ],
            # What no landing writes stays in the copy as the thread left it: the commit's own changes, and none else.
            left=sorted(o["path"] for o in overlapped if o["reason"] in NEVER_LANDS),
        )
        # Before it is sent: a landing whose row has no record step never pushed.
        await fixed()
        recorded = await execute(record, partial(steps.history, "record", **record.arguments), wrote=True)
        saga.transition(SagaState.COMPLETED)
        outcome.update(
            commit=recorded["commit"], landed=landed, picked_up=picked["picked_up"],
            # What its record set aside of the copy, where it set any aside: the thread's next open names it too.
            set_aside=[recorded["set_aside"]] if recorded["set_aside"] else [],
        )
        await row.write(state="completed", commit=recorded["commit"], files=_row_files(saga), picked_up=_row_picked_up(saga))
    except LeaseLost:
        # Another worker runs the thread now, and goes on with this landing from the journal.
        raise
    except OperationConflict:
        # Taken up again, the turn asked a step otherwise than the run it takes up.
        logger.warning("The landing %s of thread %s was taken up again on another path: it is settled, and the turn lands again", saga_id, session.id)
        return await settled(await landing_row(session_factory, saga_id))
    except Stopped:
        # Nothing of it was written, so there is nothing to put back: what it waited in is closed, and its turn gives
        # the folder back.  Its files are its turn's, none of them landed.
        logger.info("Thread %s was stopped before its landing wrote a file: the landing ends", session.id)
        outcome.update(state="compensated", reason="deleted" if await ending.deleted() else "stopped", saved=False)
        await _written(row.write, tries=2, state="compensated")
        outcome["files"] = _told(changes, [], outcome["overlapped"])
        return outcome
    except _TooLarge as large:
        logger.warning("The landing of thread %s on its computer carries no turn so large: %s", session.id, large)
        outcome.update(state="compensated", reason="too_large", saved=False)
        await _written(row.write, tries=2, state="compensated")
    except _YoursTooLarge:
        logger.warning("The landing of thread %s on its computer cannot record your edits there in one go", session.id, exc_info=True)
        # Nothing of it was done: no ref moved, no file was written, and no row is left of it.
        outcome.update(state="compensated", reason="yours_too_large", saved=False)
        outcome["files"] = _told(changes, [], [])
        await _written(row.drop)
        outcome["forgot"] = await _forgotten(saga, orchestrator, steps, None, nothing, "compensated", wait=RELEASE_WAIT)
        return outcome
    except Exception as failed:
        logger.warning("The landing of thread %s on its computer did not finish", session.id, exc_info=True)
        outcome.update(saved=False, **_why(failed))
        if ending.deadline is None and _journals(failed):
            # The journal refuses a deleted thread's next step from the moment its thread is deleted, which can be
            # before its worker hears of it: read as such, it is put back whole as one heard deleted is.
            await ending.deleted(unheard=True)
        _said_deleted(outcome, ending)
        if isinstance(failed, (TimeoutError, SagaTimeoutError)) and not _sent(saga):
            # Its computer did not answer a step for as long as a step waits, before the landing sent any apply:
            # nothing of it is in the folder, and what it waited in is closed.  Nothing more is asked of that computer.
            outcome.update(state="compensated", reason="unanswered")
            await _written(row.write, tries=2, state="compensated")
            outcome["files"] = _told(changes, [], outcome["overlapped"])
            return outcome
        try:
            if _away(failed) and ending.deadline is None:
                raise _Unsettled("its computer did not answer, or the journal took no more of it") from failed
            # Each step of it within what its thread's deletion leaves it, where it was deleted, or is heard so now.
            state, pushed = await _ended(saga, orchestrator, steps, row, nothing, ending=ending)
        except _Unsettled as unsettled:
            logger.warning("The landing %s of thread %s is left unsettled, as its row says", saga_id, session.id)
            if not _journals(unsettled.__cause__):
                # Its computer did not answer: the row says how far it got.  What the journal refused is another's to write.
                await _written(row.write)
            return {**_said_deleted(outcome, ending), "state": "unsettled", "commit": None, "landed": []}
        outcome["state"] = state
        if pushed is not None:
            # The push happened though its answer was lost: the landing counts, and nothing went back.
            outcome.pop("reason", None)
            outcome.pop("code", None)
            landed = next(s for s in saga.steps if s.tool_name == "history.record").arguments["applied"]
            outcome.update(commit=pushed, landed=landed, picked_up=_row_picked_up(saga))
    if outcome["state"] != "escalated":
        outcome["forgot"] = await _forgotten(saga, orchestrator, steps, row, nothing, outcome["state"], wait=RELEASE_WAIT, ending=ending)
    outcome["files"] = _told(changes, outcome["landed"], outcome["overlapped"])
    return outcome


def _said_deleted(outcome: dict, ending: _Ending) -> dict:
    """*outcome*, a landing's that did not finish, saying so where its thread was deleted: whatever else ended it."""
    if ending.deadline is not None:
        outcome.pop("code", None)
        outcome["reason"] = "deleted"
    return outcome


def _sent(saga: Saga) -> bool:
    """Whether the landing *saga* sent an apply: from then on, a file of it may be in the folder."""
    return any(s.tool_name == "history.apply" and s.state is not StepState.PENDING for s in saga.steps)


async def _your_edits(execute: Any, step: Callable[..., SagaStep], pickup: SagaStep, steps: Steps) -> dict:
    """Your edits in the folder since its last landing, committed on ``main`` for the landing's record to push: the pickup's answer, whole.

    They are any number of files, and one answer of its computer's is one
    frame of the link: the pickup's own answer names the first part of
    them, and how many are left, and each part after it is a step of the
    landing's own (``history.picked``), asked from the file the last one
    ended at.  So its row records each part, a turn taken up again hears
    each from the journal, and none moves a ref: on a computer a pickup
    moves none, and the record pushes it.  Each is checked as it comes: a
    part that does not go on from the one before it is no answer, and the
    landing goes no further.

    More of them than two of the folder's histories can differ by are
    more than one landing records: no part is asked, and the landing goes
    no further (:class:`_YoursTooLarge`); nor where they take more than
    :data:`PARTS_MOST` answers in all, before the next would be asked; nor
    where its computer answered the pickup or a part ``too_large``, which
    it does once it ran.  Your edits stay as they are in the folder, and
    are asked for again by the next landing there.
    """
    try:
        picked = await execute(pickup, partial(_picked_up, steps, **pickup.arguments))
        yours, left, answers = list(picked["picked_up"]), picked["more"], 1
        while left:
            if answers == PARTS_MOST:
                raise _YoursTooLarge(f"your edits in the folder take more than {PARTS_MOST} answers of its computer's")
            answers += 1
            part = step("picked", pickup=picked["commit"], after=yours[-1]["path"])
            named = await execute(part, partial(_part, steps, left, **part.arguments))
            yours += named["picked_up"]
            left = named["more"]
    except ComputerRefused as refused:
        if refused.kind == "too_large":
            raise _YoursTooLarge("your edits in the folder do not fit one answer of its computer's") from refused
        raise
    return {**picked, "picked_up": yours}


async def _picked_up(steps: Steps, **asked: Any) -> dict:
    """The pickup: your edits committed on ``main``, the first part of their files, and how many are left after it.

    Refused where they are more than one landing records.  Its files are
    named each once, in the order a pickup names them; and where any are
    left after them, by a pickup that is a commit, and in a part that
    names one at least.
    """
    picked = await steps.history("pickup", **asked)
    told = len(picked["picked_up"]) + picked["more"]
    if told > YOURS_MOST:
        raise _YoursTooLarge(f"you changed {told} files in the folder since its last landing")
    if not _in_order(picked["picked_up"], None) or (picked["more"] and (picked["commit"] is None or not picked["picked_up"])):
        raise NotAnAnswer("This computer's answer to 'pickup' names your edits otherwise than a pickup does")
    return picked


async def _part(steps: Steps, left: int, **asked: Any) -> dict:
    """A part of the pickup's files after its first, *left* of them still to name: the files it names, and how many are left after it.

    It goes on from the part before it: it names one file at least, each
    after the file that part ended at, once and in order, and as many are
    left after it as were before it, less those it names.
    """
    named = await steps.history("picked", **asked)
    if not named["picked_up"] or len(named["picked_up"]) + named["more"] != left or not _in_order(named["picked_up"], asked["after"]):
        raise NotAnAnswer("This computer's answer to 'picked' does not go on from the part before it")
    return named


def _in_order(picked: list[dict], after: str | None) -> bool:
    """Whether *picked* names each file once, in the order a pickup names them, by their paths, and each after the file *after*."""
    last = after
    for entry in picked:
        if last is not None and entry["path"] <= last:
            return False
        last = entry["path"]
    return True


def _row_picked_up(saga: Saga) -> list[dict]:
    """A completed landing row's pickup: your edits it recorded on ``main``, each ``{path, before, after}``.

    From the pickup's answer and each part after it, in the order its
    computer named them: the row holds each as its step.
    """
    return [
        entry for s in saga.steps if s.tool_name in ("history.pickup", "history.picked") for entry in (s.execute_result or {}).get("picked_up", [])
    ]


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


async def _ended(
    saga: Saga, orchestrator: Any, steps: Steps, row: _Row, nothing: set[int], *, ending: _Ending | None = None,
) -> tuple[str, str | None]:
    """End a landing that did not finish: ``completed`` with its commit where ``main`` holds it, else put back.

    As the cloud's settle (``landing._settle``): before anything goes back,
    a landing with a record step, asked or not, has the folder's history
    asked whether its saga is in ``main`` back to where it began.  One that
    is pushed is never put back.  Where a pruning's cut hides the answer it
    is given up, ``escalated``, its files left as they are.  A look its
    computer does not answer leaves the landing unsettled.  Each step is
    asked as one of a landing that has written (*ending*): a Stop leaves it,
    and its thread's deletion bounds it.
    """
    # Where it stopped, before anything goes back.
    await _written(row.write)
    record = next((s for s in saga.steps if s.tool_name == "history.record"), None)
    if record is not None:
        looked = await _looked(steps, saga, record, ending=ending)
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
    failed = await _put_back(saga, steps, row, nothing, ending=ending)
    state = "escalated" if failed else "compensated"
    await _written(row.write, tries=2, state=state)
    return state, None


async def _looked(
    steps: Steps, saga: Saga, record: SagaStep, wait: float | None = None, *, ending: _Ending | None = None,
) -> dict:
    """Whether ``main`` in the folder's history holds the landing *saga*, back to the ``main`` its *record* names.

    A look its computer does not answer, within a step's wait or *wait*, or the bound a deletion of the landing's
    thread sets (*ending*), leaves the landing unsettled.
    """
    look = partial(steps.history, "fetch", saga=saga.saga_id, since=record.arguments["main"])
    try:
        return await _within(_asked(ending, look, wrote=True), wait)
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


async def _put_back(
    saga: Saga, steps: Steps, row: _Row, nothing: set[int], *, every: bool = False, wait: float | None = None,
    ending: _Ending | None = None,
) -> list[SagaStep]:
    """Put back what a landing applied, newest first; the steps that could not be put back.

    The app's helper wrote down what each apply was about to do before it
    did it, so a put-back asks for a step by its number alone, and is safe
    for one that never ran, or was cut off: an apply that failed, whose
    answer may have been lost, is put back too.  A file changed since the
    landing wrote it is left as it is, and what the landing kept of it stays
    kept: the saga escalates.  A put-back its computer did not finish, each
    waited for a step's wait or *wait*, or past the bound a deletion of its
    thread sets (*ending*), ends the put-back there: the landing is
    unsettled, and nothing more is asked.  Each apply asked ends
    ``compensated``, or ``compensation_failed`` where it could not be put
    back: the row says which file was not.

    With *every*, a settle's: every apply the landing holds is asked, in
    the same order however often the settle begins again, whatever state
    its row gives it, since the row may show one pending that ran.
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
            unapply = partial(_land_asked, steps, "unapply", saga=saga.saga_id, step=it.arguments["step"], path=it.arguments["path"])
            did = await _within(_asked(ending, unapply, wrote=True), wait)
            if did["path"] != it.arguments["path"]:
                raise NotAnAnswer("This computer's answer to 'unapply' names another file than the one it was asked to put back")
        except LeaseLost:
            raise
        except Exception as exc:
            logger.warning("Could not put back %s", it.arguments["path"], exc_info=True)
            if committed:
                it.error = f"Compensation failed: {exc}"
                it.transition(StepState.COMPENSATION_FAILED)
            else:
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
        else:
            it.state, it.compensation_result = StepState.COMPENSATED, did
    if saga.state is SagaState.COMPENSATING:
        saga.transition(SagaState.ESCALATED if failed else SagaState.COMPLETED)
    return failed


async def _forgotten(
    saga: Saga, orchestrator: Any, steps: Steps, row: _Row | None, nothing: set[int], state: str, *, wait: float | None = None,
    ending: _Ending | None = None,
) -> bool:
    """Forget what a landing recorded, or put back whole, kept of the files it replaced, which lets the folder go; whether it did.

    A step of its saga, in its row, so that one not forgotten is asked
    again by the next landing in the folder.  ``applied`` is every apply
    that was sent, but those the app's helper said it holds nothing of: the
    folder's history lets the landing go only where ``main`` holds it, or
    where each of them is its ``before`` again, and the app's helper forgets
    only where the history says it may.  Never asked of a landing left
    escalated: what it kept stays kept until a person settles it.  Waited
    for as a step is, or for *wait*, and no longer than a deletion of its
    thread leaves it (*ending*).
    """
    applied = [
        {"path": s.arguments["path"], "before": s.arguments["before"], "after": s.arguments["after"], "step": s.arguments["step"]}
        for s in saga.steps
        if s.tool_name == "history.apply" and s.state is not StepState.PENDING and s.arguments["step"] not in nothing
    ]
    it = orchestrator.add_step(
        saga.saga_id, tool_name="history.forget", tool_call_id="", arguments={"saga": saga.saga_id, "applied": applied},
        timeout_seconds=STEP_WAIT if wait is None else wait, max_retries=0,
    )
    try:
        await orchestrator.execute_step(
            saga.saga_id, it.step_id, partial(_asked, ending, partial(_land_asked, steps, "forget", **it.arguments), wrote=True),
        )
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
    session_factory: Any, copy: ThreadCopy, turn: int, row: Any, held: tuple[UUID, str] | None, *, wait: float | None = None,
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
    by: the row is written only while that is its lease.  Each step waits
    for its computer as a landing's does, or for *wait*.  Raises
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
        looked = await _looked(steps, saga, record, wait)
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
            await _forgotten(saga, orchestrator, steps, written, set(), "completed", wait=wait)
            return "completed", []
        if looked["hidden"]:
            at_issue = _given_up(saga, every=True)
            await _written(written.write, tries=2, state="escalated")
            return "escalated", at_issue
    nothing: set[int] = set()
    try:
        failed = await _put_back(saga, steps, written, nothing, every=True, wait=wait)
    except _Unsettled:
        # As far as it got, for whoever settles it next.
        await _written(written.write)
        raise
    state = "escalated" if failed else "compensated"
    await _written(written.write, tries=2, state=state)
    if failed:
        return state, [it.arguments["path"] for it in failed]
    await _forgotten(saga, orchestrator, steps, written, nothing, state, wait=wait)
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
    ("history", "pickup"): "history.pickup", ("history", "picked"): "history.picked", ("history", "commit"): "history.commit",
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
