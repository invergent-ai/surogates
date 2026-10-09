"""Landing: a project thread's turn applied to the project's real files.

A landing is one saga, run by the worker at the turn's end, its steps
calls of the pod's ``_history`` command: commit the turn on the thread's
branch, apply each file, record the landing on ``main``.  The files to
apply are fixed before the first apply.  A step that still fails after its
retries rolls the landing back: the applied files are put back, in reverse
order, and so is the file of an apply that failed, which may have written
it before its reply was lost.  The real files are then as they were.  All
or nothing.

A file the real files changed since the thread's branch point is left out
by decision, not by failure: the newer file stays, and the thread's
version is kept in history as the landing's second parent.

The saga's durable record is its ``workstream_history`` row, written as it
runs: marked alive at every try, its steps at each turning point and,
between those, every five seconds or every twenty times what a write of
them takes, whichever is longer.  The record step is the push of the
project's history, the moment a landing counts: a landing counts only once
``main`` in the history carries its saga, and one that does is never put
back.  The next holder of the
project's lock settles a landing a killed worker left running before it
does anything else.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from functools import partial
from typing import Any

from surogates.governance.saga import SagaOrchestrator, SagaState, SagaStep, StepState, compensate_step
from surogates.governance.saga.compensator import compensate_history
from surogates.governance.saga.orchestrator import (
    SAGA_DEFAULT_MAX_RETRIES,
    SAGA_DEFAULT_RETRY_DELAY_SECONDS,
    SAGA_DEFAULT_STEP_TIMEOUT_SECONDS,
)
from surogates.sandbox.history import (
    _PRUNE_EVERY,
    LandingStepError,  # noqa: F401  (what _call raises, named here for its callers)
    step_result,
)
from surogates.sandbox.pool import sandbox_session_key
from surogates.session.events import EventType
from surogates.workstreams.history import (
    drop_landing,
    kept_refs,
    project_lock,
    running_landings,
    saga_of,
    save_landing,
    start_landing,
    touch_landing,
)

logger = logging.getLogger(__name__)

#: How long a cancelled landing waits for its put-back before the cancel goes on.
_PUT_BACK_BOUND = 300
#: A pruning's bound: this, and _PRUNE_PER_GIB for each GiB of history, to
#: clone it from the mount, repack it and write it back.
_PRUNE_BOUND = 300
_PRUNE_PER_GIB = 180
#: A pack's own name in the history's pack folder, or its index's: git's, and no other file's.
_PACK = re.compile(r"pack-[0-9a-f]{40}\.(?:pack|idx)")
#: The most files of the pack folder the worker lists to date them.  A landing leaves two packs,
#: four files, and a pruning folds them into one: a folder past this holds what no landing wrote,
#: and its packs are left to the pod's own rule.
_PACKS_LISTED = 4096
#: How long a pruning waits for the project's lock before it gives the day up.  Its pod waits with it,
#: in no wake and no session's keeping: the next completed landing prunes instead.
_PRUNE_PATIENCE = 600
#: How long each try to delete a pruning's pod is given, and how many there are: a delete never
#: answered ends no task.
_LET_GO_BOUND = 30
_LET_GO_TRIES = 2
#: What a pruning's pod is given to live beyond the wait for the lock and its own call: the settle
#: before it, and its going.  A pod whose delete is never answered ends by itself then, not a day on.
_PRUNING_POD_ROOM = 900
#: A landing's steps go to its row whole, so not at every step: between its
#: turning points at most this often, in seconds, and never so often that
#: writing them takes more than one part in _ROW_SHARE of its time.  A row
#: written at every step cost a landing the square of its files.  So a row
#: is behind its landing by the larger of _ROW_EVERY seconds and _ROW_SHARE
#: times its last write: five seconds on a quick database, twenty when a
#: write takes one.
_ROW_EVERY = 5
_ROW_SHARE = 20
#: What a landing left running says, on each apply it could not put back, when it is given up.
_GONE = "The project's history no longer has the versions from before this landing: its files cannot be put back"
#: The commits each thread's turn now running has handed on, as this worker knows them: what a Stop
#: of the turn drops, also from a pod made again since.  A thread is here from the moment a step of
#: its turn sets out to hand its copy on, until the turn's end or its stop.
_HANDED_ON: dict[str, list[str]] = {}
#: The day's prunings under way, kept until done: each outlives the wake whose landing left it.
_PRUNINGS: set[asyncio.Future] = set()
#: Each thread's put-back still running, kept until done: a cancel never cuts one short.
_PUTTING_BACK: dict[str, asyncio.Future] = {}
#: A cancelled landing's pod going, once its slow put-back is done.
_TEARDOWNS: set[asyncio.Future] = set()


def putting_back(owner: str) -> bool:
    """Whether a landing of *owner* is still putting files back; it lets its pod go itself once done."""
    return owner in _PUTTING_BACK


async def put_back_settled(owner: str) -> bool:
    """Whether no landing of *owner* is still putting files back, waiting within its bound for one."""
    pending = _PUTTING_BACK.get(owner)
    if pending is None:
        return True
    try:
        await asyncio.wait_for(asyncio.shield(pending), _PUT_BACK_BOUND)
    except BaseException:
        return False
    return True


class _Row:
    """A landing's row, as its saga runs.

    Every try of a step and every put-back marks it alive first, which
    writes no steps: another lock holder's fence runs from that mark.  The
    steps are written at the landing's turning points: once they are fixed,
    with the record before its first try, when a put-back begins, and at
    the end.  Between those they are written in place of the mark once
    ``_ROW_EVERY`` seconds have passed since the last write, or
    ``_ROW_SHARE`` times what that write took when that is longer.

    So a row is behind its landing by up to the larger of the two: five
    seconds, or twenty times a write of its steps.  It never shows a
    step done that is not: an apply it shows ``pending`` may have run, and
    one it shows ``committed`` may have been put back.  Recovery puts both
    back, which is safe to repeat.
    """

    def __init__(self, session_factory: Any, row: int, saga: Any) -> None:
        self._session_factory, self._row, self._saga = session_factory, row, saga
        self._due = time.monotonic() + _ROW_EVERY

    async def write(self, **values: Any) -> None:
        """The steps as they are, and the landing's outcome once it has one."""
        began = time.monotonic()
        await save_landing(self._session_factory, self._row, self._saga, **values)
        done = time.monotonic()
        self._due = done + max(_ROW_EVERY, _ROW_SHARE * (done - began))

    async def drop(self) -> None:
        """Take the row away: its landing changed no file."""
        await drop_landing(self._session_factory, self._row)

    async def alive(self) -> None:
        """Mark the row alive, with its steps when they were last written long enough ago."""
        if time.monotonic() >= self._due:
            await self.write()
        else:
            await touch_landing(self._session_factory, self._row)


