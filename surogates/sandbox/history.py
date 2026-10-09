"""A project's history and a thread's copy, in a shadow git repository.

Runs in a thread's pod.  The repository's ``main`` is the project's real
files at ``/project``; a thread works on its own copy, a locked worktree
of its branch ``threads/<id>`` at ``/workspace``, with no ``.git`` in it.
``refs/bases/<id>`` is the commit of ``main`` the branch's work started
from.  Git runs with ``GIT_DIR`` and ``GIT_WORK_TREE`` set, as the
checkpoint manager runs it, so no git state reaches either folder.

The history outlives the pod: it is a bare repository at ``_history/`` in
the project's files, every object in a pack and every ref in
``packed-refs``.  A thread's commands can write those files, so git never
runs there: the pod copies the packs, ``packed-refs`` and ``shallow`` to its
own disk as data, and git runs in that copy under the pod's own config.  A
pod opens by fetching ``main``, its thread's branch and base from the copy
at depth 1, and the project's lock holder pushes by writing a pack, then
``packed-refs``, as files.

A thread hands work to a helper through ``refs/handoff/<id>``: the thread's
copy as it was when the helper started, with what helpers kept onto it
since.  Each helper has a pod and a copy of its own, from the hand-off.
The thread's copy takes the hand-off up, and its branch only at the turn's
end, so a turn stopped after it handed work on lands none of it.

What a pod pushes of a copy is one commit: the copy's files on where the
copy started.  The snapshots its steps took stay in the pod, on the pod's
own branch, where a stop restores from them.  So the history holds one
version of a file for each turn that changed it, never one for each step.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from itertools import takewhile
from pathlib import Path, PurePosixPath

from surogates.tools.utils.checkpoint_manager import DEFAULT_EXCLUDES

#: The pod's log: what an open did that a person reading it later must be able to find.
logger = logging.getLogger(__name__)

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
#: Through geesefs, a large project's read of its real files is bound by request latency.
_OPEN_TIMEOUT = 570
_TIMEOUT: ContextVar[int | None] = ContextVar("history_git_timeout", default=None)
#: An open looks at the real files this many folders at once, and reads the
#: files it must read with this many gits at once, once there are more of
#: them than one git reads in _READ_ALONE: git looks and reads one file at a
#: time, and through geesefs a file is a request or more, so ten thousand
#: files looked at or read alone outlast the pod's ready bound.
_READERS = 16
_READ_ALONE = 64
#: The parents a pushed copy names at most: its base, the hand-offs it last
#: took up, and one commit that names the older ones.  A thread's failed
#: turns can each take up one more hand-off before any of them lands.
_PARENTS = 8
_ZERO = "0" * 40
#: The blob of a file with nothing in it, as an index entry holds it.
_EMPTY = bytes.fromhex("e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
MAIN = "refs/heads/main"
#: The failed helpers' copies kept apart for one thread, at most: the oldest goes for one more.
_APART = 16
#: The title of a commit that only names hand-offs, changing no file.
_EARLIER = "Earlier hand-offs"
#: What a thread's hand-off says of itself: the hand-off its copy had taken up, and the one its pod made before it.
_TOOK, _GAVE_BEFORE = "Surogate-Took", "Surogate-Gave-Before"
#: And which turn of its thread made it: the open of another turn's copy leaves its own files out.
_TURN = "Surogate-Turn"
#: The commits followed down from the history's hand-off, by a stop or an open, at most: all of them together.
_HAND_BACKS = 256
_PACKED = "# pack-refs with: peeled fully-peeled sorted \n"
#: The pruning window: main's commits of this many days, never fewer than its last _PRUNE_LEAST.
PRUNE_DAYS = 90
_PRUNE_LEAST = 20
#: A project's history is pruned at most this often.
_PRUNE_EVERY = 86_400
#: The packs a pruning leaves when its caller names no fence: those of the
#: last five minutes, a landing's fence at its defaults.
_SPARE = 300.0
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
    # A missing object is missing: never fetched by a command a config names.
    "GIT_NO_LAZY_FETCH": "1",
}
#: A file's bytes are history's as they are: no project, home or system
#: ``.gitattributes`` converts line endings or runs a filter on them.
_ATTRIBUTES = "* -text -filter -ident -working-tree-encoding\n"
#: The bucket's history's config, as the platform writes it: git never reads it there.
_CONFIG = b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
#: Who commits what is the copy's own: a snapshot, a take-up.
_CHECKPOINT = {"name": "Surogates Checkpoint", "email": "surogates@local"}



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


class LandingStepError(RuntimeError):
    """A pod's answer to a ``_history`` call is not its step's result."""


def step_result(answer: str) -> dict:
    """A pod's *answer* to a ``_history`` call, read as its step's result; LandingStepError when it is none.

    A step that ran to its end answers with its result alone.  Every other
    answer is no result: the step's own ``error``; a call the pod cut off
    at its own timeout, ``timed_out``; and the daemon's own failure, which
    has an ``exit_code`` and may name no error.  Read by the worker, on
    every such answer: a put-back taken for done from one of these would be
    recorded as done.
    """
    try:
        result = json.loads(answer)
    except ValueError:
        raise LandingStepError("the pod's answer is not a step's result") from None
    if not isinstance(result, dict):
        raise LandingStepError("the pod's answer is not a step's result")
    if "error" in result or "timed_out" in result or "exit_code" in result:
        raise LandingStepError(str(
            result.get("error") or ("the pod's step timed out" if result.get("timed_out") else None)
            or result.get("stderr") or "the pod did not run the step"
        ))
    return result


@dataclass(frozen=True)
class History:
    repo: Path      # the shadow repository: main, the branches, the objects
    project: Path   # the real files
    copy: Path      # this thread's worktree
    thread: str
    user: str       # who started the thread: main's first commit is theirs
    helper: str | None = None  # a thread's helper's own session: its pod and copy are its own
    turn: str | None = None    # the thread's turn this pod is opened for: required but in a helper's pod

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
        """The branch as the durable history had it when this copy last pushed it; in a helper's pod, what its copy last handed back from."""
        return f"refs/synced/{self.thread}"

    @property
    def handoff(self) -> str:
        """The thread's copy as it handed work on, with what its helpers kept onto it since."""
        return f"refs/handoff/{self.thread}"

    @property
    def handoff_from(self) -> str:
        """The hand-off's commit whose changes the thread has: what a take-up merges from."""
        return f"refs/handoff-from/{self.thread}"

    @property
    def handed(self) -> str:
        """In a thread's pod: the hand-off as its copy last took it up, for the commit step to push as ``handoff_from``."""
        return f"refs/handed/{self.thread}"

    @property
    def gave(self) -> str:
        """In a thread's pod: the hand-off this pod pushed, which a stop of its turn drops."""
        return f"refs/gave/{self.thread}"

    @property
    def apart(self) -> str:
        """A failed helper's copy, kept in history beside its thread's and merged onto nothing."""
        return f"refs/helpers/{self.thread}/{self.helper}"

    @property
    def untaken(self) -> str:
        """The hand-offs whose helpers' versions the thread's copy left out, when no turn of the thread names them.

        Among the thread's helpers' refs, which a pruning keeps while it keeps the thread's.
        """
        return f"refs/helpers/{self.thread}/not-taken"

    # ------------------------------------------------------------------
    # The copy
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Fetch ``main``, the branch and its base; make ``main`` the real files; then the copy.

        The history's packs are copied to the pod's disk first, as data, and
        each ref is fetched from that copy at depth 1.  So an open moves the
        whole kept history, not the project's current size: the project's
        files, and one more version of each file for every landing, kept
        turn and hand-off the history still holds.  Only a pruning bounds
        that, to ``main``'s last 20 landings or twice its files, whichever
        is more.  ``main``'s index of the real files comes with them,
        so only a file whose size or time changed is read.  A difference
        between the real files and ``main`` is a commit on ``main`` by you:
        it is this pod's own, its copy's base, and is never recorded on
        ``main``.  A branch whose files are its base's has nothing unlanded:
        it starts again at ``main``, and so does its base.  With no history
        yet, ``main``'s first commit is the real files as they are.

        A helper's copy starts where its thread handed off, else at the
        thread's branch, else at ``main``, each as the history has it: never
        at a pickup of the real files of its own, which would bring other
        threads' landings and your uploads in as its work.  A thread's copy
        takes up what its helpers kept since it last did, by the turn it is
        told it is opened for.
        """
        if self.helper is None and not self.turn:
            raise HistoryError("a thread's pod must be told its turn")
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
            refs = self._take()
            self._check_durable()
            start = (refs.get(self.handoff) or refs.get(self.branch) or refs.get(MAIN)) if self.helper else None
            self._fetch(*((start,) if start else (refs.get(r) for r in (MAIN, self.branch, self.base))))
            you = {"name": self.user, "email": f"user:{self.user}@surogate"}
            if not start and MAIN in refs:
                self._main("update-ref", MAIN, refs[MAIN])
                # geesefs trusts a listing for a second: one taken just before
                # another pod's landing would hide that landing's files.
                for folder in {"", *self._main("ls-tree", "-r", "-d", "-z", "--name-only", MAIN).split("\0")}:
                    _invalidate(self.project / folder)
                with self._folder() as history:
                    fd = _opened("index", "index", dir_fd=history) if history is not None else None
                if fd is not None:
                    with open(fd, "rb") as kept, open(self.repo / "index", "wb") as out:
                        shutil.copyfileobj(kept, out, 1 << 20)
                        dated = os.fstat(fd)
                    # Its own time: git reads again an entry no older than the index.
                    os.utime(self.repo / "index", ns=(dated.st_atime_ns, dated.st_mtime_ns))
                # The index made main's: an entry that matches keeps its size and time.
                try:
                    self._main("read-tree", "-m", "-i", MAIN)
                except HistoryError as unread:
                    # A cache git cannot read: left out, every real file is read.
                    logger.warning("The project's kept index is made again from main, and every real file read: %s", unread)
                    (self.repo / "index").unlink(missing_ok=True)
                    self._main("read-tree", MAIN)
            if not start:
                self._add_all(self._main)
                # Committed from the index as it is: ``git commit`` would look at every real file once more.
                tree, main = self._main("write-tree"), self._ref(MAIN)
                if main is None:
                    self._main("update-ref", MAIN, self._main(*_as(you), "commit-tree", tree, "-m", "The project's files"))
                elif tree != self._tree(main):
                    self._main("update-ref", MAIN, self._main(*_as(you), "commit-tree", tree, "-p", main, "-m", "Your changes"))
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
        if start:
            branch = base = start
        else:
            main = self._main("rev-parse", MAIN)
            branch, base = refs.get(self.branch), refs.get(self.base)
            if branch is None or base is None or self._tree(branch) == self._tree(base):
                branch = base = main
        self._main("update-ref", self.branch, branch)
        self._main("update-ref", self.base, base)
        # A helper hands back what it changed since its start; a thread pushes over the branch it found.
        if self.helper or self.branch in refs:
            self._main("update-ref", self.synced, branch if self.helper else refs[self.branch])
        # --lock: git gc must not prune a worktree whose .git file is gone.
        self._git(
            ["worktree", "add", "-q", "--lock", str(self.copy), f"threads/{self.thread}"],
            env={"GIT_DIR": str(self.repo)}, cwd=self.repo,
        )
        (self.copy / ".git").unlink()
        if not self.helper and refs.get(self.handoff) not in (None, refs.get(self.handoff_from)):
            self.take_up()

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
        ``commit`` is the turn as the history has it: one commit, the turn's
        files on its base, the hand-off it took up its second parent.  The
        pod's own branch keeps the steps' snapshots, and moves only with the
        record, to the landing.  Safe to repeat: every try makes and pushes
        the same commit.
        What helpers kept on the hand-off is taken up first, and lands with
        it; the hand-off, taken up whole, goes with that push.  ``not_taken``
        are the helpers' files this pod's take-ups left as the copy had them,
        at its open, in its turn or here: the landing's report names them.
        """
        self.take_up()
        self._add_all(self._copy)
        excluded, repositories, wrote_left_out = self._excluded()
        left_out = {"excluded": excluded, "repositories": repositories, "not_taken": self._not_taken()}
        base = self._main("rev-parse", self.base)
        refs = self._durable_refs()
        if not self._copy("diff", "--cached", "--name-only", base):
            taken = self._taken_up(refs)
            behind = self._behind(base)
            if left_out["not_taken"] and behind:
                # A helper's version this copy left out is told as kept.  No turn is pushed to name the
                # hand-off behind it, so a ref among the thread's helpers' names it, with those it named
                # before: the last ``_PARENTS`` of them, an older one let go.  The thread's next push
                # names them itself, and the ref goes.
                named = list(dict.fromkeys([*behind, *self._left_out_before(refs)]))[:_PARENTS]
                taken[self.untaken] = named[0] if len(named) == 1 else self._folded(named)
            if any(refs.get(ref) != to for ref, to in taken.items()):
                # What it took up and threw away stays away: no later take-up, and no later helper, starts from it.
                self._push(taken, expect={})
            return {"commit": None, "base": base, "changes": [], "overlapped": [], **left_out}
        saga = f"Surogate-Saga: {dict(map(tuple, trailers))['Surogate-Saga']}"
        if self._copy("diff", "--cached", "--name-only", "HEAD") or saga not in self._copy("log", "-1", "--format=%B").splitlines():
            self._copy(*_as(author), "commit", "-q", "--allow-empty", "-m", "Turn", "-m", _block(trailers))
        # Also what a try of this step cut off after its push named, when the ref had gone with that push.
        pushed = refs.get(self.branch)
        named = self._parents(pushed) if pushed and pushed != base and self._has(pushed) else []
        behind = self._behind(base, [*self._left_out_before(refs), *(named[1:] if named[:1] == [base] else [])])
        turn = self._one(self._copy("rev-parse", "HEAD"), base, *behind, author=author, title="Turn", trailers=trailers)
        if refs.get(self.branch) != turn:
            self._push(
                # The turn names the hand-offs whose versions earlier turns left out: their own ref is not needed.
                {self.branch: turn, self.base: base, **self._taken_up(refs), **({self.untaken: None} if self.untaken in refs else {})},
                expect={self.branch: self._ref(self.synced)},
            )
        # Also when the history had it already: a try cut off after its push never noted it.
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
        now = self._take().get(MAIN)
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
        proof that a landing pushed.  ``missing`` are those of *commits* the
        history does not have now, which no fetch can bring: a landing whose
        base stays among them can be put back by no one.
        """
        main = self._take().get(MAIN)
        wanted = [_checked_id(c, "a fetch") for c in commits]
        missing = [c for c in wanted if not self._has(c) and not self._in_durable(c)]
        self._fetch(main, *(c for c in wanted if c not in missing))
        has_saga = main is not None and saga is not None and f"Surogate-Saga: {saga}" in self._message(main)
        packs = sum(p.stat().st_size for p in (self._taken / "objects" / "pack").glob("*.pack"))
        return {"main": main, "has_saga": has_saga, "packs": packs, "missing": missing}

    def keep(self, *, author: dict[str, str], trailers: list[list[str]], base: bool) -> dict:
        """Commit the copy on the thread's branch and push the branch; its base too when *base*, or when the history has none.

        A failed turn's work, kept for the thread's next landing: one
        commit, the copy's files on its base, as a turn is.  A copy the
        branch holds already, pushed by its commit step or an earlier keep,
        is left as it is there.  ``not_taken`` are the helpers' files this
        pod's take-ups left as the copy had them, as the commit step names
        them: the list goes with the pod, so the kept turn's report says it.
        """
        tip = self._commit_copy(author, "Kept", trailers)
        refs = self._durable_refs()
        onto = self._main("rev-parse", self.base)
        behind = self._behind(onto)
        there = refs.get(self.branch)
        if there and there != onto and self._has(there) and self._tree(there) == self._tree(tip) and self._parents(there) == [onto, *behind]:
            self._main("update-ref", self.synced, there)
            return {"commit": there, "not_taken": self._not_taken()}
        kept = self._one(tip, onto, *behind, author=author, title="Kept", trailers=trailers)
        # A branch never reaches the history without its base: the overlap check is against it.
        moves_base = base or self.base not in refs
        self._push(
            {self.branch: kept, **({self.base: onto} if moves_base else {}), **self._taken_up(refs)},
            expect={self.branch: self._ref(self.synced)},
        )
        self._main("update-ref", self.synced, kept)
        return {"commit": kept, "not_taken": self._not_taken()}

    def hand_off(self, *, author: dict[str, str], trailers: list[list[str]]) -> dict:
        """Put the thread's copy on its hand-off, for a helper about to start from it; the branch stays as it was.

        What helpers kept there comes into the copy first.  Only the turn's
        end moves the branch, and ``handoff-from`` with it: until a landing
        or a keep holds what the copy took up, the hand-off is taken up
        from where the branch has it, so a copy made again in mid-turn
        loses none of it.  The hand-off is one commit, the copy's files on
        its base, the hand-off it took up behind it.  It says which
        hand-off that was, which one this pod made before it, and which
        turn of the thread made it: a stop of the turn follows those back
        to what the turn found, and so does the open of a later turn's
        copy, which leaves another turn's own files out.
        """
        not_taken = self.take_up()["not_taken"]
        own = self._commit_copy(author, "Handed on", trailers)
        refs = self._durable_refs()
        there = refs.get(self.handoff)
        if there and there == self._ref(self.gave) and self._tree(there) == self._tree(own):
            # Handed on again as it was, for the next helper of one step: the hand-off this pod made,
            # which no helper has kept onto since, holds the copy already.
            return {"commit": there, "not_taken": not_taken}
        onto = self._main("rev-parse", self.base)
        found = [
            [_TOOK, self._ref(self.handed) or "none"], [_GAVE_BEFORE, self._ref(self.gave) or "none"],
            [_TURN, self.turn],
        ]
        tip = self._one(own, onto, *self._behind(onto), author=author, title="Handed on", trailers=[*trailers, *found])
        self._push(
            # With no hand-off before it, it is taken up from the copy's base.
            {self.handoff: tip, **({} if self.handoff_from in refs else {self.handoff_from: onto})},
            expect={self.handoff: there},
        )
        for ref in (self.handed, self.gave):
            self._main("update-ref", ref, tip)
        return {"commit": tip, "not_taken": not_taken}

    def hand_back(self, *, author: dict[str, str], trailers: list[list[str]]) -> dict:
        """Merge a helper's copy onto its thread's hand-off, file by file.

        Where another helper, or the thread, changed a file first, theirs
        stays: this copy's version is kept in history, as the merge's second
        parent, and named in ``not_kept``.  With no hand-off, as after its
        thread's turn was stopped, the copy is the hand-off, from where it
        started.  The copy goes up as one commit, its files on where it
        started, or on what it last handed back.
        """
        own = self._commit_copy(author, "Kept", trailers)
        since = self._ref(self.synced)
        durable = self._take().get(self.handoff)
        if self._tree(own) == self._tree(since):
            return {"commit": durable or since, "not_kept": []}
        tip = self._one(own, since, author=author, title="Kept", trailers=trailers)
        not_kept: list[str] = []
        if durable is None:
            updates = {self.handoff: tip, self.handoff_from: since}
        elif durable == since:
            updates = {self.handoff: tip}
        else:
            self._fetch(durable)
            tree, not_kept = self._merged(since, winner=durable, loser=tip)
            updates = {self.handoff: self._commit(tree, durable, tip, author, "Kept", trailers)}
        self._push(updates, expect={self.handoff: durable})
        # Its own tip: what it hands back next is what it changed since.
        self._main("update-ref", self.synced, tip)
        return {"commit": updates[self.handoff], "not_kept": not_kept}

    def keep_apart(self, *, author: dict[str, str], trailers: list[list[str]]) -> dict:
        """Keep a failed helper's copy on a ref of its own, merged onto nothing: its files may be half made.

        ``left`` names the files it changed, which its thread is told are
        there: one commit, its files on where it started.  A thread keeps
        the copies of its last ``_APART`` failed helpers: one more lets the
        oldest go.
        """
        own = self._commit_copy(author, "Kept apart", trailers)
        since = self._ref(self.synced)
        left = sorted(self._diff(since, own)[0])
        if not left:
            return {"commit": since, "left": []}
        tip = self._one(own, since, author=author, title="Kept apart", trailers=trailers)
        self._push({self.apart: tip, **dict.fromkeys(self._oldest_apart(), None)}, expect={})
        return {"commit": tip, "left": left}

    def _oldest_apart(self) -> list[str]:
        """The refs of the thread's failed helpers' copies to let go so that, with one more, ``_APART`` are kept: the oldest.

        By the date each was kept.  The ref of versions left out is none of
        them.  One whose commit the history lacks is let go first.
        """
        prefix = f"refs/helpers/{self.thread}/"
        kept = {ref: commit for ref, commit in self._durable_refs().items() if ref.startswith(prefix) and ref not in (self.untaken, self.apart)}
        if len(kept) < _APART:
            return []

        def when(ref: str) -> int:
            try:
                self._fetch(kept[ref])
                return int(self._main("log", "-1", "--format=%ct", "--end-of-options", kept[ref]))
            except HistoryError:
                return 0

        return sorted(kept, key=lambda ref: (when(ref), ref))[:len(kept) - _APART + 1]

    def take_up(self) -> dict:
        """Bring into the thread's copy what its helpers kept on the hand-off since the copy last had it.

        Where both changed a file, the copy keeps its own version, named in
        ``not_taken``; the helper's stays in history.  The branch takes it up
        at the turn's end, with the commit step, which names every file this
        pod's take-ups left so.  A hand-off with no ``handoff-from`` is taken
        up from the copy's base.

        A copy's first take-up looks at whose hand-off it is.  One this
        turn made, its pod gone since, is taken up whole, from the base it
        was made on: what the turn handed on and what helpers kept since,
        and nothing the project's files held besides.  One another turn
        made is a turn that neither landed nor was kept, a stopped one whose
        stop was not carried out among them: its own files are left out,
        as a stop leaves them, and what it found and what helpers kept
        since is taken up.  A hand-off that names no turn is taken for
        another turn's.
        """
        refs = self._take()
        durable, handed = refs.get(self.handoff), self._ref(self.handed)
        since = handed or refs.get(self.handoff_from) or self._ref(self.base)
        if durable is None or durable == since:
            return {"not_taken": []}
        taken = durable
        if handed is None:
            followed = self._followed(
                durable, refs.get(self.handoff_from),
                gone=lambda commit, said: said.get(_TURN) != self.turn,
            )
            if followed is not None and followed[0]:
                _, taken, onto_none, _ = followed
                since = onto_none or since
            elif followed is not None and followed[3]:
                since = followed[3]
                # Its own turn's hand-off: the copy has what the turn handed on.
                _replace(self.repo / "took-its-own", b"")
        if taken is None:
            # Nothing but another turn's own files: none of it is this copy's.
            self._main("update-ref", self.handed, durable)
            return {"not_taken": []}
        self._fetch(taken, since)
        tip = self.snapshot("before taking up a helper's work")
        tree, not_taken = self._merged(since, winner=tip, loser=taken)
        self._switch(tip, self._commit(tree, tip, taken, _CHECKPOINT, "Taken up", []))
        self._main("update-ref", self.handed, durable)
        if not_taken:
            _replace(self.repo / "not-taken", json.dumps(sorted({*self._not_taken(), *not_taken})).encode())
        return {"not_taken": not_taken}

    def opened(self) -> dict:
        """What this copy's open took up: ``own``, whether a hand-off its own turn made, its pod gone since."""
        return {"own": (self.repo / "took-its-own").exists()}

    def drop_hand_off(self, gave: Iterable[str] = ()) -> dict:
        """Take the stopped turn's own files off its hand-off, so they do not land later; what helpers kept stays.

        The turn's hand-offs are those that name this pod's turn, the last
        one this pod made, and *gave*, the commits the turn handed on as its
        worker has them; each names the one its pod made before it.  With
        none of them under the history's hand-off, nothing changes.

        The hand-off goes back to the one the turn took up, with what each
        helper kept onto the turn's hand-offs since put on it again, file by
        file, as a helper's hand-back is.  So a helper's work finished
        before the turn, or during it, is there for the thread's next turn;
        a file a helper changed that the stopped turn had changed too goes
        with the turn's.  With nothing found and nothing kept since, the
        hand-off is deleted.  A helper still at work hands back onto what is
        left, or onto none, as only what it changed itself.
        """
        refs = self._take()
        now, own = refs.get(self.handoff), {self._ref(self.gave), *gave} - {None}

        def gone(commit: str, said: dict[str, str]) -> bool:
            if commit not in own and said.get(_TURN) != self.turn:
                return False
            own.add(said.get(_GAVE_BEFORE, "none"))
            return True

        followed = self._followed(now, refs.get(self.handoff_from), gone=gone) if now else None
        if followed is None or not followed[0]:
            return {"dropped": False}
        _, found, onto_none, _ = followed
        self._push(
            {self.handoff: found, **({self.handoff_from: onto_none} if onto_none or found is None else {})},
            expect={self.handoff: now},
        )
        for ref in (self.gave, self.handed):
            if self._ref(ref) is not None:
                self._main("update-ref", "-d", ref)
        return {"dropped": True}

    def _followed(
        self, top: str, floor: str | None, *, gone: Callable[[str, dict[str, str]], bool],
    ) -> tuple[bool, str | None, str | None, str | None] | None:
        """The hand-off *top* without the own files of each of the thread's hand-offs that *gone* names.

        Followed down from *top*: a helper's hand-back is its copy alone on
        the hand-off it started from, which is a thread's hand-off or
        another helper's hand-back, or a merge of the hand-off and its
        copy; a thread's hand-off says which hand-off its copy had taken
        up.  It ends at *floor*, where the hand-off is taken up from, at a
        hand-off of the thread's that is not gone, at one that found none,
        or at any other commit.  Then what each helper kept onto a hand-off
        that is gone is put on what is left, the oldest first.

        Answers whether any was gone; what is left, None for nothing; where
        that is taken up from when it is a helper's copy onto none; and,
        where the walk ended at a hand-off of the thread's, the base that
        one was made on.  None when it cannot be followed: a commit the
        history lacks, or more than ``_HAND_BACKS`` commits in all.
        """
        kept: list[str] = []
        any_gone, found, rests = False, None, None
        commit: str | None = top
        try:
            for _ in range(_HAND_BACKS):
                if commit == floor:
                    found = commit
                    break
                self._fetch(commit)
                message, parents = self._message(commit), self._parents(commit)
                if message[:1] == ["Kept"] and len(parents) == 2:
                    kept.append(parents[1])
                    commit = parents[0]
                elif message[:1] == ["Kept"] and len(parents) == 1:
                    if parents[0] == floor:
                        found = commit  # a helper's copy onto none: it is the hand-off, from where it started
                        break
                    # On the hand-off it started from, another helper's copy as well as a thread's hand-off.
                    kept.append(commit)
                    commit = parents[0]
                elif message[:1] == ["Handed on"] and parents:
                    said = dict(line.split(": ", 1) for line in message if ": " in line)
                    if not gone(commit, said):
                        found, rests = commit, parents[0]
                        break
                    any_gone = True
                    if said.get(_TOOK, "none") == "none":
                        break
                    commit = _checked_id(said[_TOOK], "a hand-off")
                else:
                    found = commit
                    break
            else:
                return None
            if not any_gone:
                return False, top, None, rests
            onto_none = None
            for tip in reversed(kept):
                self._fetch(tip)
                [since] = self._parents(tip)
                # Where the helper started may be a hand-off this pod never held: an earlier turn's.
                self._fetch(since)
                if found is None:
                    # Onto none: the helper's copy is the hand-off, taken up from where it started.
                    found, onto_none = tip, since
                    continue
                self._fetch(found)
                tree, _ = self._merged(since, winner=found, loser=tip)
                found = self._commit(tree, found, tip, _CHECKPOINT, "Kept", [["Surogate-Kind", "kept"]])
            return True, found, onto_none, rests
        except HistoryError:
            logger.warning("Could not follow the hand-off of thread %s", self.thread, exc_info=True)
            return None

    def prune(self, *, keep: list[str], now: float, spare: float = _SPARE, old: list[str] | None = None) -> dict:
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

        A pack written in the last *spare* seconds stays beside the new one,
        to the next pruning: it may be the pack of a push that lost the lock
        unseen and has not written its refs yet, whose commits no ref here
        names.  *spare* is the landings' fence, the longest such a push
        goes on; a caller that names none gets ``_SPARE``.

        Which packs are older than that is the bucket's to say, and its
        caller asks it: *old* names them, each without its ending, and no
        other pack is deleted.  A pod cannot tell through its own mount,
        which dates the files the pod wrote by the pod's clock and every
        other by the bucket's.  Told nothing, as by a caller with no way
        to ask the bucket, a pack's age runs from the time of the refs
        this pruning has just written back to the pack's own: one clock on
        a disk, two through a mount.  Neither *now*, which its caller may
        hold from before it waited for the lock, nor ``time.time`` is in it.
        """
        # Its git has no bound of its own: its call's, sized from the history, cuts it off.
        budget = _TIMEOUT.set(THREAD_POD_DEADLINE)
        try:
            return self._prune(keep=keep, now=now, spare=spare, old=old)
        finally:
            _TIMEOUT.reset(budget)

    def _prune(self, *, keep: list[str], now: float, spare: float, old: list[str] | None) -> dict:
        # The first look refuses a history whose folder is a link, before anything is written.
        refs = self._durable_refs()
        with self._folder() as history:
            marked = _looked(history, "pruned", self.durable) if history is not None else None
        if MAIN not in refs or marked and stat.S_ISREG(marked.st_mode) and now - marked.st_mtime < _PRUNE_EVERY:
            return {"pruned": False}
        self._put_durable("pruned", b"")
        self._sweep()
        refs = self._take()
        packs = [p.name for p in (self._taken / "objects" / "pack").iterdir()]
        work = self.repo / "pruning.git"
        shutil.rmtree(work, ignore_errors=True)
        try:
            # Linked, not copied: git never writes a file in place, and the cut writes its shallow anew.
            shutil.copytree(self._taken, work, copy_function=os.link)
            git = partial(self._in, work)
            for ref in git("for-each-ref", "--format=%(refname)").splitlines():
                # A kept name ending in / keeps every ref under it: a thread's helpers' copies kept apart.
                if ref != MAIN and ref not in keep and not any(ref.startswith(k) for k in keep if k.endswith("/")):
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
            with self._folder() as history:
                written = _looked(history, "packed-refs")
            with self._folder("objects", "pack") as folder:
                # A pack and its index go together, and only when neither was written within the fence:
                # as the caller found by the bucket's own dates, else by the dates this pod sees.
                young = {
                    listed.rpartition(".")[0] for listed in packs
                    if (seen := _looked(folder, listed)) is not None and (written is None or written.st_mtime - seen.st_mtime < spare)
                } if old is None else {listed.rpartition(".")[0] for listed in packs} - set(old)
                for listed in packs:
                    if listed not in (f"{name}.pack", f"{name}.idx") and listed.rpartition(".")[0] not in young:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(listed, dir_fd=folder)
                os.fsync(folder)
            return {"pruned": True, "commits": min(kept, len(mains)), "size": packed, "files": size}
        finally:
            shutil.rmtree(work, ignore_errors=True)

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
            _replace(work / "shallow", "".join(f"{c}\n" for c in shallow).encode())
        git("reflog", "expire", "--expire=now", "--all")
        git("gc", "-q", "--prune=now")
        return sum(p.stat().st_size for p in (work / "objects" / "pack").glob("pack-*.pack"))

    def _in(self, repo: Path, *args: str) -> str:
        """Git in the bare repository *repo*."""
        return self._git(list(args), env={"GIT_DIR": str(repo)}, cwd=repo)

    def _commit_copy(self, author: dict[str, str], title: str, trailers: list[list[str]]) -> str:
        """Commit the copy, if it changed, on its branch; the branch's tip."""
        self._add_all(self._copy)
        if self._copy("diff", "--cached", "--name-only", "HEAD"):
            self._copy(*_as(author), "commit", "-q", "-m", title, "-m", _block(trailers))
        return self._copy("rev-parse", "HEAD")

    def _one(self, tip: str, onto: str, *behind: str, author: dict[str, str], title: str, trailers: list[list[str]]) -> str:
        """The copy at *tip* as the one commit a push sends: its files on *onto*, with *behind* its further parents.

        The pod's own branch, the snapshots its steps took, is sent by no
        push.  The commit is the same at every try of a step: nothing of it
        is the time of the try, and its dates are *tip*'s.  So a push tried
        again sends the id the first try sent, which a landing's row may
        already name.
        """
        self._fetch(*behind)
        parents = list(dict.fromkeys((onto, *behind)))
        written, committed = self._main("log", "-1", "--date=raw", "--format=%ad%n%cd", "--end-of-options", tip).split("\n")
        return self._git(
            [*_as(author), "commit-tree", self._tree(tip), *(arg for parent in parents for arg in ("-p", parent)), "-F", "-"],
            env={"GIT_DIR": str(self.repo), "GIT_AUTHOR_DATE": written, "GIT_COMMITTER_DATE": committed},
            cwd=self.repo, input=f"{title}\n\n{_block(trailers)}\n",
        )

    def _behind(self, onto: str, also: Iterable[str] = ()) -> list[str]:
        """A thread's pushed copy's parents after *onto*, its base: the hand-offs its copy has taken up.

        A helper's version of a file the thread did not take is in the
        history as long as the commit that left it out is: through the
        hand-off this copy took up, and through those an earlier push of
        the branch named, when that push did not land and this one takes
        its place.

        They are the last taken up first, and with the base ``_PARENTS`` at
        most.  Past that the older ones go behind one commit that names
        them, the last of the list: each stays in the history as long as
        the pushed commit does, as when it was a parent itself, and none of
        their versions is let go.  *also* are named after them: the
        hand-offs whose versions earlier turns left out and no turn named.
        """
        handed, synced = self._ref(self.handed), self._ref(self.synced)
        earlier = self._parents(synced) if synced and synced != onto else []
        behind = list(dict.fromkeys([*([handed] if handed else []), *(earlier[1:] if earlier[:1] == [onto] else []), *also]))
        if len(behind) < _PARENTS:
            return behind
        return [*behind[:_PARENTS - 2], self._folded(behind[_PARENTS - 2:])]

    def _left_out_before(self, refs: dict[str, str]) -> list[str]:
        """The hand-offs the thread's ref of versions left out names, *refs* the history's as it is now."""
        named = refs.get(self.untaken)
        if named is None:
            return []
        self._fetch(named)
        return self._parents(named) if self._message(named)[:1] == [_EARLIER] else [named]

    def _folded(self, commits: list[str]) -> str:
        """One commit that names *commits* as its parents, ``_PARENTS`` at most, and changes no file.

        Its files and its dates are its first parent's and its author the
        pod's own, so every try makes the same commit, and no file's
        history shows it: it holds nothing its first parent does not.
        """
        if len(commits) > _PARENTS:
            commits = [*commits[:_PARENTS - 1], self._folded(commits[_PARENTS - 1:])]
        self._fetch(*commits)
        return self._one(commits[0], *commits, author=_CHECKPOINT, title=_EARLIER, trailers=[["Surogate-Kind", "hand-offs"]])

    def _taken_up(self, refs: dict[str, str]) -> dict[str, str | None]:
        """What a push of the branch makes of the hand-off, *refs* the history's as it is now.

        The branch then holds all the copy took up.  A hand-off it took up
        whole is dropped, with its ``handoff-from``: a helper started later
        starts at the branch, not at the copy as it was once handed on, and
        one still at work hands back onto none, as after a stop.  A hand-off
        a helper kept onto since stays, its ``handoff-from`` where the copy
        took it up: what the next take-up merges from.
        """
        handed = self._ref(self.handed)
        durable = refs.get(self.handoff)
        if durable is not None and durable == (handed or refs.get(self.handoff_from)):
            return {self.handoff: None, self.handoff_from: None}
        return {self.handoff_from: handed} if handed else {}

    def _not_taken(self) -> list[str]:
        """The helpers' files this pod's take-ups left as the copy had them."""
        try:
            return json.loads((self.repo / "not-taken").read_text())
        except FileNotFoundError:
            return []

    def _merged(self, base: str, *, winner: str, loser: str) -> tuple[str, list[str]]:
        """*winner*'s tree with each change *loser* made since *base* that *winner* did not make otherwise.

        Also the paths of *loser*'s changes it could not take: changed both
        ways, or a file where the other has a folder, or the reverse.  File
        by file, as a landing's overlap check is: an office file has no lines
        to merge.
        """
        won, _ = self._diff(base, winner)
        lost, _ = self._diff(base, loser)
        files = {n for n in self._main("ls-tree", "-r", "-z", "--name-only", winner).split("\0") if n}
        folders = {str(f) for n in files for f in PurePosixPath(n).parents}
        modes = {}
        for entry in self._main("ls-tree", "-r", "-z", loser).split("\0"):
            meta, _, path = entry.partition("\t")
            if path in lost:
                modes[path] = meta.split(" ")[0]
        held = {
            path for path, (_, after) in lost.items()
            if (path in won and won[path][1] != after)
            or (after is not None and (path in folders or any(str(f) in files for f in PurePosixPath(path).parents)))
        }
        # A file and a folder of one name go together: the change that made one removed the other.
        held |= {p for p in lost if any(p.startswith(f"{h}/") or h.startswith(f"{p}/") for h in held)}
        entries = [
            f"0 {_ZERO}\t{path}\0" if after is None else f"{modes[path]} {after}\t{path}\0"
            for path, (_, after) in sorted(lost.items()) if path not in held
        ]
        index = self.repo / "merge.index"
        index.unlink(missing_ok=True)
        env = {"GIT_DIR": str(self.repo), "GIT_INDEX_FILE": str(index)}
        try:
            self._git(["read-tree", winner], env=env, cwd=self.repo)
            self._git(["update-index", "-z", "--index-info"], env=env, cwd=self.repo, input="".join(entries))
            return self._git(["write-tree"], env=env, cwd=self.repo), sorted(held)
        finally:
            index.unlink(missing_ok=True)

    def _commit(self, tree: str, first: str, second: str, author: dict[str, str], title: str, trailers: list[list[str]]) -> str:
        return self._git(
            [*_as(author), "commit-tree", tree, "-p", first, "-p", second, "-F", "-"],
            env={"GIT_DIR": str(self.repo)}, cwd=self.repo, input=f"{title}\n\n{_block(trailers)}\n",
        )

    def _switch(self, old: str, new: str) -> str:
        """Move the copy and its branch from *old* to *new*; *new*."""
        self._copy("read-tree", "-u", "-m", old, new)
        self._main("update-ref", self.branch, new)
        return new

    def _sweep(self) -> None:
        """Delete what a write killed part way left, staged beside its file: only the lock holder writes here."""
        for inside in ((), ("objects", "pack")):
            with self._folder(*inside) as folder:
                for staged in os.listdir(folder) if folder is not None else ():
                    if staged.startswith(".~") and staged.endswith(".landing~"):
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(staged, dir_fd=folder)

    @contextlib.contextmanager
    def _folder(self, *inside: str, make: bool = False) -> Iterator[int | None]:
        """A folder of the bucket's history, opened to read and write in by its handle; None when there is none.

        A thread's commands can write the history's files, and can make a
        folder of it a link to a folder of the pod's.  So each step of the
        path is opened as a folder and never through a link, and what is
        read or written in it goes by the handle: nothing can be put in a
        folder's place between the look and the use.  With *make*, a folder
        not there yet is made.
        """
        opened: list[int] = []
        try:
            at: int | None = None
            for name in (str(self.durable), *inside):
                at = _opened_folder(name, at, make)
                if at is None:
                    break
                opened.append(at)
            yield at
        finally:
            for fd in opened:
                os.close(fd)

    # ------------------------------------------------------------------
    # The durable history
    # ------------------------------------------------------------------

    @property
    def _taken(self) -> Path:
        """The durable history copied to the pod's disk: the only repository of it git runs in."""
        return self.repo / "durable.git"

    def _take(self) -> dict[str, str]:
        """Copy the durable history to the pod's disk as the bucket has it now, as data; its refs.

        Git never runs in the bucket's history, whose files a thread's
        commands can write, only in this copy, whose ``HEAD`` and config are
        the pod's own.  A pack is named by its contents: only the packs new
        since the last copy are read, each whole and in order, which geesefs
        reads in large ranges at once, where git reads a pack a request at a
        time.  The refs are read first: a push writes its pack before
        ``packed-refs``, so each commit they name is in a pack listed after.
        """
        taken = self._taken
        if not (taken / "HEAD").exists():
            self._git(["init", "-q", "--bare", "-b", "main", str(taken)], env={}, cwd=self.repo)
        packs = taken / "objects" / "pack"
        refs, shallow = self._durable_refs(), self._durable_shallow()
        with self._folder("objects", "pack") as folder:
            if folder is not None:
                _invalidate(self.durable / "objects" / "pack")
            names = {n for n in os.listdir(folder) if n.endswith((".pack", ".idx"))} if folder is not None else set()
            here = set(os.listdir(packs))
            for gone in here - names:
                (packs / gone).unlink()
            # Each pack before its index: git reads a pack only through it.
            for name in sorted(names - here, key=lambda n: n.endswith(".idx")):
                if (fd := _opened(name, "a pack", dir_fd=folder)) is None:
                    continue  # gone since the listing: a pruning's
                staged = packs / f".~{name}"
                with open(fd, "rb") as pack, open(staged, "wb") as out:
                    shutil.copyfileobj(pack, out, 1 << 20)
                os.replace(staged, packs / name)
        _replace(taken / "packed-refs", _packed(refs))
        if shallow:
            _replace(taken / "shallow", "".join(f"{c}\n" for c in shallow).encode())
        else:
            (taken / "shallow").unlink(missing_ok=True)
        return refs

    def _durable_refs(self) -> dict[str, str]:
        """The durable history's refs as the bucket has them now, every one in ``packed-refs``."""
        with self._folder() as history:
            if history is None:
                return {}
            _looked(history, "packed-refs", self.durable)
            data = _read("packed-refs", "packed-refs", dir_fd=history)
        if data is None:
            return {}
        text = data.decode(errors="replace")
        refs = {}
        for line in text.splitlines():
            if line.startswith("#") or not line:
                continue
            sha, _, ref = line.partition(" ")
            if sha.startswith("^"):
                _checked_id(sha[1:], "its packed-refs")
                continue
            refs[_checked_ref(ref, "its packed-refs")] = _checked_id(sha, "its packed-refs")
        return refs

    def _check_durable(self) -> None:
        """Refuse a history whose ``HEAD``, config or ``shallow`` is not one the platform writes, as every reader of it should.

        Git never reads the bucket's config: the pod's copy has its own.
        One that is not the platform's is still refused, at a pod's open: it
        was written by a thread's commands, and says the history is not as
        the platform left it.
        """
        with self._folder() as history:
            head = _read("HEAD", "HEAD", dir_fd=history) if history is not None else None
            config = _read("config", "config", dir_fd=history) if history is not None else None
        if head is not None:
            text = head.decode(errors="replace")
            if not (text.startswith("ref: ") and text.endswith("\n")):
                raise HistoryError("refused the project's history: its HEAD is not one the platform writes")
            _checked_ref(text[5:-1], "its HEAD")
            if config != _CONFIG:
                raise HistoryError("refused the project's history: its config is not the platform's own")
        self._durable_shallow()

    def _durable_shallow(self) -> list[str]:
        """The commits the durable history's ``shallow`` file names."""
        with self._folder() as history:
            data = (_read("shallow", "shallow", dir_fd=history) if history is not None else None) or b""
        return [_checked_id(c, "its shallow") for c in data.decode(errors="replace").split()]

    def _fetch(self, *commits: str | None) -> None:
        """*commits* from the durable history as last taken, at depth 1, where this repository lacks them."""
        wanted = [c for c in dict.fromkeys(commits) if c and not self._has(c)]
        if wanted:
            self._git(
                ["fetch", "-q", "--depth", "1", "--no-tags", "--no-write-fetch-head", "--", str(self._taken), *wanted],
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
        A lock can be lost unseen, so the refs are read again right before
        they are written: a push that finds them moved while its pack went
        up writes none of its own over them.
        """
        refs = self._take()
        seen = dict(refs)
        moved = sorted(ref for ref, want in expect.items() if refs.get(ref) != want)
        if moved:
            raise HistoryConflict(f"{', '.join(moved)} moved in the project's history")
        with self._folder() as history:
            made = history is not None and _looked(history, "HEAD") is not None
        if made:
            self._sweep()
        else:
            for inside in (("refs",), ("objects", "pack")):
                with self._folder(*inside, make=True):
                    pass
            with self._folder("objects") as objects:
                os.fsync(objects)
            self._put_durable("config", _CONFIG)
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
        # Not sent again: what the history's refs hold, and the first parent of what is pushed where
        # the history held it when last taken, such as the hand-off a helper started from once the
        # history's own has moved on.
        held = {*have, *(first for first in map(self._first_held, tips) if first)}
        try:
            name = self._git(
                ["pack-objects", "--revs", "-q", str(outgoing / "pack")], env={"GIT_DIR": str(self.repo)}, cwd=self.repo,
                input="".join(f"{c}\n" for c in tips) + "".join(f"^{c}\n" for c in sorted(held)),
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
        if self._durable_refs() != seen:
            raise HistoryConflict("the project's history moved while it was pushed")
        self._put_durable("packed-refs", _packed(refs))

    def _first_held(self, commit: str) -> str | None:
        """*commit*'s first parent, where this pod holds it and the durable history held all of it when last taken.

        All of it: the commit with its files.  A pruning keeps a young pack
        whole and repacks only what refs reach, so the history can hold a
        commit no ref names without the files an older pack had sent.
        """
        try:
            first = self._parents(commit)[:1]
            if not first or not self._has(first[0]) or not self._in_durable(first[0]):
                return None
            self._git(["rev-list", "--objects", "--no-walk", first[0]], env={"GIT_DIR": str(self._taken)}, cwd=self.repo)
        except HistoryError:
            return None  # not the platform's own, or not whole: nothing is left out for it
        return first[0]

    def _in_durable(self, commit: str) -> bool:
        """Whether the durable history held *commit* when last taken."""
        try:
            self._git(["cat-file", "-e", f"{commit}^{{commit}}"], env={"GIT_DIR": str(self._taken)}, cwd=self.repo)
        except HistoryError:
            return False
        return True

    def _parents(self, commit: str) -> list[str]:
        """*commit*'s parents as its object names them, fetched or not: the lines right after its tree, as git reads them."""
        lines = self._main("cat-file", "commit", commit).split("\n")[1:]
        return [_checked_id(line[7:], "a commit") for line in takewhile(lambda line: line.startswith("parent "), lines)]

    def _put_durable(self, name: str, source: bytes | Path) -> None:
        """Write *name* in the durable history whole, beside it then renamed over it; durable before it returns.

        In its folder by the folder's handle: never through a link put in a folder's place.
        """
        *inside, leaf = name.split("/")
        staged = f".~{os.urandom(4).hex()}.landing~"
        with self._folder(*inside, make=True) as folder:
            try:
                fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=folder)
                with open(fd, "wb") as out:
                    if isinstance(source, Path):
                        with open(source, "rb") as src:
                            shutil.copyfileobj(src, out, 1 << 20)
                    else:
                        out.write(source)
                    os.fsync(out.fileno())
                os.replace(staged, leaf, src_dir_fd=folder, dst_dir_fd=folder)
                os.fsync(folder)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(staged, dir_fd=folder)

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
        written to the excludes, which ``main`` and every copy share.  The
        real files are looked at and read several at once, by no git alone.
        """
        # Listed file by file, git names a folder only for a repository it will not go into.
        new = [n for n in git("ls-files", "-z", "--others", "--exclude-standard").split("\0") if n]
        found = [n for n in new if n.endswith("/")]
        if found:
            with open(self.repo / "info" / "exclude", "a") as out:
                out.writelines(f"/{_pattern(n)}\n" for n in found)
        if git != self._main or not self._read_at_once([n for n in new if not n.endswith("/")]):
            git("add", "-A")

    def _read_at_once(self, new: list[str]) -> bool:
        """Make ``main``'s index the real files as ``git add -A`` would, with no git reading them alone; whether it did.

        *new* are the files the index lacks.  What else is to be read is
        decided by a look at every file the index names (:meth:`_look`),
        each as its folder's listing has it, by its size and whole-second
        time, as git compares them here.  To read are a file whose size,
        time or kind is not the index's, one the index knows by no size and
        time, which a landing wrote, and one saved in the second the index
        was written or since.  An empty file is known as any other is.  A file no longer there leaves the index.

        The files to read are then read by sixteen gits at once, and a few
        by one: git's own look and read go one file at a time, a request or
        more each through geesefs.  Each reader is a git of its own, with an
        index of its own for the files given to it, so every entry is git's,
        with the size and time git saw as it read.  The entries are then put
        together, each whole as its git wrote it.

        An index git reads and this does not, of another version or with a
        merge's entries, is made again from ``main``, and every file read:
        left to one git it would be at every open from then on, since git
        keeps what it finds, and a large project's pod would outlast none
        of them.  A reader that fails is tried once more.  Left to one
        ``git add -A`` is only what this cannot do, a file that changed
        under a reader among it; the pod's log then says why.
        """
        index = self.repo / "index"
        entries = _entries(index)
        if entries is None and self._ref(MAIN) is not None:
            logger.warning(
                "The project's kept index is made again from main, and every real file read: "
                "it is of another version, or holds a merge's entries",
            )
            index.unlink()
            self._main("read-tree", MAIN)
            entries = _entries(index)
        if entries is None:
            logger.warning("The real files are read by one git, not several at once: the index is not one the readers use")
            return False
        work = self.repo / "reading"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        before = index.read_bytes() if index.exists() else None
        try:
            written = int(index.stat().st_mtime) if before is not None else 0
            found = self._look(entries)
            gone = [path for path, seen in found.items() if seen is None or stat.S_ISDIR(seen.st_mode)]
            read = [*new, *(
                path.decode() for path, seen in found.items()
                if seen is not None and not stat.S_ISDIR(seen.st_mode) and not _known(entries[path], seen, written)
            )]
            if not read and not gone:
                return True
            # A few are one git's: sixteen gits for a handful of files cost more than they spare.
            shares = [read] if len(read) <= _READ_ALONE else [share for share in (read[n::_READERS] for n in range(_READERS)) if share]

            def once(n: int) -> None:
                # A file gone since it was listed is no error: --remove leaves it out.
                self._git(
                    ["update-index", "--add", "--remove", "-z", "--stdin"],
                    env={"GIT_DIR": str(self.repo), "GIT_WORK_TREE": str(self.project), "GIT_INDEX_FILE": str(work / str(n))},
                    cwd=self.project, input="".join(f"{path}\0" for path in shares[n]),
                )

            def reader(n: int) -> None:
                try:
                    once(n)
                except HistoryError as first:
                    # Once more, from nothing: one read the mount refused is not every reader's work thrown away.
                    logger.warning("A reader of the real files failed, and is tried once more: %s", first)
                    for left in (work / str(n), work / f"{n}.lock"):
                        left.unlink(missing_ok=True)
                    once(n)

            if read:
                with ThreadPoolExecutor(len(shares)) as readers:
                    # Each with the open's own bound for its git, which a thread of its own would not have.
                    for done in [readers.submit(contextvars.copy_context().run, reader, n) for n in range(len(shares))]:
                        done.result()
            for path in (*gone, *(path.encode() for path in read)):
                entries.pop(path, None)
            for n in range(len(shares) if read else 0):
                if (theirs := _entries(work / str(n))) is None:
                    raise HistoryError("a reader's index is not one this reads")
                entries.update(theirs)
            _replace(index, _index(entries))
            # A file and a folder of one name, from a change under the readers, is no index: git says so here.
            self._main("write-tree")
        except (HistoryError, OSError, UnicodeError) as why:
            # The index as it was, for git to read the files alone.
            logger.warning("The real files are read by one git, not several at once: %s", why)
            if before is None:
                index.unlink(missing_ok=True)
            else:
                _replace(index, before)
            return False
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return True

    def _look(self, paths: Iterable[bytes]) -> dict[bytes, os.stat_result | None]:
        """What each of *paths* is in the real files now, None for one not there: by its folder's listing, sixteen folders at a time.

        A folder is listed once and each of its files taken right from that
        listing, which through geesefs is a request for a thousand files
        where a look at each file alone is a request or two a file.  Only
        the folders that hold one of *paths* are listed, from the top down,
        and never through a link: a path under a folder that is now a link,
        or a file, is not there, as git has it.  A file gone between its
        folder's listing and the look at it is not there either, and takes
        no other file of the folder with it; one that became a folder or a
        link in that time is seen as what it is now.
        """
        files: dict[bytes, dict[bytes, bytes]] = {}
        folders: dict[bytes, set[bytes]] = {}
        found: dict[bytes, os.stat_result | None] = {}
        for path in paths:
            found[path] = None
            *above, name = path.split(b"/")
            files.setdefault(b"/".join(above), {})[name] = path
            for depth in range(len(above)):
                folders.setdefault(b"/".join(above[:depth]), set()).add(above[depth])
        root = os.fsencode(self.project)

        def one(folder: bytes) -> list[bytes]:
            inside, below, deeper = files.get(folder, {}), folders.get(folder, ()), []
            try:
                with os.scandir(os.path.join(root, folder) if folder else root) as listed:
                    for entry in listed:
                        if entry.name in inside:
                            # Gone since it was listed: that file alone, and the rest of its folder as usual.
                            with contextlib.suppress(FileNotFoundError):
                                found[inside[entry.name]] = entry.stat(follow_symlinks=False)
                        elif entry.name in below and entry.is_dir(follow_symlinks=False):
                            deeper.append(os.path.join(folder, entry.name) if folder else entry.name)
            except (FileNotFoundError, NotADirectoryError):
                pass  # gone, or a file in its place, since the folder above was listed
            return deeper

        level = [b""]
        with ThreadPoolExecutor(_READERS) as lookers:
            while level:
                level = [folder for deeper in lookers.map(one, level) for folder in deeper]
        return found

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


def _entries(index: Path) -> dict[bytes, bytes] | None:
    """A git index's entries by path, each whole as git wrote it; none for no index, None for one this does not read.

    An index of version 2, which git writes unless told otherwise, and
    with no entry of a merge.  An entry is read only as far as its length
    and its path: what git put in it is kept as it is.
    """
    try:
        data = index.read_bytes()
    except FileNotFoundError:
        return {}
    if len(data) < 32 or data[:4] != b"DIRC" or int.from_bytes(data[4:8], "big") != 2:
        return None
    entries: dict[bytes, bytes] = {}
    at = 12
    for _ in range(int.from_bytes(data[8:12], "big")):
        flags = int.from_bytes(data[at + 60:at + 62], "big")
        if flags & 0x7000 or len(data) < at + 62:
            return None  # an extended entry, or a stage of a merge
        length = flags & 0xFFF
        if length == 0xFFF:
            # A longer path ends at its first NUL.
            length = data.index(b"\0", at + 62) - (at + 62)
        size = (62 + length + 8) & ~7
        entries[data[at + 62:at + 62 + length]] = data[at:at + size]
        at += size
    return entries


def _index(entries: dict[bytes, bytes]) -> bytes:
    """A git index of *entries*, in git's order of paths, with its checksum."""
    body = b"DIRC" + (2).to_bytes(4, "big") + len(entries).to_bytes(4, "big") + b"".join(entries[path] for path in sorted(entries))
    return body + hashlib.sha1(body).digest()


