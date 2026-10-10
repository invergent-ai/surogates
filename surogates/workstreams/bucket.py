"""A project's history and its real files as the api reaches them: through the storage backend.

The api has no mount of the project's files.  It reads the project's
history, ``_history/`` in the bucket, as data, into a bare repository of
its own on its disk: the packs, ``packed-refs`` and ``shallow``, every id
and ref checked as a pod checks them (``History._take``), and nothing else.
Git runs only in that copy, under its own ``HEAD`` and config: a
``config``, ``HEAD`` or hook a thread's command wrote into the bucket never
reaches it, and each pack's index is made here, never copied.

It lands too, as a pod does: its actions are a pod's ``_history`` actions,
called as a pod's are (:meth:`BucketHistory.execute`), so a landing by the
user runs here as a thread's runs in its pod, and each settles what the
other left running.  The real files are read and written through the
backend, as the upload route writes them.  A commit is made of objects
kept apart from the copy's own, and pushed as a pod pushes: a pack of what
the bucket lacks, then ``packed-refs``.  The copy itself holds only what
the bucket holds.

The api is one process for every tenant, and a thread's command can write
anything into its project's history.  So what a history costs the api is
bounded, whatever it holds:

- its memory: a pack, ``packed-refs`` and ``shallow`` each go from the
  bucket to the api's disk a piece at a time, each within a bound.  The
  refs are then checked a line at a time, one file at a time in the whole
  process, off the loop that serves every request; and only when the bucket
  says they changed;
- git: it runs as the api's child, a few at once and each within an
  address space of its own, so a history git cannot read in that much is
  refused, not read.  A pack is given seconds to be brought in, and git is
  stopped past them.  Bringing copies in and asking a copy that is in take
  their turns apart, so the first never keeps the second waiting;
- its disk: a copy is counted by what it holds there, the indexes the api
  makes with the packs, and a copy that would pass its bound is refused
  before it does.  A copy not used for a while is removed, and the copies
  together are kept within a bound of their own;
- a version of a file: git writes it out to a file of the copy's, in a turn
  and within its seconds as a pack is brought in, and it is sent from that
  file a piece at a time.  The file is counted with the copy, within a
  bound of its own, and is gone once the version is sent, when who asked
  for it leaves first, and after a process that died with it;
- a real file: it is read to such a file a piece at a time, to be told
  from a version or kept as one, and a version is written to it from one,
  each within a version's bound and gone as soon;
- its patience: one request has a copy at a time, to bring it in or to ask
  it, and one that finds it held, or finds no turn for git, waits a few
  seconds, then is told to try again.

Each bound is refused in words that say why.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import weakref
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, TypeVar

from surogates.sandbox.history import (
    _ID,
    _PACKED,
    _REF,
    _ZERO,
    MAIN,
    HistoryConflict,
    HistoryError,
    _as,
    _block,
    _checked_id,
    _checked_ref,
    _environ,
    _replace,
    tracked,
)
from surogates.storage.backend import TooLarge
from surogates.storage.tenant import boundary_workspace_prefix
from surogates.workstreams.history import REFS_BOUND

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Where the api keeps its copies of projects' histories, one folder a project, unless its settings say.
CLONES = Path(tempfile.gettempdir()) / "surogates-history"
#: A pack of the history, as the bucket names it.
_PACK = re.compile(r"pack-[0-9a-f]{40}\.(pack|idx)")
#: What the copy notes beside a pack of the bucket's: that it has it, that git cannot read it, or what
#: stopped it from being brought in: git's memory, a pack's time, or the copy's bound.
_MARKS = ("bucket", "bad", "large", "slow", "over")
#: Git's own memory, as the api's child: one thread, no object over 32 MiB held whole to index, and a
#: pack read through 32 MiB windows, 64 MiB of them at once.
_BOUNDED = (
    "-c", "pack.threads=1", "-c", "core.bigFileThreshold=32m", "-c", "core.deltaBaseCacheLimit=32m",
    "-c", "pack.windowMemory=32m", "-c", "core.packedGitWindowSize=32m", "-c", "core.packedGitLimit=64m",
)
#: The git children this process runs at once, those that bring a copy in and those that ask one that
#: is in, and the address space each is given.  Those settings keep git far under it, but for an object
#: a pod's git stored as a change to another: git holds that one whole, with what it changes, whatever
#: it is told.  Past its address space git stops, and the history is refused in words.  Together they
#: are what every project's histories cost the api's memory at most.
_BRING_SLOTS = 2
_READ_SLOTS = 2
_GIT_MEMORY = 256 * 2**20
#: How git says it stopped for memory.
_NO_MEMORY = re.compile(r"out of memory|cannot allocate memory|mmap failed", re.IGNORECASE)
#: The seconds git has to bring one pack in: twenty, and for a pack the bucket holds more of, what a
#: slow disk needs to read it, two minutes at most.  A pack costs git by what it unpacks to, which a
#: command can make a thousand times what the bucket holds: past its seconds git is stopped.
_TAKE_LEAST = 20.0
_TAKE_RATE = 32 * 2**20
_TAKE_MOST = 120.0
#: And to answer a question of a copy that is in, or to make a copy's repository: what a request waits.
_READ_SECONDS = 8.0
#: A pack stopped for its time is tried again after this long: the api may only have been busy.
_SLOW_AGAIN = 600.0
#: Where a copy keeps the versions on their way to who asked for them, and the most of one read at once to send it.
_OUT = "out"
_PIECE = 2**20
#: The seconds a version has to be sent: two minutes, and for a larger one what a slow line needs.  Who
#: takes it slower is taking none, and its file is not kept for them.
_SEND_LEAST = 120.0
_SEND_RATE = 2**20
#: The longest line a history's ``packed-refs`` or ``shallow`` holds: an id, and a ref of a thread's.
_LINE_MOST = 4096
#: A line of each as every writer of a history writes it, by what a pod checks an id and a ref by.
_REF_LINE = re.compile(_ID.pattern.encode() + b" (" + _REF.pattern.encode() + b")\n")
_CUT_LINE = re.compile(_ID.pattern.encode() + b"\n")
#: The most names a history's packs are listed by, a pack and its index each: a thread's command can
#: write names there, and a pruning leaves one pack.
_PACKS_MOST = 10_000
#: How long a request waits for a copy another request has, or for a turn, before it is told to try
#: again: within what a page gives a History to answer.
_PATIENCE = 8.0
_LOCK_POLL = 0.05
#: A copy used this recently is never removed to make room: a request may be reading it.
_GRACE = 60.0
#: How a bound's refusal ends: said to the user as it is.
_HERE = "than Surogate can read here."
TOO_LARGE = f"This project's history is larger {_HERE}"
_FILE_TOO_LARGE = f"This project's history holds a file larger {_HERE}"
VERSION_TOO_LARGE = f"This version is larger {_HERE}"
#: What a version a pruning took answers wherever it is asked for.
NOT_KEPT = "This version is no longer kept in the project's history."
#: What a request is told when it waited its patience for a copy, or for a turn: nothing is wrong with the history.
BUSY = "This project's history is being read just now. Try again in a moment."
#: A landing's actions as a pod runs them, which this copy runs too, each by the method of its name.
_ACTIONS = frozenset({"fetch", "pickup", "apply", "unapply", "record"})
#: The most of ``main``'s own commits a look for a landing reads back through: far more than go over a
#: landing left running, and what bounds the look whatever a command made of the history.
_LOOKED_MOST = 10_000
#: The most files one question of a commit's files names at once.
_ASKED_AT_ONCE = 64
#: What the copy notes of the bucket's refs: what the bucket said of each file when it was last read.
_SEEN = "refs-seen"
#: The copies this process is reading now, which nothing removes: a version on its way out of one counts
#: it too.  Counted under a lock: a version no one sent is let go wherever its last hold goes.
_USING: Counter[Path] = Counter()
_COUNTING = threading.Lock()
#: The versions being written out whose requests may have left: each ends in its own time.
_WRITING: set[asyncio.Future] = set()
#: Git's turns: a child runs in one of these threads, so no more run at once than there are.  A copy
#: that is in is asked in turns of its own, which no copy on its way in takes.
_BRINGING = ThreadPoolExecutor(max_workers=_BRING_SLOTS, thread_name_prefix="history-bring")
_READING = ThreadPoolExecutor(max_workers=_READ_SLOTS, thread_name_prefix="history-read")
#: The refs' turn: one file of refs is checked at a time, whatever the number of projects asked at once.
_REFS = ThreadPoolExecutor(max_workers=1, thread_name_prefix="history-refs")


@dataclass(frozen=True)
class Bounds:
    """What the api spends on its copies of projects' histories: bytes on its disk, and the seconds a copy stays unused.

    *packs* is one copy's: its packs with the indexes the api makes of them,
    and all else it holds.  *file* is one version's, written out to be
    sent.  *copies* is every copy's together.
    """

    packs: int = 4 * 2**30
    file: int = 2**30
    copies: int = 8 * 2**30
    idle: float = 600.0


class Busy(HistoryError):
    """The copy, or a turn, was another request's for longer than this one waits."""