async def _call(sandbox_pool: Any, owner: str, action: str, **arguments: Any) -> dict:
    """One ``_history`` action in *owner*'s pod; LandingStepError unless it answers with the step's result."""
    return step_result(await sandbox_pool.execute(owner, "_history", json.dumps({**arguments, "action": action})))


async def land_turn(
    *,
    store: Any,
    session_factory: Any,
    sandbox_pool: Any,
    session: Any,
    saga_settings: Any,
    tool_saga_id: str | None,
    after_event_id: int,
) -> dict | None:
    """Land *session*'s turn; its outcome, or None when the turn never used its pod.

    The outcome is ``{saga, state, commit, landed, overlapped, excluded,
    repositories, not_taken, files, saved}``: *state* is ``completed``,
    ``compensated`` (rolled back whole) or ``escalated`` (a put-back
    failed); *files* are the report's, every file the turn changed,
    ``landed`` or ``not_merged``; *repositories* are the folders inside a
    git repository the turn wrote into, which never land; *not_taken* are
    the files a helper changed that the thread's copy kept its own version
    of, whose helper's version is in the history alone.

    A landing that could not start, since another thread's landing left
    running could not be settled or the lock or its row could not be had,
    lands nothing and is no saga.  The turn's copy is then kept on its
    branch, as a failed turn's is, to land with the thread's next turn: its
    state is ``compensated``, the project's files being as they were, or
    ``failed`` when the copy could not be kept either.  So is the copy of
    a landing whose commit step failed: under the landing's lock, or, when
    that keep was not made, the lock lost among the causes, under one taken
    afresh.  ``saved`` is true only of a keep that was made.

    A landing that completed with a commit leaves the day's pruning to
    its caller (:func:`prune_later`), which starts it once the turn's report
    is out: ``packs`` is the history's size, for the pruning's bound.

    The whole saga runs under the project's lock: one landing at a time per
    project, so a landing that starts after another sees its files as
    changed rather than rolling back over them.  The lock frees itself if
    its connection drops, so the landing asks it before each apply and the
    record, and stops when it is gone.  The landings a killed worker left
    running are settled first; this thread's own, if one had pushed, is
    reported with this turn's files.
    """
    owner = sandbox_session_key(session)
    if not sandbox_pool.holds_copy(owner):
        return None
    # Read before the lock, which is held for the landing alone.
    calls = await store.get_events(session.id, after=after_event_id, types=[EventType.TOOL_CALL])
    workstream = session.config["workstream_id"]
    outcome = None
    settled: list[dict] = []
    began = False
    waited: set[int] = set()
    try:
        async with project_lock(session_factory, workstream) as held:
            settled = await settle_running(session_factory, sandbox_pool, owner, workstream, saga_settings, held, waited=waited)
            began = True
            outcome = await _land(session_factory, sandbox_pool, session, owner, saga_settings, tool_saga_id, calls, held)
    except Exception as exc:
        if _cancelling():
            # The lock's dead connection failed the block's exit: the cancel goes on.
            raise asyncio.CancelledError from exc
        if outcome is None:
            # No landing of this turn is done.  Its pod goes at the turn's end, so its
            # copy is kept first: a keep moves only its thread's own refs, and waits
            # on no other thread's landing.
            logger.warning("The landing of %s did not run", session.id, exc_info=True)
            kept = await _kept(session_factory, sandbox_pool, session, saga_settings, waited)
            outcome = {
                "saga": None, "commit": None, "landed": [], "overlapped": [], "excluded": [], "repositories": [],
                # What the pod's take-ups left out is named by this report or by none: its list goes with the pod.
                "not_taken": kept.get("not_taken", []) if kept else [],
                "files": [], "saved": kept is not None, "packs": 0,
                # Before its own first step nothing of it reached the real files.
                "state": "compensated" if kept is not None and not began else "failed",
            }
        else:
            # The landing is done; only the lock's transaction did not end cleanly.
            logger.warning("The project's lock for %s ended with an error", session.id, exc_info=True)
    if outcome.pop("unkept", False):
        # Its commit step never put the turn in the history, and the keep under the landing's own lock
        # was not made: that lock was lost, or the pod refused.  Once more under a lock taken afresh,
        # as a landing that could not start is kept, or the turn's work goes with its pod.
        kept = await _kept(session_factory, sandbox_pool, session, saga_settings, waited)
        if kept is not None:
            outcome.update(saved=True, not_taken=kept.get("not_taken", []))
    known = {f["ref"] for f in outcome["files"]}
    outcome["files"] += [
        # A landing of this thread a killed worker had pushed: its files landed, and the report says so.
        {"kind": "file", "label": f["path"], "ref": f["path"], "landing": "landed",
         **({"change": "deleted"} if f["after"] is None else {})}
        for row in settled if row["thread"] == session.id and row["state"] == "completed"
        for f in row["files"] if f["merged"] and f["path"] not in known
    ]
    return outcome


