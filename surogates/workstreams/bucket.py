"""A project's history as the api reaches it: through the storage backend.

The api has no mount of the project's files.  It reads the project's
history, ``_history/`` in the bucket, as data, into a bare repository of
its own on its disk: the packs, ``packed-refs`` and ``shallow``, every id
and ref checked as a pod checks them (``History._take``), and nothing else.
Git runs only in that copy, under its own ``HEAD`` and config: a
``config``, ``HEAD`` or hook a thread's command wrote into the bucket never
reaches it, and each pack's index is made here, never copied.

The api is one process for every tenant, so what a project's history costs
it is bounded, whatever the project holds:

- nothing of a history is held whole in its memory: a pack, ``packed-refs``
  and ``shallow`` each go from the bucket to its disk a piece at a time,
  each within a bound;
- git runs as its child, a few at once and each within an address space of
  its own, so a history git cannot read in that much is refused, not read;
- a copy is brought in by one request at a time, and one that finds it held
  waits a few seconds, then is told to try again;
- a copy not used for a while is removed, and the copies together are kept
  within a bound of their own.

Each bound is refused in words that say why.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import functools
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from surogates.sandbox.history import HistoryError, _checked_id, _checked_ref, _environ, _packed, _replace
from surogates.storage.backend import TooLarge
from surogates.storage.tenant import boundary_workspace_prefix

logger = logging.getLogger(__name__)

#: Where the api keeps its copies of projects' histories, one folder a project, unless its settings say.
CLONES = Path(tempfile.gettempdir()) / "surogates-history"
#: A pack of the history, as the bucket names it.
_PACK = re.compile(r"pack-[0-9a-f]{40}\.(pack|idx)")
_GIT_TIMEOUT = 600
#: Git's own memory, as the api's child: one thread, no object over 32 MiB held whole to index, and a
#: pack read through 32 MiB windows, 64 MiB of them at once.
_BOUNDED = (
    "-c", "pack.threads=1", "-c", "core.bigFileThreshold=32m", "-c", "core.deltaBaseCacheLimit=32m",
    "-c", "pack.windowMemory=32m", "-c", "core.packedGitWindowSize=32m", "-c", "core.packedGitLimit=64m",
)
#: The git children this process runs at once, and the address space each is given.  Those settings keep
#: git far under it, but for an object a pod's git stored as a change to another: git holds that one
#: whole, with what it changes, whatever it is told.  Past its address space git stops, and the history
#: is refused in words.  Together they are what every project's histories cost the api's memory at most.
_GIT_SLOTS = 4
_GIT_MEMORY = 256 * 2**20
#: How git says it stopped for memory.
_NO_MEMORY = re.compile(r"out of memory|cannot allocate memory|mmap failed", re.IGNORECASE)
#: The most a history's ``packed-refs`` or ``shallow`` holds: a thread's command can write either.
_REFS_BOUND = 16 * 2**20
#: The most names a history's packs are listed by, a pack and its index each: a thread's command can
#: write names there, and a pruning leaves one pack.
_PACKS_MOST = 10_000
#: How long a request waits for a copy another request is bringing in, or for git's turn, before it is
#: told to try again: within what a page gives a History to answer.
_PATIENCE = 8.0
_LOCK_POLL = 0.05
#: A copy used this recently is never removed to make room: a request may be reading it.
_GRACE = 60.0
#: How a bound's refusal ends: said to the user as it is.
_HERE = "than Surogate can read here."
TOO_LARGE = f"This project's history is larger {_HERE}"
_FILE_TOO_LARGE = f"This project's history holds a file larger {_HERE}"
#: What a request is told when it waited its patience for a copy, or for git: nothing is wrong with the history.
BUSY = "This project's history is being read just now. Try again in a moment."
#: The copies this process is reading now, which nothing removes.
_USING: Counter[Path] = Counter()
#: Git's turns: a child runs in one of these threads, so no more run at once than there are.
_GIT = ThreadPoolExecutor(max_workers=_GIT_SLOTS, thread_name_prefix="history-git")


@dataclass(frozen=True)
class Bounds:
    """What the api spends on a project's history: bytes, and the seconds a copy stays unused."""

    packs: int = 4 * 2**30
    copies: int = 8 * 2**30
    idle: float = 600.0