class Slow(HistoryError):
    """Git, or the sending of a version, took longer than it is given, and was stopped."""


class NotKept(HistoryError):
    """A version the project's history no longer holds: a pruning took it."""


def said(error: object) -> str | None:
    """*error*'s words when it is a bound's refusal, which a user is told as it is; None for any other."""
    words = str(error)
    return words if words.endswith(_HERE) else None


def _use(clone: Path) -> None:
    """Mark the copy *clone* in use by one more: nothing removes it meanwhile."""
    with _COUNTING:
        _USING[clone] += 1


def _used(clone: Path) -> None:
    """The copy *clone* is in use by one fewer, and was used just now."""
    with _COUNTING:
        _USING[clone] -= 1
        if not _USING[clone]:
            del _USING[clone]
    with contextlib.suppress(OSError):
        os.utime(clone / "HEAD")


def _using(method: Any) -> Any:
    """Run *method* with its copy marked in use: nothing removes it meanwhile, and it counts as used just now."""

    @functools.wraps(method)
    async def run(self: BucketHistory, *args: Any, **kwargs: Any) -> Any:
        _use(self.clone)
        try:
            return await method(self, *args, **kwargs)
        finally:
            _used(self.clone)

    return run


class Staged:
    """A version of a file on its way to who asked for it: a file of the copy's, sent a piece at a time.

    The file is the copy's, counted with all else it holds, and the copy
    is in use, until the version is sent or let go: then the file is gone.
    It is held locked meanwhile.  One no process holds is what a request
    that died left: the next request to have the copy removes it.
    """

    def __init__(self, path: Path, held: int, size: int, clone: Path) -> None:
        self.size = size
        self._path, self._held = path, held
        _use(clone)
        # Let go with its last hold too: one made and never sent keeps neither its file nor its copy.
        self._let_go = weakref.finalize(self, _let_go, path, held, clone)

    async def send(self, take: Callable[[bytes], Awaitable[None]]) -> None:
        """Hand the version to *take* a piece at a time, each read off the loop; then it is gone, however that ended.

        Within the seconds a version has to be sent: past them it is
        stopped, and said to have been.
        """
        seconds = _send_seconds(self.size)
        try:
            async with asyncio.timeout(seconds):
                at = 0
                while at < self.size:
                    piece = await asyncio.to_thread(os.pread, self._held, min(_PIECE, self.size - at), at)
                    if not piece:
                        raise HistoryError("a version's file ended before the version did")
                    at += len(piece)
                    await take(piece)
        except TimeoutError as exc:
            raise Slow(f"a version took longer than {seconds:.0f}s to send") from exc
        finally:
            await self.gone()

    async def gone(self) -> None:
        """Let the version go, off the loop: a large file takes its time to leave a disk."""
        await asyncio.shield(asyncio.to_thread(self.close))

    def close(self) -> None:
        """Let the version go: its file removed, and its copy in use by one fewer.  Safe to repeat."""
        self._let_go()


@dataclass
class _Room:
    """The api's disk as one bringing-in of a copy reckons with it."""

    held: int  # what this copy holds there
    others: list[tuple[float, Path, int]]  # every other copy: when it was last used, and what it holds
    new: list[tuple[str, int, int]]  # the bucket's packs the copy lacks: each one's size there, and what it needs here
    stale: set[str]  # the packs a pruning took out of the bucket