async def keep_copy(
    *, session_factory: Any, sandbox_pool: Any, session: Any, saga_settings: Any, action: str = "keep",
    settle: bool = True, waited: set[int] | None = None,
) -> dict | None:
    """Keep *session*'s copy in the project's history, under its lock; None when it holds none.

    *action* is the pod's: ``keep`` a thread's failed turn on its branch,
    base and all, to land with its next turn (its files may be half made),
    answering ``not_taken``, the helpers' files its pod's take-ups left out;
    ``hand_off`` a thread's copy, for a helper about to start from it;
    ``hand_back`` a helper's, merged onto its thread's hand-off; and
    ``keep_apart`` a failed helper's, merged onto nothing.  The landings a
    killed worker left running are settled first, as every lock holder
    does, unless *settle* is false.  A keep moves only its thread's own
    refs and reads no real file, so a settle that fails does not stop it.
    It writes the history's refs all the same, so it then waits out the
    fence first, as the settle would have: no landing that lost the lock
    unseen is still writing them.  With the rows unreadable it waits as
    long as such a landing's push can live, and keeps.  *waited* are the rows a settle before this
    one already found quiet for the fence.
    """
    owner = sandbox_session_key(session)
    if not sandbox_pool.holds_copy(owner):
        return None
    if action == "hand_off":
        # Noted before the pod is asked: a Stop that cuts the call off may find the hand-off made.
        _HANDED_ON.setdefault(owner, [])
    workstream = session.config.get("workstream_id") or session.config["history_project"]
    async with _guarded(session_factory, sandbox_pool, owner, workstream, saga_settings, settle=settle, waited=waited):
        kept = await _call(sandbox_pool, owner, action, **_kept_as(session), **({"base": True} if action == "keep" else {}))
    if action == "hand_off":
        _HANDED_ON.setdefault(owner, []).append(kept["commit"])
    return kept


@contextlib.asynccontextmanager
async def _guarded(
    session_factory: Any, sandbox_pool: Any, owner: str, workstream: Any, saga_settings: Any,
    *, settle: bool = True, waited: set[int] | None = None,
) -> Any:
    """The project's lock for a write of the history's refs that is no landing: a keep, a hand-off, a stop's drop.

    The landings a killed worker left running are settled first, as every
    lock holder does, unless *settle* is false; where that fails, or is
    not asked for, the fence is waited out.  Then the lock is asked: the
    pod checks the refs it moves as it reads them just before, which holds
    only under the lock, so one lost in the wait stops the write.
    """
    waited = set() if waited is None else waited
    async with project_lock(session_factory, workstream) as held:
        fenced = False
        if settle:
            try:
                await settle_running(session_factory, sandbox_pool, owner, workstream, saga_settings, held, waited=waited)
                fenced = True
            except Exception:
                logger.warning("Could not settle the landings left running in project %s", workstream, exc_info=True)
        if not fenced:
            await _fenced(session_factory, workstream, saga_settings, waited)
        await held()
        yield


def handed_on(session: Any) -> bool:
    """Whether *session*'s turn now running has put its copy on its hand-off, or was doing so, as this worker knows it."""
    return sandbox_session_key(session) in _HANDED_ON


def turn_ended(session: Any) -> None:
    """*session*'s turn is over: what it handed on lands or is kept with it, and is no later Stop's to drop."""
    _HANDED_ON.pop(sandbox_session_key(session), None)


async def drop_hand_off(*, session_factory: Any, sandbox_pool: Any, session: Any, saga_settings: Any) -> bool:
    """Take a thread's stopped turn's own files off its hand-off, so they do not land later; whether it was done.

    A turn that handed nothing on takes no lock and asks no pod.  One that
    did goes behind the guard a keep has, since it writes the history's
    refs as a keep does.  The pod knows the last hand-off it made itself; a
    pod made again since the turn handed on is told which commits the turn
    handed on.  With no pod it does nothing: its caller opens one first.

    What the stop takes back is what the turn itself made and handed on.
    The hand-off goes back to the one the turn's copy had taken up, with
    what helpers kept onto the turn's hand-offs since: a helper's finished
    work is there for the thread's next turn.  A helper still at work is
    not stopped by this: when it ends it hands back what it changed
    itself, a file it made from the stopped turn's draft among them.

    Where this is not done, the pod or the lock not had, the hand-off
    stays as the turn left it, and the stopped turn's handed-on files
    come into the thread's next copy with the helpers' work.
    """
    owner = sandbox_session_key(session)
    gave = _HANDED_ON.pop(owner, None)
    if gave is None or not sandbox_pool.holds_copy(owner):
        return False
    async with _guarded(session_factory, sandbox_pool, owner, session.config["workstream_id"], saga_settings):
        return bool((await _call(sandbox_pool, owner, "drop_hand_off", gave=gave))["dropped"])


def _kept_as(session: Any) -> dict:
    """Who a kept copy's commit is by, and what it says of itself."""
    workstream = session.config.get("workstream_id") or session.config["history_project"]
    return {
        "author": {"name": session.title or "Thread", "email": f"thread:{session.id}@surogate"},
        "trailers": [
            ["Surogate-Project", str(workstream)],
            ["Surogate-Thread", session.config.get("history_thread") or str(session.id)],
            ["Surogate-Agent", str(session.agent_id)], ["Surogate-User", str(session.user_id)],
            ["Surogate-Kind", "turn"],
        ],
    }


async def _kept(
    session_factory: Any, sandbox_pool: Any, session: Any, saga_settings: Any, waited: set[int],
) -> dict | None:
    """The keep of a turn whose landing did not run, on its thread's branch all the same; None when it could not be kept."""
    try:
        return await keep_copy(
            session_factory=session_factory, sandbox_pool=sandbox_pool, session=session,
            saga_settings=saga_settings, settle=False, waited=waited,
        )
    except Exception:
        logger.warning("Could not keep the copy of %s", session.id, exc_info=True)
        return None


async def take_up(sandbox_pool: Any, owner: str) -> list[str]:
    """Bring what helpers kept on the hand-off into a thread's copy; the files it kept its own version of.

    No lock: it reads the history and changes the copy alone.
    """
    try:
        return (await _call(sandbox_pool, owner, "take_up"))["not_taken"]
    except Exception:
        # Taken up at the landing, whose commit step takes it up first.
        logger.warning("Could not take up the helpers' work into %s", owner, exc_info=True)
        return []