def _known(entry: bytes, seen: os.stat_result, written: int) -> bool:
    """Whether an index *entry* is its file as *seen* now, by what git compares with ``core.checkStat=minimal``.

    The file's kind and whether it is to be run, its size and the whole
    second it was saved.  Not known, so read: an entry with no size whose
    file is not the empty one, which git never looked at (a landing wrote
    the file) or marked to be read again; and a file saved in the second
    the index was *written* or since, which may have been saved again
    unseen.  An empty file's entry has no size and is known by its time,
    as git takes it.  The index holds 32 bits of each.
    """
    mode = int.from_bytes(entry[24:28], "big")
    saved, size = int.from_bytes(entry[8:12], "big"), int.from_bytes(entry[36:40], "big")
    if stat.S_ISLNK(mode) != stat.S_ISLNK(seen.st_mode) or stat.S_ISREG(mode) != stat.S_ISREG(seen.st_mode):
        return False
    if stat.S_ISREG(mode) and (mode ^ seen.st_mode) & 0o100:
        return False
    if size == 0 and entry[40:60] != _EMPTY:
        return False
    return size == seen.st_size & 0xFFFFFFFF and saved == int(seen.st_mtime) & 0xFFFFFFFF and saved < written


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


def _opened_folder(name: str, inside: int | None, make: bool) -> int | None:
    """The folder *name*, in the folder open as *inside*, opened as a folder and never through a link; None when there is none."""
    for again in (False, True):
        try:
            return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=inside)
        except FileNotFoundError:
            if not make or again:
                return None
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, dir_fd=inside)
        except OSError as exc:
            # A link, or a file, where the folder is: the first is ELOOP or, with a folder asked for, ENOTDIR.
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise HistoryError("refused the project's history: a folder of it is a link") from None
            raise
    return None


