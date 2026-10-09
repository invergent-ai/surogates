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
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from surogates.sandbox.history import (
    _ATTRIBUTES,
    _OPEN_TIMEOUT,
    _TIMEOUT,
    HISTORY_CAP,
    HISTORY_EXCLUDES,
    HISTORY_REFUSED,
    MAIN,
    History,
    HistoryError,
    _as,
    _name,
    _replace,
)

#: Why a request was not answered, beside the cloud's two codes (history.py): a thread with no
#: whole copy, which its next open makes; and a file whose name history cannot record, which
#: nothing lands past until it is renamed.  Whoever asked goes by the code, never by the words.
NO_WHOLE_COPY = "no_whole_copy"
NAME_NOT_UTF8 = "name_not_utf8"

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


@dataclass(frozen=True)
class LocalHistory(History):
    store: Path | None = None  # the folder's history in the app's data
    #: Holds one entry once this history has put its place right (:meth:`_pin`): it lasts one request.
    pinned: list[bool] = field(default_factory=list, init=False, repr=False, compare=False)

    excludes: ClassVar[list[str]] = LOCAL_EXCLUDES
    platform: ClassVar[tuple[str, ...]] = LOCAL_PLATFORM

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
    def _taken(self) -> Path:
        """Git reads the folder's history in place: nothing is copied."""
        return self.store

    def _take(self) -> dict[str, str]:
        """The history's refs now, each checked as one of its own."""
        self._pin()
        self._check_durable()
        return self._durable_refs()

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
            if len(list((self.repo / "objects" / "pack").glob("*.pack"))) > _PACKS:
                self._git(["repack", "-a", "-d", "-q"], env={"GIT_DIR": str(self.repo)}, cwd=self.repo)
            return {"copy": "moved" if self._to_main() else "kept"}
        finally:
            _TIMEOUT.reset(budget)

    def changed(self) -> dict:
        """The files the copy changed since its base, committed or not: what a landing looks at in the folder first.

        A landing on the computer asks the file helper for these files'
        revisions before its pickup, so a save of yours after that look is
        either picked up or found at the apply.  Nothing is committed and
        no ref moves.
        """
        self._add_all(self._copy)
        names = self._copy("diff", "--cached", "--name-only", "--no-renames", "-z", self._main("rev-parse", self.base))
        return {"paths": sorted(n for n in names.split("\0") if n)}

    def record(self, **step: Any) -> dict:
        """The cloud's record, and then the copy is made the landing's files.

        A pod is made anew from the landing; a copy here outlives it.  A
        file the landing left out is the newer one in the copy from now on,
        as the thread's next turn must find it, and the thread's own version
        is the landing's second parent.  What history leaves out stays.
        """
        recorded = super().record(**step)
        self._copy("read-tree", "-u", "--reset", recorded["commit"])
        return recorded

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