def prune_later(
    *, session_factory: Any, sandbox_pool: Any, sandbox_id: str, session_id: str, workstream: Any, packs: int,
    saga_settings: Any, storage: Any = None, bucket: str | None = None, prefix: str = "",
) -> None:
    """Start the day's pruning after a landing, in no wake: the turn's lease goes without waiting for it.

    A pruning is bounded by the history's size, minutes for a large one,
    and nothing of the turn is behind it: its end and its report are
    written.  So the thread's next message starts its next turn while the
    pruning runs, in a pod of its own.  The pruning holds the project's
    lock and is fenced as before, so that turn's landing, as any other
    thread's, waits at the lock until the pruning is done.  The landing's
    pod, *sandbox_id*, goes once it is, and no other pod of its session:
    its delete is tried twice, and where a pruning runs the pod is first
    given a deadline that fits one, in place of a turn's day.
    """
    pruning = asyncio.ensure_future(_pruned_then_gone(
        session_factory=session_factory, sandbox_pool=sandbox_pool, sandbox_id=sandbox_id, session_id=session_id,
        workstream=workstream, packs=packs, saga_settings=saga_settings, storage=storage, bucket=bucket, prefix=prefix,
    ))
    _PRUNINGS.add(pruning)
    pruning.add_done_callback(_PRUNINGS.discard)


async def _pruned_then_gone(*, sandbox_pool: Any, sandbox_id: str, session_id: str, packs: int, **pruning: Any) -> None:
    try:
        await prune_after(sandbox_pool=sandbox_pool, sandbox_id=sandbox_id, packs=packs, **pruning)
    finally:
        # Also when the worker stops under it: its pod goes all the same, and that pod alone.  Each try
        # within a bound: a loop that closes cancels this once and then waits for it, with no bound of its own.
        for left in reversed(range(_LET_GO_TRIES)):
            try:
                await asyncio.wait_for(
                    asyncio.shield(sandbox_pool.destroy_released(sandbox_id, session_id, alone=True)), _LET_GO_BOUND,
                )
                break
            except BaseException:
                if not left:
                    logger.warning("Could not let pod %s go after its pruning", sandbox_id, exc_info=True)


class _Released:
    """A pod its session has let go of, asked as a session's pod is asked: for a settle through the pod that prunes."""

    def __init__(self, sandbox_pool: Any, sandbox_id: str) -> None:
        self._pool, self._sandbox_id = sandbox_pool, sandbox_id

    async def execute(self, owner: str, name: str, input: str, **kwargs: Any) -> str:
        return await self._pool.execute_released(self._sandbox_id, name, input, **kwargs)


async def _old_packs(storage: Any, bucket: str, prefix: str, fence: float) -> list[str] | None:
    """The history's packs the bucket itself dates older than *fence*, each named without its ending.

    Both ends of a pack's age are the bucket's: a mark written now, as
    the bucket dates it, and the pack and its index, as the bucket dates
    them.  The pod that prunes cannot tell: its mount dates the files it
    wrote itself by its own clock, and no clock of the worker's is in it
    here either.

    The folder is a pod's to write, so it is read as hostile: ``_PACKS_LISTED``
    files of it at most, and of those only the ones named as git names a
    pack or its index.  None when it holds more: no list is made of a
    folder of any size, and the pod goes by its own rule.  The mark is the
    one thing the worker writes there, and the store writes it through no
    link: a mark it refuses stops the pruning.
    """
    folder = f"{prefix}_history/objects/pack/"
    listed = await storage.list_entries(bucket, folder, limit=_PACKS_LISTED + 1)
    if len(listed) > _PACKS_LISTED:
        logger.warning(
            "The pack folder of %s/%s holds more than %d files: its packs' ages are left to the pod that prunes",
            bucket, prefix, _PACKS_LISTED,
        )
        return None
    # Through no link and over nothing but a plain file: the folder is a pod's to write.
    now = _epoch(await storage.mark(bucket, f"{prefix}_history/pruning"))
    written: dict[str, float] = {}
    for entry in listed:
        name = entry["key"].removeprefix(folder)
        if _PACK.fullmatch(name):
            # As young as the younger of its two files.
            stem = name.rpartition(".")[0]
            written[stem] = max(written.get(stem, 0.0), _epoch(entry["modified"]))
    return sorted(stem for stem, at in written.items() if now - at >= fence)


async def _due(storage: Any, bucket: str, prefix: str) -> bool:
    """Whether the day's pruning is due, by the mark the last one left: asked before anything else is.

    The pod decides, and marks the day; this spares the lock, the settle,
    the listing and the pod at every landing of a day already pruned.  By
    the worker's clock against the bucket's date for the mark: a day is
    coarse enough for both.
    """
    try:
        marked = (await storage.stat(bucket, f"{prefix}_history/pruned"))["modified"]
    except KeyError:
        return True
    return time.time() - _epoch(marked) >= _PRUNE_EVERY


def _epoch(modified: Any) -> float:
    """An object's date as the store gives it, in seconds: a datetime from a bucket, a number from a disk."""
    return modified.timestamp() if hasattr(modified, "timestamp") else float(modified)