def _looked(folder: int, name: str, path: Path | None = None) -> os.stat_result | None:
    """What *name* in the folder open as *folder* is, as the bucket has it now, never through a link; None when it is not there.

    With the folder's *path*, geesefs is told to check with the bucket again, past its cache.
    """
    try:
        found = os.stat(name, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        found = None
    if path is not None:
        _invalidate(path / name if found is not None else path)
    return found


def _opened(path: Path | str, what: str, *, dir_fd: int | None = None) -> int | None:
    """*path* of the durable history opened to read as data, past the page cache; None when there is none.

    Never through a link, and only a file: a thread's commands can make a
    link to a file of the pod's.  With *dir_fd*, *path* is a name in the
    folder open as it.  A refusal names *what*, never the path or
    anything read: it reaches the pod's logs.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise HistoryError(f"refused the project's history: its {what} is a link") from None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise HistoryError(f"refused the project's history: its {what} is not a file")
    # Another pod may have rewritten it.
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    return fd


def _read(path: Path | str, what: str, *, dir_fd: int | None = None) -> bytes | None:
    """The bytes of *path* of the durable history, read as :func:`_opened` opens it; None when there is none."""
    if (fd := _opened(path, what, dir_fd=dir_fd)) is None:
        return None
    with open(fd, "rb") as file:
        return file.read()


def _replace(target: Path, data: bytes) -> None:
    """Write *target* on the pod's disk anew, never in place: another repository may link the old file."""
    staged = target.with_name(f".~{target.name}")
    staged.write_bytes(data)
    os.replace(staged, target)


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
    """*value*, a commit id read from *where*; refused when it is anything else, never quoted: it reaches the pod's logs."""
    if not _ID.fullmatch(value):
        raise HistoryError(f"refused the project's history: {where} holds what is not a commit id")
    return value


def _checked_ref(value: str, where: str) -> str:
    """*value*, a ref of the history read from *where*; refused when it is anything else."""
    if not _REF.fullmatch(value) or ".." in value:
        raise HistoryError(f"refused the project's history: {where} holds what is not one of its refs")
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