class Busy(HistoryError):
    """The copy, or git, was another request's for longer than this one waits."""


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
        await self.sync()
        out = await self._git("cat-file", "--batch-check=%(objectname) %(objecttype)", input="".join(f"{b}\n" for b in wanted).encode())
        return {line.split()[0] for line in out.splitlines() if line.endswith(" blob")}

    @_using
    async def sync(self) -> dict[str, str]:
        """Bring the copy to the bucket's history; its refs, none before the project's first landing.

        Packs are named by their contents and never change, so only the new
        ones are read, and a pack a pruning took out goes from the copy too.
        A history whose packs pass the bound is refused before any is read.
        One request brings a copy in at a time: another waits its patience
        for it, then is told to try again.  A copy on its way in is not given
        up with the request that began it: the next one finds it there.
        """
        bringing = asyncio.ensure_future(self._brought())
        # One its request left is ended by no one: what it raises is then no one's to hear.
        bringing.add_done_callback(lambda done: done.cancelled() or done.exception())
        return await asyncio.shield(bringing)

    async def _brought(self) -> dict[str, str]:
        async with _alone(self.clone):
            await self._opened()
            # The refs first: a push writes its pack before packed-refs, so each commit they name is in a pack listed after.
            text = await self._small("packed-refs")
            if text is None:
                return {}
            refs = _refs(text)
            shallow = (await self._small("shallow") or "").split()
            entries = await self.storage.list_entries(self.bucket, f"{self._durable}objects/pack/", limit=_PACKS_MOST + 1)
            if len(entries) > _PACKS_MOST:
                raise HistoryError(TOO_LARGE)
            listed = {PurePosixPath(e["key"]).name: e["size"] for e in entries}
            # A pack whose index has not gone up yet is one its push has not finished.
            sizes = {n[:-5]: size for n, size in listed.items() if _PACK.fullmatch(n) and n.endswith(".pack") and f"{n[:-5]}.idx" in listed}
            if sum(sizes.values()) > self.bounds.packs:
                raise HistoryError(TOO_LARGE)
            pack = self.clone / "objects" / "pack"
            pack.mkdir(parents=True, exist_ok=True)
            # Each pack of the bucket's the copy has, or could not read, is marked.
            marked = {mark.stem for kind in ("bucket", "bad") for mark in pack.glob(f"*.{kind}")}
            new = sorted(set(sizes) - marked)
            # So is one git stopped on for memory: it is not read again while git has no more.
            stopped = {mark.stem for mark in pack.glob("*.large")}
            if any(_stopped(pack / f"{stem}.large") for stem in stopped & set(new)):
                raise HistoryError(_FILE_TOO_LARGE)
            await asyncio.to_thread(_make_room, self.clone, sum(sizes[stem] for stem in new), self.bounds)
            for stem in new:
                await self._take(stem, sizes[stem])
            for stem in (marked | stopped) - set(sizes):
                for old in pack.glob(f"{stem}.*"):
                    old.unlink(missing_ok=True)
            if shallow:
                _replace(self.clone / "shallow", "".join(f"{_checked_id(c, 'its shallow')}\n" for c in shallow).encode())
            else:
                (self.clone / "shallow").unlink(missing_ok=True)
            _replace(self.clone / "packed-refs", _packed(refs))
            return refs

    @property
    def _durable(self) -> str:
        return f"{self.prefix}_history/"

    async def _opened(self) -> None:
        """Make the copy's own repository, where there is none yet: bare, with no hook of any template's."""
        if not (self.clone / "HEAD").exists():
            await _child(["git", "init", "-q", "--bare", "--template=", str(self.clone)], env=_environ({}))

    @contextlib.contextmanager
    def _scratch(self) -> Any:
        """A folder of the copy's for a file on its way, gone with the block."""
        folder = Path(tempfile.mkdtemp(prefix="scratch-", dir=self.clone))
        try:
            yield folder
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    async def _small(self, name: str) -> str | None:
        """The history's *name* as text, None when it has none; refused past what a history's holds."""
        with self._scratch() as scratch:
            try:
                await self.storage.download(self.bucket, f"{self._durable}{name}", scratch / name, limit=_REFS_BOUND)
            except KeyError:
                return None
            except TooLarge as exc:
                raise HistoryError(f"refused the project's history: its {name} is larger than a history's") from exc
            return (scratch / name).read_text(errors="replace")

    async def _take(self, stem: str, size: int) -> None:
        """Copy the bucket's pack *stem* into the copy, and make its index here.

        Git reads a pack only through its index, and a thread's command can
        write the bucket's: the copy's own is made from the pack, so an id
        names the bytes it is the hash of.  A pack git cannot read is left
        out, and not read again.
        """
        pack = self.clone / "objects" / "pack"
        with self._scratch() as scratch:
            staged = scratch / f"{stem}.pack"
            try:
                await self.storage.download(self.bucket, f"{self._durable}objects/pack/{stem}.pack", staged, limit=size)
            except KeyError:
                return  # gone since the listing: a pruning's
            except TooLarge as exc:
                raise HistoryError("refused the project's history: a pack grew while it was read") from exc
            try:
                # Indexed beside no other pack: git would open every pack the copy has to index each new one.
                await self._git("index-pack", str(staged), objects=scratch)
            except Busy:
                raise
            except HistoryError as exc:
                if said(exc) is not None:
                    (pack / f"{stem}.large").write_text(str(_GIT_MEMORY))
                    raise
                logger.warning("A pack of %s cannot be read, and is left out: %s", self._durable, stem)
                (pack / f"{stem}.bad").touch()
                return
            for kind in ("pack", "idx"):  # the index last: git reads a pack through it
                os.replace(scratch / f"{stem}.{kind}", pack / f"{stem}.{kind}")
            (pack / f"{stem}.bucket").touch()
            (pack / f"{stem}.large").unlink(missing_ok=True)

    async def _git(self, *args: str, input: bytes | None = None, objects: Path | None = None) -> str:
        """Run git in the copy, over its objects or those in *objects*; its output."""
        env = {"GIT_DIR": str(self.clone), **({"GIT_OBJECT_DIRECTORY": str(objects)} if objects else {})}
        out = await _child(["git", *_BOUNDED, *args], input=input, env=_environ(env), cwd=self.clone)
        return out.decode().removesuffix("\n")