async def prune_after(
    *, session_factory: Any, sandbox_pool: Any, sandbox_id: str, workstream: Any, packs: int, saga_settings: Any,
    storage: Any = None, bucket: str | None = None, prefix: str = "",
) -> None:
    """Prune the project's history after a landing, in the landing's pod, under the project's lock again.

    The pod, *sandbox_id*, is the turn's, already let go of by its session
    and not yet destroyed: the turn's report is out, and nothing waits on
    this but the pod's end.  The pod prunes at most once a day, and
    refuses when the history's refs moved under it.  It never fails its
    caller: the landing stands, and the history is pruned on a later day.

    It is fenced as a landing is: the landings left running are settled
    first, through this pod.  One written within the fence lost the lock
    unseen, and may be writing a pack whose commits no ref names yet: it
    is waited for, then settled.  One left ``escalated``, or settled here
    with a row that could still not be written, holds no pruning back.  A
    settle that fails leaves the pruning to the next landing, the day not
    marked; so does a lock not had within ``_PRUNE_PATIENCE``.  And the pod
    leaves every pack younger than the fence, for a push no row tells of:
    a keep's or a hand-off's.  Which packs are older is asked of the
    bucket, through *storage*, where the project's files are under
    *prefix* of *bucket*: the pod is told them by name.  With no storage,
    or a pack folder too full to list, it is told nothing, and goes by
    the dates it sees.  And the bucket is asked first whether the day was
    pruned already: then nothing more is done.
    """
    try:
        if storage is not None and not await _due(storage, bucket, prefix):
            return
        # A pruning runs.  First, a life that fits one: the pod is a turn's, made to last a day, and in
        # no session's keeping now.  The longest its pruning can take, and room: a delete never
        # answered leaves it that long.  Asked only here: on a day already pruned the pod just goes.
        life = _PRUNE_PATIENCE + _PRUNE_BOUND + _PRUNE_PER_GIB * packs / 2**30 + _PRUNING_POD_ROOM
        try:
            await asyncio.wait_for(sandbox_pool.expire_released(sandbox_id, life), _LET_GO_BOUND)
        except Exception:
            logger.warning("Could not give pod %s a pruning's life: it keeps a turn's", sandbox_id, exc_info=True)
        async with asyncio.timeout(_PRUNE_PATIENCE) as patience, project_lock(session_factory, workstream) as held:
            # The lock is had: the settle's waits and the pod's call have bounds of their own.
            patience.reschedule(None)
            await settle_running(
                session_factory, _Released(sandbox_pool, sandbox_id), sandbox_id, workstream, saga_settings, held,
            )
            old = await _old_packs(storage, bucket, prefix, _fence(saga_settings)) if storage is not None else None
            request = {
                "action": "prune", "keep": await kept_refs(session_factory, workstream), "now": time.time(),
                "spare": _fence(saga_settings), **({"old": old} if old is not None else {}),
            }
            await held()
            step_result(await sandbox_pool.execute_released(
                sandbox_id, "_history", json.dumps(request), timeout=_PRUNE_BOUND + _PRUNE_PER_GIB * packs / 2**30,
            ))
    except Exception:
        logger.warning("Could not prune the history of project %s", workstream, exc_info=True)


async def _fenced(session_factory: Any, workstream_id: Any, saga_settings: Any, waited: set[int]) -> None:
    """Wait until no landing of the project can still be alive, settling none: each row quiet for the fence.

    For a lock holder that writes the history's refs without having
    settled the landings left running.  A row in *waited* was found quiet
    for the fence before, and is written since only by who settles it.
    The rows are read as a step is tried.  Unread, no mark is seen, and a
    fence proves nothing: it is the longest a live landing goes without
    marking its row, not the longest it lives.  The wait is then for as
    long as a landing's push can still be alive once this lock is taken
    (:func:`_life`), a fence at a time, the rows asked for again after
    each: read, they say again which landing lives.  The caller then asks
    its lock, as after any wait.
    """
    fence, life = _fence(saga_settings), _life(saga_settings)
    orchestrator = _orchestrator(saga_settings)
    blind = 0.0
    while True:
        try:
            rows = await orchestrator.attempt(partial(running_landings, session_factory, workstream_id))
        except Exception:
            if blind >= life:
                return
            logger.warning(
                "Could not read the landings running in project %s: %d s waited of the %d s a landing's push can live",
                workstream_id, blind, life, exc_info=True,
            )
            wait = min(fence, life - blind)
            await asyncio.sleep(wait)
            blind += wait
            continue
        waited.update(row.id for row, quiet in rows if quiet >= fence)
        alive = [quiet for row, quiet in rows if row.id not in waited]
        if not alive:
            return
        await asyncio.sleep(fence - min(alive))


def _fence(saga_settings: Any) -> float:
    """The longest a live landing goes without writing its row, and a second more.

    Each try of a step writes it first, so that is a try and the wait
    before the next; the looks outside the steps have a try's bound too.
    """
    timeout, retries, delay = (
        (saga_settings.default_step_timeout, saga_settings.default_max_retries, saga_settings.retry_delay)
        if saga_settings is not None else
        (SAGA_DEFAULT_STEP_TIMEOUT_SECONDS, SAGA_DEFAULT_MAX_RETRIES, SAGA_DEFAULT_RETRY_DELAY_SECONDS)
    )
    return timeout + delay * retries + 1


def _life(saga_settings: Any) -> float:
    """The longest a landing's push is still alive after its lock has gone to another, and a second more.

    A landing asks for its lock before each apply and before its record,
    never inside a step, and not between its look at the history and its
    first push, the commit step.  So one that lost its lock unseen can
    still be in that look, a try's bound, and then in a push through
    every try the step has and the pauses between them.
    """
    timeout, retries, delay = (
        (saga_settings.default_step_timeout, saga_settings.default_max_retries, saga_settings.retry_delay)
        if saga_settings is not None else
        (SAGA_DEFAULT_STEP_TIMEOUT_SECONDS, SAGA_DEFAULT_MAX_RETRIES, SAGA_DEFAULT_RETRY_DELAY_SECONDS)
    )
    # A step's pause before its n-th retry is n delays.
    return timeout + (1 + retries) * timeout + delay * retries * (retries + 1) / 2 + 1


def _orchestrator(saga_settings: Any) -> SagaOrchestrator:
    return SagaOrchestrator(**(
        {
            "default_step_timeout": saga_settings.default_step_timeout,
            "default_max_retries": saga_settings.default_max_retries,
            "retry_delay": saga_settings.retry_delay,
        } if saga_settings is not None else {}
    ))


