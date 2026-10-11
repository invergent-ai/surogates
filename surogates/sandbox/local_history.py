"""A folder's history and a thread's copy on the user's computer.

The cloud's :class:`History`, run by git in the desktop's VM: the guest's
agent runs this file as the guest's root, outside every chat's namespaces,
one request a run.  A folder's place in the app's data is laid out as a
pod sees a project:

    <store>/history.git/       the folder's history: packs and packed-refs, each written whole
    <store>/clones/<thread>/   a thread's own repository, which borrows the history's objects
    <store>/threads/<thread>/  the thread's copy, where its tools and commands work
    <store>/threads/<thread>.making   there while that copy is being made, and gone once its making has ended
    <store>/waiting/<thread>/  the platform's own folders of a copy that was let go, until the copy is made again
    <store>/set-aside/         a copy or a repository that was made again, as it was, under a name no thread has
    <store>/pruning.git/       the history as a pruning cuts it, its files linked, there while the pruning runs

and the folder itself, shared read-only, is the real files.  Git reads the
history where it lies: it is on this computer's disk, and no thread's
command can reach it.  A thread's repository holds only what the history
lacks: it reads the history's objects as its own, the folder's first commit
is the history's first, pushed with the folder's first copy, and a push is
followed by a repack that lets go of what the history now holds.  A
landing's applies are not git's here: the desktop's file helper writes the
folder, and the record takes what it applied.

The history is pruned as the cloud's is, once a day by the day of whoever
asks, after a landing.  Every repository in the place reads it, a thread's
and one set aside whole alike, so a pruning cuts it only to what each of
them still reads, and lets an old pack go only once each reads all it names
without it.

A place outlives the guest that wrote it, and nothing one boot's guest left
there is trusted by the next.  Before its first git, each run puts right
whatever would lead git out of the place or have it run a program, makes
again a repository it finds redirected, and overrides the rest.

A copy outlives its turn too, and a request can be cut at any step: by its
bound, a stop, a lost guest.  The history is safe by itself after one.  No
request writes a file of the folder; a copy cut short in its making is made
again before anything is read from it; and a push that counted is caught up
with, a landing's by making the copy the landing's files, before the copy is
read as anything's base.  So is a clean copy's move to ``main`` that was cut
between its files and its base.  What history leaves out in a copy, a
``__pycache__/`` or a ``.env``, outlives the turn that wrote it too: a turn's
write there is only what is new or written again since the copy last
started from its base, and never what the harness keeps there.

Nothing a thread made is removed to make its copy again.  A copy that is
not whole, or a repository that is not, is renamed into the place's
set-aside folder as it is, where no request reads it as a thread's.  Only
what holds nothing of the thread's own is removed: a copy whose making did
not end, which no thread ever worked in, and one that is, file for file,
what it is made again from.  So is a copy its thread let go once idle, where
its branch holds every file of it: its next open makes it again from there.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from itertools import takewhile
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, NoReturn

from surogates.sandbox.history import (
    _ATTRIBUTES,
    _CHECKPOINT,
    _ID,
    _OPEN_TIMEOUT,
    _TIMEOUT,
    _ZERO,
    FAILED,
    HISTORY_CAP,
    HISTORY_EXCLUDES,
    HISTORY_REFUSED,
    MAIN,
    History,
    HistoryError,
    _as,
    _checked_id,
    _name,
    _opened_folder,
    _replace,
)

#: What a request did that a person reading the guest's log later must be able to find.
logger = logging.getLogger(__name__)

#: Why a request was not answered, beside the cloud's two codes (history.py): a thread with no
#: whole copy, which its next open makes; a file whose name history cannot record, which
#: nothing lands past until it is renamed; a request that is none this history takes; and a
#: landing the history holds whose copy could not be made its files, or a move of the copy to
#: ``main`` that could not be finished, so that nothing is read from the copy; a landing
#: neither recorded nor put back whole, whose kept files are not to be forgotten; and a
#: snapshot the copy is not put back to, taken on another base than the copy's is now.
#: Whoever asked goes by the code, never by the words.
NO_WHOLE_COPY = "no_whole_copy"
NAME_NOT_UTF8 = "name_not_utf8"
NOT_A_REQUEST = "not_a_request"
RECORD_UNFINISHED = "record_unfinished"
MOVE_UNFINISHED = "move_unfinished"
LANDING_UNSETTLED = "landing_unsettled"
NOT_ON_BASE = "not_on_base"

#: On a folder of the user's these are the platform's: the whiteboard's
#: canvas, the harness's own files, and where a coding tool checks a
#: repository out in a thread's copy.  ``_artifacts/`` and ``_history/``
#: there are the user's.
LOCAL_PLATFORM = ("_whiteboard/", ".surogates-results/", ".threads/")
LOCAL_EXCLUDES = [e for e in HISTORY_EXCLUDES if not e.startswith("/")] + [f"/{folder}" for folder in LOCAL_PLATFORM]

#: A thread's repository's config, whole: what ``git init`` and this module set, and nothing else.
_CONFIG = (
    "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n\tlogallrefupdates = true\n"
    "\tcheckStat = minimal\n[user]\n\tname = Surogates Checkpoint\n\temail = surogates@local\n"
)
#: The folder's history's, as a push writes it.
_STORE_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
#: Over any config a file holds: no hook, monitor, signing program or transport but a path,
#: no maintenance a commit would leave running behind its request, and no step into a
#: submodule's repository, whose config is its own; and the copy's files are another user's
#: in the guest than git's.
_OVERRIDES = {
    "core.hooksPath": "/dev/null", "core.fsmonitor": "false", "commit.gpgSign": "false",
    "gc.auto": "0", "maintenance.auto": "false", "fetch.recurseSubmodules": "false", "submodule.recurse": "false",
    "safe.directory": "*", "core.checkStat": "minimal",
    # The history's objects are read as packs alone: nothing beside them steers a walk of them.
    "core.commitGraph": "false", "core.multiPackIndex": "false", "pack.useBitmaps": "false",
}
#: What git reads in a repository to find another, or a program: none is left in a thread's
#: repository or in the folder's history, but the one this module writes, a thread's
#: repository's alternates.
_REDIRECTS = ("commondir", "hooks", "modules", "objects/info/alternates", "objects/info/http-alternates")
#: Where a thread's repository names the object store it borrows, and that store, the folder's
#: history's, from the repository's own objects: the file's one line.
_BORROWS = "objects/info/alternates"
_ALTERNATES = "../../../history.git/objects\n"
#: A file in a thread's repository: each of the history's packs it may read objects from, by its id.
_BORROWED = "borrowed"
#: What the history's packs folder holds: a push's packs and their indexes.
_PACK = re.compile(r"pack-([0-9a-f]{40})\.(pack|idx)")
#: A file in a thread's repository once its first open has ended: without it, the repository's making was cut short.
_MADE = "made"
#: Beside a thread's copy, under the copy's name and this, from before the copy's first file is
#: written until its making has ended: a copy that has it was never worked in.
_MAKING = ".making"
#: What is removed for holding nothing of a thread's own is renamed so first, beside where it was:
#: a removal cut part way leaves nothing under a thread's name that is half of what it was.
_GOING = ".going"
#: A thread's repository is packed again at its turn's start once it holds more packs than this.
_PACKS = 20
#: Past this size a file is made no delta of another in a push's pack, nor at the repack after a push.
_DELTAS_BELOW = "1m"
#: A file in a thread's repository: what history left out of its copy when the copy last started from its base, each
#: name with what tells it from a later write of it (:meth:`LocalHistory._remember`).
_SEEN = "left-out"
#: The most of that file one request reads.
_SEEN_BYTES = 64 << 20
#: How a landing's own commit names each file it left out that no landing writes, its path spelt
#: as JSON: the copy keeps the thread's version of each (:meth:`LocalHistory.record`).
_LEFT = "Surogate-Left"
#: What a thread's copy held of its own when a record or a move made it other files is kept
#: for the last sixteen times that happened: the oldest goes for one more.
_ASIDE = 16
#: Where a place keeps a thread's copy and its repository that were made again, each renamed there as it was.
_SET_ASIDE = "set-aside"
#: The most one answer names of the files a pickup picked up, in characters of each as JSON spells it, which one
#: frame of the desktop's link carries with room: a part.  Every part after the first is asked for after it, from
#: the file it ended at (:meth:`LocalHistory.picked`).  A part holds one file at least, however long its name.
_PICKED_CHARS = 1 << 20
#: A thread's name is one name, of these alone: it names a folder of the place's, and none of another's.
_NAMED = "[A-Za-z0-9_-]{1,64}"
#: What one is named there: the order it was set aside in, counted over the place; when, by this
#: computer's clock, in UTC; whose it was; and which of the two it is.
_WHOLE = re.compile(rf"([0-9]{{8}})-[0-9]{{8}}T[0-9]{{6}}Z-({_NAMED})\.(?:copy|repository)(\.gone)?")
#: A thread keeps the last four times a copy or a repository of its own was set aside there, and a
#: place the last sixteen of all its threads', in this many bytes of its disk at most: the oldest
#: goes for one more, and never a thread's newest.  Of those of its own that went, a thread is told
#: the last sixteen.
_ASIDE_WHOLE = 4
_ASIDE_WHOLE_IN_ALL = 16
_ASIDE_BYTES = 10 << 30
_ASIDE_GONE = 16
#: Where a place keeps what a thread's copy that was let go held of the platform's own, under the thread's name.
_WAITING = "waiting"
#: Where a pruning cuts the folder's history, in the place beside it: a copy of it whose files are the history's own,
#: linked.  One a cut pruning left links packs the history may have let go, and the next request removes it.
_PRUNING = "pruning.git"
#: Where the cut's copy of the history names each commit a repository in the place reaches, while it is cut.
_READ_THERE = "refs/kept"
#: A pruning's day is told by a time of the asker's clock, in seconds: none for centuries is this or more.
_LATEST = 1 << 33
_PINNED = {
    "GIT_CONFIG_COUNT": str(len(_OVERRIDES)), "GIT_ALLOW_PROTOCOL": "file",
    **{f"GIT_CONFIG_KEY_{n}": key for n, key in enumerate(_OVERRIDES)},
    **{f"GIT_CONFIG_VALUE_{n}": value for n, value in enumerate(_OVERRIDES.values())},
}
_EXCLUDED = re.compile("|".join(
    f"^{_name(p.strip('/'))}/" if p.startswith("/") else
    f"(?:^|/){_name(p.rstrip('/'))}/" if p.endswith("/") else
    f"(?:^|/){_name(p)}(?:/|$)"
    for p in LOCAL_EXCLUDES
))

_THREAD = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: A ref a pruning may be told to keep: a thread's branch, or its base.
_KEPT = re.compile(rf"refs/(?:heads/threads|bases)/{_THREAD.pattern}")
_USER = re.compile(r"[A-Za-z0-9_.@-]{1,128}")
#: Each action a request may name, the method it runs and the arguments it takes.
_ACTIONS: dict[str, tuple[str, frozenset[str]]] = {
    "open": ("open", frozenset({"moves"})),
    "close": ("close", frozenset()),
    "changed": ("changed", frozenset()),
    "snapshot": ("snapshot", frozenset({"reason"})),
    "restore": ("restore", frozenset({"commit"})),
    "fetch": ("fetch", frozenset({"commits", "saga", "since"})),
    "pickup": ("pickup", frozenset({"author", "trailers"})),
    "picked": ("picked", frozenset({"pickup", "after"})),
    "commit": ("commit_turn", frozenset({"author", "trailers", "pickup"})),
    "record": ("record", frozenset({"turn", "applied", "author", "trailers", "main", "pickup", "left"})),
    "keep": ("keep", frozenset({"author", "trailers", "base"})),
    "forget": ("forget", frozenset({"saga", "applied"})),
    "prune": ("prune", frozenset({"keep", "now"})),
}
#: Why a folder's history takes no part in what the cloud's does besides.
_NO_WRITER = "a folder's history writes no file of the folder: the desktop's file helper lands a turn's files"
_NO_HELPER = "a thread on a computer has no helper with a copy of its own"


@dataclass(frozen=True)
class LocalHistory(History):
    store: Path | None = None  # the folder's history in the app's data
    #: Holds one entry once this history has put its place right (:meth:`_pin`): it lasts one request.
    pinned: list[bool] = field(default_factory=list, init=False, repr=False, compare=False)
    #: Holds one entry where this request found the thread's repository not borrowing the folder's
    #: history as it must (:meth:`_borrows`): no git runs in it, and the thread's next open makes it again.
    astray: list[bool] = field(default_factory=list, init=False, repr=False, compare=False)
    #: Holds, once a pruning has read them, the history's commits every repository in the place reaches from its refs,
    #: which the cut keeps (:meth:`_cut`), and those the history was cut at before.  It lasts one pruning.
    reached: list[set[str]] = field(default_factory=list, init=False, repr=False, compare=False)

    excludes: ClassVar[list[str]] = LOCAL_EXCLUDES
    platform: ClassVar[tuple[str, ...]] = LOCAL_PLATFORM
    # A thread's copy on a computer holds them from its first step on: the
    # turn's mark and what its tools keep, and the whiteboard's canvas.
    # Counted as a write history leaves out, they would hold every deletion
    # of every turn.
    harness: ClassVar[tuple[str, ...]] = (".surogates-results/", "_whiteboard/")

    def __post_init__(self) -> None:
        # The cloud tells a thread's pod its turn so that its copy takes up its helpers' hand-offs by it.
        if self.helper is not None or self.turn is not None:
            _refuse(_NO_HELPER)
        # Its name is a folder's in the place and part of one's where its copy is set aside: it leads to neither's.
        if not (isinstance(self.thread, str) and re.fullmatch(_NAMED, self.thread)):
            _refuse("it names no thread")

    @classmethod
    def at(cls, place: Path, folder: Path, *, thread: str, user: str) -> LocalHistory:
        """*thread*'s history in *place*, the folder's place in the app's data, over *folder*, the real files."""
        return cls(
            repo=place / "clones" / thread, project=folder, copy=place / "threads" / thread,
            thread=thread, user=user, store=place / "history.git",
        )

    @property
    def durable(self) -> Path:
        return self.store

    @property
    def landed(self) -> str:
        """The thread's landing its copy was last made the files of: a record is finished once this names it."""
        return f"refs/landed/{self.thread}"

    @property
    def moving(self) -> str:
        """Where a clean copy is being moved to, from before the first of its files moves until its base has."""
        return f"refs/moving/{self.thread}"

    @property
    def pushing(self) -> str:
        """The thread's branch as this repository last pushed it, named from before the push: a push cut before the repository noted it is its own."""
        return f"refs/pushing/{self.thread}"

    @property
    def aside(self) -> str:
        """Under it, what the copy held of its own when a record or a move made it other files: ``<nth>-<what it was made>``."""
        return f"refs/set-aside/{self.thread}"

    @property
    def making(self) -> Path:
        """The mark of the copy's making: there from before the copy's first file until the making has ended, and never beside a copy a thread worked in."""
        return self.copy.with_name(f"{self.copy.name}{_MAKING}")

    @property
    def aside_whole(self) -> Path:
        """Where the place keeps what was made again: a thread's copy or its repository, whole, as ``<nth>-<when>-<thread>.<which>``."""
        return self.store.parent / _SET_ASIDE

    @property
    def waiting(self) -> Path:
        """Where the platform's own folders of the thread's copy wait while it is let go, for it to be made again (:meth:`close`)."""
        return self.store.parent / _WAITING / self.thread

    @property
    def _taken(self) -> Path:
        """Git reads the folder's history in place: nothing is copied."""
        return self.store

    @property
    def _pruning(self) -> Path:
        """In the place, beside the history: where a thread's repository's files are, nothing of a pruning's is."""
        return self.store.parent / _PRUNING

    def _take(self) -> dict[str, str]:
        """The history's refs now, each checked as one of its own."""
        self._pin()
        self._check_durable()
        refs = self._durable_refs()
        if any(ref.startswith(("refs/handoff", "refs/helpers/")) for ref in refs):
            # None is written here, so none is taken up into a copy, by a turn no request names.
            raise HistoryError(
                "refused the project's history: it holds a hand-off, which a folder's history never does", code=HISTORY_REFUSED,
            )
        return refs

    def _init(self) -> None:
        """The cloud's, the repository borrowing the folder's history from before its first git: it holds no second copy of what the history holds."""
        if (self.repo / "HEAD").exists():
            return
        self.repo.mkdir(parents=True, exist_ok=True)
        with self._folder("objects", make=True):
            # Where its one alternate leads, there before git first looks: the history's first push makes the rest.
            pass
        self._borrow()
        self.astray.clear()
        super()._init()

    def _push(self, updates: dict[str, str | None], *, expect: dict[str, str | None]) -> None:
        """The cloud's push, and then the thread's repository lets go of what the history now holds.

        The branch it pushes is named first (:attr:`pushing`): the history's
        objects are the repository's to read, so only that tells a push cut
        before the repository noted it from a branch it never pushed
        (:meth:`_finish_record`).  The pack the push wrote is noted before
        anything is read from it (:meth:`_borrow`), and the repack lets go of
        every object the history holds.
        """
        if (branch := updates.get(self.branch)) is not None:
            self._main("update-ref", self.pushing, branch)
        super()._push(updates, expect=expect)
        self._borrow()
        self._pack(versions=False)

    def _fetch(self, *commits: str | None) -> None:
        """Nothing: the thread's repository reads each commit the history holds where it lies, with its parents (:meth:`_borrow`)."""

    def _pack(self, *, versions: bool) -> None:
        """Pack the thread's repository again, its own objects alone: what the history holds stays the history's.

        With *versions*, as at a turn's start once its snapshots have left
        enough packs, git looks for deltas between large files too: the
        versions of one file its snapshots took, often a small change each.
        Without, as after every push, it looks among small files alone, and
        keeps every delta it has: between large files the search costs much,
        and in media and other compressed files finds nothing.  Upkeep: a
        repository git cannot pack again still serves its thread's turn, and
        holds all it held.
        """
        large = [] if versions else ["-c", f"core.bigFileThreshold={_DELTAS_BELOW}"]
        try:
            self._git([*large, "repack", "-a", "-d", "-l", "-q"], env={"GIT_DIR": str(self.repo)}, cwd=self.repo)
        except HistoryError as why:
            logger.warning("A thread's repository could not be packed again, and is left as it is: %s", why)

    def _git(self, args: list[str], *, env: dict[str, str], cwd: Path, input: str | None = None) -> str:
        self._pin()
        within = env.get("GIT_DIR")
        if (self.astray and within in (str(self.repo), str(self._admin))) or (within == str(self._admin) and not (
            (self._admin / "index").is_file() and self.copy.is_dir() and not os.path.lexists(self.making)
        )):
            # A repository that does not borrow the history as it must may name what neither it nor
            # the history holds: no git reads it.  A copy whose making was cut short holds some of
            # its files: committed, the rest would land as deletions.  Git writes a copy's index
            # when the last of its files is written, and the mark of its making goes after that.
            raise HistoryError(
                "refused the request: this thread has no whole copy, and its next open makes one", code=NO_WHOLE_COPY,
            )
        if args[0] in ("add", "update-index"):
            # Every file read goes into one pack, whether a copy's add takes it or the readers of
            # the folder do: a loose object apiece is a file made through the share, and each
            # later look for one a round trip to this computer.
            args = ["-c", "core.bigFileThreshold=1", *args]
        elif args[0] == "pack-objects":
            # A push's pack is no thin one: a delta in it is only ever of another file new in the same push,
            # and between large files the search costs much and finds nothing in media and documents.
            args = ["-c", f"core.bigFileThreshold={_DELTAS_BELOW}", *args]
        elif args[:2] == ["worktree", "add"]:
            # A copy's files are made through the share too: several at once.
            args = ["-c", "checkout.workers=8", "-c", "checkout.thresholdForParallelism=200", *args]
        try:
            return super()._git(args, env={**env, **_PINNED}, cwd=cwd, input=input)
        except UnicodeDecodeError:
            raise HistoryError(
                "refused the request: a file's name is not UTF-8, which history cannot record", code=NAME_NOT_UTF8,
            ) from None

    def open(self, moves: bool = True) -> dict:
        """Make the thread's copy, or bring the one it has to its next turn.

        With none yet, the cloud's open: ``main`` is the folder as it is, by
        you, the branch starts there, and the copy is its worktree, with no
        ``.git`` in it.  The repository borrows the history's objects from
        before its first git (:meth:`_init`).  On a folder with no history,
        ``main`` is then pushed with its index: the folder's first commit is
        its history's first, which every later thread's copy is made from
        and borrows, and a first open has ended only once it is.  A folder
        history cannot record gets none
        (``{"history": "off", "reason": ...}``, see :meth:`_off`), and nothing
        is made.  A repository whose first open did not end, cut short or
        set aside for a redirect in it, or one that does not borrow the
        history as it must (:meth:`_borrows`), is made again as the first,
        and whatever is at its copy's path with it.  A copy whose folder was
        removed, whose own making was cut short, or whose index is gone, is
        made again from its branch.  What of the platform's own waited
        beside the copies while the copy was let go (:meth:`close`) is put
        back into it once its making has ended, whichever way it was made
        (:meth:`_back`).  Then, where it *moves*, as at a turn's start, a
        copy with nothing unlanded moves to ``main``'s tip, your
        edits picked up as at its first open (``moved``); one with unlanded
        work stays where it is, and so does its base (``kept``).  Inside a
        turn, as the desktop opens a copy before a step works in it, it
        does not move: it stays on the base its turn began on (``kept``).

        A copy is being made from before its first file is written until
        its making has ended, at a first open once the repository is marked
        as made: :attr:`making` is there all that while.  What a cut left
        of one is no copy a thread worked in, whatever it holds: it is
        removed and made again, and no request reads it meanwhile.  Any
        other copy that is made again is set aside first, whole, where it
        is anything but what it is made again from, and so is its
        repository where that holds a commit of the thread's own
        (:meth:`_make_way`, :meth:`_make_way_in_its_repository`).
        ``set_aside_folders`` names the thread's copies and repositories the
        place keeps so, each a folder of its set-aside folder, the oldest
        first, and ``set_aside_gone`` those that went for newer ones
        (:meth:`_asides_whole`): read there at every open, whatever else the
        open answers, a folder with no history as any other.

        Before the copy is read as anything's base, and before a copy that
        does not move is answered, what a request of the thread's was cut in
        the middle of is finished (:meth:`_catch_up`): a host works in the
        copy right after.  ``set_asides``
        are the snapshots of what the copy held of its own when that, or a
        record, made it other files, the oldest first: all the thread's
        repository holds, read from its refs at every open, so that one
        whose answer was lost is told again.
        """
        budget = _TIMEOUT.set(_OPEN_TIMEOUT)
        try:
            self._pin()
            # First, what an open that was cut left kept past the bound: before this one sets anything aside.
            self._asides_whole()
            if os.path.lexists(self.making) and os.path.lexists(self.copy):
                # A making that did not end: no thread worked in what it left, whatever that holds.
                _let_go(self.copy)
            if self.astray or not ((self.repo / "HEAD").is_file() and (self.repo / _MADE).is_file()):
                # No repository, one whose first open did not end, or one that may name what neither
                # it nor the history holds: neither it nor whatever is at its copy's path is whole.
                self._make_way()
                if (reason := self._off()) is not None:
                    return {"history": "off", "reason": reason, **self._asides_whole()}
                self.copy.parent.mkdir(parents=True, exist_ok=True)
                self.making.write_bytes(b"")
                self._open()
                refs = self._take()
                if MAIN not in refs:
                    main = self._main("rev-parse", MAIN)
                    self._push({MAIN: main}, expect={MAIN: None})
                    with contextlib.suppress(HistoryError, OSError):
                        # A cache: without it the next thread's first copy reads every file of the folder.
                        self._keep_index(main)
                elif (found := self._landing(refs)) is not None:
                    # Made from the history as it is: no record of the thread's is owed to this copy.
                    self._main("update-ref", self.landed, found[0])
                # Last: until the folder's first commit is the history's, the first open has not ended.
                (self.repo / _MADE).write_bytes(b"")
                self.making.unlink()
                self._back()
                return {"copy": "made", **self._asides_whole()}
            if os.path.lexists(self.copy) and not (self._admin / "index").is_file():
                # It was whole once, and its index is gone: git reads no file of it.
                self._make_way_in_its_repository()
            if not self.copy.exists():
                shutil.rmtree(self._admin, ignore_errors=True)
                self.copy.parent.mkdir(parents=True, exist_ok=True)
                self.making.write_bytes(b"")
                # A copy made holds nothing history leaves out: what the old one held is no later turn's to be told from.
                (self.repo / _SEEN).unlink(missing_ok=True)
                self._git(
                    ["worktree", "add", "-q", "--lock", str(self.copy), f"threads/{self.thread}"],
                    env={"GIT_DIR": str(self.repo)}, cwd=self.repo,
                )
                (self.copy / ".git").unlink()
                self.making.unlink()
            self._back()
            if len(list((self.repo / "objects" / "pack").glob("*.pack"))) > _PACKS:
                self._pack(versions=True)
            if moves:
                # Its snapshot before the turn finishes first what a cut request left.
                moved = self._to_main()
            else:
                self._catch_up()
                moved = False
            asides = [commit for _, commit in sorted(self._asides().values())]
            return {"copy": "moved" if moved else "kept", **({"set_asides": asides} if asides else {}), **self._asides_whole()}
        finally:
            _TIMEOUT.reset(budget)

    def close(self) -> dict:
        """Let the thread's copy go, its root idle: removed where its branch holds every file of it, else kept as it is.

        ``removed``: after what a request of the thread's was cut in the
        middle of is finished (:meth:`_catch_up`), every file and link of the
        copy is its branch's tip, so its next open makes it again from the
        branch and nothing of the thread's is lost.  The platform's own
        folders in it, which no branch holds, wait beside the copies until
        then (:attr:`waiting`).  ``kept``: it holds a change no snapshot has,
        anything history leaves out that is not the platform's, a folder
        with nothing in it, a repository of its own at its top, or the files
        a stop put back, which are older than its branch's tip; or its
        repository is none a request reads.  It stays as it is.  ``gone``:
        there is none.

        The copy leaves its name before its index goes, renamed to go
        (:data:`_GOING`): cut anywhere, the whole copy is at its name, or
        nothing is, and its next open makes it again.  So a copy with no
        index was never let go here.  One whose making did not end holds
        nothing of a thread's, and goes; any other is the thread's to set
        aside whole where it holds anything of its own, as an open does
        (:meth:`_make_way_in_its_repository`).
        """
        self._pin()
        if not os.path.lexists(self.copy):
            return {"copy": "gone"}
        if self.astray or not ((self.repo / "HEAD").is_file() and (self.repo / _MADE).is_file()):
            # Read by no git, and made again with its repository at its next open.
            return {"copy": "kept"}
        if os.path.lexists(self.making):
            _let_go(self.copy)
            self.making.unlink()
            return {"copy": "removed"}
        if not (self._admin / "index").is_file():
            self._make_way_in_its_repository()
            return {"copy": "removed"}
        self._catch_up()
        self._add_all(self._copy)
        # All it holds, whichever turn wrote it: what an earlier turn left out is made again by no branch either.
        excluded, repositories, _ = History._excluded(self)
        if excluded or repositories or self._holds_git() or self._copy("diff", "--cached", "--name-only", "HEAD"):
            return {"copy": "kept"}
        held = [self.copy / name.rstrip("/") for name in self.platform]
        held = [at for at in held if at.is_dir() and not at.is_symlink()]
        if any(os.path.lexists(self.waiting / at.name) for at in held):
            # Never put over what waits there already, which the copy did not take back.
            return {"copy": "kept"}
        for at in held:
            self.waiting.mkdir(parents=True, exist_ok=True)
            os.rename(at, self.waiting / at.name)
        going = self.copy.with_name(f"{self.copy.name}{_GOING}")
        os.rename(self.copy, going)
        shutil.rmtree(self._admin, ignore_errors=True)
        _removed(going)
        return {"copy": "removed"}

    def _holds_git(self) -> bool:
        """Whether the copy holds a name ``.git`` anywhere, which git never lists and passes over without a word.

        It may be a repository of the thread's own, with its commits, a
        submodule's checkout, or no repository at all; no branch holds any
        of it.  The platform's own folders at the top, which wait beside the
        copies whole, are not looked into.  No link is followed.
        """
        top = {name.rstrip("/") for name in self.platform}
        for at, folders, files in os.walk(self.copy):
            if ".git" in folders or ".git" in files:
                return True
            if at == str(self.copy):
                folders[:] = [name for name in folders if name not in top]
        return False

    def _back(self) -> None:
        """Put the platform's own folders that waited beside the copies back into the thread's copy, now whole.

        Each goes back only where the copy holds nothing at its name, and
        the rest wait still.  Through no link: :meth:`_pin` took away one
        where they wait.
        """
        if not self.waiting.is_dir():
            return
        for name in self.platform:
            held = self.waiting / name.rstrip("/")
            if held.is_dir() and not held.is_symlink() and not os.path.lexists(self.copy / held.name):
                os.rename(held, self.copy / held.name)
        with contextlib.suppress(OSError):
            # Only once nothing waits there.
            self.waiting.rmdir()

    def _make_way(self) -> None:
        """Take a repository that is not whole, and the copy beside it, out of the way of the ones made again.

        Neither is removed while it holds anything of the thread's own: it
        is renamed into the place's set-aside folder as it is, the
        repository first, and no git runs in either.  A copy with no
        repository beside it is none a request reads, so a cut between the
        two leaves nothing taken for whole, and the open after it sets the
        copy aside under a name of its own.

        The copy was whole once: one whose making did not end has gone by
        now, by its mark.  It is the thread's own unless it is, file for
        file, what it is made again from (:meth:`_holds_its_own`); the
        repository is, where its refs name a commit of the thread's
        (:meth:`_names_its_own`).  Which each is, is decided for both before
        either is touched, and never by a mark or an index of theirs.
        """
        own = {}
        if os.path.lexists(self.repo) or os.path.lexists(self.copy):
            # First, and before anything is renamed: a history that is not the platform's own refuses the open.
            refs = self._take()
            if os.path.lexists(self.repo):
                own[self.repo] = self._names_its_own(refs)
            if os.path.lexists(self.copy):
                own[self.copy] = self._holds_its_own(refs)
        self._out_of_the_way(own)

    def _make_way_in_its_repository(self) -> None:
        """Take a copy that has lost its index out of the way of the one made again from its branch, its repository whole.

        The copy was whole once, and a thread may have worked in it.  Where
        it is its branch's files exactly, it is removed and the one made
        again is the same.  Else it is set aside whole, what it no longer
        holds with the rest, and the copy made again holds as the thread's
        work only what the copy and the branch both held.  Each file the two
        did not hold alike is first put, in a commit on the branch, as the
        thread's base has it.  So a file a snapshot took, and the copy no
        longer held, does not come back as the thread's work; one a snapshot
        took away, and the copy held again, is not taken away as its work;
        and the thread's next landing writes neither into the folder.  The
        commit comes before the rename: cut after it, the same copy is found
        beside a branch that has those files so already.
        """
        held = self._held()
        try:
            tip, base = self._files(self.repo, self.branch), self._files(self.repo, self.base)
        except HistoryError as doubt:
            # No branch or no base to make a copy from again: the open fails on it, with the copy kept.
            logger.warning("A thread's copy whose branch or base could not be read is set aside as it is: %s", doubt)
            return self._out_of_the_way({self.copy: True})
        if held == tip:
            return self._out_of_the_way({self.copy: False})
        held = held or {}
        entries = "".join(
            "{} {}\t{}\0".format(*base.get(name, ("0", _ZERO)), name)
            for name in sorted({*tip, *held}) if held.get(name) != tip.get(name) and tip.get(name) != base.get(name)
        )
        if entries:
            index = self.repo / "again.index"
            index.unlink(missing_ok=True)
            env = {"GIT_DIR": str(self.repo), "GIT_INDEX_FILE": str(index)}
            try:
                self._git(["read-tree", self.branch], env=env, cwd=self.repo)
                self._git(["update-index", "-z", "--index-info"], env=env, cwd=self.repo, input=entries)
                tree = self._git(["write-tree"], env=env, cwd=self.repo)
            finally:
                index.unlink(missing_ok=True)
            self._main("update-ref", self.branch, self._main(
                *_as(_CHECKPOINT), "commit-tree", tree, "-p", self._main("rev-parse", self.branch),
                "-m", "What the copy no longer held as its branch did, as its base has it",
            ))
        self._out_of_the_way({self.copy: True})

    def _out_of_the_way(self, own: dict[Path, bool]) -> None:
        """Set aside whole each folder of *own* that is the thread's own, and remove the others; then the bound.

        The bound is kept at once, and not at the open's end alone: an open
        cut while it makes the copy again leaves no more kept than may be.
        """
        stem = None
        for left, kept in own.items():
            if kept:
                stem = self._set_aside_whole(left, stem)
            else:
                _let_go(left)
        if stem is not None:
            self._asides_whole()

    def _names_its_own(self, refs: dict[str, str]) -> bool:
        """Whether the thread's repository names a commit of the thread's own, read as data and never by git.

        Every ref it has is read where it lies, packed or loose.  ``main``
        there is the folder's files, never a thread's work, and a commit the
        folder's history holds is kept there, by *refs* a history of the
        platform's own.  A ref that names anything else names a snapshot,
        a turn as the thread committed it or what a record set aside, which
        this repository alone holds.  So does one that cannot be read: in
        doubt, it is the thread's own.
        """
        try:
            named = {}
            packed = self.repo / "packed-refs"
            for line in packed.read_text().splitlines() if packed.is_file() else ():
                if not line.startswith("#"):
                    commit, _, ref = line.partition(" ")
                    named[ref or commit] = commit.removeprefix("^")
            for at, _, files in os.walk(self.repo / "refs"):
                for name in files:
                    named[os.path.relpath(Path(at, name), self.repo)] = Path(at, name).read_text().removesuffix("\n")
            commits = set(named.values()) - {named.get(MAIN)}
            if commits and refs:
                said = self._git(
                    ["cat-file", "--batch-check"], env={"GIT_DIR": str(self._taken)}, cwd=self._taken,
                    input="".join(f"{commit}\n" for commit in sorted(commits)),
                )
                commits -= {line[:40] for line in said.splitlines() if line[40:].startswith(" commit ")}
            return bool(commits)
        except (HistoryError, OSError, ValueError) as doubt:
            logger.warning("A thread's repository whose refs could not be read is taken to hold its work: %s", doubt)
            return True

    def _holds_its_own(self, refs: dict[str, str]) -> bool:
        """Whether the thread's copy, beside a repository that is not whole, is anything but what it is made again from.

        By the *refs* of the folder's history that is the thread's branch
        there, where it holds work not landed, and otherwise the folder as
        it is.  The copy is the thread's own where a file or a link in it is
        not there alike, by its bytes and the mode git would record; where
        it holds what history leaves out, or what is neither a file nor a
        link; and where it lacks a file that is there.  A thread took that
        one away: a copy whose making was cut is not read here.  A folder
        that holds nothing is nothing a copy holds.  In doubt, and wherever
        anything cannot be read, it is the thread's own.
        """
        try:
            held = self._held()
            if held is None:
                return True
            branch, base = (self._files(self._taken, refs[ref]) if ref in refs else None for ref in (self.branch, self.base))
            if None not in (branch, base) and branch != base:
                return held != branch
            return any(found != self._in_folder(name) for name, found in held.items()) or any(name not in held for name in self._tracked())
        except (HistoryError, OSError, ValueError) as doubt:
            logger.warning("A thread's copy that could not be read against what it is made again from is taken to hold its work: %s", doubt)
            return True

    def _held(self) -> dict[str, tuple[str, str]] | None:
        """Each file and link in the thread's copy by its name, as git would record it (:func:`_recorded`); None where the copy cannot be read whole.

        What is neither a file nor a link is there by its name, as no blob that any commit holds.
        """
        held = {}
        try:
            folders = [self.copy]
            while folders:
                with os.scandir(folders.pop()) as entries:
                    for entry in entries:
                        if entry.is_dir(follow_symlinks=False):
                            folders.append(Path(entry.path))
                        else:
                            held[os.path.relpath(entry.path, self.copy)] = _recorded(entry.path) or ("", "")
        except OSError as doubt:
            logger.warning("A thread's copy that could not be read whole is taken to hold its work: %s", doubt)
            return None
        return held

    def _tracked(self) -> Iterator[str]:
        """The name of each file and link of the folder that history records, by the excludes alone, as :meth:`_off` goes through it.

        A folder that holds a repository of its own is left out, as git
        leaves it, and so is what is neither a file nor a link.
        """
        for at, folders, files in os.walk(self.project):
            inside = os.path.relpath(at, self.project)
            prefix = "" if inside == "." else f"{inside}/"
            links = [f for f in folders if os.path.islink(os.path.join(at, f))]
            folders[:] = [
                f for f in folders
                if f not in links and _EXCLUDED.search(f"{prefix}{f}/") is None and not os.path.lexists(os.path.join(at, f, ".git"))
            ]
            for name in (*links, *files):
                kind = os.lstat(os.path.join(at, name)).st_mode
                if _EXCLUDED.search(f"{prefix}{name}") is None and (stat.S_ISREG(kind) or stat.S_ISLNK(kind)):
                    yield f"{prefix}{name}"

    def _files(self, repo: Path, commit: str) -> dict[str, tuple[str, str]]:
        """What *commit* holds, as the repository *repo* has it: the mode and the blob of each file and link, by its name."""
        listed = self._git(["ls-tree", "-r", "-z", commit], env={"GIT_DIR": str(repo)}, cwd=repo)
        return {name: tuple(said.split(" ")[::2]) for said, _, name in (entry.partition("\t") for entry in listed.split("\0") if entry)}

    def _in_folder(self, name: str) -> tuple[str, str] | None:
        """What the folder holds at *name* now, as :func:`_recorded` reads it, following no link on the way; None for nothing it can read."""
        opened: list[int] = []
        try:
            opened.append(os.open(self.project, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
            *folders, last = PurePosixPath(name).parts
            for folder in folders:
                opened.append(os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=opened[-1]))
            return _recorded(last, dir_fd=opened[-1])
        except OSError:
            return None
        finally:
            for fd in opened:
                os.close(fd)

    def _set_aside_whole(self, left: Path, stem: str | None = None) -> str:
        """Rename *left*, the thread's repository or its copy, into the place's set-aside folder as it is; the stem of its name there.

        The name is this module's own: ``<nth>-<when>-<thread>`` and which
        of the two it is.  ``<nth>`` is one more than any name there has,
        whatever that name is of, so nothing is at the name before: a rename
        replaces no folder that holds anything, and goes through no link.
        With *stem*, the repository's just set aside, the copy takes its
        name.  The folder is opened as a folder, never through a link, and
        the rename goes by its handle.

        A repository is kept borrowing the folder's history by the one line,
        which names the history from the set-aside folder as from the
        repositories': it reads all it names as it is, its own objects and
        the history's.  One whose alternates were gone, or named another
        store and were taken away, is given the line first, where its
        objects' folders are its own, and its note of the history's packs
        goes before that: cut before the rename, it is still no repository
        that borrows as it must.  One whose history no longer holds a pack it
        read from cannot be made whole by anything here.

        Room is made for it first, by the bound on what the place keeps on
        its disk (:meth:`_make_room`): cut after, nothing is kept past it.
        """
        if left == self.repo and not self._lends() and all(
            at.is_dir() and not at.is_symlink() for at in (left, left / "objects", left / "objects" / "info")
        ):
            if os.path.lexists(left / _BORROWED):
                _removed(left / _BORROWED)
            _pinned(left / _BORROWS, _ALTERNATES)
        folder = _opened_folder(str(self.aside_whole), None, True)
        try:
            if stem is None:
                last = max((int(named[1]) for named in map(_WHOLE.fullmatch, os.listdir(folder)) if named), default=0)
                stem = f"{last + 1:08d}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{self.thread}"
            name = f"{stem}.{'repository' if left == self.repo else 'copy'}"
            if not _WHOLE.fullmatch(name):
                raise HistoryError("the place has no name left for what is set aside")
            self._make_room(folder, left, stem[:8])
            os.rename(left, name, dst_dir_fd=folder)
        finally:
            os.close(folder)
        logger.warning("Set aside whole as %s, to be made again: %s", name, left)
        return stem

    def _make_room(self, folder: int, left: Path, nth: str) -> None:
        """Let go of the oldest the set-aside folder open as *folder* keeps, until with *left* it holds ``_ASIDE_BYTES`` of the disk at most.

        *left* is about to be set aside there as its *nth* time, this
        thread's newest from then on.  The oldest time goes first, a
        repository with the copy set aside at the same time, and never a
        thread's newest: where those hold more, the place keeps more.  What
        each holds is what its files take of the disk, no link followed: a
        file most of which was never written counts for what it holds.  One
        that goes is renamed ``<name>.gone``, and is emptied as those the
        count lets go are (:meth:`_asides_whole`).  Letting go is upkeep: one
        that cannot be renamed is kept, and what is set aside is all the same.
        """
        kept, _ = _asides_in(folder)
        taken = {name: _on_disk(name, folder) for _, _, name in kept}
        newest = {thread: at for at, thread, _ in kept} | {self.thread: nth}
        held = sum(taken.values()) + _on_disk(left)
        for at in sorted({at for at, _, _ in kept} - set(newest.values())):
            if held <= _ASIDE_BYTES:
                return
            for name in [name for of, _, name in kept if of == at]:
                try:
                    os.rename(name, f"{name}.gone", src_dir_fd=folder, dst_dir_fd=folder)
                except OSError as why:
                    logger.warning("What was set aside as %s could not be let go, and is kept: %s", name, why)
                    continue
                held -= taken[name]
                logger.warning("What was set aside as %s is let go, for room for what is set aside now", name)

    def _asides_whole(self) -> dict[str, list[str]]:
        """Bound what the place keeps set aside whole, and what an open says of this thread's.

        ``set_aside_folders`` are the thread's copies and repositories kept
        there, by name, the oldest first.  A thread keeps the last
        ``_ASIDE_WHOLE`` times one was set aside, each time one count, and
        a place the last ``_ASIDE_WHOLE_IN_ALL`` of all its threads': the
        oldest goes for one more, whoever's it is, and never a thread's
        newest; and no more of the disk than room was made for as each was
        set aside (:meth:`_make_room`).  The thread's own beyond what it
        keeps go first, and another's only where the place then still keeps
        more than it may.
        One that goes is first renamed ``<name>.gone``, from when
        it is no longer kept, and then emptied: cut anywhere, the next open
        finishes it, and none is said to be kept with part of it gone.  The
        empty folder stays for its name: ``set_aside_gone`` are those of
        this thread's, as they were named, at every open, for the last
        ``_ASIDE_GONE`` times one went.  Letting go is upkeep, and where it
        fails the open goes on.  Only folders of this module's naming are
        read, by the handle of the folder they are in.
        """
        folder = _opened_folder(str(self.aside_whole), None, False)
        if folder is None:
            return {}
        try:
            kept, gone = _asides_in(folder)
            times = list(dict.fromkeys(nth for nth, _, _ in kept))
            mine = list(dict.fromkeys(nth for nth, thread, _ in kept if thread == self.thread))
            newest = {thread: nth for nth, thread, _ in kept}.values()
            going = set(mine[:-_ASIDE_WHOLE])
            going |= {nth for nth in [nth for nth in times if nth not in going][:-_ASIDE_WHOLE_IN_ALL] if nth not in newest}
            for name in [name for nth, _, name in kept if nth in going]:
                try:
                    os.rename(name, f"{name}.gone", src_dir_fd=folder, dst_dir_fd=folder)
                    logger.warning("What was set aside as %s is let go, for what was set aside since", name)
                except OSError as why:
                    logger.warning("What was set aside as %s could not be let go, and is kept: %s", name, why)
            kept, gone = _asides_in(folder)
            untold = list(dict.fromkeys(nth for nth, thread, _ in gone if thread == self.thread))[:-_ASIDE_GONE]
            for nth, thread, name in list(gone):
                try:
                    _emptied(name, folder)
                    if thread == self.thread and nth in untold:
                        os.rmdir(name, dir_fd=folder)
                        gone.remove((nth, thread, name))
                except OSError as why:
                    logger.warning("What was set aside as %s could not be let go whole, and is at the next open: %s", name, why)
        finally:
            os.close(folder)
        said = {
            "set_aside_folders": [name for _, thread, name in kept if thread == self.thread],
            "set_aside_gone": [name.removesuffix(".gone") for _, thread, name in gone if thread == self.thread],
        }
        return {key: names for key, names in said.items() if names}

    def changed(self) -> dict:
        """The files the copy changed since its base, committed or not: what a landing looks at in the folder first.

        A landing on the computer asks the file helper for these files'
        revisions before its pickup, so a save of yours after that look is
        either picked up or found at the apply.  Nothing is committed and
        no ref moves.
        """
        self._catch_up()
        self._add_all(self._copy)
        names = self._copy("diff", "--cached", "--name-only", "--no-renames", "-z", self._main("rev-parse", self.base))
        return {"paths": sorted(n for n in names.split("\0") if n)}

    def snapshot(self, reason: str) -> str:
        self._catch_up()
        return super().snapshot(reason)

    def restore(self, commit: str) -> None:
        """Put the copy back to *commit*, a snapshot of the stretch it is on: one built on its base as it stands.

        A copy here outlives its landing, and so do its snapshots.  One
        taken before a record or a move is the copy as it was on another
        base: put back to it whole, every file another thread landed since
        and every edit of yours picked up since would be this thread's own
        change back, and its next landing would delete and overwrite them.
        So would what a record or a move set aside.  Each is refused.
        """
        self._catch_up()
        if self._has(commit) and self._main("rev-list", "--count", "--end-of-options", f"{commit}..{self.base}") != "0":
            raise HistoryError(
                "refused the request: this snapshot is not built on the copy's base as it stands, and put back to it "
                "the copy would undo what landed since", code=NOT_ON_BASE,
            )
        super().restore(commit)

    def commit_turn(self, **step: Any) -> dict:
        self._catch_up()
        return super().commit_turn(**step)

    def keep(self, **step: Any) -> dict:
        self._catch_up()
        return super().keep(**step)

    def record(self, *, left: Sequence[str] = (), **step: Any) -> dict:
        """The cloud's record, and then the copy is made the landing's files, but for *left*.

        A pod is made anew from the landing; a copy here outlives it.  A
        file the landing left out is the newer one in the copy from now on,
        as the thread's next turn must find it, and the thread's own version
        is the landing's second parent.  What history leaves out stays.
        ``set_aside`` is a snapshot of what the copy held beyond its turn,
        written after the turn was committed, where making the copy the
        landing's took it away; None where it took nothing.  It is kept on
        a ref of the thread's repository, and is not one the copy is put
        back to (:meth:`restore`).

        *left* are the files no landing ever writes, a name that runs code
        or a link's place in the folder, each by its path and never a
        pattern: nobody else changed them, so each stays in the copy as the
        turn left it, a file it deleted still gone, and the folder's history
        records none of them as landed.  They are the thread's work still,
        for a later turn to land.  The landing's own commit names them, so
        a record cut after its push leaves them so too, finished by the next
        act (:meth:`_finish_record`).  A *left* that would keep the thread's
        version where somebody else changed the file since the turn began,
        or where a file of theirs is in its way, is refused before anything
        is pushed (:meth:`_as_landed`).

        Safe to repeat wherever ``main`` is by then: no lock is held across
        a computer's absence, so another thread may have landed since a try
        cut after its push.  The landing is found by its saga where the
        thread's own branch has it, and the copy of a thread that has worked
        on since is left alone.
        """
        if not isinstance(left, (list, tuple)) or not all(_walked(path) for path in left):
            _refuse("a file it left out has no path in the folder")
        if any(f"{key}: ".startswith(f"{_LEFT}: ") for key, _ in step["trailers"]):
            _refuse("a trailer it names is the history's own")
        self._catch_up()
        saga = f"Surogate-Saga: {dict(map(tuple, step['trailers']))['Surogate-Saga']}"
        found = self._landing(self._take())
        if found is not None and saga in found[2]:
            commit = found[0]
        else:
            if left:
                # Checked against the very files the landing will record, before anything is pushed.
                landing = self._landed(step["turn"], step["applied"], step.get("pickup") or step["main"] or self._main("rev-parse", MAIN))
                if self._as_landed(landing, step["turn"], list(left)) is None:
                    _refuse("a file it left out, or one in its way, was changed by somebody else since the thread's turn began")
            named = [*step["trailers"], *([_LEFT, json.dumps(path)] for path in left)]
            commit = super().record(**{**step, "trailers": named})["commit"]
            self._catch_up()
        return {"commit": commit, "set_aside": self._asides().get(step["turn"], (None, None))[1]}

    def _landing(self, refs: dict[str, str]) -> tuple[str, str, list[str]] | None:
        """The thread's landing where *refs*, the history's, still have its branch: it, its turn, and its message.

        A record's push moves the thread's branch to the landing, which has
        two parents, the turn its second.  The thread's next push puts a
        turn there, or a kept one, which has one here: no helper's hand-off
        stands behind it.  Read in the history itself: this repository may
        not hold the landing yet.
        """
        landing = refs.get(self.branch)
        if landing is None:
            return None
        parents, message = self._stored(landing)
        if len(parents) != 2:
            return None
        return landing, parents[1], message

    def _stored(self, commit: str) -> tuple[list[str], list[str]]:
        """*commit*'s parents and its message line by line, as the history itself has it.

        By the line end alone: a trailer's value may hold any other character, and none of them makes a line of its own.
        """
        said = self._git(["cat-file", "commit", commit], env={"GIT_DIR": str(self._taken)}, cwd=self._taken).split("\n")
        parents = [_checked_id(line[7:], "a commit") for line in takewhile(lambda line: line.startswith("parent "), said[1:])]
        return parents, said[said.index("") + 1:] if "" in said else []

    def _catch_up(self) -> None:
        """Finish what a request of this thread's was cut in the middle of, before the copy is read as anything's base.

        A copy outlives the request that changes it, and a request's bound,
        a stop or a lost guest can fall between the copy's files and the
        base they are read against.  No act reads the copy before this.
        """
        if not (self.repo / "HEAD").is_file():
            return
        self._finish_record()
        self._finish_move()

    def _finish_record(self) -> None:
        """Finish a record of this thread's that was cut after its push.

        The push is the moment a landing counts; making the copy the
        landing's files comes after it.  Cut between, the copy still holds
        the turn's files, on a base that is the landing: read so, every file
        another thread landed would be one this thread deleted, and the
        thread's old version of a file the landing left out would be its
        change to the newer one.  The history holds a landing of the
        thread's that :attr:`landed` does not name, and the copy is made its
        files now, but for those its commit names as left out, which no
        landing writes: each is as the turn has it (:meth:`_as_landed`).

        What the copy holds that is neither the turn's file nor the
        landing's is set aside first, on a ref (:meth:`_set_aside`).  A copy
        whose index is those files already was made so, git writing the
        index last, and is left as it is, with whatever the thread has
        written since.  Safe to cut anywhere: the ref that says it is done
        moves last.  Where it cannot be done the request is refused, and
        nothing was read from the copy.

        A turn, or a kept one, that this repository pushed and a cut kept it
        from noting is noted here too.  The thread's next push expects the
        branch where this repository last left it, and a copy here outlives
        the turn that pushed: left unnoted, every later landing of the
        thread's would be refused as one whose branch moved.  It named the
        branch it was pushing first (:attr:`pushing`).  A branch this
        repository did not push is still that, though it reads every commit
        the history holds.
        """
        refs = self._take()
        found = self._landing(refs)
        if found is None:
            pushed = refs.get(self.branch)
            if pushed is not None and pushed != self._ref(self.synced) and pushed == self._ref(self.pushing):
                self._main("update-ref", self.synced, pushed)
            return
        if self._ref(self.landed) == found[0]:
            return
        landing, turn, message = found
        left = _left_in(message)
        try:
            # First, and before anything is written: a copy that is not whole is its next open's to make.
            index = self._copy("write-tree")
            files = self._as_landed(landing, turn, left)
            if files is None:
                # No request records such a landing: the history is not as the platform wrote it.
                raise HistoryError(
                    "refused the project's history: a landing names as left out a file somebody else changed since its turn began",
                    code=HISTORY_REFUSED,
                )
            if index != files:
                self._add_all(self._copy)
                held = self._copy("write-tree")
                sides = [commit for commit in (landing, turn) if self._has(commit)]
                if self._beyond(held, sides):
                    self._set_aside(held, turn, onto=sides[-1])
                self._copy("read-tree", "-u", "--reset", files)
            # Its base is this landing: what the copy holds beside it was there before the next turn.
            self._remember()
            for ref in (self.branch, self.base, self.synced, self.landed):
                self._main("update-ref", ref, landing)
        except HistoryError as why:
            if why.code != FAILED:
                raise
            raise HistoryError(
                "refused the request: a landing of this thread's is in the history, and its copy "
                f"could not be made the landing's files: {why}", code=RECORD_UNFINISHED,
            ) from None

    def _as_landed(self, landing: str, turn: str, left: list[str]) -> str | None:
        """The tree a copy is made after *landing*: its files, but each of *left* as *turn*, the thread's, has it.

        A file the turn deleted is not there, nor one of the landing's where
        a file of the turn's needs its name for a folder.  The same tree for
        the same landing at every try, made where a cut leaves nothing the
        next request reads.

        None where that tree would hold the thread's own over somebody
        else's.  The copy may differ from the landing only where the
        landing's file is still the turn's base's: nobody changed it since
        the turn began.  Anywhere else (another thread's landing, a save of
        yours picked up, a file of theirs in the way of the thread's) the
        copy would hold the thread's version, or no file, on a base that has
        theirs, and its next landing would write that over theirs as its
        own change.
        """
        wanted = set(left)
        fields = iter(self._main("diff", "--raw", "-z", "--no-renames", "--no-abbrev", landing, turn).split("\0"))
        entries = []
        for meta in fields:
            if not meta:
                break
            path = next(fields)
            if path in wanted:
                _, mode, _, blob, _ = meta.split(" ")
                entries.append(f"0 {_ZERO}\t{path}\0" if blob == _ZERO else f"{mode} {blob}\t{path}\0")
        if not entries:
            return self._tree(landing)
        index = self.repo / "left.index"
        index.unlink(missing_ok=True)
        env = {"GIT_DIR": str(self.repo), "GIT_INDEX_FILE": str(index)}
        try:
            self._git(["read-tree", landing], env=env, cwd=self.repo)
            # Each in place of whatever of the landing's is in its way: git's index-info replaces it.
            self._git(["update-index", "-z", "--index-info"], env=env, cwd=self.repo, input="".join(entries))
            files = self._git(["write-tree"], env=env, cwd=self.repo)
        finally:
            index.unlink(missing_ok=True)
        return None if self._differ(landing, files) & self._differ(f"{turn}^1", landing) else files

    def _differ(self, one: str, other: str) -> set[str]:
        """The names at which the trees of *one* and *other* differ."""
        return {name for name in self._main("diff", "--name-only", "--no-renames", "-z", one, other).split("\0") if name}

    def _finish_move(self) -> None:
        """Finish a move of the thread's clean copy to ``main`` that was cut after it began.

        A copy with nothing unlanded moves to ``main`` at its turn's start:
        its files, then its branch, then its base (:meth:`_to_main`).  Cut
        between, the copy holds ``main``'s files, or some of them, on the
        base it had: read so, your edits and every other thread's landing
        since would be this thread's own changes, and its next landing would
        write them over whatever the folder holds by then.  :attr:`moving`
        names where the copy was going, from before the first of its files
        moved until its base has, and the copy had nothing unlanded when it
        was written: so the move is finished here.

        A copy whose index is already where it was going had every file
        moved, git writing the index last, and is left as it is, with
        whatever the thread has written since.  Otherwise what it holds that
        is neither a file of where it was nor of where it was going is set
        aside first (:meth:`_set_aside`).  Safe to cut anywhere: the base
        moves last, and the ref goes after it.  Where it cannot be done the
        request is refused, and nothing was read from the copy.
        """
        to = self._ref(self.moving)
        if to is None:
            return
        try:
            # First, and before anything is written: a copy that is not whole is its next open's to make.
            if self._copy("write-tree") != self._tree(to):
                was = self._main("rev-parse", self.base)
                self._add_all(self._copy)
                held = self._copy("write-tree")
                if self._beyond(held, [was, to]):
                    self._set_aside(held, to, onto=was)
                self._copy("read-tree", "-u", "--reset", to)
            for ref in (self.branch, self.base):
                self._main("update-ref", ref, to)
            self._remember()
            self._main("update-ref", "-d", self.moving)
        except HistoryError as why:
            if why.code != FAILED:
                raise
            raise HistoryError(
                f"refused the request: this thread's copy was being moved to main, and the move could not be finished: {why}",
                code=MOVE_UNFINISHED,
            ) from None

    def _beyond(self, held: str, sides: list[str]) -> bool:
        """Whether the tree *held* has, at some name, what none of *sides* has there: a file that is neither's."""
        differing = [
            {name for name in self._main("diff", "--name-only", "--no-renames", "-z", side, held).split("\0") if name}
            for side in sides
        ]
        return bool(set.intersection(*differing))

    def _set_aside(self, tree: str, of: str, *, onto: str) -> None:
        """Keep *tree*, what the copy holds, on a ref of its own before the copy is made *of*'s files.

        *of* is the turn whose landing the copy is made, or where its move
        was going.  The snapshot is the copy as it was on *onto*, its first
        parent: the turn, or the base the move began on.  What differs from
        that is what was written since.  It is not one the copy is put back
        to (:meth:`restore` refuses it): the copy's base has moved, and put
        back whole it would undo what landed since.  One taken before for
        the same *of*, by a try cut part way through the copy's files, is
        its second parent: that one may hold a file this one no longer does.
        A thread keeps its last ``_ASIDE``, by the order they were set aside
        in, which their names count.
        """
        kept = self._asides()
        ref, earlier = kept.get(of, (None, None))
        if ref is None:
            last = max((int(name.rpartition("/")[2].partition("-")[0]) for name, _ in kept.values()), default=0)
            ref = f"{self.aside}/{last + 1:08d}-{of}"
        self._main("update-ref", ref, self._main(
            *_as(_CHECKPOINT), "commit-tree", tree, *(arg for parent in (onto, earlier) if parent for arg in ("-p", parent)),
            "-m", "Set aside before the copy was made other files",
        ))
        for old, _ in sorted({**kept, of: (ref, None)}.values())[:-_ASIDE]:
            self._main("update-ref", "-d", old)

    def _asides(self) -> dict[str, tuple[str, str]]:
        """What was set aside of the thread's copy, by what the copy was then made: each its ref and its snapshot."""
        kept = {}
        for line in self._main("for-each-ref", "--format=%(objectname) %(refname)", f"{self.aside}/").splitlines():
            commit, _, ref = line.partition(" ")
            if re.fullmatch(r"[0-9]{8}-[0-9a-f]{40}", ref.rpartition("/")[2]):
                kept[ref[-40:]] = (ref, commit)
        return kept

    def _excluded(self) -> tuple[list[str], list[str], bool]:
        """The cloud's, counting only what the copy's turns wrote since it last started from its base.

        A pod's copy starts with nothing history leaves out, so whatever it
        holds its turn made.  A copy here outlives its turn: a
        ``__pycache__/``, a ``node_modules/``, a checked-out repository or a
        ``.env`` an earlier landing found in it is there for good.  Counted
        again at every landing, it would be said again to be unsaved, and
        would hold every deletion of every later turn as a move git could
        not see.  So what the copy held when its base was last set
        (:meth:`_remember`) is neither said nor counted: only a name that is
        new since, or one written again since, a folder's by anything in it.
        What the harness writes is never counted.  Each name is looked up
        once, by the names git lists, however many files are in them.
        """
        excluded, repositories, _ = super()._excluded()
        seen, now = self._seen(), self._left_out()
        names = set(seen["names"])
        fresh = {name for name, token in now.items() if seen["files"].get(name) != token}
        wrote = any(not name.startswith(self.harness) for name in fresh)
        return (
            [name for name in excluded if name not in names or name in fresh],
            [name for name in repositories if name not in names or name in fresh],
            wrote,
        )

    def _left_out(self) -> dict[str, str]:
        """Each name history leaves out in the copy, with what tells it from a later write of it.

        Listed by git as the cloud's :meth:`_excluded` lists them, by the
        excludes alone: a file, by its :func:`_state`; or a folder all of
        which is left out, a ``node_modules/`` or another repository's,
        which git does not go into, as one name, told apart by the name
        and the state of everything in it.  A folder's are read by this
        computer, not git, and never decoded: a name in it that is not
        UTF-8 counts with the folder, and refuses nothing.  Looked at
        without following a link.
        """
        found: dict[str, str] = {}
        listed = self._copy("ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory").split("\0")
        copy = os.open(self.copy, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            for name in listed:
                if not name:
                    continue
                if not name.endswith("/"):
                    try:
                        found[name] = _state(os.stat(name, dir_fd=copy, follow_symlinks=False))
                    except OSError:
                        pass  # gone since it was listed: no file of the copy's now
                    continue
                inside = hashlib.sha1()
                folders = [name.rstrip("/")]
                while folders:
                    at = folders.pop()
                    try:
                        with os.scandir(os.path.join(self.copy, at)) as entries:
                            for entry in sorted(entries, key=lambda e: e.name):
                                if entry.is_dir(follow_symlinks=False):
                                    folders.append(f"{at}/{entry.name}")
                                else:
                                    said = f"{at}/{entry.name}\0{_state(entry.stat(follow_symlinks=False))}\0"
                                    inside.update(said.encode("utf-8", "surrogateescape"))
                    except OSError:
                        inside.update(b"\0unread\0")
                found[name] = inside.hexdigest()
        finally:
            os.close(copy)
        return found

    def _seen(self) -> dict[str, Any]:
        """What :meth:`_remember` last wrote, read as data; nothing where it wrote none, or what is there is not it.

        A copy with no note was made since its base was last set, and holds
        nothing history leaves out that its turns did not write.
        """
        nothing: dict[str, Any] = {"names": [], "files": {}}
        if (data := _read_as_data(self.repo / _SEEN, _SEEN_BYTES)) is None:
            return nothing
        try:
            # One longer than a request reads is cut short, and no note.
            seen = json.loads(data)
        except (ValueError, RecursionError):
            return nothing
        names, files = (seen.get("names"), seen.get("files")) if isinstance(seen, dict) else (None, None)
        if not isinstance(names, list) or not isinstance(files, dict) or not all(isinstance(token, str) for token in files.values()):
            return nothing
        return {"names": [name for name in names if isinstance(name, str)], "files": files}

    def _remember(self) -> None:
        """Write down what history leaves out of the copy now, which has just started from its base.

        Where a record makes the copy the landing's files and where an open
        moves it to ``main``, and where the next act finishes either that was
        cut.  Not where a failed turn is kept: nothing of that turn has
        landed, and what it wrote is still its thread's to land.  Upkeep:
        where it cannot be written the last note stands, which counts more
        as written since, never less.  Replaced, never written through.
        """
        try:
            excluded, repositories, _ = super()._excluded()
            seen = {"names": sorted({*excluded, *repositories}), "files": self._left_out()}
            _replace(self.repo / _SEEN, json.dumps(seen).encode())
        except (HistoryError, OSError) as why:
            logger.warning("What history leaves out of a thread's copy could not be noted, and the last note stands: %s", why)

    def forget(self, *, saga: str, applied: list[dict] | None = None) -> dict:
        """Whether what the landing of *saga* kept of the folder's files may be forgotten; refused while it may not.

        The file helper keeps each file a landing replaces until the landing
        is settled, for its put-back, and cannot tell when that is.
        ``landing`` is the landing where ``main`` holds it: recorded, so
        that each replaced file is a version under it.  It is None where the
        landing was put back whole: each file of *applied*, what whoever
        asks says the landing applied, is its ``before`` in the folder now.
        Anything else there is not: the landing's own file, one changed
        since it wrote it, a link, a folder.  Then, and for a landing not
        recorded, it is refused, and whoever asked forgets nothing.

        It goes by the folder and the folder's history alone, follows no
        link, and neither reads nor makes the thread's repository.
        """
        if not (isinstance(saga, str) and saga):
            _refuse("it names no saga")
        if not isinstance(applied, list):
            _refuse("it names no files a landing applied")
        files = []
        for change in applied:
            if not _walked(change["path"]):
                _refuse("a file it applied has no path in the folder")
            if "before" not in change:
                _refuse("a file it applied has no version from before it")
            files.append((PurePosixPath(change["path"]).parts, change["before"]))
        main = self._take().get(MAIN)
        if main is not None and (landing := self._landing_of(saga, main, None)[0]) is not None:
            return {"landing": landing}
        if any(self._unfollowed(parts) != before for parts, before in files):
            raise HistoryError(
                "refused the request: this landing was neither recorded nor put back whole: a file it applied is not "
                "what was there before it, and what it replaced is kept for its put-back", code=LANDING_UNSETTLED,
            )
        return {"landing": None}

    def _unfollowed(self, parts: tuple[str, ...]) -> str | None:
        """The blob id of the folder's file at *parts* now, None where there is none, following no link.

        ``""``, which is no blob's id, where what is there is no file: a
        link or a folder at its name, or a link or a file on the way to it.
        """
        opened: list[int] = []
        try:
            opened.append(os.open(self.project, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
            for name in parts[:-1]:
                opened.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=opened[-1]))
            # Non-blocking, so a pipe answers at once and is no file.
            opened.append(os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=opened[-1]))
            info = os.fstat(opened[-1])
            if not stat.S_ISREG(info.st_mode):
                return ""
            blob = hashlib.sha1(b"blob %d\0" % info.st_size)
            while chunk := os.read(opened[-1], 1 << 20):
                blob.update(chunk)
            return blob.hexdigest()
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                return ""
            raise
        finally:
            for fd in opened:
                os.close(fd)

    def pickup(self, *, author: dict[str, str], trailers: list[list[str]], push: bool = False) -> dict:
        """The cloud's pickup, its files named in parts: this answer names the first, and ``more`` how many are left after it.

        Your edits since ``main`` are any number of files, and one answer
        to the desktop is one frame of its link: a part names as many as
        :data:`_PICKED_CHARS` holds, and each part after it is asked for
        from the file the last one ended at (:meth:`picked`).  No ref
        moves, as in the cloud: the landing's record pushes the pickup.
        Only so: a folder's history records no routine's run.
        """
        if push:
            _refuse("a folder's history records no routine's run: a pickup is a landing's first step here, pushed with its record")
        picked = super().pickup(author=author, trailers=trailers)
        return {**picked, **_part(picked["picked_up"])}

    def picked(self, *, pickup: str, after: str) -> dict:
        """The files the pickup *pickup* picked up after the file *after*, as many as one answer names: ``{picked_up, more}``.

        A pickup's own answer names its first part, and each part after it
        is asked from the last file the one before it named; ``more`` is
        how many are left after this one.  Each is in the order the pickup
        names its files, by their paths, so its parts name each file once.
        It only reads.  *pickup* is the commit a pickup answered with, your
        edits on ``main``: any commit of this thread's repository with one
        parent is answered so, its files what it changed of that parent.  A
        part asked again is the same part.
        """
        if not _walked(after):
            _refuse("it names no file to go on from")
        parents = self._parents(pickup) if self._has(pickup) else []
        if len(parents) != 1:
            _refuse("it names no commit of this thread's with one parent")
        versions, _ = self._diff(parents[0], pickup)
        return _part([{"path": path, "before": was, "after": now} for path, (was, now) in sorted(versions.items()) if path > after])

    def apply(self, *args: Any, **kwargs: Any) -> dict:
        _refuse(_NO_WRITER)

    def unapply(self, *args: Any, **kwargs: Any) -> dict:
        _refuse(_NO_WRITER)

    def _put(self, *args: Any, **kwargs: Any) -> list[str]:
        _refuse(_NO_WRITER)

    def _remove(self, *args: Any, **kwargs: Any) -> None:
        _refuse(_NO_WRITER)

    def prune(self, *, keep: list[str], now: float) -> dict:
        """The cloud's pruning, keeping besides the branch and the base of every thread that has a repository in the place.

        A thread's repository outlives a pruning, with the branch it last
        pushed, and its next push expects the history's branch to be that.
        The server names the threads it knows to work in the folder; one
        resolved longer ago than the window and then reopened is none of
        them, and has its repository here all the same.  A pruning that
        deleted its refs would refuse every landing it tries from then on.
        Its repository's name is what is read of it here.

        At most once a day by *now*, the asker's clock (:meth:`_mark_pruned`).
        Every pack the cut leaves out goes: one request runs on a place at a
        time, so no push is under way beside the pruning, as one can be in the
        cloud.  What each repository in the place reads is kept as the cut
        goes (:meth:`_cut`, :meth:`_letting_go`).
        """
        self._pin()
        clones = self.repo.parent
        here = [
            name for name in (sorted(os.listdir(clones)) if clones.is_dir() else [])
            if re.fullmatch(_NAMED, name) and (clones / name).is_dir() and not (clones / name).is_symlink()
        ]
        theirs = [ref for thread in here for ref in (f"refs/heads/threads/{thread}", f"refs/bases/{thread}")]
        try:
            return super().prune(keep=sorted({*keep, *theirs}), now=now, spare=0.0)
        finally:
            self.reached.clear()

    def _mark_pruned(self, now: float) -> None:
        """The cloud's mark, dated by the clock that asked for the pruning: the server's alone says when a day has passed.

        This computer's clock may be a day from the server's, either way: by
        its own, a history would be pruned at every landing, or never.
        """
        super()._mark_pruned(now)
        with self._folder() as history:
            os.utime("pruned", (now, now), dir_fd=history, follow_symlinks=False)

    def _cut(self, git: Callable[..., str], work: Path, *, mains: list[str], kept: int) -> int:
        """The cloud's cut, every commit a repository in the place reaches from its refs kept as a tip.

        A thread's repository here borrows the history's objects, and one
        set aside whole does too: the base it is on, ``main`` as it last
        read it and the commits its own snapshots stand on are in the
        history's packs alone.  A cut that let one go would leave that
        repository without its objects, whichever thread asked for the
        pruning.  So each is named in the cut's copy of the history while it
        is cut, and nowhere after: the history's refs stay its own.
        """
        if not self.reached:
            self.reached[:] = [self._reached(work), set((_read_as_data(work / "shallow", _SEEN_BYTES) or b"").decode(errors="replace").split())]
        tips = sorted(self.reached[0])
        held = {"GIT_DIR": str(work)}
        if tips:
            self._git(["update-ref", "--stdin"], env=held, cwd=work, input="".join(f"create {_READ_THERE}/{n} {c}\n" for n, c in enumerate(tips)))
        try:
            return super()._cut(git, work, mains=mains, kept=kept)
        finally:
            if tips:
                self._git(["update-ref", "--stdin"], env=held, cwd=work, input="".join(f"delete {_READ_THERE}/{n}\n" for n in range(len(tips))))

    def _readers(self) -> list[tuple[Path, bool]]:
        """Every repository in the place that reads the folder's history's objects as its own, and whether it reads them as it must.

        A thread's, under ``clones/``, and each repository the place keeps
        set aside whole, which is at the same depth: the one line of either
        names this history.  One reads them as it must where the history
        holds every pack its note names and it holds no link: it is whole,
        and a pruning keeps it so.  Any other is astray already, or none git
        may read: of it, the history keeps what its refs name, read as data,
        and nothing of it is touched.  Each is read as data, following no
        link.
        """
        lent = self._lent()
        found = []
        for folder in (self.repo.parent, self.aside_whole):
            if folder.is_symlink() or not folder.is_dir():
                continue
            for name in sorted(os.listdir(folder)):
                ours = re.fullmatch(_NAMED, name) if folder == self.repo.parent else _WHOLE.fullmatch(name) and name.endswith(".repository")
                repo = folder / name
                if not ours or repo.is_symlink() or not repo.is_dir() or not _reads_the_history(repo):
                    continue
                noted = _read_as_data(repo / _BORROWED, _SEEN_BYTES)
                packs = noted.decode(errors="replace").split() if noted is not None else None
                whole = packs is not None and all(_ID.fullmatch(pack) for pack in packs) and set(packs) <= lent and not _linked(repo)
                found.append((repo, whole))
        return found

    def _reached(self, work: Path) -> set[str]:
        """The history's commits each repository in the place reaches from its refs, read in *work*, the history before its cut.

        Its refs, its ``HEAD`` and its copy's name some.  Its own commits
        lead to others: the base a snapshot was taken on, the turn a
        set-aside snapshot was written after, ``main`` with your edits on it
        as it last read them.  Each is kept as the cut keeps a tip, with its
        files.  What only a reflog names is the window's to keep or let go
        (:meth:`_letting_go`).
        """
        refs = sorted(set(self._durable_refs().values()))
        found: set[str] = set()
        for repo, whole in self._readers():
            named = _named_by(repo)
            found |= named.refs
            if not whole:
                continue
            seen = _seen_from(repo, work)
            commits = [commit for commit, kind in self._kinds(seen, named.refs).items() if kind == "commit"]
            if commits:
                walked = self._git(
                    ["rev-list", "--parents", "--stdin"], env=seen, cwd=work,
                    input="".join(f"{commit}\n" for commit in commits) + "".join(f"^{ref}\n" for ref in refs),
                )
                found |= set(walked.split())
        return {commit for commit, kind in self._kinds({"GIT_DIR": str(work)}, found).items() if kind == "commit"}

    def _kinds(self, env: dict[str, str], ids: set[str]) -> dict[str, str]:
        """Of *ids*, those git finds in the objects *env* reads, each with its kind."""
        if not ids:
            return {}
        said = self._git(["cat-file", "--batch-check"], env=env, cwd=Path(env["GIT_DIR"]), input="".join(f"{i}\n" for i in sorted(ids)))
        return {line[:40]: line.split()[1] for line in said.splitlines() if not line.endswith(" missing")}

    def _letting_go(self, work: Path, new: str, gone: set[str]) -> None:
        """Have each repository in the place that reads the history as it must read all it names without the packs *gone*; then they go.

        Each in turn, and each step safe to cut, since every pack is still
        there until the last repository has done (:meth:`_made_whole`).
        Then the history's ``shallow``, the cut's, is the repository's own,
        and its note names the new pack and none of *gone*: at no point does
        it name a pack the history no longer holds, nor read one it does not
        name.  Each written whole, and on the disk before the packs go.

        Each is read with the history cut where this cut cuts it and where
        the last one did: a commit that was cut stays cut, its parents gone
        with that pruning, though this cut keeps nothing that leads to it.
        """
        cut = (work / "shallow").read_bytes() if (work / "shallow").is_file() else b""
        tips = set(self._git(["for-each-ref", "--format=%(objectname)"], env={"GIT_DIR": str(work)}, cwd=work).split())
        kept, before = (self.reached[0], self.reached[1]) if self.reached else (set(), set())
        kept = tips | kept
        if before - set(cut.decode().split()):
            _replace(work / "shallow", "".join(f"{commit}\n" for commit in sorted({*before, *cut.decode().split()})).encode())
        fresh, going = new.removeprefix("pack-"), {name.removeprefix("pack-") for name in gone}
        for repo, whole in self._readers():
            if not whole:
                continue
            self._made_whole(repo, work, kept)
            if cut:
                _durably(repo / "shallow", cut)
            elif os.path.lexists(repo / "shallow"):
                _removed(repo / "shallow")
            noted = set((_read_as_data(repo / _BORROWED, _SEEN_BYTES) or b"").decode(errors="replace").split())
            _durably(repo / _BORROWED, "".join(f"{pack}\n" for pack in sorted({*noted, fresh} - going)).encode())

    def _made_whole(self, repo: Path, work: Path, kept: set[str]) -> None:
        """Have *repo* read all it names from its own objects and the history's as cut in *work*, before the old packs go.

        Its refs reach nothing the cut let go: it kept what they reach
        (:meth:`_reached`).  A reflog names every commit a ref of the
        repository's ever was, its branch's every landing and snapshot among
        them, and nothing reads it but git: an entry whose commit, or a
        commit it leads to, is neither the repository's own nor the cut's is
        let go, as the window let go of it, so git packs the repository
        again whole.  Of what its refs, its other reflog entries and its
        indexes lead to, whatever is neither its own nor the cut's it takes
        back, in a pack of its own: a file only an old commit held, which
        its copy's index or a snapshot of its own names.  *kept* are the
        cut's tips.
        """
        named = _named_by(repo)
        if None in named.logs.values():
            raise HistoryError(f"a repository in the place holds a reflog longer than a pruning reads: {repo.name}")
        own = _held_in(repo / "objects")
        seen = _seen_from(repo, work)
        logs = {log: lines for log, lines in named.logs.items() if lines is not None}
        logged = {i for lines in logs.values() for line in lines for i in _logged(line)}
        kinds = self._kinds(seen, named.refs | logged)
        broken: set[str] = set()
        walked = [c for c in sorted(logged) if kinds.get(c) == "commit"]
        if walked:
            listed = self._git(
                ["rev-list", "--parents", "--topo-order", "--stdin"], env=seen, cwd=work,
                input="".join(f"{c}\n" for c in walked) + "".join(f"^{c}\n" for c in sorted(kept)),
            ).splitlines()
            parents = {line[:40]: line.split()[1:] for line in listed}
            # Not reached from the cut's tips: the repository's own, or what the cut let go; and what leads to that.
            broken = set(parents) - own
            for commit in reversed(listed):
                if any(parent in broken for parent in parents[commit[:40]]):
                    broken.add(commit[:40])
        for log, lines in logs.items():
            left = [line for line in lines if not set(_logged(line)) & broken]
            if len(left) != len(lines):
                _durably(log, b"".join(line + b"\n" for line in left))
        tips = sorted(i for i in kinds if i not in broken)
        listed = set()
        if tips:
            listed |= {line[:40] for line in self._git(
                ["rev-list", "--objects", "--stdin"], env=seen, cwd=work,
                input="".join(f"{i}\n" for i in tips) + "".join(f"^{c}\n" for c in sorted(kept)),
            ).splitlines()}
        for index in named.indexes:
            listed |= {line[:40] for line in self._git(
                ["rev-list", "--objects", "--indexed-objects"], env={**seen, "GIT_INDEX_FILE": str(index)}, cwd=work,
            ).splitlines()}
        missing = sorted(set(self._kinds(seen, listed - own)) - set(self._kinds({"GIT_DIR": str(work)}, listed - own)))
        if missing:
            self._git(["pack-objects", "-q", str(repo / "objects" / "pack" / "pack")], env=seen, cwd=work, input="".join(f"{i}\n" for i in missing))
            logger.warning("A repository in the place took back %d objects a pruning let go of in the history: %s", len(missing), repo.name)

    def hand_off(self, **kwargs: Any) -> dict:
        _refuse(_NO_HELPER)

    def hand_back(self, **kwargs: Any) -> dict:
        _refuse(_NO_HELPER)

    def keep_apart(self, **kwargs: Any) -> dict:
        _refuse(_NO_HELPER)

    def drop_hand_off(self, *args: Any, **kwargs: Any) -> dict:
        _refuse(_NO_HELPER)

    def opened(self) -> dict:
        _refuse(_NO_HELPER)

    def _to_main(self) -> bool:
        """Move a copy with nothing unlanded, and its base, to ``main`` as the folder is now; whether it moved.

        Its files, then its branch, then its base, with :attr:`moving` naming where it is going
        from before the first to after the last: cut anywhere, the next act finishes it (:meth:`_finish_move`).
        """
        tip = self.snapshot("before a turn")
        if self._tree(tip) != self._tree(self._main("rev-parse", self.base)):
            return False
        begun = int(time.time())
        main = self._take().get(MAIN)
        if main is not None:
            self._main("update-ref", MAIN, main)
        self._read_real(main)
        tree, on = self._main("write-tree"), self._main("rev-parse", MAIN)
        if tree != self._tree(on):
            # Committed from the index as it is, as the first open does: ``git commit`` would look at every real file once more.
            you = {"name": self.user, "email": f"user:{self.user}@surogate"}
            self._main("update-ref", MAIN, self._main(*_as(you), "commit-tree", tree, "-p", on, "-m", "Your changes"))
        # A file saved in the second the read began is read again by the next look, as at the first open.
        os.utime(self.repo / "index", (begun, begun))
        start = self._main("rev-parse", MAIN)
        # Where the copy is going, before the first of its files moves: a move cut from here on is finished from it.
        self._main("update-ref", self.moving, start)
        self._switch(tip, start)
        self._main("update-ref", self.base, start)
        # Its base is the folder as it is now: what the copy holds beside it was there before this turn.
        self._remember()
        self._main("update-ref", "-d", self.moving)
        return True

    def _off(self) -> str | None:
        """Why the folder gets no history, or None: by the excludes alone, and no link is followed.

        ``cap``: it holds more files than history tracks.  ``names``: a file
        or a folder in it has a name that is not UTF-8, which no answer to
        this computer could carry.
        """
        count = 0
        for at, folders, files in os.walk(self.project):
            inside = os.path.relpath(at, self.project)
            prefix = "" if inside == "." else f"{inside}/"
            folders[:] = [f for f in folders if _EXCLUDED.search(f"{prefix}{f}/") is None]
            tracked = [f for f in files if _EXCLUDED.search(f"{prefix}{f}") is None]
            if not all(_utf8(name) for name in (*folders, *tracked)):
                return "names"
            count += len(tracked)
            if count > HISTORY_CAP:
                return "cap"
        return None

    def _pin(self) -> None:
        """Put right what an earlier guest left in the place, once, before this history's first git.

        The guest that wrote the place is not this one, and nothing it left
        is trusted.  Whatever would lead git out of the place, have it run a
        program, or have it read as an object what is no file of the
        repository's, is taken out before git runs:

        - a link, or anything else that is no folder, where the place keeps
          its repositories, its copies, what waits beside them and what it
          sets aside, or this thread's own: removed;
        - a link, or anything else that is no file, where the making of this
          thread's copy is marked: removed, as no mark of this module's;
        - what a removal that was cut left of this thread's repository or
          copy, under the name it was going by: removed;
        - anything but files and folders in the folder's history, in any
          folder of it: the history is refused whole, before a byte is
          written through it;
        - anything but files and folders in the thread's repository, a link
          among its loose objects as any other: the repository is set aside
          as it is, with no git run in it, and its next open makes it again;
        - in the history's objects, anything but its packs and their
          indexes, which is all a push writes there: removed;
        - in both, a ``commondir``, ``hooks``, ``modules`` or alternates,
          which name another repository or a program: removed, but for the
          thread's repository's own alternates where they are the one line
          that borrows the history's objects; and each config is the one
          this module writes;
        - in the thread's repository, the attributes convert nothing, no
          worktree is there but the copy's, and the copy's own git folder
          names its repository, its copy and its branch;
        - a thread's repository that does not borrow the history as it must
          (:meth:`_borrows`) is read by no git, and its next open makes it
          again; one that does notes the history's packs as they are now.

        Each file is replaced, never written through.
        """
        if self.pinned:
            return
        for folder in (self.repo.parent, self.copy.parent, self.waiting.parent, self.aside_whole, self.repo, self.copy, self.waiting):
            if os.path.lexists(folder) and (folder.is_symlink() or not folder.is_dir()):
                _removed(folder)
        if os.path.lexists(self.making) and (self.making.is_symlink() or not self.making.is_file()):
            _removed(self.making)
        for left in (self.repo, self.copy):
            if os.path.lexists(going := left.with_name(f"{left.name}{_GOING}")):
                _removed(going)
        if os.path.lexists(self._pruning):
            # A pruning's work that was cut: it links packs the history may have let go, which it would keep on the disk.
            _removed(self._pruning)
        if os.path.lexists(self.store) and (self.store.is_symlink() or not self.store.is_dir() or _linked(self.store)):
            raise HistoryError(
                "refused the project's history: something in it is neither a file nor a folder", code=HISTORY_REFUSED,
            )
        if self.repo.is_dir() and _linked(self.repo):
            self._set_aside_whole(self.repo)
            self._asides_whole()
        _packs_alone(self.store / "objects")
        for repo, config in ((self.repo, _CONFIG), (self.store, _STORE_CONFIG)):
            if not repo.is_dir():
                continue
            for name in _REDIRECTS:
                if os.path.lexists(repo / name) and not (repo == self.repo and name == _BORROWS and self._lends()):
                    _removed(repo / name)
            if (repo / "HEAD").exists() or os.path.lexists(repo / "config"):
                _pinned(repo / "config", config)
        if (self.repo / "HEAD").exists():
            # A request ended by its bound, or by a stop, leaves its git's locks: one request
            # runs on a place at a time (the agent's turns), so any lock here is a dead one's.
            for held in (self.repo, self.repo / "refs", self.repo / "worktrees"):
                for lock in (held.glob("*.lock") if held == self.repo else held.rglob("*.lock")):
                    lock.unlink(missing_ok=True)
            (self.repo / "info").mkdir(exist_ok=True)
            _pinned(self.repo / "info" / "attributes", _ATTRIBUTES)
            if (self.repo / "worktrees").is_dir():
                for other in (self.repo / "worktrees").iterdir():
                    if other != self._admin:
                        _removed(other)
            if self._admin.is_dir():
                _pinned(self._admin / "commondir", "../..\n")
                _pinned(self._admin / "gitdir", f"{self.copy / '.git'}\n")
                _pinned(self._admin / "HEAD", f"ref: {self.branch}\n")
                if os.path.lexists(self._admin / "config.worktree"):
                    _removed(self._admin / "config.worktree")
            if self._borrows():
                self._borrow()
            else:
                self.astray.append(True)
        self.pinned.append(True)

    def _lends(self) -> bool:
        """Whether the thread's repository's alternates are the one line that borrows the folder's history's objects: a file of its own, read as data."""
        return _read_as_data(self.repo / _BORROWS, len(_ALTERNATES) + 1) == _ALTERNATES.encode()

    def _lent(self) -> set[str]:
        """The folder's history's packs a thread's repository may read objects from, each by its id: those whose pack and index are both there."""
        try:
            with os.scandir(self.store / "objects" / "pack") as entries:
                held = {named.groups() for entry in entries if (named := _PACK.fullmatch(entry.name)) and entry.is_file(follow_symlinks=False)}
        except (FileNotFoundError, NotADirectoryError):
            return set()
        return {pack for pack, kind in held if kind == "pack" and (pack, "idx") in held}

    def _borrows(self) -> bool:
        """Whether the thread's repository reads the objects of the folder's history, and of no other store, as it did.

        Its alternates are the one line, and each of the history's packs its
        note names (:meth:`_borrow`) is in the history still.  Otherwise it
        may name what neither it nor the history holds: a repack lets go of
        every object its alternates find elsewhere, and a history taken away
        by hand and made again by another thread's first copy holds none of
        the packs it read from, whatever its name.  Read as data, following
        no link.
        """
        noted = _read_as_data(self.repo / _BORROWED, _SEEN_BYTES)
        if not self._lends() or noted is None:
            return False
        packs = noted.decode(errors="replace").split()
        return all(_ID.fullmatch(pack) for pack in packs) and set(packs) <= self._lent()

    def _borrow(self) -> None:
        """Have the thread's repository read the folder's history's objects where they lie, and note the history's packs it may read them from.

        Its alternates are one line, written whole: git reads the objects in
        the history's packs as its own, so the repository holds no second
        copy of what the history holds.  An object there is not written
        here, and one written here before the history held it goes at the
        next repack (:meth:`_pack`).  Each pack is noted before anything is
        read from it, so that a history that no longer holds one is known
        (:meth:`_borrows`).  The repository reads the history's commits as
        the history does, cut where the history's are.  No git runs here:
        :meth:`_pin` calls it before it has ended.
        """
        (self.repo / "objects" / "info").mkdir(parents=True, exist_ok=True)
        _pinned(self.repo / _BORROWED, "".join(f"{pack}\n" for pack in sorted(self._lent())))
        _pinned(self.repo / _BORROWS, _ALTERNATES)
        cut = "".join(f"{commit}\n" for commit in self._durable_shallow())
        if cut:
            _pinned(self.repo / "shallow", cut)
        elif os.path.lexists(self.repo / "shallow"):
            _removed(self.repo / "shallow")


def _refuse(why: str) -> NoReturn:
    """Refuse what a folder's history takes no part in, in words."""
    raise HistoryError(f"refused the request: {why}", code=NOT_A_REQUEST)


def _utf8(name: str) -> bool:
    """Whether *name*, as the system gave it, is UTF-8: a byte that is not comes as a lone surrogate."""
    try:
        name.encode()
    except UnicodeEncodeError:
        return False
    return True


def _walked(path: object) -> bool:
    """Whether *path* names a file in the folder as a landing names one: spelt as it is walked, from the folder's top, down, each part a name."""
    if not (isinstance(path, str) and _utf8(path)) or "\0" in path:
        return False
    parts = PurePosixPath(path)
    return not parts.is_absolute() and bool(parts.parts) and ".." not in parts.parts and str(parts) == path


def _part(picked: list[dict]) -> dict:
    """*picked*, a pickup's files from where its asker has them, as one answer names them: those that fit it, and how many are left.

    As many as :data:`_PICKED_CHARS` holds of them as JSON spells them,
    every character past the basic ones as its escape, which is never less
    than the desktop counts; and never more than a list of an answer holds.
    """
    size = 0
    for n, entry in enumerate(picked):
        size += len(json.dumps(entry)) + 2
        if n and (size > _PICKED_CHARS or n == HISTORY_CAP):
            return {"picked_up": picked[:n], "more": len(picked) - n}
    return {"picked_up": picked, "more": 0}


def _left_in(message: list[str]) -> list[str]:
    """The files a landing's own commit names as left out, each read as data; refused where one is no file of the folder."""
    left = []
    for line in message:
        if line.startswith(f"{_LEFT}: "):
            try:
                path = json.loads(line[len(_LEFT) + 2:])
            except (ValueError, RecursionError):
                path = None
            if not _walked(path):
                raise HistoryError(
                    "refused the project's history: a landing names as left out what is no file of the folder", code=HISTORY_REFUSED,
                )
            left.append(path)
    return left


def _linked(top: Path) -> bool:
    """Whether the repository at *top* holds anything but files and folders: a link, a pipe, a device.

    Git follows a link wherever it finds one: it takes a linked folder for
    its own, writes its reflogs and a commit's message through a linked
    file, and reads one among its loose objects as the object it is named
    for.  So no folder is left out, the loose objects' own included.
    """
    folders = [top]
    while folders:
        at = folders.pop()
        with os.scandir(at) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    folders.append(Path(entry.path))
                elif not entry.is_file(follow_symlinks=False):
                    return True
    return False


def _packs_alone(objects: Path) -> None:
    """Make the folder's history's *objects* what its pushes write there: ``pack``, holding packs and their indexes alone.

    What a thread's repository borrows is read there, and git reads all of
    it.  Anything else is an earlier guest's: a loose object, which a
    thread's git would read as the object it is named for; ``info``, with
    its alternates and its commit graph; a multi-pack index, a bitmap, a
    reverse index, a promisor or a keep mark.  A link among them is no
    file: :func:`_linked` has refused the history whole for it first.
    """
    if not objects.is_dir():
        return
    for entry in list(objects.iterdir()):
        if entry.name != "pack":
            _removed(entry)
    if (objects / "pack").is_dir():
        for entry in list((objects / "pack").iterdir()):
            if _PACK.fullmatch(entry.name) is None or not entry.is_file():
                _removed(entry)


def _reads_the_history(repo: Path) -> bool:
    """Whether the repository *repo* reads the folder's history's objects as its own: its alternates are the one line, read as data."""
    return _read_as_data(repo / _BORROWS, len(_ALTERNATES) + 1) == _ALTERNATES.encode()


@dataclass(frozen=True)
class _Named:
    """What a repository names, read as data: the objects its refs and ``HEAD`` name, each reflog's lines, and its indexes.

    A reflog is None where it is longer than a request reads.
    """

    refs: set[str]
    logs: dict[Path, list[bytes] | None]
    indexes: list[Path]


def _named_by(repo: Path) -> _Named:
    """What the repository *repo* names, its copy's own among it, read where it lies as data and following no link.

    Its packed refs, peeled ones too, each loose ref and each ``HEAD`` that
    names an object and not another ref; each reflog, a line an entry,
    which names the object a ref was and the one it became; and its main
    index and its copy's.  What a write killed part way left staged beside
    a file is none of git's.
    """
    refs: set[str] = set()
    logs: dict[Path, list[bytes] | None] = {}
    indexes: list[Path] = []
    tops = [repo]
    if (repo / "worktrees").is_dir() and not (repo / "worktrees").is_symlink():
        tops += [at for at in sorted((repo / "worktrees").iterdir()) if at.is_dir() and not at.is_symlink()]
    for top in tops:
        files = [top / "HEAD", *([top / "packed-refs"] if top == repo else [])]
        for at, _, names in os.walk(top / "refs"):
            files += [Path(at, name) for name in names if not name.startswith(".~")]
        for file in files:
            for line in (_read_as_data(file, _SEEN_BYTES) or b"").decode(errors="replace").splitlines():
                if _ID.fullmatch(said := line.removeprefix("^").partition(" ")[0]):
                    refs.add(said)
        for at, _, names in os.walk(top / "logs"):
            for name in names:
                if not name.startswith(".~"):
                    held = _read_as_data(Path(at, name), _SEEN_BYTES + 1)
                    logs[Path(at, name)] = None if held is not None and len(held) > _SEEN_BYTES else (held or b"").splitlines()
        found = _looked_at(top / "index")
        if found is not None and stat.S_ISREG(found.st_mode):
            indexes.append(top / "index")
    return _Named(refs, logs, indexes)


def _logged(line: bytes) -> list[str]:
    """The objects a reflog's entry names: the one its ref was and the one it became, but none for a ref made or deleted."""
    return [said for field in line.split(b" ", 2)[:2] if _ID.fullmatch(said := field.decode(errors="replace")) and said != _ZERO]


def _looked_at(path: Path) -> os.stat_result | None:
    """What *path* is, not following a link; None where there is nothing."""
    try:
        return os.lstat(path)
    except OSError:
        return None


def _seen_from(repo: Path, work: Path) -> dict[str, str]:
    """A git's view of the objects *repo* reads, its own and the history's through its one line, in the repository *work*.

    Nothing of *repo*'s but its objects is read: no config of its, no
    hook, no ``shallow``.  Git reads *work*'s instead, the history's as
    a pruning has it.
    """
    return {"GIT_DIR": str(work), "GIT_OBJECT_DIRECTORY": str(repo / "objects")}


def _held_in(objects: Path) -> set[str]:
    """The objects the store *objects* holds itself, loose and in its packs, by their ids: none it reads through its alternates.

    Read as data.  A pack's index that is none git writes names nothing:
    what it holds is then counted as held elsewhere, which errs on the side
    of taking it back.
    """
    held: set[str] = set()
    try:
        with os.scandir(objects) as entries:
            for entry in entries:
                if re.fullmatch(r"[0-9a-f]{2}", entry.name) and entry.is_dir(follow_symlinks=False):
                    held |= {f"{entry.name}{name}" for name in os.listdir(entry.path) if re.fullmatch(r"[0-9a-f]{38}", name)}
        names = set(os.listdir(objects / "pack"))
    except (FileNotFoundError, NotADirectoryError):
        return held
    for name in names:
        if (found := _PACK.fullmatch(name)) and found[2] == "idx" and f"pack-{found[1]}.pack" in names:
            held |= _in_index(objects / "pack" / name)
    return held


def _in_index(path: Path) -> set[str]:
    """The objects a pack's index of git's second kind names, read as data and following no link; none for any other file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return set()
    with open(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return set()
        # Its kind, then how many objects it names below each first byte: the last is all of them.
        head = file.read(8 + 256 * 4)
        if len(head) != 8 + 256 * 4 or head[:8] != b"\xfftOc\x00\x00\x00\x02":
            return set()
        names = file.read(20 * int.from_bytes(head[-4:], "big"))
    return {names[at:at + 20].hex() for at in range(0, len(names) - 19, 20)}


def _durably(path: Path, data: bytes) -> None:
    """Write *path* anew holding *data*, beside it and then renamed over it, and on the disk with its folder's entry before it returns."""
    staged = path.with_name(f".~{path.name}")
    staged.write_bytes(data)
    _synced(staged)
    os.replace(staged, path)
    _synced(path.parent)


def _synced(path: Path) -> None:
    """fsync the file or the folder *path*, following no link."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_as_data(path: Path, most: int) -> bytes | None:
    """At most *most* bytes of the file at *path*, following no link; None where it is no file, or cannot be read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        # Looked at before it is read: a folder opens, and open() refuses it without closing it.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with open(fd, "rb", closefd=False) as file:
            return file.read(most)
    except OSError:
        return None
    finally:
        os.close(fd)


def _pinned(path: Path, text: str) -> None:
    """Make *path* a file holding *text*, replacing whatever is there."""
    if _read_as_data(path, len(text) + 1) == text.encode():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    _replace(path, text.encode())


def _removed(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _let_go(path: Path) -> None:
    """Remove the folder *path*, which holds nothing of a thread's own: renamed first, so that a cut leaves it whole under its name or not there."""
    going = path.with_name(f"{path.name}{_GOING}")
    os.rename(path, going)
    _removed(going)


def _asides_in(folder: int) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """What the set-aside folder open as *folder* holds of this module's naming, the oldest first: the kept, and those let go.

    Each is its count, its thread and its name.  Only a folder is one: a
    link or a file under such a name is nothing this module left.
    """
    found = sorted(
        (named[1], named[2], name) for name in os.listdir(folder)
        if (named := _WHOLE.fullmatch(name)) and stat.S_ISDIR(os.stat(name, dir_fd=folder, follow_symlinks=False).st_mode)
    )
    return [aside for aside in found if not aside[2].endswith(".gone")], [aside for aside in found if aside[2].endswith(".gone")]


def _on_disk(top: str | Path, inside: int | None = None) -> int:
    """What the folder *top*, in the folder open as *inside*, and all it holds take of the disk, in bytes: by handle, following no link."""
    return sum(
        os.stat(name, dir_fd=at, follow_symlinks=False).st_blocks * 512
        for _, folders, files, at in os.fwalk(top, dir_fd=inside) for name in (*folders, *files)
    )


def _emptied(name: str, inside: int) -> None:
    """Remove all that the folder *name* holds, in the folder open as *inside*, and leave it there: by its handle, through no link."""
    folder = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=inside)
    try:
        for held in os.listdir(folder):
            if stat.S_ISDIR(os.stat(held, dir_fd=folder, follow_symlinks=False).st_mode):
                shutil.rmtree(held, dir_fd=folder)
            else:
                os.unlink(held, dir_fd=folder)
    finally:
        os.close(folder)


def _state(found: os.stat_result) -> str:
    """What tells a file from a later write of it: which file it is, its size and when it was written.

    Not when it last changed otherwise: a link made to it, a new mode, or a
    move of the folder it is in changes that, and writes nothing.
    """
    return f"{found.st_ino}:{found.st_size}:{found.st_mtime_ns}"


def _recorded(name: str | Path, *, dir_fd: int | None = None) -> tuple[str, str] | None:
    """What git would record of the file or the link at *name*, its mode and its blob, following no link; None for anything else.

    With *dir_fd*, *name* is a name in the folder open as it.
    """
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        target = os.fsencode(os.readlink(name, dir_fd=dir_fd))
        return "120000", hashlib.sha1(b"blob %d\0" % len(target) + target).hexdigest()
    if not stat.S_ISREG(info.st_mode):
        return None
    # Non-blocking, so whatever took the file's place since the look answers at once; and closed here, as a folder
    # there is refused by open() without its handle closed.
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        with open(fd, "rb", closefd=False) as file:
            blob = hashlib.sha1(b"blob %d\0" % os.fstat(fd).st_size)
            while chunk := file.read(1 << 20):
                blob.update(chunk)
    finally:
        os.close(fd)
    return "100755" if info.st_mode & stat.S_IXUSR else "100644", blob.hexdigest()


def _ids(value: Any, where: str) -> None:
    """Refuse a request whose commit and blob ids are anything else: git would read one as an option."""
    for item in value if isinstance(value, list) else [value]:
        if item is not None:
            if not isinstance(item, str):
                raise HistoryError(f"refused the request: {where} holds what is not a commit id", code=NOT_A_REQUEST)
            try:
                _checked_id(item, where)
            except HistoryError as refused:
                # The id is the request's, not the history's: so is the refusal.
                raise HistoryError(str(refused), code=NOT_A_REQUEST) from None


def run(request: dict[str, Any]) -> dict[str, Any]:
    """One request of the desktop's, as the guest's agent passes it; its answer.

    ``{store, folder, thread, user, action, args}``: the folder's place and
    the folder as the guest mounts them, and the action with its arguments.
    Every id is checked before git sees it, and a path with a NUL is refused.
    """
    if not isinstance(request, dict):
        raise HistoryError("refused the request: it is not one", code=NOT_A_REQUEST)
    store, folder = request.get("store"), request.get("folder")
    thread, user, action, args = request.get("thread"), request.get("user"), request.get("action"), request.get("args", {})
    if not (isinstance(store, str) and isinstance(folder, str) and os.path.isabs(store) and os.path.isabs(folder)):
        raise HistoryError("refused the request: it names no place", code=NOT_A_REQUEST)
    if not (isinstance(thread, str) and _THREAD.fullmatch(thread)):
        raise HistoryError("refused the request: it names no thread", code=NOT_A_REQUEST)
    if not (isinstance(user, str) and _USER.fullmatch(user)):
        raise HistoryError("refused the request: it names no user", code=NOT_A_REQUEST)
    method, takes = _ACTIONS.get(action, (None, frozenset())) if isinstance(action, str) else (None, frozenset())
    if method is None or not isinstance(args, dict) or not args.keys() <= takes:
        raise HistoryError("refused the request: it names no action this computer's history takes", code=NOT_A_REQUEST)
    for key in ("commit", "turn", "main", "pickup", "commits", "since"):
        _ids(args.get(key), f"its {key}")
    if not isinstance(args.get("moves", True), bool):
        raise HistoryError("refused the request: whether it moves the copy is neither true nor false", code=NOT_A_REQUEST)
    if not isinstance(args.get("applied", []), list):
        raise HistoryError("refused the request: it names no files a landing applied", code=NOT_A_REQUEST)
    for change in args.get("applied", []):
        if not isinstance(change, dict) or not isinstance(change.get("path"), str) or "\0" in change["path"]:
            raise HistoryError("refused the request: a file it applied has no path", code=NOT_A_REQUEST)
        _ids([change.get("before"), change.get("after")], "a file it applied")
    if action == "prune":
        keep, now = args.get("keep"), args.get("now")
        if not isinstance(keep, list) or type(now) not in (int, float) or not 0 <= now < _LATEST:
            _refuse("a pruning takes the refs it keeps, and the time")
        if not all(isinstance(ref, str) and _KEPT.fullmatch(ref) for ref in keep):
            _refuse("a pruning keeps a thread's branch and its base, and no other ref")
    history = LocalHistory.at(Path(store), Path(folder), thread=thread, user=user)
    answer = getattr(history, method)(**args)
    if action == "snapshot":
        return {"hash": answer}
    return answer if isinstance(answer, dict) else {}


def main() -> int:
    """One JSON request on stdin, its answer on stdout; ``{"error": {"code": ..., "message": ...}}`` for one refused or failed.

    The code is the history's own for the refusal, and ``failed`` for
    anything else that went wrong; the message is its words, as they were.
    """
    try:
        answer = run(json.loads(sys.stdin.buffer.read()))
    except HistoryError as exc:
        answer = {"error": {"code": exc.code, "message": str(exc)}}
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError, KeyError) as exc:
        answer = {"error": {"code": FAILED, "message": str(exc)}}
    sys.stdout.write(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
