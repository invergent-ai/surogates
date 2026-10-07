"""A project's history and a thread's copy, in a shadow git repository.

Runs in a thread's pod.  The repository's ``main`` is the project's real
files at ``/project``; a thread works on its own copy, a locked worktree
of its branch ``threads/<id>`` at ``/workspace``, with no ``.git`` in it.
``refs/bases/<id>`` is the commit of ``main`` the branch's work started
from.  Git runs with ``GIT_DIR`` and ``GIT_WORK_TREE`` set, as the
checkpoint manager runs it, so no git state reaches either folder.

The history outlives the pod: it is a bare repository at ``_history/`` in
the project's files, every object in a pack and every ref in
``packed-refs``.  A pod opens by fetching ``main``, its thread's branch and
base at depth 1, and the project's lock holder pushes by writing a pack,
then ``packed-refs``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from itertools import takewhile
from pathlib import Path, PurePosixPath

from surogates.tools.utils.checkpoint_manager import DEFAULT_EXCLUDES

#: Where a thread's pod mounts the project's real files.
PROJECT_MOUNT = "/project"

#: The platform's own folders: never project files, never reported as unsaved.
PLATFORM_EXCLUDES = ("_history/", "_artifacts/", "_whiteboard/", ".threads/")

#: Paths that are neither in a copy nor in history.  ``*.log`` is tracked:
#: a log a user drops into a project is a document a thread is asked about.
HISTORY_EXCLUDES = [e for e in DEFAULT_EXCLUDES if e != "*.log"] + [
    # Office's owner files, LibreOffice's locks, Word's temp files.
    "~$*", ".~lock.*#", "~WRL*.tmp", "~WRD*.tmp", "*.tmp",
    # GNOME's save temp files, partial downloads, editor backups and locks.
    ".goutputstream-*", "*.crdownload", "*.part", "*~", ".#*", "*.swp",
    "Thumbs.db", "desktop.ini", "._*",
] + [f"/{folder}" for folder in PLATFORM_EXCLUDES]

#: A project with more files history would track than this has no history:
#: its threads work on the real files.
HISTORY_CAP = 50_000
#: A thread's pod lasts as long as its turn, however long that is: its copy
#: lives in it, and a deadline mid-turn would make the copy again from the
#: history.  It is deleted at the turn's end; a pod a killed worker left
#: behind goes after a day, where any other goes after an hour.
THREAD_POD_DEADLINE = 86_400

_GIT_TIMEOUT = 120
#: The open's git calls: the pod's ready bound of ten minutes, less a margin.
#: Through geesefs, a large project's fetch is bound by request latency.
_OPEN_TIMEOUT = 570
_TIMEOUT: ContextVar[int | None] = ContextVar("history_git_timeout", default=None)
_ZERO = "0" * 40
MAIN = "refs/heads/main"
_PACKED = "# pack-refs with: peeled fully-peeled sorted \n"
#: The pruning window: main's commits of this many days, never fewer than its last _PRUNE_LEAST.
PRUNE_DAYS = 90
_PRUNE_LEAST = 20
#: A project's history is pruned at most this often.
_PRUNE_EVERY = 86_400
#: What the pod takes from the bucket's history, which a thread's commands
#: can write: an id is 40 hex digits, and a ref one of the history's own.
_ID = re.compile(r"[0-9a-f]{40}")
_REF = re.compile(r"refs/(?:heads|bases|handoff|handoff-from|helpers)(?:/[A-Za-z0-9_.][A-Za-z0-9_.-]*)+")
#: Only the repository's own config, ignores and attributes: none from the
#: pod's home, where a thread's commands can write, and none from the
#: system.  Paths are read as spelt: a file may be named ``:notes.md``.
_HERMETIC = {
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1",
    "XDG_CONFIG_HOME": "/dev/null/none", "GIT_LITERAL_PATHSPECS": "1",
}
#: A file's bytes are history's as they are: no project, home or system
#: ``.gitattributes`` converts line endings or runs a filter on them.
_ATTRIBUTES = "* -text -filter -ident -working-tree-encoding\n"



def _name(glob: str) -> str:
    """*glob*, a pattern for one name, as a regex: its ``*`` and ``?`` never cross a ``/``."""
    return "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in glob)


#: The excludes, as one regex over a path: ``x/`` names a folder anywhere,
#: ``/x/`` the top folder, and any other a file or folder anywhere.
_EXCLUDED = re.compile("|".join(
    f"^{_name(p.strip('/'))}/" if p.startswith("/") else
    f"(?:^|/){_name(p.rstrip('/'))}/" if p.endswith("/") else
    f"(?:^|/){_name(p)}(?:/|$)"
    for p in HISTORY_EXCLUDES
))


def tracked(path: str) -> bool:
    """Whether history would track the project's file *path*, by the excludes.

    By them alone: a project's own ``.gitignore``, and a folder holding a
    git repository, leave out more, so a count of these errs high.
    """
    return _EXCLUDED.search(path) is None


class HistoryError(RuntimeError):
    """A history operation failed."""


class HistoryConflict(HistoryError):
    """A real file is not the version a landing expected."""


@dataclass(frozen=True)
class History:
    repo: Path      # the shadow repository: main, the branches, the objects
    project: Path   # the real files
    copy: Path      # this thread's worktree
    thread: str
    user: str       # who started the thread: main's first commit is theirs

    @property
    def branch(self) -> str:
        return f"refs/heads/threads/{self.thread}"

    @property
    def base(self) -> str:
        return f"refs/bases/{self.thread}"

    @property
    def durable(self) -> Path:
        """The project's history, in the bucket beside its files."""
        return self.project / "_history"

    @property
    def synced(self) -> str:
        """The branch as the durable history had it when this copy last took it up or pushed it."""
        return f"refs/synced/{self.thread}"

    # ------------------------------------------------------------------
    # The copy
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Fetch ``main``, the branch and its base; make ``main`` the real files; then the copy.

        Each is fetched at depth 1: a pod moves the project's current size,
        not its history.  ``main``'s index of the real files comes with them,
        so only a file whose size or time changed is read.  A difference
        between the real files and ``main`` is a commit on ``main`` by you:
        it is this pod's own, its copy's base, and is never recorded on
        ``main``.  A branch whose files are its base's has nothing unlanded:
        it starts again at ``main``, and so does its base.  With no history
        yet, ``main``'s first commit is the real files as they are.
        """
        budget = _TIMEOUT.set(_OPEN_TIMEOUT)
        try:
            self._open()
        finally:
            _TIMEOUT.reset(budget)

    def _open(self) -> None:
        begun = int(time.time())
        fresh = not (self.repo / "HEAD").exists()
        try:
            if fresh:
                self.repo.mkdir(parents=True, exist_ok=True)
                self._main("init", "-q", "-b", "main")
                self._main("config", "user.name", "Surogates Checkpoint")
                self._main("config", "user.email", "surogates@local")
                # The kept index decides by size and time alone: geesefs's
                # inode numbers and change times differ from pod to pod.
                self._main("config", "core.checkStat", "minimal")
                (self.repo / "info").mkdir(exist_ok=True)
                (self.repo / "info" / "exclude").write_text("\n".join(HISTORY_EXCLUDES) + "\n")
                (self.repo / "info" / "attributes").write_text(_ATTRIBUTES)
            refs = self._durable_refs()
            self._check_durable()
            self._fetch(*(refs.get(r) for r in (MAIN, self.branch, self.base)))
            you = {"name": self.user, "email": f"user:{self.user}@surogate"}
            if MAIN in refs:
                self._main("update-ref", MAIN, refs[MAIN])
                # geesefs trusts a listing for a second: one taken just before
                # another pod's landing would hide that landing's files.
                for folder in {"", *self._main("ls-tree", "-r", "-d", "-z", "--name-only", MAIN).split("\0")}:
                    _invalidate(self.project / folder)
                if (self.durable / "index").is_file():
                    # Its own time: git reads again an entry no older than the index.
                    shutil.copy2(self.durable / "index", self.repo / "index")
                # The index made main's: an entry that matches keeps its size and time.
                try:
                    self._main("read-tree", "-m", "-i", MAIN)
                except HistoryError:
                    # A cache git cannot read: left out, every real file is read.
                    (self.repo / "index").unlink(missing_ok=True)
                    self._main("read-tree", MAIN)
            self._add_all(self._main)
            if self._ref(MAIN) is None:
                self._main(*_as(you), "commit", "-q", "--allow-empty", "-m", "The project's files")
            elif self._main("write-tree") != self._tree(MAIN):
                self._main(*_as(you), "commit", "-q", "-m", "Your changes")
            if (self.repo / "index").is_file():
                # geesefs gives whole seconds: an entry from the second the
                # open began may be saved again unseen, so git reads it again.
                os.utime(self.repo / "index", (begun, begun))
        except Exception:
            if fresh:
                # Half made (a read of the real files failed): the next
                # readiness check makes it again.
                shutil.rmtree(self.repo, ignore_errors=True)
            raise
        main = self._main("rev-parse", MAIN)
        branch, base = refs.get(self.branch), refs.get(self.base)
        if branch is None or base is None or self._tree(branch) == self._tree(base):
            branch = base = main
        self._main("update-ref", self.branch, branch)
        self._main("update-ref", self.base, base)
        if self.branch in refs:
            self._main("update-ref", self.synced, refs[self.branch])
        # --lock: git gc must not prune a worktree whose .git file is gone.
        self._git(
            ["worktree", "add", "-q", "--lock", str(self.copy), f"threads/{self.thread}"],
            env={"GIT_DIR": str(self.repo)}, cwd=self.repo,
        )
        (self.copy / ".git").unlink()

    def snapshot(self, reason: str) -> str:
        """Commit the copy on the branch if it changed; the branch's tip."""
        self._add_all(self._copy)
        if self._copy("diff", "--cached", "--name-only"):
            self._copy("commit", "-q", "-m", reason)
        return self._copy("rev-parse", "HEAD")

    def restore(self, commit: str) -> None:
        """Put the copy back to *commit*, removing files it did not have.

        The copy's state is snapshotted first, so the restore can be undone.
        The branch does not move: it keeps the snapshots whose changes were undone.
        """
        self.snapshot(f"before restoring {commit[:8]}")
        self._copy("read-tree", "-u", "--reset", commit)

    # ------------------------------------------------------------------
    # Landing: the steps a landing saga runs, one call each
    # ------------------------------------------------------------------

    def commit_turn(self, *, author: dict[str, str], trailers: list[list[str]]) -> dict:
        """Commit the turn on the branch, and say what a landing would apply.

        ``changes`` are the files the turn changed since its base whose real
        file is still the base's version, or already the turn's.
        ``overlapped`` are those whose real file changed otherwise since, or
        that the real files cannot take as a file (a folder is there, or a
        file where its folder would be): a landing leaves them out.  Each
        says why: ``changed``, ``shape``, or ``with`` for one held with them.  A
        rename's two sides, and a file and a folder of one name, are left
        out together.  While any write of the turn is left out, or it wrote
        into a repository, every deletion git did not pair is too: it may be
        a move git could not see.  ``commit`` is None when the turn changed
        nothing since its base.  ``repositories`` are the folders holding a
        git repository that the turn wrote into: they never land.

        The turn, its base and the branch are pushed before the first apply,
        so whoever puts a file back after a crash can read both its versions.
        Safe to repeat: a turn already committed and pushed is used again.
        """
        self._add_all(self._copy)
        excluded, repositories, wrote_left_out = self._excluded()
        left_out = {"excluded": excluded, "repositories": repositories}
        base = self._main("rev-parse", self.base)
        if not self._copy("diff", "--cached", "--name-only", base):
            return {"commit": None, "base": base, "changes": [], "overlapped": [], **left_out}
        saga = f"Surogate-Saga: {dict(map(tuple, trailers))['Surogate-Saga']}"
        if self._copy("diff", "--cached", "--name-only", "HEAD") or saga not in self._copy("log", "-1", "--format=%B").splitlines():
            self._copy(*_as(author), "commit", "-q", "--allow-empty", "-m", "Turn", "-m", _block(trailers))
        turn = self._copy("rev-parse", "HEAD")
        if self._durable_refs().get(self.branch) != turn:
            self._push({self.branch: turn, self.base: base}, expect={self.branch: self._ref(self.synced)})
            self._main("update-ref", self.synced, turn)
        versions, renames = self._diff(base, turn)
        # A file and a folder of one name land together, as a rename's two sides do.
        shapes = [(p, str(f)) for p in versions for f in PurePosixPath(p).parents if str(f) in versions]
        links = renames + shapes
        real = {path: self._real(path) for path in versions if self._fits(path)}
        changed = {p for p in real if real[p] not in versions[p]}
        shaped = (set(versions) - set(real)) | {p for pair in shapes for p in pair}
        held = _together(changed | (set(versions) - set(real)), links)
        if repositories or wrote_left_out or any(versions[p][1] is not None for p in held):
            # A move git cannot pair (an edited docx, a move onto a name that
            # exists, or into a path history leaves out) reads as a deletion
            # and a write that does not land: the deletion waits too.
            paired = {p for pair in renames for p in pair}
            unpaired = {p for p, (_, after) in versions.items() if after is None and p not in paired}
            held = _together(held | unpaired, links)
        changes = [
            # A real file already as the turn left it lands as a no-op.  Writes
            # land before deletions: a landing cut off between a move's two
            # halves leaves the file under both names, never under neither.
            {"path": path, "before": real[path], "after": versions[path][1]}
            for path in sorted(versions, key=lambda p: (versions[p][1] is None, p)) if path not in held
        ]
        overlapped = [
            # Why each waits: the real file changed since, it is a change of
            # shape, or it goes with one of those.  Its two versions are the
            # history's: the real file's, and the thread's that did not land.
            {
                "path": path, "reason": "changed" if path in changed else "shape" if path in shaped else "with",
                "before": real.get(path), "after": versions[path][1],
            }
            for path in sorted(held)
        ]
        return {"commit": turn, "base": base, "changes": changes, "overlapped": overlapped, **left_out}

    def apply(self, path: str, before: str | None, after: str | None) -> dict:
        """Write the turn's version of *path* into the real files, if the real file is still *before*.

        Safe to repeat: a real file that is already *after* is left as it
        is, so a retry after a lost reply does not fail.  ``made`` are the
        folders it made for the file, which its put-back takes away again.
        """
        real = self._real(path)
        made: list[str] = []
        if real != after:
            if real != before:
                raise HistoryConflict(f"{path} changed since the thread started")
            if after is None:
                # A folder the deletion emptied goes with it, as git's own checkout takes it away.
                self._remove(path, takewhile(lambda f: f != self.project, (self.project / path).parents))
            else:
                made = self._put(path, after)
        return {"path": path, "before": before, "after": after, "made": made}

    def unapply(
        self, path: str, before: str | None, after: str | None, *, ran: bool = True, made: Iterable[str] = (),
    ) -> dict:
        """Put back *path*'s version from before the landing, where the real file is still the landing's.

        Safe to repeat: a real file that is already *before* is left as it
        is.  A real file that is neither is someone else's change: a
        conflict, unless the apply failed (*ran* false), and then it found
        the file changed and wrote nothing.  The folders the apply *made*
        for its file go with it, if nothing else is in them; a folder that
        was already there stays, empty or not.
        """
        real = self._real(path)
        if real != before:
            if real == after and before is None:
                self._remove(path, (self._inside(folder) for folder in made))
            elif real == after:
                self._put(path, before)
            elif ran:
                raise HistoryConflict(f"{path} changed after the landing wrote it")
        return {"path": path, "before": before, "after": after}

    def record(
        self, *, turn: str, applied: list[dict], author: dict[str, str], trailers: list[list[str]],
        main: str | None,
    ) -> dict:
        """Write the landing on ``main`` and push it: main's files with *applied*, the turn its second parent.

        *main* is ``main`` in the durable history when the landing began
        (None: there was none yet).  The push is the commit point: the
        rewrite of ``packed-refs``, which also moves the branch and its base
        to the landing, and is refused if ``main`` moved since.  The first
        parent is *main*: this pod's pickup of your changes stays its own, as
        the base of its copy, and is never recorded on ``main``.  With no
        history yet, it is ``main``'s first commit, the real files by you.
        Safe to repeat: a landing already pushed is found by its saga.
        """
        saga = f"Surogate-Saga: {dict(map(tuple, trailers))['Surogate-Saga']}"
        now = self._durable_refs().get(MAIN)
        if now != main:
            self._fetch(now)
            if now is not None and saga in self._message(now):
                # The push moved them all: so does this pod.
                for ref in (MAIN, self.branch, self.base, self.synced):
                    self._main("update-ref", ref, now)
                return {"commit": now}
            raise HistoryConflict("main moved in the project's history since the landing began")
        self._fetch(main)
        main_tip = main or self._main("rev-parse", MAIN)
        index = self.repo / "landing.index"
        index.unlink(missing_ok=True)
        env = {"GIT_DIR": str(self.repo), "GIT_WORK_TREE": str(self.project), "GIT_INDEX_FILE": str(index)}
        self._git(["read-tree", main_tip], env=env, cwd=self.project)
        written = {c["path"] for c in applied if c["after"] is not None}
        modes = {}
        for entry in self._main("ls-tree", "-r", "-z", turn).split("\0"):
            meta, _, path = entry.partition("\t")
            if path in written:
                modes[path] = meta.split(" ")[0]
        # On stdin, not the command line: a landing may hold any number of files.
        entries = "".join(
            f"0 {_ZERO}\t{c['path']}\0" if c["after"] is None else f"{modes[c['path']]} {c['after']}\t{c['path']}\0"
            for c in applied
        )
        self._git(["update-index", "-z", "--index-info"], env=env, cwd=self.project, input=entries)
        tree = self._git(["write-tree"], env=env, cwd=self.project)
        index.unlink()
        # The message on stdin too: a landing may leave out any number of files, each a trailer.
        landing = self._git(
            [*_as(author), "commit-tree", tree, "-p", main_tip, "-p", turn, "-F", "-"],
            env={"GIT_DIR": str(self.repo), "GIT_WORK_TREE": str(self.project)}, cwd=self.project,
            input=f"Landing\n\n{_block(trailers)}\n",
        )
        self._push({MAIN: landing, self.branch: landing, self.base: landing}, expect={MAIN: main})
        for ref in (MAIN, self.branch, self.base, self.synced):
            self._main("update-ref", ref, landing)
        with contextlib.suppress(HistoryError, OSError):
            # A cache: without it the next pod reads every real file once.
            self._keep_index(landing)
        return {"commit": landing}

    def fetch(self, commits: Iterable[str] = (), saga: str | None = None) -> dict:
        """``main`` in the durable history now, fetched with *commits*: a landing's first look, under the project's lock.

        ``has_saga`` says whether ``main`` is the landing of *saga*: the only
        proof that a landing pushed.
        """
        main = self._durable_refs().get(MAIN)
        self._fetch(main, *(_checked_id(c, "a fetch") for c in commits))
        has_saga = main is not None and saga is not None and f"Surogate-Saga: {saga}" in self._message(main)
        packs = sum(p.stat().st_size for p in (self.durable / "objects" / "pack").glob("*.pack"))
        return {"main": main, "has_saga": has_saga, "packs": packs}

    def keep(self, *, author: dict[str, str], trailers: list[list[str]], base: bool) -> dict:
        """Commit the copy on the thread's branch and push the branch; its base too when *base*, or when the history has none.

        A failed turn's work, kept for the thread's next landing.
        """
        self._add_all(self._copy)
        if self._copy("diff", "--cached", "--name-only", "HEAD"):
            self._copy(*_as(author), "commit", "-q", "-m", "Kept", "-m", _block(trailers))
        tip = self._copy("rev-parse", "HEAD")
        # A branch never reaches the history without its base: the overlap check is against it.
        moves_base = base or self.base not in self._durable_refs()
        self._push(
            {self.branch: tip, **({self.base: self._ref(self.base)} if moves_base else {})},
            expect={self.branch: self._ref(self.synced)},
        )
        self._main("update-ref", self.synced, tip)
        return {"commit": tip}

    def prune(self, *, keep: list[str], now: float) -> dict:
        """Cut the durable history back to its window, at most once a day, under the project's lock.

        Kept: ``main``'s commits of the last 90 days and never fewer than its
        last 20, cut further while the history is more than twice the size
        of ``main``'s files, never below those 20; every ``main`` a thread's
        pod alive now may have opened on, those of a pod's deadline and the
        one before them; and the refs in *keep*, each live thread's branch
        and base, with their history inside the window.  Every other ref
        goes.  The cut is git's ``shallow`` file, so the commits that stay
        keep their ids.  The history is pruned in a full copy on the pod's
        disk and goes back as one pack.  It is marked pruned first: one cut
        off by its bound is tried again the next day, not at every landing.
        """
        # Its git has no bound of its own: its call's, sized from the history, cuts it off.
        budget = _TIMEOUT.set(THREAD_POD_DEADLINE)
        try:
            return self._prune(keep=keep, now=now)
        finally:
            _TIMEOUT.reset(budget)

    def _prune(self, *, keep: list[str], now: float) -> dict:
        refs = self._durable_refs()
        marker = self.durable / "pruned"
        _invalidate(marker if os.path.lexists(marker) else self.durable)
        if MAIN not in refs or marker.is_file() and now - marker.stat().st_mtime < _PRUNE_EVERY:
            return {"pruned": False}
        self._put_durable("pruned", b"")
        self._sweep()
        _invalidate(self.durable / "objects" / "pack")
        packs = list((self.durable / "objects" / "pack").iterdir())
        work = self.repo / "pruning.git"
        shutil.rmtree(work, ignore_errors=True)
        try:
            self._mirror(work, refs, packs)
            git = partial(self._in, work)
            for ref in set(git("for-each-ref", "--format=%(refname)").splitlines()) - {MAIN, *keep}:
                git("update-ref", "-d", ref)
            mains = [line.split() for line in git("log", "--first-parent", "--format=%H %ct", MAIN).splitlines()]
            # A pod lives at most THREAD_POD_DEADLINE: the main it opened on is one of these, or the one before.
            least = max(_PRUNE_LEAST, 1 + sum(int(t) >= now - THREAD_POD_DEADLINE for _, t in mains))
            kept = max(least, sum(int(t) >= now - PRUNE_DAYS * 86_400 for _, t in mains))
            size = sum(int(e.split()[3]) for e in git("ls-tree", "-r", "-l", MAIN).splitlines() if e.split()[1] == "blob")
            while True:
                packed = self._cut(git, work, mains=[c for c, _ in mains], kept=kept)
                if packed <= 2 * size or kept <= least:
                    break
                kept = max(least, kept // 2)
            [name] = {p.stem for p in (work / "objects" / "pack").glob("pack-*.pack")}
            for kind in ("pack", "idx"):
                self._put_durable(f"objects/pack/{name}.{kind}", work / "objects" / "pack" / f"{name}.{kind}")
            if (work / "shallow").is_file():
                self._put_durable("shallow", work / "shallow")
            git("pack-refs", "--all", "--prune")
            if self._durable_refs() != refs:
                # The lock was lost and a landing pushed meanwhile: its refs and packs stand.
                raise HistoryConflict("the project's history moved while it was pruned")
            self._put_durable("packed-refs", work / "packed-refs")
            # Only now: until packed-refs names the new pack's commits, the old packs hold them.
            for old in packs:
                if old.name not in (f"{name}.pack", f"{name}.idx"):
                    old.unlink(missing_ok=True)
            _sync(self.durable / "objects" / "pack")
            return {"pruned": True, "commits": min(kept, len(mains)), "size": packed, "files": size}
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _mirror(self, work: Path, refs: dict[str, str], packs: list[Path]) -> None:
        """The durable history as *refs* and *packs*, in a repository at *work* on the pod's disk.

        Its packs are copied whole, each read once in order: ``git clone``
        of a history with a ``shallow`` file reads them object by object
        instead, one request a read through geesefs (four minutes a GiB at
        20 ms a request).  Its ``HEAD`` and config are its own, not the bucket's.
        """
        self._git(["init", "-q", "--bare", "-b", "main", str(work)], env={}, cwd=self.repo)
        (work / "packed-refs").write_bytes(_packed(refs))
        if shallow := self._durable_shallow():
            (work / "shallow").write_text("".join(f"{c}\n" for c in shallow))
        for pack in packs:
            if pack.suffix in (".pack", ".idx"):
                shutil.copyfile(pack, work / "objects" / "pack" / pack.name)

    def _cut(self, git: Callable[..., str], work: Path, *, mains: list[str], kept: int) -> int:
        """Cut *work*'s history to *mains*' newest *kept* and what only they reach; the size of its one pack after.

        A commit older ``main`` reaches goes, whichever branch it is on; the
        tip of every ref stays, however old.
        """
        tips = set(git("for-each-ref", "--format=%(objectname)").split())
        parents = {line.split()[0]: line.split()[1:] for line in git("rev-list", "--all", "--parents").splitlines()}
        stays = (set(git("rev-list", "--all", f"^{mains[kept]}").split()) if kept < len(mains) else set(parents)) | tips
        # A commit already cut stays cut: git shows it with no parents.
        was = set((work / "shallow").read_text().split()) if (work / "shallow").is_file() else set()
        shallow = sorted({c for c in stays if any(p not in stays for p in parents[c])} | (was & stays))
        if shallow:
            (work / "shallow").write_text("".join(f"{c}\n" for c in shallow))
        git("reflog", "expire", "--expire=now", "--all")
        git("gc", "-q", "--prune=now")
        return sum(p.stat().st_size for p in (work / "objects" / "pack").glob("pack-*.pack"))

    def _in(self, repo: Path, *args: str) -> str:
        """Git in the bare repository *repo*."""
        return self._git(list(args), env={"GIT_DIR": str(repo)}, cwd=repo)

    def _sweep(self) -> None:
        """Delete what a write killed part way left, staged beside its file: only the lock holder writes here."""
        for folder in (self.durable, self.durable / "objects" / "pack"):
            for staged in folder.glob(".~*.landing~"):
                staged.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # The durable history
    # ------------------------------------------------------------------

    def _durable_refs(self) -> dict[str, str]:
        """The durable history's refs as the bucket has them now, every one in ``packed-refs``."""
        target = self.durable / "packed-refs"
        _invalidate(target if os.path.lexists(target) else self.durable)
        try:
            fd = os.open(target, os.O_RDONLY | os.O_CLOEXEC)
        except (FileNotFoundError, NotADirectoryError):
            return {}
        with open(fd, "rb") as file:
            # Past the page cache: another pod may have rewritten it.
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            text = file.read().decode(errors="replace")
        refs = {}
        for line in text.splitlines():
            if line.startswith("#") or not line:
                continue
            sha, _, ref = line.partition(" ")
            if sha.startswith("^"):
                _checked_id(sha[1:], "packed-refs")
                continue
            refs[_checked_ref(ref, "packed-refs")] = _checked_id(sha, "packed-refs")
        return refs

    def _check_durable(self) -> None:
        """Refuse a history whose ``HEAD`` or ``shallow`` is not one the platform writes: git reads both."""
        head = self.durable / "HEAD"
        if head.is_file():
            text = head.read_text(errors="replace")
            if not (text.startswith("ref: ") and text.endswith("\n")):
                raise HistoryError(f"refused the project's history: HEAD holds {text[:60]!r}")
            _checked_ref(text[5:-1], "HEAD")
        self._durable_shallow()

    def _durable_shallow(self) -> list[str]:
        """The commits the durable history's ``shallow`` file names."""
        shallow = self.durable / "shallow"
        return [_checked_id(c, "shallow") for c in (shallow.read_text(errors="replace").split() if shallow.is_file() else [])]

    def _fetch(self, *commits: str | None) -> None:
        """*commits* from the durable history, at depth 1, where this repository lacks them."""
        wanted = [c for c in dict.fromkeys(commits) if c and not self._has(c)]
        if wanted:
            _invalidate(self.durable / "objects" / "pack")
            self._git(
                ["fetch", "-q", "--depth", "1", "--no-tags", "--no-write-fetch-head", "--", str(self.durable), *wanted],
                env={"GIT_DIR": str(self.repo)}, cwd=self.repo,
            )

    def _push(self, updates: dict[str, str | None], *, expect: dict[str, str | None]) -> None:
        """Make the durable history's refs *updates*, where *expect* still holds: a pack, then ``packed-refs``.

        Every object is in a pack and every ref in ``packed-refs``, each file
        written whole, so git's loose objects, lock files and folder renames
        never reach the bucket.  A commit whose parents the history lacks,
        this pod's own depth-1 boundary when a pruning has cut below it since,
        joins the history's ``shallow`` file.  The rewrite of ``packed-refs``
        is the moment a push counts.  Only the project's lock holder pushes.
        """
        refs = self._durable_refs()
        moved = sorted(ref for ref, want in expect.items() if refs.get(ref) != want)
        if moved:
            raise HistoryConflict(f"{', '.join(moved)} moved in the project's history")
        if (self.durable / "HEAD").exists():
            self._sweep()
        else:
            for folder in ("refs", "objects/pack"):
                (self.durable / folder).mkdir(parents=True, exist_ok=True)
            _sync(self.durable / "objects")
            self._put_durable("config", b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n")
            # Last: its presence is what makes the folder a repository.
            self._put_durable("HEAD", b"ref: refs/heads/main\n")
        tips = [c for c in updates.values() if c]
        have = [c for c in set(refs.values()) if self._has(c)]
        cut: set[str] = set()
        if tips and (self.repo / "shallow").is_file():
            # By the parents, not the commit: an earlier try's pack may hold it already.
            sent = set(self._main("rev-list", *tips, *(["--not", *have] if have else [])).split())
            cut = {
                c for c in (self.repo / "shallow").read_text().split()
                if c in sent and not all(self._in_durable(p) for p in self._parents(c))
            }
        outgoing = self.repo / "outgoing"
        shutil.rmtree(outgoing, ignore_errors=True)
        outgoing.mkdir()
        try:
            name = self._git(
                ["pack-objects", "--revs", "-q", str(outgoing / "pack")], env={"GIT_DIR": str(self.repo)}, cwd=self.repo,
                input="".join(f"{c}\n" for c in tips) + "".join(f"^{c}\n" for c in have),
            )
            if _objects(outgoing / f"pack-{name}.idx"):
                # The index last: git reads a pack only through it.
                for kind in ("pack", "idx"):
                    self._put_durable(f"objects/pack/pack-{name}.{kind}", outgoing / f"pack-{name}.{kind}")
        finally:
            shutil.rmtree(outgoing, ignore_errors=True)
        if cut:
            self._put_durable("shallow", "".join(f"{c}\n" for c in sorted({*self._durable_shallow(), *cut})).encode())
        refs.update(updates)
        self._put_durable("packed-refs", _packed(refs))

    def _in_durable(self, commit: str) -> bool:
        """Whether the durable history holds *commit*."""
        try:
            self._git(["cat-file", "-e", f"{commit}^{{commit}}"], env={"GIT_DIR": str(self.durable)}, cwd=self.repo)
        except HistoryError:
            return False
        return True

    def _parents(self, commit: str) -> list[str]:
        """*commit*'s parents as its object names them, fetched or not."""
        header = self._main("cat-file", "commit", commit).partition("\n\n")[0]
        return [line.split()[1] for line in header.splitlines() if line.startswith("parent ")]

    def _put_durable(self, name: str, source: bytes | Path) -> None:
        """Write *name* in the durable history whole, beside it then renamed over it; durable before it returns."""
        target = self.durable / name
        staged = target.with_name(f".~{os.urandom(4).hex()}.landing~")
        try:
            with open(staged, "wb") as out:
                if isinstance(source, Path):
                    with open(source, "rb") as src:
                        shutil.copyfileobj(src, out, 1 << 20)
                else:
                    out.write(source)
                os.fsync(out.fileno())
            os.replace(staged, target)
            _sync(target.parent)
        finally:
            staged.unlink(missing_ok=True)

    def _keep_index(self, landing: str) -> None:
        """main's index of the real files, made the landing's, into the durable history.

        The next pod reads only the real files whose size or time changed
        since; an entry the landing changed has neither, so it is read.
        """
        kept = self.repo / "kept.index"
        # With its time: the second its pod's open began.
        shutil.copy2(self.repo / "index", kept)
        try:
            self._git(
                ["read-tree", "-m", "-i", landing],
                env={"GIT_DIR": str(self.repo), "GIT_WORK_TREE": str(self.project), "GIT_INDEX_FILE": str(kept)},
                cwd=self.project,
            )
            self._put_durable("index", kept)
        finally:
            kept.unlink(missing_ok=True)

    def _ref(self, name: str) -> str | None:
        """The commit *name* names here, or None."""
        try:
            return self._main("rev-parse", "--verify", "-q", f"{name}^{{commit}}")
        except HistoryError:
            return None

    def _has(self, commit: str) -> bool:
        return self._ref(commit) is not None

    def _tree(self, commit: str) -> str:
        return self._main("rev-parse", f"{commit}^{{tree}}")

    def _message(self, commit: str) -> list[str]:
        """*commit*'s message, line by line."""
        return self._main("log", "-1", "--format=%B", "--end-of-options", commit).splitlines()

    def _diff(self, base: str, turn: str) -> tuple[dict[str, tuple[str | None, str | None]], list[tuple[str, str]]]:
        """Each file the turn changed since *base*, as ``(before, after)``, and the renames git paired."""
        fields = iter(self._main("diff", "--raw", "-z", "-M", "--no-abbrev", base, turn).split("\0"))
        versions: dict[str, tuple[str | None, str | None]] = {}
        renames: list[tuple[str, str]] = []
        for meta in fields:
            if not meta:
                break
            _, _, old, new, status = meta.split(" ")
            if status.startswith("R"):
                source, target = next(fields), next(fields)
                versions[source], versions[target] = (_blob(old), None), (None, _blob(new))
                renames.append((source, target))
            else:
                versions[next(fields)] = (_blob(old), _blob(new))
        return versions, renames

    def _fits(self, path: str) -> bool:
        """Whether the real files can take a file at *path*: inside them, not a folder, under no file."""
        try:
            target = self._inside(path)
        except HistoryError:
            return False
        folder = target.parent
        while not folder.exists():
            folder = folder.parent
        return folder.is_dir() and not target.is_dir()

    def _add_all(self, git: Callable[..., str]) -> None:
        """``git add -A``, leaving out every folder that holds a git repository.

        Git takes such a folder for a submodule: a link to a commit, not
        its files, and an add that fails while it has none.  Each one is
        written to the excludes, which ``main`` and every copy share.
        """
        # Listed file by file, git names a folder only for a repository it will not go into.
        found = [n for n in git("ls-files", "-z", "--others", "--exclude-standard").split("\0") if n.endswith("/")]
        if found:
            with open(self.repo / "info" / "exclude", "a") as out:
                out.writelines(f"/{_pattern(n)}\n" for n in found)
        git("add", "-A")

    def _excluded(self) -> tuple[list[str], list[str]]:
        """The excluded files and folders in the copy, its folders holding a git repository, and whether it wrote any file history leaves out, the platform's folders included.

        A copy starts with none, so the turn made them.  The platform's own
        folders are left out.
        """
        out = self._copy("ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory")
        ignored = {n for n in out.split("\0") if n}
        # After an add, what is left untracked is the folders with no file in
        # them: history has none, so they are not saved.  A folder the real
        # files have is one the turn emptied, not made.
        empty = self._copy("ls-files", "-z", "--others", "--exclude-standard", "--directory").split("\0")
        made = {n for n in empty if n and not (self.project / n).is_dir()}
        names = sorted(ignored | made)
        names = [n for n in names if not n.startswith(PLATFORM_EXCLUDES)]
        repositories = {
            n for n in names
            if n.endswith("/") and ((self.copy / n / ".git").exists() or (self.project / n / ".git").exists())
        }
        return [n for n in names if n not in repositories], sorted(repositories), bool(ignored)

    def _inside(self, path: str) -> Path:
        """*path* in the real files; refused if it would leave them."""
        target = self.project / path
        if (
            Path(path).is_absolute() or ".." in Path(path).parts
            or not target.parent.resolve().is_relative_to(self.project.resolve())
        ):
            raise HistoryError(f"{path} is outside the project's files")
        return target

    def _real(self, path: str) -> str | None:
        """The blob id of the real file at *path*, as the bucket has it now; None when there is none.

        The real files are a geesefs mount that other pods and the Library
        change.  geesefs is told to check the path with the bucket again,
        and the file is opened anew and read past the page cache, so a save
        or another landing since this pod opened is seen.  History keeps a
        file's bytes as they are (``info/attributes``), so its blob id is
        the git hash of those bytes.
        """
        target = self._inside(path)
        _invalidate(target if os.path.lexists(target) else target.parent)
        try:
            # Non-blocking, so a FIFO answers at once and is no file.
            fd = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        except (FileNotFoundError, NotADirectoryError):
            return None
        with open(fd, "rb") as file:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            blob = hashlib.sha1(b"blob %d\0" % info.st_size)
            while chunk := file.read(1 << 20):
                blob.update(chunk)
        return blob.hexdigest()

    def _put(self, path: str, blob: str) -> list[str]:
        """Make the real file at *path* blob *blob*, durable before it returns; the folders it made, deepest first.

        geesefs uploads a file when it is fsynced, and a folder's changes
        when the folder is: a landing answers only once the bucket has them,
        and an fsync that fails fails the write.
        """
        target = self._inside(path)
        made = [
            str(folder.relative_to(self.project))
            for folder in takewhile(lambda f: f != self.project and not f.exists(), target.parents)
        ]
        # Written beside the real file, then renamed over it: a write cut
        # short leaves the real file whole.  A short name, so a real file's
        # near the length limit fits too; history leaves out the *~ name.
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".~{os.urandom(4).hex()}.landing~")
        try:
            with open(staged, "wb") as out:
                result = subprocess.run(
                    ["git", "cat-file", "blob", blob], stdout=out, stderr=subprocess.PIPE,
                    env=_environ({"GIT_DIR": str(self.repo)}), timeout=_GIT_TIMEOUT,
                )
                if result.returncode != 0:
                    raise HistoryError(f"git cat-file failed: {result.stderr.decode(errors='replace').strip()}")
                os.fsync(out.fileno())
            os.replace(staged, target)
            _sync(target.parent)
        except subprocess.TimeoutExpired as exc:
            raise HistoryError(f"git cat-file timed out after {_GIT_TIMEOUT}s") from exc
        finally:
            # Gone once renamed; whatever cut the write short, nothing is left beside the real file.
            staged.unlink(missing_ok=True)
        return made

    def _remove(self, path: str, folders: Iterable[Path]) -> None:
        """Remove the real file at *path*, then each of *folders* while it is empty; durable before it returns."""
        target = self._inside(path)
        target.unlink(missing_ok=True)
        self._take_away(folders)
        _sync(target.parent)

    @staticmethod
    def _take_away(folders: Iterable[Path]) -> None:
        """Remove each of *folders*, deepest first, until one is not empty."""
        for folder in folders:
            try:
                folder.rmdir()
            except OSError:  # not empty, or gone
                return

    # ------------------------------------------------------------------
    # Git
    # ------------------------------------------------------------------

    @property
    def _admin(self) -> Path:
        return self.repo / "worktrees" / self.copy.name

    def _main(self, *args: str) -> str:
        """Git on ``main``, the real files its work tree."""
        return self._git(list(args), env={"GIT_DIR": str(self.repo), "GIT_WORK_TREE": str(self.project)}, cwd=self.project)

    def _copy(self, *args: str) -> str:
        """Git in the copy, on the thread's branch."""
        return self._git(list(args), env={"GIT_DIR": str(self._admin), "GIT_WORK_TREE": str(self.copy)}, cwd=self.copy)

    def _git(self, args: list[str], *, env: dict[str, str], cwd: Path, input: str | None = None) -> str:
        timeout = _TIMEOUT.get() or _GIT_TIMEOUT
        try:
            result = subprocess.run(
                ["git", *args], capture_output=True, text=True, env=_environ(env), cwd=cwd,
                input=input, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # The git it killed held the index's lock, and no later git could run.
            Path(f"{env.get('GIT_INDEX_FILE') or Path(env['GIT_DIR']) / 'index'}.lock").unlink(missing_ok=True)
            raise HistoryError(f"git {args[0]} timed out after {timeout}s") from exc
        if result.returncode != 0:
            raise HistoryError(f"git {args[0]} failed: {result.stderr.strip()}")
        # Only the line end: a name may start or end with a space.
        return result.stdout.removesuffix("\n")


def _environ(env: dict[str, str]) -> dict[str, str]:
    """The pod's environment for a git with *env*: none of the pod's own git variables reach it."""
    inherited = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    return {**inherited, **env, **_HERMETIC}


def _sync(folder: Path) -> None:
    """fsync *folder*, or the nearest folder above it still there: geesefs then uploads the changes in it."""
    while not folder.is_dir():
        folder = folder.parent
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _invalidate(path: Path) -> None:
    """Have geesefs check *path* with the bucket again, past its cache; nothing off geesefs."""
    with contextlib.suppress(OSError):
        os.setxattr(path, ".invalidate", b"")


def _pattern(name: str) -> str:
    """An exclude pattern for *name* alone.  A newline, which a pattern cannot hold, matches any one character."""
    return re.sub(r"([\\*?\[])", r"\\\1", name).replace("\n", "?")


def _together(held: set[str], links: list[tuple[str, str]]) -> set[str]:
    """*held*, and every path linked to one held: a pair lands whole or not at all."""
    while more := ({b for a, b in links if a in held} | {a for a, b in links if b in held}) - held:
        held |= more
    return held


def _as(author: dict[str, str]) -> list[str]:
    """Git options that make *author* a commit's author and committer."""
    return ["-c", f"user.name={author['name']}", "-c", f"user.email={author['email']}"]


def _block(trailers: list[list[str]]) -> str:
    """A commit message's trailer paragraph.  A line end in a value is spelt out: a name cannot add a trailer."""
    return "\n".join(f"{key}: {value}".replace("\r", "\\r").replace("\n", "\\n") for key, value in trailers)


def _checked_id(value: str, where: str) -> str:
    """*value*, a commit id read from *where*; refused when it is anything else."""
    if not _ID.fullmatch(value):
        raise HistoryError(f"refused the project's history: {where} holds {value[:60]!r}, not a commit id")
    return value


def _checked_ref(value: str, where: str) -> str:
    """*value*, a ref of the history read from *where*; refused when it is anything else."""
    if not _REF.fullmatch(value) or ".." in value:
        raise HistoryError(f"refused the project's history: {where} holds {value[:60]!r}, not one of its refs")
    return value


def _packed(refs: dict[str, str | None]) -> bytes:
    """A ``packed-refs`` file of *refs*, those set."""
    return (_PACKED + "".join(f"{refs[r]} {r}\n" for r in sorted(refs) if refs[r])).encode()


def _objects(index: Path) -> int:
    """How many objects the pack of *index* holds: the last entry of its fan-out table."""
    with open(index, "rb") as file:
        file.seek(8 + 255 * 4)
        return int.from_bytes(file.read(4), "big")


def _blob(sha: str) -> str | None:
    """A ``diff --raw`` blob id, None for an absent file."""
    return None if sha == _ZERO else sha
