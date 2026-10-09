"""A folder's history and a thread's copy on the user's computer.

The cloud's :class:`History`, run by git in the desktop's VM: the guest's
agent runs this file as the guest's root, outside every chat's namespaces,
one request a run.  A folder's place in the app's data is laid out as a
pod sees a project:

    <store>/history.git/       the folder's history: packs and packed-refs, each written whole
    <store>/clones/<thread>/   a thread's own repository, fetched from it at depth 1
    <store>/threads/<thread>/  the thread's copy, where its tools and commands work

and the folder itself, shared read-only, is the real files.  Git reads the
history where it lies: it is on this computer's disk, and no thread's
command can reach it.  A landing's applies are not git's here: the desktop's
file helper writes the folder, and the record takes what it applied.

A place outlives the guest that wrote it, and nothing one boot's guest left
there is trusted by the next.  Before its first git, each run puts right
whatever would lead git out of the place or have it run a program, makes
again a repository it finds redirected, and overrides the rest.

A copy outlives its turn too, and a request can be cut at any step: by its
bound, a stop, a lost guest.  The history is safe by itself after one.  No
request writes a file of the folder; a copy cut short in its making is made
again before anything is read from it; and a push that counted is caught up
with, a landing's by making the copy the landing's files, before the copy is
read as anything's base.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from itertools import takewhile
from pathlib import Path
from typing import Any, ClassVar, NoReturn

from surogates.sandbox.history import (
    _ATTRIBUTES,
    _CHECKPOINT,
    _OPEN_TIMEOUT,
    _TIMEOUT,
    FAILED,
    HISTORY_CAP,
    HISTORY_EXCLUDES,
    HISTORY_REFUSED,
    MAIN,
    History,
    HistoryError,
    _as,
    _ID,
    _checked_id,
    _name,
    _replace,
)

#: Why a request was not answered, beside the cloud's two codes (history.py): a thread with no
#: whole copy, which its next open makes; a file whose name history cannot record, which
#: nothing lands past until it is renamed; a request that is none this history takes; and a
#: landing the history holds whose copy could not be made its files, so that nothing is read
#: from the copy; and a landing neither recorded nor put back, whose kept files are not to be
#: forgotten.  Whoever asked goes by the code, never by the words.
NO_WHOLE_COPY = "no_whole_copy"
NAME_NOT_UTF8 = "name_not_utf8"
NOT_A_REQUEST = "not_a_request"
RECORD_UNFINISHED = "record_unfinished"
LANDING_UNSETTLED = "landing_unsettled"

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
}
#: What git reads in a repository to find another, or a program, and this module never writes:
#: none is left in a thread's repository or in the folder's history.
_REDIRECTS = ("commondir", "hooks", "modules", "objects/info/alternates", "objects/info/http-alternates")
#: A file in a thread's repository once its first open has ended: without it, the repository's making was cut short.
_MADE = "made"
#: A thread's repository is packed again at its turn's start once it holds more packs than this.
_PACKS = 20
#: A file in a thread's repository from when a landing's record was finished until an answer says so.
_FINISHED = "finished"
#: What a thread's copy held beyond its turn when a record made it the landing's files is kept
#: for its last sixteen such records: the oldest goes for one more.
_ASIDE = 16
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
_USER = re.compile(r"[A-Za-z0-9_.@-]{1,128}")
#: Each action a request may name, the method it runs and the arguments it takes.
_ACTIONS: dict[str, tuple[str, frozenset[str]]] = {
    "open": ("open", frozenset()),
    "changed": ("changed", frozenset()),
    "snapshot": ("snapshot", frozenset({"reason"})),
    "restore": ("restore", frozenset({"commit"})),
    "fetch": ("fetch", frozenset({"commits", "saga"})),
    "pickup": ("pickup", frozenset({"author", "trailers"})),
    "commit": ("commit_turn", frozenset({"author", "trailers", "pickup"})),
    "record": ("record", frozenset({"turn", "applied", "author", "trailers", "main", "pickup"})),
    "keep": ("keep", frozenset({"author", "trailers", "base"})),
    "forget": ("forget", frozenset({"saga"})),
}
#: Why a folder's history takes no part in what the cloud's does besides.
_NO_WRITER = "a folder's history writes no file of the folder: the desktop's file helper lands a turn's files"
_NO_HELPER = "a thread on a computer has no helper with a copy of its own"


@dataclass(frozen=True)
class LocalHistory(History):
    store: Path | None = None  # the folder's history in the app's data
    #: Holds one entry once this history has put its place right (:meth:`_pin`): it lasts one request.
    pinned: list[bool] = field(default_factory=list, init=False, repr=False, compare=False)

    excludes: ClassVar[list[str]] = LOCAL_EXCLUDES
    platform: ClassVar[tuple[str, ...]] = LOCAL_PLATFORM

    def __post_init__(self) -> None:
        # The cloud tells a thread's pod its turn so that its copy takes up its helpers' hand-offs by it.
        if self.helper is not None or self.turn is not None:
            _refuse(_NO_HELPER)

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
    def aside(self) -> str:
        """Under it, what the copy held beyond its turn when a record made it the landing's files: ``<nth>-<turn>``."""
        return f"refs/set-aside/{self.thread}"

    @property
    def _taken(self) -> Path:
        """Git reads the folder's history in place: nothing is copied."""
        return self.store

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

    def _git(self, args: list[str], *, env: dict[str, str], cwd: Path, input: str | None = None) -> str:
        self._pin()
        if env.get("GIT_DIR") == str(self._admin) and not ((self._admin / "index").is_file() and self.copy.is_dir()):
            # A copy whose making was cut short holds some of its files: committed, the rest would
            # land as deletions.  Git writes a copy's index when the last of its files is written.
            raise HistoryError(
                "refused the request: this thread has no whole copy, and its next open makes one", code=NO_WHOLE_COPY,
            )
        if args[0] in ("add", "update-index"):
            # Every file read goes into one pack, whether a copy's add takes it or the readers of
            # the folder do: a loose object apiece is a file made through the share, and each
            # later look for one a round trip to this computer.
            args = ["-c", "core.bigFileThreshold=1", *args]
        elif args[:2] == ["worktree", "add"]:
            # A copy's files are made through the share too: several at once.
            args = ["-c", "checkout.workers=8", "-c", "checkout.thresholdForParallelism=200", *args]
        try:
            return super()._git(args, env={**env, **_PINNED}, cwd=cwd, input=input)
        except UnicodeDecodeError:
            raise HistoryError(
                "refused the request: a file's name is not UTF-8, which history cannot record", code=NAME_NOT_UTF8,
            ) from None

    def open(self) -> dict:
        """Make the thread's copy, or bring the one it has to its next turn.

        With none yet, the cloud's open: ``main`` is the folder as it is, by
        you, the branch starts there, and the copy is its worktree, with no
        ``.git`` in it.  A folder history cannot record gets none
        (``{"history": "off", "reason": ...}``, see :meth:`_off`), and nothing
        is made.  A repository whose first open did not end, cut short or
        taken away for a redirect in it, is made again as the first, and
        whatever is at its copy's path with it.  A copy whose folder was
        removed, or whose own making was cut short, is made again from its
        branch.  Then a copy with nothing unlanded moves to ``main``'s tip,
        your edits picked up as at its first open (``moved``); one with
        unlanded work stays where it is, and so does its base (``kept``).

        Before the copy is read as anything's base, a landing of the
        thread's that the history holds and the copy was never made the
        files of is finished (:meth:`_catch_up`).  ``finished`` then
        names it, once, with what was set aside: by this open, or by an
        act that came before it.
        """
        budget = _TIMEOUT.set(_OPEN_TIMEOUT)
        try:
            self._pin()
            if not ((self.repo / "HEAD").is_file() and (self.repo / _MADE).is_file()):
                # No repository, or one whose first open did not end: neither it nor whatever
                # is at its copy's path is whole.
                for left in (self.repo, self.copy):
                    if os.path.lexists(left):
                        _removed(left)
                if (reason := self._off()) is not None:
                    return {"history": "off", "reason": reason}
                self.copy.parent.mkdir(parents=True, exist_ok=True)
                self._open()
                if (found := self._landing(self._take())) is not None:
                    # Made from the history as it is: no record of the thread's is owed to this copy.
                    self._main("update-ref", self.landed, found[0])
                (self.repo / _MADE).write_bytes(b"")
                return {"copy": "made"}
            if os.path.lexists(self.copy) and not (self._admin / "index").is_file():
                # Its making was cut short: it holds some of its branch's files, and no more.
                _removed(self.copy)
            if not self.copy.exists():
                shutil.rmtree(self._admin, ignore_errors=True)
                self._git(
                    ["worktree", "add", "-q", "--lock", str(self.copy), f"threads/{self.thread}"],
                    env={"GIT_DIR": str(self.repo)}, cwd=self.repo,
                )
                (self.copy / ".git").unlink()
            self._catch_up()
            if len(list((self.repo / "objects" / "pack").glob("*.pack"))) > _PACKS:
                self._git(["repack", "-a", "-d", "-q"], env={"GIT_DIR": str(self.repo)}, cwd=self.repo)
            moved = self._to_main()
            finished = self._said()
            return {"copy": "moved" if moved else "kept", **({"finished": finished} if finished else {})}
        finally:
            _TIMEOUT.reset(budget)

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
        self._catch_up()
        super().restore(commit)

    def commit_turn(self, **step: Any) -> dict:
        self._catch_up()
        return super().commit_turn(**step)

    def keep(self, **step: Any) -> dict:
        self._catch_up()
        return super().keep(**step)

    def record(self, **step: Any) -> dict:
        """The cloud's record, and then the copy is made the landing's files.

        A pod is made anew from the landing; a copy here outlives it.  A
        file the landing left out is the newer one in the copy from now on,
        as the thread's next turn must find it, and the thread's own version
        is the landing's second parent.  What history leaves out stays.
        ``set_aside`` is a snapshot of what the copy held beyond its turn,
        written after the turn was committed, where making the copy the
        landing's took it away; None where it took nothing.

        Safe to repeat wherever ``main`` is by then: no lock is held across
        a computer's absence, so another thread may have landed since a try
        cut after its push.  The landing is found by its saga where the
        thread's own branch has it, and the copy of a thread that has worked
        on since is left alone.
        """
        self._catch_up()
        saga = f"Surogate-Saga: {dict(map(tuple, step['trailers']))['Surogate-Saga']}"
        found = self._landing(self._take())
        if found is not None and saga in found[2]:
            commit = found[0]
        else:
            commit = super().record(**step)["commit"]
            self._catch_up()
        aside = self._asides().get(step["turn"], (None, None))[1]
        # Told here, by this answer: the thread's next open has nothing left to say of it.
        self._said(commit)
        return {"commit": commit, "set_aside": aside}

    def _landing(self, refs: dict[str, str]) -> tuple[str, str, list[str]] | None:
        """The thread's landing where *refs*, the history's, still have its branch: it, its turn, and its message.

        A record's push moves the thread's branch and its base to the
        landing together, and the thread's next push, of a turn or of a
        kept one, moves the branch off its base again.  Read in the history
        itself: this repository may not hold the landing yet.
        """
        landing = refs.get(self.branch)
        if landing is None or refs.get(self.base) != landing:
            return None
        parents, message = self._stored(landing)
        if len(parents) != 2 or message[:1] != ["Landing"]:
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
        """Finish a record of this thread's that was cut after its push, before the copy is read as anything's base.

        The push is the moment a landing counts; making the copy the
        landing's files comes after it, and a request's bound, a stop or a
        lost guest can fall between.  The copy then still holds the turn's
        files, on a base that is the landing: read so, every file another
        thread landed would be one this thread deleted, and the thread's old
        version of a file the landing left out would be its change to the
        newer one.  So no act reads the copy before this: the history holds
        a landing of the thread's that :attr:`landed` does not name, and the
        copy is made its files now.

        What the copy holds beyond the turn and the landing is set aside
        first, on a ref (:meth:`_set_aside`).  A copy whose index is the
        landing's already was made so, git writing the index last, and is
        left as it is, with whatever the thread has written since.  Safe to
        cut anywhere: the ref that says it is done moves last.  Where it
        cannot be done the request is refused, and nothing was read from
        the copy.  A note is left for whoever is told of it: the record's
        own answer, or the thread's next open.

        A turn, or a kept one, that this repository pushed and a cut kept it
        from noting is noted here too.  The thread's next push expects the
        branch where this repository last left it, and a copy here outlives
        the turn that pushed: left unnoted, every later landing of the
        thread's would be refused as one whose branch moved.  A branch this
        repository did not make is still that.
        """
        if not (self.repo / "HEAD").is_file():
            return
        refs = self._take()
        found = self._landing(refs)
        if found is None:
            pushed = refs.get(self.branch)
            if pushed is not None and pushed != self._ref(self.synced) and self._has(pushed):
                self._main("update-ref", self.synced, pushed)
            return
        if self._ref(self.landed) == found[0]:
            return
        landing, turn, _ = found
        try:
            # First, and before anything is written: a copy that is not whole is its next open's to make.
            index = self._copy("write-tree")
            self._fetch(landing)
            if index != self._tree(landing):
                self._add_all(self._copy)
                held = self._copy("write-tree")
                if held not in {self._tree(commit) for commit in (landing, turn) if self._has(commit)}:
                    self._set_aside(held, turn)
                self._copy("read-tree", "-u", "--reset", landing)
            _replace(self.repo / _FINISHED, f"{landing} {turn}\n".encode())
            for ref in (self.branch, self.base, self.synced, self.landed):
                self._main("update-ref", ref, landing)
        except HistoryError as why:
            if why.code != FAILED:
                raise
            raise HistoryError(
                "refused the request: a landing of this thread's is in the history, and its copy "
                f"could not be made the landing's files: {why}", code=RECORD_UNFINISHED,
            ) from None

    def _set_aside(self, tree: str, turn: str) -> None:
        """Keep *tree*, what the copy holds, on the ref of *turn*'s set-aside, before a record takes it away.

        A snapshot the thread can be put back to (:meth:`restore`).  One
        taken before for the same turn, by a try cut part way through the
        copy's files, is its second parent: that one may hold a file this
        one no longer does.  A thread keeps its last ``_ASIDE``, by the
        order they were set aside in, which their names count.
        """
        kept = self._asides()
        ref, earlier = kept.get(turn, (None, None))
        if ref is None:
            last = max((int(name.rpartition("/")[2].partition("-")[0]) for name, _ in kept.values()), default=0)
            ref = f"{self.aside}/{last + 1:08d}-{turn}"
        before = [commit for commit in (self._copy("rev-parse", "HEAD"), earlier) if commit]
        self._main("update-ref", ref, self._copy(
            *_as(_CHECKPOINT), "commit-tree", tree, *(arg for parent in before for arg in ("-p", parent)),
            "-m", "Set aside before a landing's record",
        ))
        for old, _ in sorted({**kept, turn: (ref, None)}.values())[:-_ASIDE]:
            self._main("update-ref", "-d", old)

    def _asides(self) -> dict[str, tuple[str, str]]:
        """What the thread's records set aside, by the turn: each its ref and its snapshot."""
        kept = {}
        for line in self._main("for-each-ref", "--format=%(objectname) %(refname)", f"{self.aside}/").splitlines():
            commit, _, ref = line.partition(" ")
            if re.fullmatch(r"[0-9]{8}-[0-9a-f]{40}", ref.rpartition("/")[2]):
                kept[ref[-40:]] = (ref, commit)
        return kept

    def _said(self, only: str | None = None) -> dict | None:
        """The record that was finished and not yet told of, with what it set aside; told once.  With *only*, that landing's alone."""
        note = self.repo / _FINISHED
        try:
            landing, turn = note.read_text().split()
        except (FileNotFoundError, ValueError):
            return None
        if not (_ID.fullmatch(landing) and _ID.fullmatch(turn)) or only not in (None, landing):
            return None
        said = {"landing": landing, "set_aside": self._asides().get(turn, (None, None))[1]}
        note.unlink()
        return said

    def forget(self, *, saga: str) -> dict:
        """Whether what the landing of *saga* kept of the folder's files may be forgotten; refused while it may not.

        The file helper keeps each file a landing replaces until the landing
        is settled, for its put-back, and cannot tell when that is.  The
        history can: ``landing`` is the landing where ``main`` holds it,
        recorded, so that each replaced file is a version under it; or None
        where the landing was put back whole, no file its turn wrote or
        deleted being as the turn left it in the folder.  Asked before
        either, or of a saga the history holds no turn of, it is refused,
        and whoever asked forgets nothing.  It reads, and writes nothing.

        The turn is the thread's branch as the history has it, pushed before
        the landing's first apply, or the thread's own commit of it, where a
        turn kept since has moved the branch.  A file already as the turn
        left it before the landing, which the landing never wrote, counts as
        written: the kept files then stay until the landing is recorded.
        """
        if not (isinstance(saga, str) and saga):
            _refuse("it names no saga")
        said = f"Surogate-Saga: {saga}"
        self._init()
        refs = self._take()
        if (main := refs.get(MAIN)) is not None:
            # The newest first: a landing's own pickup, which carries its saga too, lies under it.
            log = self._git(["log", "--first-parent", "-z", "--format=%H%n%B", main], env={"GIT_DIR": str(self._taken)}, cwd=self._taken)
            for landing, message in _logged(log):
                if said in message:
                    return {"landing": landing}
        pushed, base = refs.get(self.branch), None
        if pushed is not None:
            parents, message = self._stored(pushed)
            pushed, base = (pushed, parents[0]) if parents and said in message else (None, None)
        if pushed is None and self._ref(self.base) and self._ref(self.branch):
            own = self._main("log", "--first-parent", "-z", "--format=%H%n%B", f"{self.base}..{self.branch}")
            pushed, base = next(
                ((turn, self._ref(self.base)) for turn, message in _logged(own) if said in message),
                (None, None),
            )
        if pushed is None:
            raise HistoryError(
                "refused the request: the history holds neither this landing nor its turn, and cannot tell what it wrote",
                code=LANDING_UNSETTLED,
            )
        self._fetch(pushed, base)
        versions, _ = self._diff(base, pushed)
        if any(self._real(path) == after for path, (_, after) in versions.items()):
            raise HistoryError(
                "refused the request: this landing was neither recorded nor put back: a file is in the folder as "
                "it left it, and what it replaced is kept for its put-back", code=LANDING_UNSETTLED,
            )
        return {"landing": None}

    def pickup(self, *, author: dict[str, str], trailers: list[list[str]], push: bool = False) -> dict:
        if push:
            _refuse("a folder's history records no routine's run: a pickup is a landing's first step here, pushed with its record")
        return super().pickup(author=author, trailers=trailers)

    def apply(self, *args: Any, **kwargs: Any) -> dict:
        _refuse(_NO_WRITER)

    def unapply(self, *args: Any, **kwargs: Any) -> dict:
        _refuse(_NO_WRITER)

    def _put(self, *args: Any, **kwargs: Any) -> list[str]:
        _refuse(_NO_WRITER)

    def _remove(self, *args: Any, **kwargs: Any) -> None:
        _refuse(_NO_WRITER)

    def prune(self, **kwargs: Any) -> dict:
        _refuse("a folder's history is not pruned: nothing on this computer cuts it back yet")

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
        """Move a copy with nothing unlanded, and its base, to ``main`` as the folder is now; whether it moved."""
        tip = self.snapshot("before a turn")
        if self._tree(tip) != self._tree(self._main("rev-parse", self.base)):
            return False
        begun = int(time.time())
        main = self._take().get(MAIN)
        self._fetch(main)
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
        self._switch(tip, start)
        self._main("update-ref", self.base, start)
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
          its repositories and copies, or this thread's own: removed;
        - anything but files and folders in the folder's history, in any
          folder of it: the history is refused whole, before a byte is
          written through it;
        - anything but files and folders in the thread's repository, a link
          among its loose objects as any other: the repository is removed,
          and its next open makes it again;
        - in both, a ``commondir``, ``hooks``, ``modules`` or alternates,
          which name another repository or a program: removed; and each
          config is the one this module writes;
        - in the thread's repository, the attributes convert nothing, no
          worktree is there but the copy's, and the copy's own git folder
          names its repository, its copy and its branch.

        Each file is replaced, never written through.
        """
        if self.pinned:
            return
        for folder in (self.repo.parent, self.copy.parent, self.repo, self.copy):
            if os.path.lexists(folder) and (folder.is_symlink() or not folder.is_dir()):
                _removed(folder)
        if os.path.lexists(self.store) and (self.store.is_symlink() or not self.store.is_dir() or _linked(self.store)):
            raise HistoryError(
                "refused the project's history: something in it is neither a file nor a folder", code=HISTORY_REFUSED,
            )
        if self.repo.is_dir() and _linked(self.repo):
            shutil.rmtree(self.repo)
        for repo, config in ((self.repo, _CONFIG), (self.store, _STORE_CONFIG)):
            if not repo.is_dir():
                continue
            for name in _REDIRECTS:
                if os.path.lexists(repo / name):
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
        self.pinned.append(True)


def _logged(log: str) -> list[tuple[str, list[str]]]:
    """``git log -z --format=%H%n%B``'s commits, each its id and its message line by line, by the line end alone."""
    return [(lines[0], lines[1:]) for lines in (entry.split("\n") for entry in log.split("\0") if entry)]


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


def _pinned(path: Path, text: str) -> None:
    """Make *path* a file holding *text*, replacing whatever is there."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        with open(fd, "rb") as file:
            if file.read(len(text) + 1) == text.encode():
                return
    except OSError:
        pass
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    _replace(path, text.encode())


def _removed(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


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
    for key in ("commit", "turn", "main", "pickup", "commits"):
        _ids(args.get(key), f"its {key}")
    for change in args.get("applied", []):
        if not isinstance(change, dict) or not isinstance(change.get("path"), str) or "\0" in change["path"]:
            raise HistoryError("refused the request: a file it applied has no path", code=NOT_A_REQUEST)
        _ids([change.get("before"), change.get("after")], "a file it applied")
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
