"""A project's history as the api reaches it: through the storage backend.

The api has no mount of the project's files.  It reads the project's
history, ``_history/`` in the bucket, as data, into a bare repository of
its own on its disk: the packs, ``packed-refs`` and ``shallow``, every id
and ref checked as a pod checks them (``History._take``), and nothing else.
Git runs only in that copy, under its own ``HEAD`` and config: a
``config``, ``HEAD`` or hook a thread's command wrote into the bucket never
reaches it, and each pack's index is made here, never copied.

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
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, TypeVar

from surogates.sandbox.history import _ID, _PACKED, _REF, MAIN, HistoryError, _checked_id, _checked_ref, _environ, _replace
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
#: What a request is told when it waited its patience for a copy, or for a turn: nothing is wrong with the history.
BUSY = "This project's history is being read just now. Try again in a moment."
#: What the copy notes of the bucket's refs: what the bucket said of each file when it was last read.
_SEEN = "refs-seen"
#: The copies this process is reading now, which nothing removes.
_USING: Counter[Path] = Counter()
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
    and all else it holds.  *copies* is every copy's together.
    """

    packs: int = 4 * 2**30
    copies: int = 8 * 2**30
    idle: float = 600.0


class Busy(HistoryError):
    """The copy, or a turn, was another request's for longer than this one waits."""


class Slow(HistoryError):
    """Git took longer than it is given, and was stopped."""


def said(error: object) -> str | None:
    """*error*'s words when it is a bound's refusal, which a user is told as it is; None for any other."""
    words = str(error)
    return words if words.endswith(_HERE) else None


def _using(method: Any) -> Any:
    """Run *method* with its copy marked in use: nothing removes it meanwhile, and it counts as used just now."""

    @functools.wraps(method)
    async def run(self: BucketHistory, *args: Any, **kwargs: Any) -> Any:
        _USING[self.clone] += 1
        try:
            return await method(self, *args, **kwargs)
        finally:
            _USING[self.clone] -= 1
            if not _USING[self.clone]:
                del _USING[self.clone]
            with contextlib.suppress(OSError):
                os.utime(self.clone / "HEAD")

    return run


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
        bounds = Bounds(settings.packs_bound, settings.copies_bound, settings.copy_idle)
        return cls(storage, bucket, prefix, (Path(settings.copies_path) if settings.copies_path else CLONES) / key, bounds)

    @_using
    async def held(self, blobs: Iterable[str | None]) -> set[str]:
        """Which of *blobs*, versions of files, the project's history still holds: the others were pruned."""
        wanted = sorted({_checked_id(b, "a version") for b in blobs if b})
        if not wanted:
            return set()
        return (await self._brought(wanted))[1]

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

    def _brought(self, wanted: list[str]) -> Awaitable[tuple[str | None, set[str]]]:
        """The copy brought to the bucket's history and asked which of *wanted* it holds, by a task its request does not end."""
        bringing = asyncio.ensure_future(self._bring(wanted))
        # One its request left is ended by no one: what it raises is then no one's to hear.
        bringing.add_done_callback(lambda done: done.cancelled() or done.exception())
        return asyncio.shield(bringing)

    async def _bring(self, wanted: list[str]) -> tuple[str | None, set[str]]:
        async with _alone(self.clone):
            await self._opened()
            # The refs first: a push writes its pack before packed-refs, so each commit they name is in a pack listed after.
            there, main = await self._refs()
            if not there:
                return None, set()
            entries = await self.storage.list_entries(self.bucket, f"{self._durable}objects/pack/", limit=_PACKS_MOST + 1)
            if len(entries) > _PACKS_MOST:
                raise HistoryError(TOO_LARGE)
            room = await asyncio.to_thread(_reckoned, self.clone, entries, self.bounds)
            for stem, size, need in room.new:
                await self._take(stem, size, need, room)
            if room.stale:
                await asyncio.to_thread(_cleared, self.clone / "objects" / "pack", room.stale)
            # Asked while the copy is this request's: one project runs one git at a time.
            return main, (await self._asked(wanted) if wanted else set())

    @property
    def _durable(self) -> str:
        return f"{self.prefix}_history/"

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
        seen: dict[str, list | None] = {}
        for name in ("packed-refs", "shallow"):
            try:
                of = await self.storage.stat(self.bucket, f"{self._durable}{name}")
            except KeyError:
                seen[name] = None
            else:
                seen[name] = [of["size"], str(of.get("modified")), of.get("etag")]
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

    async def _asked(self, wanted: list[str]) -> set[str]:
        """Which of the versions *wanted* the copy holds."""
        asked = "".join(f"{blob}\n" for blob in wanted).encode()
        out = await self._git(_READING, _READ_SECONDS, "cat-file", "--batch-check=%(objectname) %(objecttype)", input=asked)
        return {line.split()[0] for line in out.splitlines() if line.endswith(" blob")}

    async def _git(
        self, turns: ThreadPoolExecutor, seconds: float, *args: str, input: bytes | None = None, objects: Path | None = None,
    ) -> str:
        """Run git in the copy, over its objects or those in *objects*, in one of *turns* and within *seconds*; its output."""
        env = {"GIT_DIR": str(self.clone), **({"GIT_OBJECT_DIRECTORY": str(objects)} if objects else {})}
        out = await _child(turns, seconds, ["git", *_BOUNDED, *args], input=input, env=_environ(env), cwd=self.clone)
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
        return subprocess.run(limited, capture_output=True, timeout=seconds, **how)

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
    return result.stdout


def _named(command: list[str]) -> str:
    """The git command *command* runs, past its settings."""
    words = iter(command[1:])
    for word in words:
        if word != "-c":
            return word
        next(words, None)
    return "git"


def _seconds(size: int) -> float:
    """The seconds git has to bring in a pack of *size* bytes."""
    return min(_TAKE_MOST, max(_TAKE_LEAST, size / _TAKE_RATE))


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
async def _alone(clone: Path) -> AsyncIterator[None]:
    """Hold the copy *clone* alone, among this process's requests and any other process's on this disk.

    One that finds it held waits its patience, then is told to try again.
    """
    clone.parent.mkdir(parents=True, exist_ok=True)
    held = os.open(_lock_of(clone), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        until = time.monotonic() + _PATIENCE
        while not _locked(held):
            if time.monotonic() >= until:
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
    but this one.
    """
    pack = clone / "objects" / "pack"
    pack.mkdir(parents=True, exist_ok=True)
    for left in clone.glob("scratch-*"):
        shutil.rmtree(left, ignore_errors=True)
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