class BucketHistory:
    """A project's history in its bucket, for the api."""

    def __init__(self, storage: Any, bucket: str, prefix: str, clone: Path, bounds: Bounds = Bounds()) -> None:
        self.storage, self.bucket, self.prefix, self.clone, self.bounds = storage, bucket, prefix, clone, bounds

    @classmethod
    def of(cls, storage: Any, master: Any, settings: Any = None) -> BucketHistory:
        """The history of the project whose master is *master*: its workspace prefix's ``_history/``.

        *settings* are the api's for its copies (``Settings.history``); none, the defaults.
        """
        bucket = master.config["storage_bucket"]
        prefix = boundary_workspace_prefix(master.config, master, master.id)
        key = hashlib.sha256(f"{bucket}/{prefix}".encode()).hexdigest()[:16]
        if settings is None:
            return cls(storage, bucket, prefix, CLONES / key)
        bounds = Bounds(settings.packs_bound, settings.file_bound, settings.copies_bound, settings.copy_idle)
        return cls(storage, bucket, prefix, (Path(settings.copies_path) if settings.copies_path else CLONES) / key, bounds)

    @_using
    async def held(self, blobs: Iterable[str | None]) -> set[str]:
        """Which of *blobs*, versions of files, the project's history still holds: the others were pruned."""
        wanted = sorted({_checked_id(b, "a version") for b in blobs if b})
        if not wanted:
            return set()
        return set((await self._brought(wanted))[1])

    @_using
    async def version(self, blob: str) -> Staged:
        """*blob*, a version of a file, written out to a file of the copy's: its caller sends it, or lets it go.

        ``NotKept`` once a pruning took it.  Refused in words where it is
        larger than a version may be, than the copy has room for, or than
        git writes out within its memory and its seconds.  Git, once it
        writes, ends in its own time: a version whose request left
        meanwhile is let go as it ends.
        """
        _checked_id(blob, "a version")
        writing = asyncio.ensure_future(self._staged(blob))
        _WRITING.add(writing)
        writing.add_done_callback(_WRITING.discard)
        try:
            return await asyncio.shield(writing)
        except asyncio.CancelledError:
            writing.add_done_callback(_unsent)
            raise

    @_using
    async def sync(self) -> str | None:
        """Bring the copy to the bucket's history; its ``main``, none before the project's first landing.

        Packs are named by their contents and never change, so only the new
        ones are read, and a pack a pruning took out goes from the copy too.
        The refs are read again only when the bucket says they changed.  A
        history that would pass a bound is refused before the bound is passed.
        One request has a copy at a time: another waits its patience for it,
        then is told to try again.  A copy on its way in is not given up
        with the request that began it: the next one finds it there.
        """
        return (await self._brought([]))[0]

    # ------------------------------------------------------------------
    # As a pod: a landing's steps, its put-backs and its settling
    # ------------------------------------------------------------------

    async def execute(self, owner: str, name: str, input: str, *, timeout: float | None = None) -> str:
        """A pod's ``_history`` command, run here: what a landing's steps, its put-backs and its settling call.

        Answered as a pod answers: a step's result, or its ``error`` in words.
        """
        request = json.loads(input or "{}")
        action = request.pop("action", None) if name == "_history" and isinstance(request, dict) else None
        if action not in _ACTIONS:
            return json.dumps({"error": f"Unknown history action: {action}"})
        try:
            return json.dumps(await getattr(self, action)(**request))
        except (HistoryError, TypeError, OSError) as exc:
            return json.dumps({"error": str(exc)})

    @_using
    async def fetch(self, commits: Iterable[str] = (), saga: str | None = None, since: str | None = None) -> dict:
        """``main`` in the bucket's history now, as a pod's look answers: ``{main, landing, hidden, packs, missing}``.

        ``landing`` is the landing of *saga* among ``main``'s own commits,
        looked for back to *since*, ``main`` as that landing began on it;
        ``hidden`` says it was not found and may be where the look did not
        reach.  The copy holds every commit the bucket does, so *commits*
        need no fetch: ``missing`` are those of them the history lacks.  A
        settle's look, as patient as a step: the step it settles may still
        be at work in the copy.
        """
        wanted = [_checked_id(commit, "a fetch") for commit in commits]
        if since is not None:
            _checked_id(since, "a fetch")

        async def look() -> dict:
            main, _ = await self._current([])
            missing = await self._lacked(wanted) if wanted and main is not None else wanted
            landing, hidden = await self._landing_of(saga, main, since) if main is not None and saga else (None, False)
            packs = await asyncio.to_thread(_packs, self.clone)
            return {"main": main, "landing": landing, "hidden": hidden, "packs": packs, "missing": missing}

        return await self._held(look, patient=True)

    @_using
    async def edits(self, paths: Iterable[str]) -> dict:
        """What the real files at *paths* changed since ``main``: ``{main, picked_up}``, each ``{path, before, after}``.

        Your edits to the files a landing by you is about to write, as its
        pickup records them.  Only those files: the rest of your edits are
        the next landing's to pick up.  Nothing of them is kept here: the
        pickup reads each again.  ``main`` is the bucket's now, None before
        the project's first landing, when no edit is told from it.
        """
        wanted = sorted(set(paths))
        for path in wanted:
            self._key(path)

        async def look() -> dict:
            main, _ = await self._current([])
            if main is None:
                return {"main": None, "picked_up": []}
            recorded = await self._entries(main, wanted)
            picked = []
            for path in wanted:
                before, after = recorded.get(path, (None, None))[1], await self._real(path)
                if before != after:
                    picked.append({"path": path, "before": before, "after": after})
            return {"main": main, "picked_up": picked}

        return await self._held(look)

    @_using
    async def pickup(self, *, main: str, picked_up: list[dict], author: dict[str, str], trailers: list[list[str]]) -> dict:
        """Commit *picked_up* on *main*, by *author*, and push it: ``{main, commit, picked_up, packs}``, as a pod's pickup answers.

        Your edits as :meth:`edits` told of them, recorded on ``main``
        before a file of them is written over: a put-back, by any holder of
        the project's lock, then finds the version it writes back in the
        history.  Each file is read again as it is kept, and must still be
        what was told of: one saved since is refused, and nothing is pushed.
        Refused where ``main`` moved since *main*; safe to repeat, since a
        pickup already pushed is found by its saga.
        """
        _checked_id(main, "a pickup")
        self._changes(picked_up, "a pickup")
        saga = _saga_of(trailers)

        async def push() -> dict:
            now, _ = await self._current([])
            if now != main:
                pushed = (await self._landing_of(saga, now, main))[0] if now is not None else None
                if pushed is None:
                    raise HistoryConflict("main moved in the project's history since the pickup began")
                commit = pushed
            else:
                async with self._apart() as (scratch, new):
                    for change in picked_up:
                        if await self._real(change["path"], new) != change["after"]:
                            raise HistoryConflict(f"{change['path']} changed while it was picked up")
                    commit = await self._commit(main, picked_up, author, "Your changes", trailers, scratch, new)
                    await self._push(commit, expect=main, scratch=scratch, new=new)
            return {"main": main, "commit": commit, "picked_up": picked_up, "packs": await asyncio.to_thread(_packs, self.clone)}

        return await self._held(push, patient=True)

    @_using
    async def apply(self, path: str, before: str | None, after: str | None) -> dict:
        """Make the real file at *path* its version *after*, if it is still *before*: None is no file.

        Safe to repeat: a real file that is already *after* is left as it
        is.  A bucket has no folders of its own to make, so ``made`` is none.
        """
        self._changes([{"path": path, "before": before, "after": after}], "an apply")

        async def write() -> dict:
            kept = (await self._current([blob for blob in (before, after) if blob]))[1]
            real = await self._real(path)
            if real != after:
                if real != before:
                    raise HistoryConflict(f"{path} changed since the landing began")
                await self._write(path, after, kept, there=real is not None)
            return {"path": path, "before": before, "after": after, "made": []}

        return await self._held(write, patient=True)

    @_using
    async def unapply(
        self, path: str, before: str | None, after: str | None, *, ran: bool = True, made: Iterable[str] = (),
    ) -> dict:
        """Put back *path*'s version from before the landing, where the real file is still the landing's.

        Safe to repeat: a real file that is already *before* is left as it
        is.  A real file that is neither is someone else's change: a
        conflict, unless the apply failed (*ran* false), and then it found
        the file changed and wrote nothing.  A bucket has no folder of its
        own to take away, so *made* is none of its to read.
        """
        self._changes([{"path": path, "before": before, "after": after}], "a put-back")

        async def write() -> dict:
            kept = (await self._current([blob for blob in (before, after) if blob]))[1]
            real = await self._real(path)
            if real != before:
                if real == after:
                    await self._write(path, before, kept, there=real is not None)
                elif ran:
                    raise HistoryConflict(f"{path} changed after the landing wrote it")
            return {"path": path, "before": before, "after": after}

        return await self._held(write, patient=True)

    @_using
    async def record(
        self, *, applied: list[dict], author: dict[str, str], trailers: list[list[str]], main: str | None,
    ) -> dict:
        """Write the landing on ``main`` and push it, ``main``'s files with *applied*: the moment it counts.

        Its one parent is *main*: a landing by the user has no thread's
        turn to merge.  Refused where ``main`` moved since *main*; safe to
        repeat, since a landing already pushed is found by its saga.
        """
        if main is not None:
            _checked_id(main, "a record")
        self._changes(applied, "a record")
        saga, title = _saga_of(trailers), str(dict(map(tuple, trailers)).get("Surogate-Kind", "landing")).capitalize()

        async def push() -> dict:
            now, _ = await self._current([])
            if now != main:
                pushed = (await self._landing_of(saga, now, main))[0] if now is not None else None
                if pushed is None:
                    raise HistoryConflict("main moved in the project's history since the landing began")
                return {"commit": pushed}
            if main is None:
                raise HistoryError("the project has no history yet for a landing to be recorded in")
            async with self._apart() as (scratch, new):
                commit = await self._commit(main, applied, author, title, trailers, scratch, new)
                await self._push(commit, expect=main, scratch=scratch, new=new)
            return {"commit": commit}

        return await self._held(push, patient=True)

    # ------------------------------------------------------------------
    # The real files, and a commit's, for whoever lands here
    # ------------------------------------------------------------------

    @_using
    async def real(self, path: str) -> str | None:
        """The blob id of the real file at *path*, None when there is none.

        The file goes to the api's disk a piece at a time and is hashed
        there: one larger than a version may be is refused in words.
        """
        self._key(path)

        async def read() -> str | None:
            await self._current([])
            return await self._real(path)

        return await self._held(read)

    @_using
    async def recorded(self, main: str | None, path: str) -> str | None:
        """The blob of *path* in the files of *main*, a commit of the history's; None when it has none there."""
        self._key(path)
        if main is None:
            return None
        _checked_id(main, "a commit")

        async def read() -> str | None:
            await self._current([])
            return (await self._entries(main, [path])).get(path, (None, None))[1]

        return await self._held(read)

    @_using
    async def takes(self, path: str) -> bool:
        """Whether the real files can take a file at *path*: no folder is there, and no file where a folder of it would be."""
        return await self._fits(self._key(path))

    def _brought(self, wanted: list[str]) -> Awaitable[tuple[str | None, dict[str, int]]]:
        """The copy brought to the bucket's history and asked which of *wanted* it holds, by a task its request does not end."""
        bringing = asyncio.ensure_future(self._bring(wanted))
        # One its request left is ended by no one: what it raises is then no one's to hear.
        bringing.add_done_callback(lambda done: done.cancelled() or done.exception())
        return asyncio.shield(bringing)

    async def _bring(self, wanted: list[str]) -> tuple[str | None, dict[str, int]]:
        async with _alone(self.clone):
            return await self._current(wanted)

    async def _current(self, wanted: list[str]) -> tuple[str | None, dict[str, int]]:
        """Bring the copy, which is this request's alone, to the bucket's history; its ``main``, and those of *wanted* it holds."""
        await self._opened()
        # The refs first: a push writes its pack before packed-refs, so each commit they name is in a pack listed after.
        there, main = await self._refs()
        if not there:
            return None, {}
        entries = await self.storage.list_entries(self.bucket, f"{self._durable}objects/pack/", limit=_PACKS_MOST + 1)
        if len(entries) > _PACKS_MOST:
            raise HistoryError(TOO_LARGE)
        room = await asyncio.to_thread(_reckoned, self.clone, entries, self.bounds)
        for stem, size, need in room.new:
            await self._take(stem, size, need, room)
        if room.stale:
            await asyncio.to_thread(_cleared, self.clone / "objects" / "pack", room.stale)
        # Asked while the copy is this request's: one project runs one git at a time.
        return main, (await self._asked(wanted) if wanted else {})

    async def _staged(self, blob: str) -> Staged:
        """The version *blob* written out by git, while the copy is this request's alone.

        Long work, as a pack is: it takes a turn that brings copies in,
        never one that asks a copy that is in.
        """
        async with _alone(self.clone):
            size = (await self._current([blob]))[1].get(blob)
            if size is None:
                raise NotKept(NOT_KEPT)
            return await self._written_out(blob, size)

    async def _written_out(self, blob: str, size: int) -> Staged:
        """The version *blob*, of *size* bytes, written out by git to a file of the copy's, which is this request's alone."""
        staged = Staged(*await asyncio.to_thread(_room_for, self.clone, size, self.bounds), size, self.clone)
        try:
            await self._git(_BRINGING, _seconds(size), "cat-file", "blob", blob, into=staged._held)
            if os.fstat(staged._held).st_size != size:
                raise HistoryError("git cat-file wrote a version out at another size than the history holds it at")
        except BaseException as exc:
            await staged.gone()
            # Past git's memory, or its seconds, it is a version larger than is read here: said as the bounds are.
            if isinstance(exc, Slow) or (isinstance(exc, HistoryError) and said(exc) is not None):
                raise HistoryError(VERSION_TOO_LARGE) from exc
            raise
        return staged

    async def _held(self, work: Callable[[], Awaitable[T]], *, patient: bool = False) -> T:
        """Run *work* with the copy this request's alone, to its end.

        The copy is waited for as a request waits for one; a landing's
        step is *patient*, and waits as long as its saga gives it, since
        the step before it may still be at work there.  One that leaves
        while it waits begins nothing.  Once it has the copy its work ends
        in its own time, though its request leaves: what it began to write
        is never left half made with the copy let go under it, and
        whoever has the copy next finds what it wrote.
        """
        hold = _alone(self.clone, patient=patient)
        await hold.__aenter__()

        async def to_its_end() -> T:
            try:
                return await work()
            finally:
                await hold.__aexit__(None, None, None)

        working = asyncio.ensure_future(to_its_end())
        # One its request left is ended by no one: what it raises is then no one's to hear.
        working.add_done_callback(lambda done: done.cancelled() or done.exception())
        return await asyncio.shield(working)

    @property
    def _durable(self) -> str:
        return f"{self.prefix}_history/"

    def _key(self, path: str) -> str:
        """*path*'s key among the real files; refused where it is none of the project's files, which no landing writes."""
        if not landable(path):
            raise HistoryError(f"{path!r} is not one of the project's files")
        return f"{self.prefix}{path}"

    def _changes(self, changes: list[dict], where: str) -> None:
        """Refuse *changes*, each ``{path, before, after}``, unless each path is a file of the project's and each version an id."""
        for change in changes:
            self._key(change["path"])
            for blob in (change.get("before"), change.get("after")):
                if blob is not None:
                    _checked_id(blob, where)

    async def _opened(self) -> None:
        """Make the copy's own repository, where there is none yet: bare, with no hook of any template's."""
        if not (self.clone / "HEAD").exists():
            await _child(_BRINGING, _READ_SECONDS, ["git", "init", "-q", "--bare", "--template=", str(self.clone)], env=_environ({}))

    @contextlib.contextmanager
    def _scratch(self) -> Any:
        """A folder of the copy's for a file on its way, gone with the block."""
        folder = Path(tempfile.mkdtemp(prefix="scratch-", dir=self.clone))
        try:
            yield folder
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    async def _refs(self) -> tuple[bool, str | None]:
        """Bring the copy's ``packed-refs`` and ``shallow`` to the bucket's; whether the history has any yet, and its ``main``.

        What the bucket says of the two files is asked first, and noted with
        the copy once they are read.  While it says the same, they are not
        read again: the copy has them, or the refusal they were met with,
        which is said again as it was.
        """
        seen = await self._seen()
        if seen["packed-refs"] is None:
            return False, None
        note = self.clone / _SEEN
        noted = _noted(note)
        if noted.get("seen") == seen:
            if noted.get("refused"):
                raise HistoryError(noted["refused"])
            return True, noted.get("main")
        try:
            main = await self._read(seen)
        except (Busy, Slow):
            raise
        except HistoryError as exc:
            _replace(note, json.dumps({"seen": seen, "refused": str(exc)}).encode())
            raise
        _replace(note, json.dumps({"seen": seen, "main": main}).encode())
        return True, main

    async def _seen(self) -> dict[str, list | None]:
        """What the bucket says of the history's ``packed-refs`` and ``shallow`` now: None for one it has not."""
        seen: dict[str, list | None] = {}
        for name in ("packed-refs", "shallow"):
            try:
                of = await self.storage.stat(self.bucket, f"{self._durable}{name}")
            except KeyError:
                seen[name] = None
            else:
                seen[name] = [of["size"], str(of.get("modified")), of.get("etag")]
        return seen

    async def _read(self, seen: dict[str, list | None]) -> str | None:
        """Read the bucket's refs into the copy, each id and ref checked; its ``main``.  Refused past what a history's hold.

        In the refs' one turn, from the bucket to the check: so the api
        holds a piece of one file on its way and a line of one file, for
        every project asked at once together.
        """
        if any(of and of[0] > REFS_BOUND for of in seen.values()):
            raise HistoryError(TOO_LARGE)
        loop = asyncio.get_running_loop()
        with self._scratch() as scratch:

            def read() -> str | None:
                coming = asyncio.run_coroutine_threadsafe(asyncio.wait_for(self._fetched(seen, scratch), _READ_SECONDS), loop)
                try:
                    coming.result(_READ_SECONDS + _PATIENCE)
                except TimeoutError as exc:
                    coming.cancel()
                    raise Slow(f"the project's refs took longer than {_READ_SECONDS:.0f}s to read") from exc
                return _checked(scratch, self.clone)

            return await _turn(_REFS, read)

    async def _fetched(self, seen: dict[str, list | None], scratch: Path) -> None:
        """The bucket's refs, those it has, each a file in *scratch*."""
        for name, of in seen.items():
            if of is None:
                continue
            try:
                await self.storage.download(self.bucket, f"{self._durable}{name}", scratch / name, limit=REFS_BOUND)
            except KeyError:
                continue  # gone since it was asked of
            except TooLarge as exc:
                raise HistoryError(TOO_LARGE) from exc

    async def _take(self, stem: str, size: int, need: int, room: _Room) -> None:
        """Copy the bucket's pack *stem* into the copy, and make its index here.

        Git reads a pack only through its index, and a thread's command can
        write the bucket's: the copy's own is made from the pack, so an id
        names the bytes it is the hash of.  A pack git cannot read is left
        out, and not read again.  One the copy has no room for, with the
        index it would have, or that git cannot bring in within its memory
        or its seconds, is refused in words and marked, so that it is not
        read again to be refused again.
        """
        pack = self.clone / "objects" / "pack"
        if room.held + need > self.bounds.packs:
            raise HistoryError(TOO_LARGE)
        await self._make_room(room, need)
        with self._scratch() as scratch:
            staged = scratch / f"{stem}.pack"
            try:
                size = await self.storage.download(self.bucket, f"{self._durable}objects/pack/{stem}.pack", staged, limit=size)
            except KeyError:
                return  # gone since the listing: a pruning's
            except TooLarge as exc:
                raise HistoryError("refused the project's history: a pack grew while it was read") from exc
            # A pack says how many objects it holds, and an index is as large as they are many, whatever they hold.
            whole = size + _index_size(staged, size)
            if room.held + whole > self.bounds.packs:
                (pack / f"{stem}.over").write_text(str(whole))
                raise HistoryError(TOO_LARGE)
            await self._make_room(room, whole)
            seconds = _seconds(size)
            try:
                # Indexed beside no other pack: git would open every pack the copy has to index each new one.  And
                # with no second index of its own beside the first: the copy keeps, and has counted, a pack and one index.
                await self._git(_BRINGING, seconds, "-c", "pack.writeReverseIndex=false", "index-pack", str(staged), objects=scratch)
            except Busy:
                raise
            except Slow as exc:
                logger.warning("A pack of %s was not brought in within %.0fs, and is not tried for a while: %s", self._durable, seconds, stem)
                (pack / f"{stem}.slow").write_text(str(seconds))
                raise HistoryError(TOO_LARGE) from exc
            except HistoryError as exc:
                if said(exc) is not None:
                    (pack / f"{stem}.large").write_text(str(_GIT_MEMORY))
                    raise
                logger.warning("A pack of %s cannot be read, and is left out: %s", self._durable, stem)
                (pack / f"{stem}.bad").touch()
                return
            taken = sum((scratch / f"{stem}.{kind}").stat().st_size for kind in ("pack", "idx"))
            for kind in ("pack", "idx"):  # the index last: git reads a pack through it
                os.replace(scratch / f"{stem}.{kind}", pack / f"{stem}.{kind}")
            (pack / f"{stem}.bucket").touch()
            for kind in ("large", "slow", "over"):
                (pack / f"{stem}.{kind}").unlink(missing_ok=True)
            room.held += taken

    async def _make_room(self, room: _Room, need: int) -> None:
        """Remove other copies, the least recently used first, while the copies with *need* more of this one pass their bound."""
        if room.held + need + sum(size for _, _, size in room.others) > self.bounds.copies:
            await asyncio.to_thread(_make_room, room.others, room.held + need, self.bounds)

    async def _asked(self, wanted: list[str]) -> dict[str, int]:
        """Those of the versions *wanted* the copy holds, each with the bytes it holds of it."""
        asked = "".join(f"{blob}\n" for blob in wanted).encode()
        out = await self._git(
            _READING, _READ_SECONDS, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)", input=asked,
        )
        return {blob: int(size) for blob, kind, size in (line.split() for line in out.splitlines() if " blob " in line)}

    async def _lacked(self, commits: list[str]) -> list[str]:
        """Those of *commits* the copy does not hold as commits."""
        out = await self._git(
            _READING, _READ_SECONDS, "cat-file", "--batch-check=%(objectname) %(objecttype)",
            input="".join(f"{commit}\n" for commit in commits).encode(),
        )
        have = {line.split()[0] for line in out.splitlines() if line.endswith(" commit")}
        return [commit for commit in commits if commit not in have]

    async def _landing_of(self, saga: str, main: str, since: str | None) -> tuple[str | None, bool]:
        """The landing of *saga* among ``main``'s own commits back to *since*, and whether it may be where the look did not reach.

        As a pod looks: by first parents alone, the newest commit that
        carries the saga, its name whole as its trailer has it.  Not found,
        it did not push where the look met *since*, or the first commit
        ``main`` ever had.  Where the look ended at a pruning's cut
        instead, or at the most commits a look reads, it may be behind.
        """
        out = await self._git(
            _READING, _READ_SECONDS, "log", "--first-parent", f"--max-count={_LOOKED_MOST + 1}",
            "--format=%H %(trailers:key=Surogate-Saga,valueonly,separator=%x2C)", "--end-of-options", main,
        )
        own = [line.partition(" ")[::2] for line in out.splitlines()]
        for commit, carried in own[:_LOOKED_MOST]:
            if carried == saga:
                return commit, False
            if commit == since:
                return None, False
        if len(own) > _LOOKED_MOST:
            return None, True
        return None, await asyncio.to_thread(_cut_at, self.clone / "shallow", own[-1][0])

    async def _entries(self, commit: str, paths: list[str]) -> dict[str, tuple[str, str]]:
        """Each of *paths* that is a file in *commit*'s files: its mode there, and its blob.

        A path is asked of git as it is spelt, never as a pattern.
        """
        found: dict[str, tuple[str, str]] = {}
        for at in range(0, len(paths), _ASKED_AT_ONCE):
            asked = paths[at:at + _ASKED_AT_ONCE]
            out = await self._git(_READING, _READ_SECONDS, "ls-tree", "-z", commit, "--", *asked)
            for entry in out.split("\0"):
                meta, _, path = entry.partition("\t")
                if path in asked and meta.split()[1] == "blob":
                    found[path] = (meta.split()[0], meta.split()[2])
        return found

    @contextlib.asynccontextmanager
    async def _apart(self) -> AsyncIterator[tuple[Path, Path]]:
        """A folder of the copy's for a commit on its way, and in it one for the objects it is made of; gone with the block.

        Git writes the commit's objects there, apart from the copy's own:
        so the copy holds only what the bucket holds, and what goes up is
        what the bucket lacks.  Counted with the copy meanwhile, and
        removed off the loop: a large file's bytes are among them.
        """
        scratch = Path(tempfile.mkdtemp(prefix="scratch-", dir=self.clone))
        try:
            new = scratch / "objects"
            (new / "pack").mkdir(parents=True)
            yield scratch, new
        finally:
            await asyncio.shield(asyncio.to_thread(shutil.rmtree, scratch, True))

    @contextlib.asynccontextmanager
    async def _on_its_way(self, size: int, *, beside: int = 0, too_large: str = VERSION_TOO_LARGE) -> AsyncIterator[Staged]:
        """A file of the copy's for *size* bytes on their way to or from the real files; gone with the block, however it ends."""
        made = await asyncio.to_thread(functools.partial(_room_for, self.clone, size, self.bounds, beside=beside, too_large=too_large))
        staged = Staged(*made, size, self.clone)
        try:
            yield staged
        finally:
            await staged.gone()

    async def _real(self, path: str, new: Path | None = None) -> str | None:
        """The blob id of the real file at *path*, None when there is none; kept among the objects in *new*, when given.

        The file goes to a file of the copy's a piece at a time, within
        what a version may be and what the copy has room for, and is
        hashed there: by the api, or by git as it keeps it.  History keeps
        a file's bytes as they are.  One kept is counted twice on its way:
        its object, then the pack that takes it to the bucket.
        """
        key = self._key(path)
        try:
            size = (await self.storage.stat(self.bucket, key))["size"]
        except KeyError:
            return None
        async with self._on_its_way(size, beside=size if new else 0, too_large=f"{path} is larger {_HERE}") as staged:
            try:
                await self.storage.download(self.bucket, key, staged._path, limit=size)
            except KeyError:
                return None  # gone since it was asked of
            except TooLarge as exc:
                raise HistoryConflict(f"{path} changed while it was read") from exc
            if new is None:
                return await asyncio.to_thread(_blob_id, staged._path)
            return await self._git(_BRINGING, _seconds(size), "hash-object", "-w", "--no-filters", "--", str(staged._path), new=new)

    async def _write(self, path: str, blob: str | None, kept: dict[str, int], *, there: bool) -> None:
        """Make the real file at *path* the version *blob*, one of *kept*, the versions the copy holds; take it away for None.

        A version goes from the copy to the bucket through a file of the
        copy's, a piece at a time.  A file is written where one is *there*
        already, or where the real files can take one.
        """
        key = self._key(path)
        if blob is None:
            await self.storage.delete(self.bucket, key)
            # A store may swallow a delete's failure: the landing must not record what did not happen.
            if await self.storage.exists(self.bucket, key):
                raise HistoryError(f"{path} could not be deleted")
            return
        if blob not in kept:
            raise NotKept(NOT_KEPT)
        if not there and not await self._fits(key):
            raise HistoryConflict(f"{path} cannot be written: a folder is there, or a file where its folder would be")
        staged = await self._written_out(blob, kept[blob])
        try:
            await self.storage.upload(self.bucket, key, staged._path)
        finally:
            await staged.gone()

    async def _fits(self, key: str) -> bool:
        """Whether the real files can take a file at *key*, as a landing asks before it writes one."""
        if await self.storage.list_entries(self.bucket, f"{key}/", limit=1):
            return False
        folders = key[len(self.prefix):].split("/")[:-1]
        for depth in range(1, len(folders) + 1):
            if await self.storage.exists(self.bucket, f"{self.prefix}{'/'.join(folders[:depth])}"):
                return False
        return True

    async def _commit(
        self, parent: str, changes: list[dict], author: dict[str, str], title: str, trailers: list[list[str]],
        scratch: Path, new: Path,
    ) -> str:
        """A commit on *parent*, by *author*, of its files with *changes*, each ``{path, after}``: None takes the file away.

        Its objects go among those in *new*.  A file keeps the mode
        *parent* has for its path, and is a plain file where it has none.
        The paths reach git on its input, and the message too: a trailer
        may hold any name.
        """
        index = scratch / "index"
        modes = await self._entries(parent, [change["path"] for change in changes])
        entries = "".join(
            f"0 {_ZERO}\t{change['path']}\0" if change["after"] is None
            else f"{modes.get(change['path'], ('100644',))[0]} {change['after']}\t{change['path']}\0"
            for change in changes
        )
        await self._git(_READING, _READ_SECONDS, "read-tree", parent, index=index, new=new)
        await self._git(_READING, _READ_SECONDS, "update-index", "-z", "--index-info", input=entries.encode(), index=index, new=new)
        tree = await self._git(_READING, _READ_SECONDS, "write-tree", index=index, new=new)
        message = f"{title}\n\n{_block(trailers)}\n".encode()
        return await self._git(_READING, _READ_SECONDS, *_as(author), "commit-tree", tree, "-p", parent, "-F", "-", input=message, new=new)

    async def _push(self, commit: str, *, expect: str, scratch: Path, new: Path) -> None:
        """Make the bucket's ``main`` *commit*, where it is still *expect*: a pack of what the bucket lacks, then ``packed-refs``.

        As a pod pushes.  The pack is made apart, of the objects in *new*
        alone, and is the copy's too once the bucket has it.  Only the
        holder of the project's lock pushes; a lock can be lost unseen, so
        the bucket is asked of its refs again right before they are
        written: a push that finds them moved while its pack went up writes
        none of its own over them.  The rewrite of ``packed-refs`` is the
        moment a push counts.
        """
        size = await asyncio.to_thread(_size, new)
        # Counted before it is made: the pack is the copy's once it is the bucket's.
        await asyncio.to_thread(_room, self.clone, size, self.bounds, TOO_LARGE)
        outgoing = scratch / "outgoing"
        outgoing.mkdir()
        name = await self._git(
            _BRINGING, _seconds(size), "-c", "pack.writeReverseIndex=false", "pack-objects", "--revs", "--local", "-q",
            str(outgoing / "pack"), input=f"{commit}\n^{expect}\n".encode(), new=new,
        )
        if not _ID.fullmatch(name):
            raise HistoryError("git pack-objects named no pack")
        pack = self.clone / "objects" / "pack"
        for kind in ("pack", "idx"):  # the index last: a pod reads a pack only through it
            await self.storage.upload(self.bucket, f"{self._durable}objects/pack/pack-{name}.{kind}", outgoing / f"pack-{name}.{kind}")
        for kind in ("pack", "idx"):
            os.replace(outgoing / f"pack-{name}.{kind}", pack / f"pack-{name}.{kind}")
        (pack / f"pack-{name}.bucket").touch()
        if await self._seen() != _noted(self.clone / _SEEN).get("seen"):
            raise HistoryConflict("the project's history moved while it was pushed")
        refs = scratch / "packed-refs"
        await asyncio.to_thread(_moved, self.clone / "packed-refs", refs, commit)
        await self.storage.upload(self.bucket, f"{self._durable}packed-refs", refs)

    async def _git(
        self, turns: ThreadPoolExecutor, seconds: float, *args: str,
        input: bytes | None = None, objects: Path | None = None, into: int | None = None,
        new: Path | None = None, index: Path | None = None,
    ) -> str:
        """Run git in the copy, over its objects or those in *objects*, in one of *turns* and within *seconds*.

        Its output; none where it is written to the open file *into*.  With
        *new*, it reads the copy's objects and writes its own to that
        folder, apart; with *index*, that file is its index.
        """
        env = {"GIT_DIR": str(self.clone), **({"GIT_OBJECT_DIRECTORY": str(objects)} if objects else {})}
        if new is not None:
            env.update(GIT_OBJECT_DIRECTORY=str(new), GIT_ALTERNATE_OBJECT_DIRECTORIES=str(self.clone / "objects"))
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        out = await _child(
            turns, seconds, ["git", *_BOUNDED, *args], input=input, env=_environ(env), cwd=self.clone,
            stdout=subprocess.PIPE if into is None else into,
        )
        return out.decode().removesuffix("\n")