async def _land(
    session_factory: Any, sandbox_pool: Any, session: Any, owner: str,
    saga_settings: Any, tool_saga_id: str | None, calls: list, held: Any,
) -> dict:
    orchestrator = _orchestrator(saga_settings)
    saga = orchestrator.create_saga(session.id, kind="landing")
    thread = {"name": session.title or "Thread", "email": f"thread:{session.id}@surogate"}
    audit = [
        ["Surogate-Project", str(session.config["workstream_id"])],
        ["Surogate-Thread", str(session.id)],
        ["Surogate-Agent", str(session.agent_id)],
        ["Surogate-User", str(session.user_id)],
        ["Surogate-Saga", saga.saga_id],
        *([["Surogate-Tool-Saga", tool_saga_id]] if tool_saga_id else []),
        *([["Surogate-Events", f"{calls[0].id}-{calls[-1].id}"]] if calls else []),
    ]
    row = _Row(session_factory, await start_landing(
        session_factory, saga, workstream_id=session.config["workstream_id"], thread_id=session.id,
        agent_id=str(session.agent_id), user_id=session.user_id, tool_saga_id=tool_saga_id,
        events=(calls[0].id, calls[-1].id) if calls else None,
    ), saga)

    def step(name: str, **arguments: Any) -> SagaStep:
        return orchestrator.add_step(
            saga.saga_id, tool_name=f"history.{name}", tool_call_id="", arguments=arguments,
        )

    async def run(it: SagaStep) -> dict:
        # Each try marks the row alive first: another lock holder's fence runs from here.
        await row.alive()
        return await _call(sandbox_pool, owner, it.tool_name.removeprefix("history."), **it.arguments)

    async def execute(it: SagaStep) -> dict:
        return await orchestrator.execute_step(saga.saga_id, it.step_id, lambda: run(it))

    outcome: dict[str, Any] = {
        "saga": saga.saga_id, "state": "completed", "commit": None,
        "landed": [], "overlapped": [], "excluded": [], "repositories": [], "not_taken": [], "files": [],
        # Whether the turn's work is in the history: its commit step pushed
        # it, and held no file, whose version the next copy would lack.
        "saved": False,
        # The size of the history's packs, which a pruning's bound is sized from.
        "packs": 0,
    }
    changes: list[dict] = []
    main: str | None = None
    commit = step("commit", author=thread, trailers=[*audit, ["Surogate-Kind", "turn"]])
    try:
        # The first look, outside the steps: it changes nothing, and under
        # the lock no one else moves main until this landing is done.
        looked = await asyncio.wait_for(_call(sandbox_pool, owner, "fetch"), commit.timeout_seconds)
        main = looked["main"]
        outcome["packs"] = looked["packs"]
        turn = await execute(commit)
        changes = turn["changes"]
        outcome.update(
            overlapped=turn["overlapped"], excluded=turn["excluded"], repositories=turn["repositories"],
            not_taken=turn["not_taken"], saved=not turn["overlapped"],
        )
        if turn["commit"] is not None:
            applies = [step("apply", **change) for change in changes]
            # The steps, fixed: all a put-back by another lock holder needs.
            await row.write()
            for it in applies:
                # A lock lost unseen frees the project: this landing stops, and is put back.
                await held()
                await execute(it)
            landed = [it.execute_result for it in applies]
            record = step(
                "record", turn=turn["commit"], applied=landed, author=thread, main=main,
                trailers=[
                    *audit, ["Surogate-Kind", "landing"],
                    *(["Surogate-Not-Merged", o["path"]] for o in turn["overlapped"]),
                ],
            )
            # Before its first try: a landing whose row has no record step never pushed.
            await row.write()
            await held()
            recorded = await execute(record)
            outcome.update(commit=recorded["commit"], landed=landed)
        saga.transition(SagaState.COMPLETED)
        if turn["commit"] is None:
            # Nothing changed, nothing landed: no change to the project's files to record.
            await _written(row.drop)
        else:
            await row.write(state="completed", commit=outcome["commit"], files=_row_files(saga, "completed"))
    except BaseException as exc:
        logger.warning("Landing of session %s did not finish", session.id, exc_info=True)
        # Kept, and shielded: a cancel never cuts a put-back short.
        put_back = asyncio.ensure_future(_settle(saga, orchestrator, sandbox_pool, owner, row))
        _PUTTING_BACK[owner] = put_back
        put_back.add_done_callback(lambda done: _PUTTING_BACK.pop(owner, None) if _PUTTING_BACK.get(owner) is done else None)
        if not isinstance(exc, Exception) or _cancelling():
            # Cancelled: the turn's lease went to another worker, which cannot
            # reach this pod.  What was applied still goes back, then the cancel
            # goes on, though a row write it ran into failed in its place.
            await _after_cancel(put_back, sandbox_pool, owner)
            if isinstance(exc, Exception):
                raise asyncio.CancelledError from exc
            raise
        try:
            state, pushed = await asyncio.shield(put_back)
        except asyncio.CancelledError:
            await _after_cancel(put_back, sandbox_pool, owner)
            raise
        outcome.update(state=state)
        if pushed is not None:
            # The push happened though its answer was lost: the landing counts.
            outcome.update(commit=pushed, landed=[s.execute_result for s in saga.steps if s.tool_name == "history.apply"])
        if commit.state is not StepState.COMMITTED:
            # Its commit step never put the turn in the history, and its pod goes
            # at the turn's end: the copy is kept on its branch first.
            try:
                await held()
                kept = await _call(sandbox_pool, owner, "keep", **_kept_as(session), base=True)
                # What the commit step would have named: the pod's list goes with the pod.
                outcome.update(saved=True, not_taken=kept.get("not_taken", []))
            except Exception:
                logger.warning("Could not keep the copy of %s under its landing's lock", session.id, exc_info=True)
                # Its caller's, once this lock is let go: under one taken afresh, which a lock lost here is no bar to.
                outcome["unkept"] = True
    applied = {c["path"]: c for c in outcome["landed"]}
    reasons = {o["path"]: o["reason"] for o in outcome["overlapped"]}
    paths = sorted({c["path"] for c in changes} | set(reasons))
    outcome["files"] = [
        {
            "kind": "file", "label": path, "ref": path,
            "landing": "landed" if path in applied else "not_merged",
            # A landed deletion is no file to open: the report names it apart.
            **({"change": "deleted"} if path in applied and applied[path]["after"] is None else {}),
            # Why a file was left out, for the report's line on it.
            **({"reason": reasons[path]} if path in reasons else {}),
        }
        for path in paths
    ]
    return outcome


