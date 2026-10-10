"""A folder's history and a thread's copy on the user's computer.

The cloud's :class:`History`, run by git in the desktop's VM: the guest's
agent runs this file as the guest's root, outside every chat's namespaces,
one request a run.  A folder's place in the app's data is laid out as a
pod sees a project:

    <store>/history.git/       the folder's history: packs and packed-refs, each written whole
    <store>/clones/<thread>/   a thread's own repository, fetched from it at depth 1
    <store>/threads/<thread>/  the thread's copy, where its tools and commands work
    <store>/set-aside/         a copy or a repository that was made again, as it was, under a name no thread has

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
read as anything's base.  So is a clean copy's move to ``main`` that was cut
between its files and its base.

Nothing a thread made is removed to make its copy again.  A copy that is
not whole, or a repository that is not, is renamed into the place's
set-aside folder as it is, where no request reads it as a thread's; only
one that holds nothing of the thread's own is removed.
"""

from __future__ import annotations

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
}
#: What git reads in a repository to find another, or a program, and this module never writes:
#: none is left in a thread's repository or in the folder's history.
_REDIRECTS = ("commondir", "hooks", "modules", "objects/info/alternates", "objects/info/http-alternates")
#: A file in a thread's repository once its first open has ended: without it, the repository's making was cut short.
_MADE = "made"
#: A thread's repository is packed again at its turn's start once it holds more packs than this.
_PACKS = 20
#: What a thread's copy held of its own when a record or a move made it other files is kept
#: for the last sixteen times that happened: the oldest goes for one more.
_ASIDE = 16
#: Where a place keeps a thread's copy and its repository that were made again, each renamed there as it was.
_SET_ASIDE = "set-aside"
#: A thread's name is one name, of these alone: it names a folder of the place's, and none of another's.
_NAMED = "[A-Za-z0-9_-]{1,64}"
#: What one is named there: the order it was set aside in, counted over the place; when, by this
#: computer's clock, in UTC; whose it was; and which of the two it is.
_WHOLE = re.compile(rf"([0-9]{{8}})-[0-9]{{8}}T[0-9]{{6}}Z-({_NAMED})\.(?:copy|repository)(\.gone)?")
#: A thread keeps the last four times a copy or a repository of its own was set aside there, and a
#: place the last sixteen of all its threads': the oldest goes for one more, and never a thread's
#: newest.  Of those of its own that went, a thread is told the last sixteen.
_ASIDE_WHOLE = 4
_ASIDE_WHOLE_IN_ALL = 16
_ASIDE_GONE = 16
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
    "fetch": ("fetch", frozenset({"commits", "saga", "since"})),
    "pickup": ("pickup", frozenset({"author", "trailers"})),
    "commit": ("commit_turn", frozenset({"author", "trailers", "pickup"})),
    "record": ("record", frozenset({"turn", "applied", "author", "trailers", "main", "pickup"})),
    "keep": ("keep", frozenset({"author", "trailers", "base"})),
    "forget": ("forget", frozenset({"saga", "applied"})),
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
    def aside(self) -> str:
        """Under it, what the copy held of its own when a record or a move made it other files: ``<nth>-<what it was made>``."""
        return f"refs/set-aside/{self.thread}"

    @property
    def aside_whole(self) -> Path:
        """Where the place keeps what was made again: a thread's copy or its repository, whole, as ``<nth>-<when>-<thread>.<which>``."""
        return self.store.parent / _SET_ASIDE

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
        set aside for a redirect in it, is made again as the first, and
        whatever is at its copy's path with it.  A copy whose folder was
        removed, whose own making was cut short, or whose index is gone, is
        made again from its branch.  Then a copy with nothing unlanded moves
        to ``main``'s tip, your edits picked up as at its first open
        (``moved``); one with unlanded work stays where it is, and so does
        its base (``kept``).

        What is made again is set aside first, whole, where it holds
        anything of the thread's own (:meth:`_make_way`).
        ``set_aside_folders`` names the thread's copies and repositories the
        place keeps so, each a folder of its set-aside folder, the oldest
        first, and ``set_aside_gone`` those that went for newer ones
        (:meth:`_asides_whole`): read there at every open, whatever else the
        open answers, a folder with no history as any other.

        Before the copy is read as anything's base, what a request of the
        thread's was cut in the middle of is finished, by the snapshot the
        move to ``main`` begins with (:meth:`_catch_up`).  ``set_asides``
        are the snapshots of what the copy held of its own when that, or a
        record, made it other files, the oldest first: all the thread's
        repository holds, read from its refs at every open, so that one
        whose answer was lost is told again.
        """
        budget = _TIMEOUT.set(_OPEN_TIMEOUT)
        try:
            self._pin()
            if not ((self.repo / "HEAD").is_file() and (self.repo / _MADE).is_file()):
                # No repository, or one whose first open did not end: neither it nor whatever
                # is at its copy's path is whole.
                self._make_way(whole=False)
                if (reason := self._off()) is not None:
                    return {"history": "off", "reason": reason, **self._asides_whole()}
                self.copy.parent.mkdir(parents=True, exist_ok=True)
                self._open()
                if (found := self._landing(self._take())) is not None:
                    # Made from the history as it is: no record of the thread's is owed to this copy.
                    self._main("update-ref", self.landed, found[0])
                (self.repo / _MADE).write_bytes(b"")
                return {"copy": "made", **self._asides_whole()}
            if os.path.lexists(self.copy) and not (self._admin / "index").is_file():
                # Its making was cut short, or its index is gone: git reads no file of it.
                self._make_way(whole=True)
            if not self.copy.exists():
                shutil.rmtree(self._admin, ignore_errors=True)
                self._git(
                    ["worktree", "add", "-q", "--lock", str(self.copy), f"threads/{self.thread}"],
                    env={"GIT_DIR": str(self.repo)}, cwd=self.repo,
                )
                (self.copy / ".git").unlink()
            if len(list((self.repo / "objects" / "pack").glob("*.pack"))) > _PACKS:
                try:
                    self._git(["repack", "-a", "-d", "-q"], env={"GIT_DIR": str(self.repo)}, cwd=self.repo)
                except HistoryError as why:
                    # Upkeep: a repository git cannot pack again still serves its thread's turn.
                    logger.warning("A thread's repository could not be packed again, and is left as it is: %s", why)
            moved = self._to_main()
            asides = [commit for _, commit in sorted(self._asides().values())]
            return {"copy": "moved" if moved else "kept", **({"set_asides": asides} if asides else {}), **self._asides_whole()}
        finally:
            _TIMEOUT.reset(budget)

    def _make_way(self, *, whole: bool) -> None:
        """Take the thread's copy out of the way of the one made again, and with it a repository that is not *whole*.

        Neither is removed while it holds anything of the thread's own: it
        is renamed into the place's set-aside folder as it is, the
        repository first, and no git runs in either.  A copy with no
        repository beside it is none a request reads, so a cut between the
        two leaves nothing taken for whole, and the open after it sets the
        copy aside under a name of its own.

        One that holds nothing of the thread's own is removed, as what a cut
        left of a copy's making is.  Which it is, is decided for both before
        either is touched, and never by a mark or an index of what is made
        again: a repository by the commits its refs name
        (:meth:`_names_its_own`), a copy by its files
        (:meth:`_holds_its_own`).
        """
        own = {}
        if whole:
            own[self.copy] = self._holds_its_own(None)
        elif os.path.lexists(self.repo) or os.path.lexists(self.copy):
            # First, and before anything is renamed: a history that is not the platform's own refuses the open.
            refs = self._take()
            if os.path.lexists(self.repo):
                own[self.repo] = self._names_its_own(refs)
            if os.path.lexists(self.copy):
                own[self.copy] = self._holds_its_own(refs)
        stem = None
        for left, kept in own.items():
            if kept:
                stem = self._set_aside_whole(left, stem)
            else:
                _removed(left)

    def _names_its_own(self, refs: dict[str, str]) -> bool:
        """Whether the thread's repository names a commit of the thread's own, read as data and never by git.

        Every ref it has is read where it lies, packed or loose.  ``main``
        there is the folder's files, never a thread's work, and a commit the
        folder's history holds is kept there, by *refs* a history of the
        platform's own.  A ref that names any other commit names a
        snapshot, a turn not pushed or what a record set aside, which this
        repository alone holds.  So does one that cannot be read as a ref:
        in doubt, it is the thread's own.
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
            if not all(_ID.fullmatch(commit) for commit in commits):
                return True
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

    def _holds_its_own(self, refs: dict[str, str] | None) -> bool:
        """Whether the thread's copy holds anything of the thread's own: a file that nothing kept holds at its name.

        Each file and each link in it is read, with the mode git would
        record for it.  Beside a whole repository, with no *refs*, what is
        kept is the thread's branch there, which the copy is made again
        from: a copy whose making was cut holds some of its files and no
        more.  Else it is the thread's branch and ``main`` in the folder's
        history, whose *refs* these are, and the folder itself as it is.  A
        file that is not there in the copy is nothing it holds, and neither
        is a folder.  What history leaves out is the thread's own, and so is
        anything that cannot be read, or is neither a file nor a link: in
        doubt, it is.
        """
        try:
            if refs is None:
                kept = [self._files(self.repo, self.branch)]
            else:
                kept = [self._files(self._taken, refs[ref]) for ref in (self.branch, MAIN) if ref in refs]
            # Git's own, which a copy's making leaves in it until it has ended.
            gits = ("100644", _blob_of(f"gitdir: {self._admin}\n".encode()))
            folders = [self.copy]
            while folders:
                with os.scandir(folders.pop()) as entries:
                    for entry in entries:
                        if entry.is_dir(follow_symlinks=False):
                            folders.append(Path(entry.path))
                            continue
                        name, found = os.path.relpath(entry.path, self.copy), _recorded(entry.path)
                        if found is None or not (
                            any(files.get(name) == found for files in kept)
                            or (refs is not None and self._in_folder(name) == found)
                            or (name, found) == (".git", gits)
                        ):
                            return True
            return False
        except (HistoryError, OSError, ValueError) as doubt:
            logger.warning("A thread's copy that could not be read whole is taken to hold its work: %s", doubt)
            return True

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
        """
        folder = _opened_folder(str(self.aside_whole), None, True)
        try:
            if stem is None:
                last = max((int(named[1]) for named in map(_WHOLE.fullmatch, os.listdir(folder)) if named), default=0)
                stem = f"{last + 1:08d}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{self.thread}"
            name = f"{stem}.{'repository' if left == self.repo else 'copy'}"
            if not _WHOLE.fullmatch(name):
                raise HistoryError("the place has no name left for what is set aside")
            os.rename(left, name, dst_dir_fd=folder)
        finally:
            os.close(folder)
        logger.warning("Set aside whole as %s, to be made again: %s", name, left)
        return stem

    def _asides_whole(self) -> dict[str, list[str]]:
        """Bound what the place keeps set aside whole, and what an open says of this thread's.

        ``set_aside_folders`` are the thread's copies and repositories kept
        there, by name, the oldest first.  A thread keeps the last
        ``_ASIDE_WHOLE`` times one was set aside, each time one count, and
        a place the last ``_ASIDE_WHOLE_IN_ALL`` of all its threads': the
        oldest goes for one more, whoever's it is, and never a thread's
        newest.  One that goes is first renamed ``<name>.gone``, from when
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
            found = sorted(
                (named[1], named[2], name) for name in os.listdir(folder)
                if (named := _WHOLE.fullmatch(name)) and stat.S_ISDIR(os.stat(name, dir_fd=folder, follow_symlinks=False).st_mode)
            )
            kept = [entry for entry in found if not entry[2].endswith(".gone")]
            gone = [entry for entry in found if entry[2].endswith(".gone")]
            times = list(dict.fromkeys(nth for nth, _, _ in kept))
            mine = list(dict.fromkeys(nth for nth, thread, _ in kept if thread == self.thread))
            newest = {thread: nth for nth, thread, _ in kept}.values()
            going = {*mine[:-_ASIDE_WHOLE], *(nth for nth in times[:-_ASIDE_WHOLE_IN_ALL] if nth not in newest)}
            for nth, thread, name in [entry for entry in kept if entry[0] in going]:
                try:
                    os.rename(name, f"{name}.gone", src_dir_fd=folder, dst_dir_fd=folder)
                except OSError as why:
                    logger.warning("What was set aside as %s could not be let go, and is kept: %s", name, why)
                    continue
                logger.warning("What was set aside as %s is let go, for what was set aside since", name)
                kept.remove((nth, thread, name))
                gone.append((nth, thread, f"{name}.gone"))
            gone.sort()
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

    def record(self, **step: Any) -> dict:
        """The cloud's record, and then the copy is made the landing's files.

        A pod is made anew from the landing; a copy here outlives it.  A
        file the landing left out is the newer one in the copy from now on,
        as the thread's next turn must find it, and the thread's own version
        is the landing's second parent.  What history leaves out stays.
        ``set_aside`` is a snapshot of what the copy held beyond its turn,
        written after the turn was committed, where making the copy the
        landing's took it away; None where it took nothing.  It is kept on
        a ref of the thread's repository, and is not one the copy is put
        back to (:meth:`restore`).

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
        files now.

        What the copy holds that is neither the turn's file nor the
        landing's is set aside first, on a ref (:meth:`_set_aside`).  A copy
        whose index is the landing's already was made so, git writing the
        index last, and is left as it is, with whatever the thread has
        written since.  Safe to cut anywhere: the ref that says it is done
        moves last.  Where it cannot be done the request is refused, and
        nothing was read from the copy.

        A turn, or a kept one, that this repository pushed and a cut kept it
        from noting is noted here too.  The thread's next push expects the
        branch where this repository last left it, and a copy here outlives
        the turn that pushed: left unnoted, every later landing of the
        thread's would be refused as one whose branch moved.  A branch this
        repository did not make is still that.
        """
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
                sides = [commit for commit in (landing, turn) if self._has(commit)]
                if self._beyond(held, sides):
                    self._set_aside(held, turn, onto=sides[-1])
                self._copy("read-tree", "-u", "--reset", landing)
            for ref in (self.branch, self.base, self.synced, self.landed):
                self._main("update-ref", ref, landing)
        except HistoryError as why:
            if why.code != FAILED:
                raise
            raise HistoryError(
                "refused the request: a landing of this thread's is in the history, and its copy "
                f"could not be made the landing's files: {why}", code=RECORD_UNFINISHED,
            ) from None

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
            path = PurePosixPath(change["path"])
            # Spelt as it is walked: from the folder's top, down, each part a name.
            if path.is_absolute() or not path.parts or ".." in path.parts or str(path) != change["path"]:
                _refuse("a file it applied has no path in the folder")
            if "before" not in change:
                _refuse("a file it applied has no version from before it")
            files.append((path.parts, change["before"]))
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
        """Move a copy with nothing unlanded, and its base, to ``main`` as the folder is now; whether it moved.

        Its files, then its branch, then its base, with :attr:`moving` naming where it is going
        from before the first to after the last: cut anywhere, the next act finishes it (:meth:`_finish_move`).
        """
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
        # Where the copy is going, before the first of its files moves: a move cut from here on is finished from it.
        self._main("update-ref", self.moving, start)
        self._switch(tip, start)
        self._main("update-ref", self.base, start)
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
          its repositories, its copies and what it sets aside, or this
          thread's own: removed;
        - anything but files and folders in the folder's history, in any
          folder of it: the history is refused whole, before a byte is
          written through it;
        - anything but files and folders in the thread's repository, a link
          among its loose objects as any other: the repository is set aside
          as it is, with no git run in it, and its next open makes it again;
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
        for folder in (self.repo.parent, self.copy.parent, self.aside_whole, self.repo, self.copy):
            if os.path.lexists(folder) and (folder.is_symlink() or not folder.is_dir()):
                _removed(folder)
        if os.path.lexists(self.store) and (self.store.is_symlink() or not self.store.is_dir() or _linked(self.store)):
            raise HistoryError(
                "refused the project's history: something in it is neither a file nor a folder", code=HISTORY_REFUSED,
            )
        if self.repo.is_dir() and _linked(self.repo):
            self._set_aside_whole(self.repo)
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


def _blob_of(data: bytes) -> str:
    """The id git gives a blob that holds *data*."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _recorded(name: str | Path, *, dir_fd: int | None = None) -> tuple[str, str] | None:
    """What git would record of the file or the link at *name*, its mode and its blob, following no link; None for anything else.

    With *dir_fd*, *name* is a name in the folder open as it.
    """
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        return "120000", _blob_of(os.fsencode(os.readlink(name, dir_fd=dir_fd)))
    if not stat.S_ISREG(info.st_mode):
        return None
    # Non-blocking, so whatever took the file's place since the look answers at once.
    with open(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd), "rb") as file:
        blob = hashlib.sha1(b"blob %d\0" % os.fstat(file.fileno()).st_size)
        while chunk := file.read(1 << 20):
            blob.update(chunk)
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
    if not isinstance(args.get("applied", []), list):
        raise HistoryError("refused the request: it names no files a landing applied", code=NOT_A_REQUEST)
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