async def _turn(turns: ThreadPoolExecutor, run: Callable[[], T]) -> T:
    """Run *run* in one of *turns*; its answer.  Told to try again when its turn has not come within a request's patience."""
    turn = turns.submit(run)
    answer = asyncio.wrap_future(turn)
    await asyncio.wait({answer}, timeout=_PATIENCE)
    # One not yet begun is taken back: it never runs.  One that began is waited for.
    if turn.cancel():
        raise Busy(BUSY)
    return await answer


async def _child(turns: ThreadPoolExecutor, seconds: float, command: list[str], **how: Any) -> bytes:
    """Run *command*, git, as a child within its address space, in one of *turns*; its output.

    Past *seconds* it is stopped, and gone before its turn is given back.
    Refused in words when it stopped for memory.
    """

    def run() -> subprocess.CompletedProcess:
        # The shell sets the limit on itself and becomes git: the limit is git's, in kibibytes, and no code runs between.
        limited = ["sh", "-c", 'ulimit -v "$0" && exec "$@"', str(_GIT_MEMORY // 1024), *command]
        return subprocess.run(limited, stderr=subprocess.PIPE, timeout=seconds, **{"stdout": subprocess.PIPE, **how})

    try:
        result = await _turn(turns, run)
    except subprocess.TimeoutExpired as exc:
        raise Slow(f"git {_named(command)} took longer than {seconds:.0f}s") from exc
    if result.returncode != 0:
        words = result.stderr.decode(errors="replace").strip()
        if _NO_MEMORY.search(words):
            logger.warning("git %s stopped within %d MiB: %s", _named(command), _GIT_MEMORY >> 20, words)
            raise HistoryError(_FILE_TOO_LARGE)
        raise HistoryError(f"git {_named(command)} failed: {words}")
    return result.stdout or b""


def landable(path: str) -> bool:
    """Whether *path* names one of a project's files as a landing writes them: by its own name, in no folder of
    the platform's, none the history leaves out, and neither climbing nor naming a folder."""
    try:
        path.encode()
    except UnicodeEncodeError:
        return False
    return bool(path) and "\0" not in path and all(part not in ("", ".", "..") for part in path.split("/")) and tracked(path)


def _saga_of(trailers: list[list[str]]) -> str:
    """The saga a commit with *trailers* says it is of; refused when it names none."""
    saga = dict(map(tuple, trailers)).get("Surogate-Saga")
    if not saga:
        raise HistoryError("a landing's commit names its saga")
    return str(saga)


def _blob_id(source: Path) -> str:
    """The git blob id of the file *source*, read a piece at a time: history keeps a file's bytes as they are."""
    with open(source, "rb") as file:
        digest = hashlib.sha1(b"blob %d\0" % os.fstat(file.fileno()).st_size)
        while piece := file.read(_PIECE):
            digest.update(piece)
    return digest.hexdigest()


def _packs(clone: Path) -> int:
    """The bytes of the copy *clone*'s packs: what a pruning's bound is sized from."""
    total = 0
    with contextlib.suppress(OSError), os.scandir(clone / "objects" / "pack") as found:
        for entry in found:
            if entry.name.endswith(".pack"):
                with contextlib.suppress(OSError):
                    total += entry.stat(follow_symlinks=False).st_size
    return total


def _cut_at(shallow: Path, commit: str) -> bool:
    """Whether the copy's *shallow* names *commit*: a pruning cut the history there."""
    try:
        with open(shallow, "rb") as cuts:
            return any(line.strip() == commit.encode() for line in cuts)
    except OSError:
        return False


def _moved(refs: Path, target: Path, main: str) -> None:
    """Write to *target* the copy's ``packed-refs``, *refs*, with ``main`` at *main*: a line at a time, in the order it has."""
    branch, moved = f" {MAIN}\n".encode(), False
    with open(refs, "rb") as source, open(target, "wb") as out:
        for raw in source:
            if raw.endswith(branch) and len(raw) == 40 + len(branch):
                raw, moved = main.encode() + branch, True
            out.write(raw)
    if not moved:
        raise HistoryError("the project's history has no main to move")


def _named(command: list[str]) -> str:
    """The git command *command* runs, past its settings."""
    words = iter(command[1:])
    for word in words:
        if word != "-c":
            return word
        next(words, None)
    return "git"


def _seconds(size: int) -> float:
    """The seconds git has to bring in a pack of *size* bytes, or to write out a version of as many."""
    return min(_TAKE_MOST, max(_TAKE_LEAST, size / _TAKE_RATE))


def _send_seconds(size: int) -> float:
    """The seconds a version of *size* bytes has to be sent."""
    return max(_SEND_LEAST, size / _SEND_RATE)


def _let_go(path: Path, held: int, clone: Path) -> None:
    """Remove the version written out at *path*, close its handle *held*, and count its copy in use by one fewer."""
    with contextlib.suppress(OSError):
        os.unlink(path)
    with contextlib.suppress(OSError):
        os.close(held)
    _used(clone)


def _unsent(written: asyncio.Future) -> None:
    """Let go, off the loop, the version *written* out for a request that left before it ended."""
    if not written.cancelled() and written.exception() is None:
        asyncio.get_running_loop().run_in_executor(None, written.result().close)


def _room(clone: Path, need: int, bounds: Bounds, too_large: str) -> None:
    """Make sure the copy *clone* has room for *need* more bytes, with all else it holds.

    Refused in the words *too_large* where it has none.  Told to try
    again where it has room for them only once the files on their way in
    and out of it have gone.  Other copies make room for them as they do
    for a pack; and where those left cannot go, in use or used just now,
    it waits for room too: nothing is written past what the copies may
    hold together.
    """
    held, going = _size(clone), _size(clone / _OUT)
    if held - going + need > min(bounds.packs, bounds.copies):
        raise HistoryError(too_large)
    if held + need > bounds.packs:
        raise Busy(BUSY)
    # With what the copies in use hold, which are removed for no one.
    mine = held + need + sum(_size(other) for other in clone.parent.iterdir() if other != clone and _USING.get(other) and other.is_dir())
    others = _others(clone, bounds)
    _make_room(others, mine, bounds)
    if mine + sum(kept for _, _, kept in others) > bounds.copies:
        raise Busy(BUSY)


def _room_for(clone: Path, size: int, bounds: Bounds, *, beside: int = 0, too_large: str = VERSION_TOO_LARGE) -> tuple[Path, int]:
    """A file of the copy *clone* for *size* bytes on their way: its path, and its handle, locked.

    A version of a file written out, or a real file read in.  Refused in
    the words *too_large* where it is larger than a version may be, or
    than the copy has room for with the *beside* bytes it brings with it
    (:func:`_room`).
    """
    if size > bounds.file:
        raise HistoryError(too_large)
    _room(clone, size + beside, bounds, too_large)
    out = clone / _OUT
    out.mkdir(exist_ok=True)
    path = out / os.urandom(8).hex()
    handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC, 0o600)
    # Held while it is this process's to send: a request that has the copy tells by it what a dead one left.
    fcntl.flock(handle, fcntl.LOCK_EX)
    return path, handle


def _swept(clone: Path) -> None:
    """Remove the versions written out of the copy *clone* that no process holds: what a request that died left."""
    with contextlib.suppress(OSError), os.scandir(clone / _OUT) as found:
        for entry in found:
            with contextlib.suppress(OSError):
                held = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    if _locked(held):
                        os.unlink(entry.path)
                finally:
                    os.close(held)


def _index_size(pack: Path, size: int) -> int:
    """The bytes of the index git makes of *pack*, of *size* bytes, by the number of objects it says it holds."""
    with open(pack, "rb") as file:
        head = file.read(12)
    objects = int.from_bytes(head[8:12], "big") if head[:4] == b"PACK" and len(head) == 12 else 0
    # A table of 256 counts, then an id, a checksum and a place for each object, a longer place past 2 GiB, and two hashes.
    return 1072 + objects * (28 + (8 if size >= 2**31 else 0))


def _lock_of(clone: Path) -> Path:
    """The file a copy is held by, beside it: it outlives the copy, so a copy removed and made anew is held by the same."""
    return clone.with_name(f".{clone.name}.lock")


@contextlib.asynccontextmanager
async def _alone(clone: Path, *, patient: bool = False) -> AsyncIterator[None]:
    """Hold the copy *clone* alone, among this process's requests and any other process's on this disk.

    One that finds it held waits its patience, then is told to try again;
    a *patient* one waits for as long as its caller does.
    """
    clone.parent.mkdir(parents=True, exist_ok=True)
    held = os.open(_lock_of(clone), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        until = None if patient else time.monotonic() + _PATIENCE
        while not _locked(held):
            if until is not None and time.monotonic() >= until:
                raise Busy(BUSY)
            await asyncio.sleep(_LOCK_POLL)
        yield
    finally:
        os.close(held)


def _locked(held: int) -> bool:
    """Take the lock of the open file *held*, when no one has it."""
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _noted(note: Path) -> dict:
    """What the copy noted in *note*; nothing when it noted nothing it can read."""
    try:
        noted = json.loads(note.read_text())
    except (OSError, ValueError):
        return {}
    return noted if isinstance(noted, dict) else {}


def _marked(mark: Path) -> float | None:
    """The number the mark *mark* holds, None when there is no such mark."""
    try:
        return float(mark.read_text())
    except (OSError, ValueError):
        return None


def _lines(source: BinaryIO, name: str) -> Iterator[bytes]:
    """The lines of a history's *name*, one held at a time; refused at a line longer than a history's holds."""
    while raw := source.readline(_LINE_MOST + 1):
        if len(raw) > _LINE_MOST:
            raise HistoryError(f"refused the project's history: its {name} holds a line longer than a ref's")
        yield raw


def _checked(scratch: Path, clone: Path) -> str | None:
    """Make the copy's own ``packed-refs`` and ``shallow`` of the bucket's, read into *scratch*; the history's ``main``.

    A thread's command can write either file, so every id and ref is
    checked as a pod checks them.  Each is read and written a line at a
    time, never held whole: so the refs must come in order, each once, as
    every writer of a history writes them (a pod's push, and a pruning's
    ``git pack-refs``), since git is told they are sorted.
    """
    main, last, branch = None, b"", MAIN.encode()
    staged = scratch / ".~packed-refs"
    with open(staged, "wb") as out:
        out.write(_PACKED.encode())
        if (scratch / "packed-refs").is_file():
            with open(scratch / "packed-refs", "rb") as source:
                for raw in _lines(source, "packed-refs"):
                    if (plain := _REF_LINE.fullmatch(raw)) and b".." not in plain[1]:
                        ref = plain[1]
                    else:
                        # A line that is no plain ref: a comment, a tag's own commit, or what a pod's check refuses.
                        line = raw.decode(errors="replace").removesuffix("\n")
                        if not line or line.startswith("#"):
                            continue
                        sha, _, name = line.partition(" ")
                        if sha.startswith("^"):
                            _checked_id(sha[1:], "its packed-refs")
                            continue
                        raw = f"{_checked_id(sha, 'its packed-refs')} {_checked_ref(name, 'its packed-refs')}\n".encode()
                        ref = name.encode()
                    if ref <= last:
                        raise HistoryError("refused the project's history: its packed-refs is not in order")
                    last = ref
                    out.write(raw)
                    if ref == branch:
                        main = raw[:40].decode()
    cut = scratch / ".~shallow"
    if (scratch / "shallow").is_file():
        with open(scratch / "shallow", "rb") as source, open(cut, "wb") as out:
            for raw in _lines(source, "shallow"):
                if _CUT_LINE.fullmatch(raw):
                    out.write(raw)
                    continue
                for commit in raw.decode(errors="replace").split():
                    out.write(f"{_checked_id(commit, 'its shallow')}\n".encode())
    # Both checked: only now does the copy take either.
    if cut.is_file() and cut.stat().st_size:
        os.replace(cut, clone / "shallow")
    else:
        (clone / "shallow").unlink(missing_ok=True)
    os.replace(staged, clone / "packed-refs")
    return main


def _reckoned(clone: Path, entries: list[dict], bounds: Bounds) -> _Room:
    """What bringing the copy *clone* to the packs the bucket lists as *entries* takes of the api's disk.

    Refused in words, before any pack is read, when the copy would pass
    its bound, and when a pack it lacks is one git was stopped on.  What a
    killed request left on its way in is removed: no request has the copy
    but this one.  So is a version one left on its way out.
    """
    pack = clone / "objects" / "pack"
    pack.mkdir(parents=True, exist_ok=True)
    for left in clone.glob("scratch-*"):
        shutil.rmtree(left, ignore_errors=True)
    _swept(clone)
    listed = {PurePosixPath(e["key"]).name: e["size"] for e in entries}
    # A pack whose index has not gone up yet is one its push has not finished.
    sizes = {n[:-5]: size for n, size in listed.items() if _PACK.fullmatch(n) and n.endswith(".pack") and f"{n[:-5]}.idx" in listed}
    # Each pack of the bucket's the copy has, could not read, or was stopped on, is marked.
    marks: dict[str, set[str]] = {}
    with os.scandir(pack) as found:
        for entry in found:
            stem, _, kind = entry.name.rpartition(".")
            if kind in _MARKS:
                marks.setdefault(stem, set()).add(kind)
    new = sorted(stem for stem in sizes if not marks.get(stem, set()) & {"bucket", "bad"})
    need = {}
    for stem in new:
        # One git stopped on is not read again: for memory while git has no more, for time until a while has passed.
        if (_marked(pack / f"{stem}.large") or 0) >= _GIT_MEMORY:
            raise HistoryError(_FILE_TOO_LARGE)
        if (_marked(pack / f"{stem}.slow") or 0) >= _seconds(sizes[stem]) and time.time() - (pack / f"{stem}.slow").stat().st_mtime < _SLOW_AGAIN:
            raise HistoryError(TOO_LARGE)
        # One found to need more than the bucket holds of it, with its index, is counted for that.
        need[stem] = max(sizes[stem], int(_marked(pack / f"{stem}.over") or 0))
    stale = set(marks) - set(sizes)
    if not new:
        # Nothing to bring in takes no room: the copies are not measured, only those not used for a while removed.
        _others(clone, bounds, measured=False)
        return _Room(0, [], [], stale)
    held, going = _size(clone), _cleared(pack, stale, remove=False)
    if held - going + sum(need.values()) > bounds.packs:
        # With room once the versions on their way out of the copy have gone, it is only told to try again.
        if held - going - _size(clone / _OUT) + sum(need.values()) <= bounds.packs:
            raise Busy(BUSY)
        raise HistoryError(TOO_LARGE)
    if held + sum(need.values()) > bounds.packs:
        # The copy has room for the new packs only without those a pruning took: they go first.
        held, stale = held - _cleared(pack, stale), set()
    return _Room(held, _others(clone, bounds), [(stem, sizes[stem], need[stem]) for stem in new], stale)


def _cleared(pack: Path, stems: set[str], *, remove: bool = True) -> int:
    """Take from the copy's *pack* folder the packs *stems*, with their indexes and marks; their bytes.  Only counted, without *remove*."""
    freed = 0
    if not stems:
        return freed
    with os.scandir(pack) as found:
        for entry in found:
            if entry.name.rpartition(".")[0] not in stems:
                continue
            with contextlib.suppress(OSError):
                freed += entry.stat(follow_symlinks=False).st_size
                if remove:
                    os.unlink(entry.path)
    return freed


def _others(mine: Path, bounds: Bounds, *, measured: bool = True) -> list[tuple[float, Path, int]]:
    """The other copies on the api's disk, the least recently used first: when each was used, and what it holds.

    Those not used for a while are removed.  Never one this process is
    using: a request may be reading it.  Without *measured*, what each
    holds is not reckoned.
    """
    now, others = time.time(), []
    for clone in mine.parent.iterdir():
        if clone == mine or _USING.get(clone) or not clone.is_dir():
            continue
        try:
            used = (clone / "HEAD").stat().st_mtime
        except OSError:
            used = 0.0  # no repository: one half made, or half removed
        if now - used > bounds.idle:
            _remove(clone)
        else:
            others.append((used, clone, _size(clone) if measured else 0))
    return sorted(others)


def _make_room(others: list[tuple[float, Path, int]], mine: int, bounds: Bounds) -> None:
    """Remove of *others* the least recently used, while the copies, with the *mine* bytes of this one, pass their bound.

    Never one used within the last minute: a request may be reading it.
    Those removed are taken out of *others*.
    """
    now, total = time.time(), mine + sum(size for _, _, size in others)
    for other in list(others):
        used, clone, size = other
        if total <= bounds.copies or now - used < _GRACE:
            break
        if _remove(clone):
            others.remove(other)
            total -= size


def _size(folder: Path) -> int:
    """The bytes *folder* holds on the api's disk: every file under it, whatever it is."""
    total = 0
    with contextlib.suppress(OSError), os.scandir(folder) as found:
        for entry in found:
            with contextlib.suppress(OSError):
                total += _size(Path(entry.path)) if entry.is_dir(follow_symlinks=False) else entry.stat(follow_symlinks=False).st_size
    return total


def _remove(clone: Path) -> bool:
    """Take *clone* away, unless a request is bringing it in; whether it went.

    Renamed first, so a request that comes meanwhile finds none and makes it anew.
    """
    if clone.name.startswith("."):
        shutil.rmtree(clone, ignore_errors=True)  # one a removal left half done
        return True
    try:
        held = os.open(_lock_of(clone), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    except OSError:
        return False
    try:
        if not _locked(held):
            return False
        gone = clone.with_name(f".gone-{os.urandom(4).hex()}")
        with contextlib.suppress(OSError):
            os.replace(clone, gone)
            shutil.rmtree(gone, ignore_errors=True)
        return True
    finally:
        os.close(held)