def _cancelling() -> bool:
    """Whether this task is being cancelled, though an error may have taken the cancel's place."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _row_files(saga: Any, state: str) -> list[dict]:
    """A landing row's files: each it landed, and each it left out, with its two versions; none unless it completed."""
    if state != "completed":
        return []
    commit = next((s.execute_result for s in saga.steps if s.tool_name == "history.commit"), None) or {}
    landed = [s.execute_result for s in saga.steps if s.tool_name == "history.apply" and s.state is StepState.COMMITTED]
    return [
        *({"path": c["path"], "before": c["before"], "after": c["after"], "merged": True} for c in landed),
        *({"path": o["path"], "before": o["before"], "after": o["after"], "merged": False} for o in commit.get("overlapped", [])),
    ]


async def _settle(
    saga: Any, orchestrator: SagaOrchestrator, sandbox_pool: Any, owner: str, row: _Row,
    *, recovered: bool = False, held: Any = None, at_issue: list[str] | None = None,
) -> tuple[str, str | None]:
    """End a landing that did not finish: ``completed`` with its commit when it pushed, else put back.

    It pushed only when ``main`` in the history carries its saga.  ``main``
    moved without it means another landing went first, with this one's lock
    lost, or a command rewrote the history, and is taken for not pushed.  So
    is a landing that pushed and was then landed over: that takes a lost lock
    and a fence that fell short.  A
    *recovered* landing's steps are as its row last had them: a step it
    was in, or had done since, shows ``pending``.  Its put-backs ask *held*
    first, as a landing's applies do.  *at_issue* takes the files of a
    landing left ``escalated`` that a person has to check: those whose
    put-back failed, or, for one given up, each that may have been written.
    """
    async def look(*, found: bool = False, **arguments: Any) -> dict:
        """A look at the history through the pod, tried as a step is, each try marking the row alive first.

        With *found*, a look that does not see the commits it asks for is
        a failed try, and they are looked for twice at least, a step's
        pause apart: _Unseen when the last look lacks them still.
        """
        async def once() -> dict:
            await _written(row.alive)
            looked = await _call(sandbox_pool, owner, "fetch", **arguments)
            if found and looked["missing"]:
                raise _Unseen(looked["missing"])
            return looked

        return await orchestrator.attempt(once, least=2 if found else 1)

    if not recovered:
        # Where it stopped, before anything goes back: its own row may be behind, by five seconds or twenty writes.
        await _written(row.write)
    gone: list[str] = []
    committed = next(
        (s.execute_result for s in saga.steps if s.tool_name == "history.commit" and s.state is StepState.COMMITTED), None,
    )
    if recovered and committed is not None and committed["commit"] is not None:
        # The base alone: a put-back writes its versions and reads no other.  Fetched by id, since this
        # pod never had it; and not taken for lost on one look's word.
        try:
            await look(commits=[committed["base"]], found=True)
        except _Unseen as unseen:
            gone = unseen.missing
    record = next((s for s in saga.steps if s.tool_name == "history.record"), None)
    # Whatever its state: a try that pushed shows ``pending`` again in its retry's wait.
    if record is not None:
        looked = await look(saga=saga.saga_id)
        if looked["has_saga"]:
            if saga.state is SagaState.RUNNING:
                saga.transition(SagaState.COMPLETED)
            await _written(row.write, tries=2, state="completed", commit=looked["main"], files=_row_files(saga, "completed"))
            return "completed", looked["main"]
    if gone:
        # Nobody has the versions from before: given up once, with why, so
        # that no later landing of the project fails on it.
        logger.error("Landing %s cannot be put back: the project's history lacks %s", saga.saga_id, ", ".join(gone))
        for it in saga.steps:
            if it.tool_name == "history.apply" and it.state is not StepState.COMPENSATED:
                it.state, it.error = StepState.COMPENSATION_FAILED, _GONE
                if at_issue is not None:
                    at_issue.append(it.arguments["path"])
        await _written(row.write, tries=2, state="escalated")
        return "escalated", None
    failed = await _put_back(saga, orchestrator, sandbox_pool, owner, row, recovered=recovered, held=held)
    if at_issue is not None:
        at_issue.extend(it.arguments["path"] for it in failed if it.tool_name == "history.apply")
    state = "escalated" if failed else "compensated"
    await _written(row.write, tries=2, state=state)
    return state, None