async def _child(command: list[str], **how: Any) -> bytes:
    """Run *command*, git, as a child within its address space, in its turn; its output.

    Refused in words when its turn has not come within a request's patience,
    and when git stopped for memory.
    """

    def run() -> subprocess.CompletedProcess:
        # The shell sets the limit on itself and becomes git: the limit is git's, in kibibytes, and no code runs between.
        limited = ["sh", "-c", 'ulimit -v "$0" && exec "$@"', str(_GIT_MEMORY // 1024), *command]
        return subprocess.run(limited, capture_output=True, timeout=_GIT_TIMEOUT, **how)

    turn = _GIT.submit(run)
    answer = asyncio.wrap_future(turn)
    try:
        await asyncio.wait({answer}, timeout=_PATIENCE)
        # One not yet begun is taken back: it never runs.  One that began is waited for.
        if turn.cancel():
            raise Busy(BUSY)
        result = await answer
    except subprocess.TimeoutExpired as exc:
        raise HistoryError(f"git {_named(command)} timed out after {_GIT_TIMEOUT}s") from exc
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


def _stopped(mark: Path) -> bool:
    """Whether *mark* says git stopped on its pack for memory, with as much as it has now."""
    try:
        return int(mark.read_text()) >= _GIT_MEMORY
    except (OSError, ValueError):
        return False


def _refs(text: str) -> dict[str, str]:
    """The refs of a ``packed-refs``, every id and ref checked as a pod checks them: a thread's command can write it."""
    refs = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        sha, _, ref = line.partition(" ")
        if sha.startswith("^"):
            _checked_id(sha[1:], "its packed-refs")
            continue
        refs[_checked_ref(ref, "its packed-refs")] = _checked_id(sha, "its packed-refs")
    return refs


def _make_room(mine: Path, need: int, bounds: Bounds) -> None:
    """Remove the copies not used for a while, then the least recently used while the copies, with *need* more, pass their bound.

    Never *mine*, one this process is using, or one used within the last
    minute: a request may be reading it.
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
            others.append((used, clone, _size(clone)))
    total = need + _size(mine) + sum(size for _, _, size in others)
    for used, clone, size in sorted(others):
        if total <= bounds.copies or now - used < _GRACE:
            break
        if _remove(clone):
            total -= size


def _size(clone: Path) -> int:
    """The bytes of *clone*'s packs, which are nearly all of it."""
    return sum(p.stat().st_size for p in (clone / "objects" / "pack").glob("*.pack"))


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