class _Unseen(Exception):
    """A look did not see the commits it asked the history for."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"the project's history lacks {', '.join(missing)}")
        self.missing = missing


async def _tell_escalated(session_factory: Any, thread_id: Any, saga: Any, at_issue: list[str]) -> None:
    """Report a landing a settle left ``escalated`` to its thread's master, as a turn's own is reported.

    No turn of the thread ends here, so the report has no words of its:
    it is ``recovered``, names the files *at_issue*, each to check, and
    says ``gone`` when the history had nothing to put back.  The master
    reads it at its next wake, and the thread's row shows it as the
    thread's last report.  It is written once a landing, by its ``saga``:
    a row whose ``escalated`` could not be written is settled again by
    each later lock holder, and told by the first.  As best it can: the
    row reads ``escalated`` whatever comes of the telling.
    """
    from sqlalchemy import select

    from surogates.db.models import Event
    from surogates.session.store import SessionStore
    from surogates.workstreams.store import WorkstreamStore

    try:
        store = SessionStore(session_factory)
        thread = await store.get_session(thread_id)
        named = await WorkstreamStore(session_factory).get_thread(thread_id)
        if thread.parent_id is None:
            return
        async with session_factory() as db:
            told = await db.scalar(select(Event.id).where(
                Event.session_id == thread.parent_id, Event.type == EventType.WORKER_COMPLETE.value,
                Event.data["saga"].astext == saga.saga_id,
            ).limit(1))
        if told is not None:
            return
        await store.emit_event(thread.parent_id, EventType.WORKER_COMPLETE, {
            "worker_id": str(thread_id), "title": named.title if named is not None else thread.title, "result": "",
            "files": [{"kind": "file", "label": path, "ref": path, "landing": "not_merged"} for path in at_issue],
            "landing": "escalated", "recovered": True, "saga": saga.saga_id,
            **({"gone": True} if any(s.error == _GONE for s in saga.steps) else {}),
        })
    except Exception:
        logger.warning("Could not tell the master of thread %s that its landing escalated", thread_id, exc_info=True)


async def _written(write: Any, *, tries: int = 1, **values: Any) -> None:
    """Write a landing's row, or mark it alive, as best it can: an outcome already known stands without it.

    A write that fails is caught up by the next, or by the next lock
    holder's settle, which puts the files back again: that is safe to repeat.
    """
    for _ in range(tries):
        try:
            return await write(**values)
        except Exception:
            logger.warning("A landing's row was not written", exc_info=True)


async def settle_running(
    session_factory: Any, sandbox_pool: Any, owner: str, workstream_id: Any, saga_settings: Any, held: Any,
    *, waited: set[int] | None = None,
) -> list[dict]:
    """Settle the project's landings left running, through *owner*'s pod; each ``{thread, state, files}``.

    A row written within the fence is waited for: its worker may still be
    in a step, or putting files back past its bound.  A settle that loses
    the lock stops, its row left for the next holder.  A landing whose
    base the history no longer has, looked for twice, is given up,
    ``escalated``: no one can put its files back, and no later landing
    waits on it.  A landing left ``escalated`` here, given up or with a
    put-back that failed, is reported to its thread's master, whichever
    lock holder found it so.  *waited* takes the rows found quiet for the
    fence, whose landings are dead whatever comes of settling them.
    """
    fence = _fence(saga_settings)
    settled: list[dict] = []
    # A row settled here whose last write failed still reads running: it is the next holder's.
    done: set[int] = set()
    while running := [r for r in await running_landings(session_factory, workstream_id) if r[0].id not in done]:
        row, quiet = running[0]
        if quiet < fence:
            await asyncio.sleep(fence - quiet)
            continue
        if waited is not None:
            waited.add(row.id)
        saga = saga_of(row)
        orchestrator = _orchestrator(saga_settings)
        orchestrator.adopt(saga)
        at_issue: list[str] = []
        state, _ = await _settle(
            saga, orchestrator, sandbox_pool, owner, _Row(session_factory, row.id, saga),
            recovered=True, held=held, at_issue=at_issue,
        )
        done.add(row.id)
        logger.warning("Settled landing %s of %s, left running: %s", row.saga_id, row.thread_id, state)
        if state == "escalated" and row.thread_id is not None:
            await _tell_escalated(session_factory, row.thread_id, saga, at_issue)
        settled.append({"thread": row.thread_id, "state": state, "files": _row_files(saga, state)})
    return settled


async def _after_cancel(put_back: asyncio.Future, sandbox_pool: Any, owner: str) -> None:
    """Wait, bounded, for a cancelled landing's put-back, then let the thread's pod go.

    No later turn on this worker takes up the copy whose writes were put
    back.  A put-back still running keeps its pod, and lets it go once done.
    """
    if not await put_back_settled(owner):
        logger.warning("The cancelled landing of %s is still putting files back", owner)
        waited_on = sandbox_pool.sandbox_of(owner)
        put_back.add_done_callback(lambda _: _let_go(sandbox_pool, owner, waited_on))
        return
    try:
        await asyncio.shield(sandbox_pool.destroy_for_session(owner))
    except BaseException:
        logger.warning("The cancelled landing of %s did not let its pod go", owner, exc_info=True)


def _let_go(sandbox_pool: Any, owner: str, sandbox_id: str | None) -> None:
    """Destroy *owner*'s pod *sandbox_id* in the background, kept until done; a later turn's pod stays."""
    going = asyncio.ensure_future(_destroy_if_still(sandbox_pool, owner, sandbox_id))
    _TEARDOWNS.add(going)
    going.add_done_callback(_TEARDOWNS.discard)


async def _destroy_if_still(sandbox_pool: Any, owner: str, sandbox_id: str | None) -> None:
    if sandbox_id is not None and (released := await sandbox_pool.release_for_session(owner, only=sandbox_id)):
        await sandbox_pool.destroy_released(released, owner)


async def _put_back(
    saga: Any, orchestrator: SagaOrchestrator, sandbox_pool: Any, owner: str, row: _Row,
    *, recovered: bool = False, held: Any = None,
) -> list[SagaStep]:
    """Put back what a landing applied; the steps that could not be put back.

    An apply that failed, or that was cut off, may still have written its
    file: its reply was lost, or it ran out of time.  It is put back first,
    only where the real file is the turn's version.  A *recovered*
    landing's apply still ``pending`` may have too: the kill came before its
    state was written.  Such an apply's put-back knows neither the folders
    it made, which stay, empty, nor whether it ran: a file changed since is
    left as it is and is no conflict.  Each put-back marks the saga's row
    alive first, and the row never shows one done before it is: a worker
    killed in one leaves it to run again.  With *held*, each put-back asks
    it first, and a lock lost stops them all.
    """
    unsure = (StepState.FAILED, StepState.EXECUTING, *((StepState.PENDING,) if recovered else ()))
    failed: list[SagaStep] = []
    lost: list[Exception] = []

    async def still_held() -> None:
        if lost:
            raise lost[0]
        if held is not None:
            try:
                await held()
            except Exception as exc:
                lost.append(exc)
                raise

    for it in saga.steps:
        if it.tool_name == "history.apply" and it.state in unsure:
            await still_held()
            await _written(row.alive)
            try:
                await asyncio.wait_for(compensate_history(it, sandbox_pool, owner, ran=False), it.timeout_seconds)
            except Exception:
                logger.warning("Could not put back %s", it.arguments.get("path"), exc_info=True)
                failed.append(it)

    async def compensate(it: SagaStep) -> Any:
        await still_held()
        await _written(row.alive)
        return await compensate_step(it, sandbox_pool=sandbox_pool, session_id=owner)

    failed += await orchestrator.compensate(saga.saga_id, compensate)
    if lost:
        # Not a put-back that failed: the next holder of the lock does the rest.
        raise lost[0]
    return failed
