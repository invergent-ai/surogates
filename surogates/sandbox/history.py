"""A project's history and a thread's copy, in a shadow git repository.

Runs in a thread's pod.  The repository's ``main`` is the project's real
files at ``/project``; a thread works on its own copy, a locked worktree
of its branch ``threads/<id>`` at ``/workspace``, with no ``.git`` in it.
``refs/bases/<id>`` is the commit of ``main`` the branch's work started
from.  Git runs with ``GIT_DIR`` and ``GIT_WORK_TREE`` set, as the
checkpoint manager runs it, so no git state reaches either folder.

Lives as long as its pod: ``main`` is made from the real files when the
pod opens.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
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

_GIT_TIMEOUT = 120
_ZERO = "0" * 40
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

    # ------------------------------------------------------------------
    # The copy
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Make ``main`` from the real files, then the branch, its base and the copy."""
        if not (self.repo / "HEAD").exists():
            self.repo.mkdir(parents=True, exist_ok=True)
            try:
                self._main("init", "-q", "-b", "main")
                self._main("config", "user.name", "Surogates Checkpoint")
                self._main("config", "user.email", "surogates@local")
                (self.repo / "info").mkdir(exist_ok=True)
                (self.repo / "info" / "exclude").write_text("\n".join(HISTORY_EXCLUDES) + "\n")
                (self.repo / "info" / "attributes").write_text(_ATTRIBUTES)
                self._add_all(self._main)
                name, email = self.user, f"user:{self.user}@surogate"
                self._main(
                    "-c", f"user.name={name}", "-c", f"user.email={email}",
                    "commit", "-q", "--allow-empty", "-m", "The project's files",
                )
            except Exception:
                # Half made (a read of the real files failed): the next
                # readiness check makes it again.
                shutil.rmtree(self.repo, ignore_errors=True)
                raise
        main = self._main("rev-parse", "refs/heads/main")
        self._main("update-ref", self.branch, main)
        self._main("update-ref", self.base, main)
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
        file where its folder would be): a landing leaves them out.  A
        rename's two sides, and a file and a folder of one name, are left
        out together.  ``commit`` is None when the turn changed nothing
        since its base.  ``repositories`` are the folders holding a git
        repository that the turn wrote into: they never land.
        """
        self._add_all(self._copy)
        excluded, repositories = self._excluded()
        left_out = {"excluded": excluded, "repositories": repositories}
        base = self._main("rev-parse", self.base)
        if not self._copy("diff", "--cached", "--name-only", base):
            return {"commit": None, "changes": [], "overlapped": [], **left_out}
        self._copy(*_as(author), "commit", "-q", "--allow-empty", "-m", "Turn", "-m", _block(trailers))
        turn = self._copy("rev-parse", "HEAD")
        versions, links = self._diff(base, turn)
        real = {path: self._real(path) for path in versions if self._fits(path)}
        held = _together({p for p, kept in versions.items() if p not in real or real[p] not in kept}, links)
        changes = [
            # A real file already as the turn left it lands as a no-op.
            {"path": path, "before": real[path], "after": versions[path][1]}
            for path in sorted(versions) if path not in held
        ]
        overlapped = [{"path": path} for path in sorted(held)]
        return {"commit": turn, "changes": changes, "overlapped": overlapped, **left_out}

    def apply(self, path: str, before: str | None, after: str | None) -> dict:
        """Write the turn's version of *path* into the real files, if the real file is still *before*.

        Safe to repeat: a real file that is already *after* is left as it
        is, so a retry after a lost reply does not fail.
        """
        real = self._real(path)
        if real != after:
            if real != before:
                raise HistoryConflict(f"{path} changed since the thread started")
            self._put(path, after)
        return {"path": path, "before": before, "after": after}

    def unapply(
        self, path: str, before: str | None, after: str | None, *, ran: bool = True,
    ) -> dict:
        """Put back *path*'s version from before the landing, where the real file is still the landing's.

        Safe to repeat: a real file that is already *before* is left as it
        is.  A real file that is neither is someone else's change: a
        conflict, unless the apply failed (*ran* false), and then it found
        the file changed and wrote nothing.
        """
        real = self._real(path)
        if real != before:
            if real == after:
                self._put(path, before)
            elif ran:
                raise HistoryConflict(f"{path} changed after the landing wrote it")
        return {"path": path, "before": before, "after": after}

    def record(
        self, *, turn: str, applied: list[dict], author: dict[str, str], trailers: list[list[str]],
    ) -> dict:
        """Write the landing on ``main``: main's files with *applied*, the turn its second parent.

        The commit point.  The branch and its base move to the landing.
        """
        main = self._main("rev-parse", "refs/heads/main")
        index = self.repo / "landing.index"
        index.unlink(missing_ok=True)
        env = {"GIT_DIR": str(self.repo), "GIT_WORK_TREE": str(self.project), "GIT_INDEX_FILE": str(index)}
        self._git(["read-tree", main], env=env, cwd=self.project)
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
        landing = self._main(
            *_as(author), "commit-tree", tree, "-p", main, "-p", turn, "-m", "Landing", "-m", _block(trailers),
        )
        self._main("update-ref", "refs/heads/main", landing, main)
        self._main("update-ref", self.branch, landing)
        self._main("update-ref", self.base, landing)
        return {"commit": landing}

    def _diff(self, base: str, turn: str) -> tuple[dict[str, tuple[str | None, str | None]], list[tuple[str, str]]]:
        """Each file the turn changed since *base*, as ``(before, after)``, and the pairs that land together.

        A pair is a rename's two sides, or a file and a folder of one name.
        """
        fields = iter(self._main("diff", "--raw", "-z", "-M", "--no-abbrev", base, turn).split("\0"))
        versions: dict[str, tuple[str | None, str | None]] = {}
        links: list[tuple[str, str]] = []
        for meta in fields:
            if not meta:
                break
            _, _, old, new, status = meta.split(" ")
            if status.startswith("R"):
                source, target = next(fields), next(fields)
                versions[source], versions[target] = (_blob(old), None), (None, _blob(new))
                links.append((source, target))
            else:
                versions[next(fields)] = (_blob(old), _blob(new))
        links += [(p, str(folder)) for p in versions for folder in PurePosixPath(p).parents if str(folder) in versions]
        return versions, links

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
        """The excluded files and folders in the copy, and its folders holding a git repository; at most ten each.

        A copy starts with none, so the turn made them.  The platform's own
        folders are left out.
        """
        out = self._copy("ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory")
        # After an add, what is left untracked is the folders with no file in
        # them: history has none, so they are not saved.  A folder the real
        # files have is one the turn emptied, not made.
        empty = self._copy("ls-files", "-z", "--others", "--exclude-standard", "--directory").split("\0")
        made = {n for n in empty if n and not (self.project / n).is_dir()}
        names = sorted({n for n in out.split("\0") if n} | made)
        names = [n for n in names if not n.startswith(PLATFORM_EXCLUDES)]
        repositories = {
            n for n in names
            if n.endswith("/") and ((self.copy / n / ".git").exists() or (self.project / n / ".git").exists())
        }
        return [n for n in names if n not in repositories][:10], sorted(repositories)[:10]

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
        """The blob id of the real file at *path*; None when there is none."""
        target = self._inside(path)
        return self._main("hash-object", "--", str(target)) if target.is_file() else None

    def _put(self, path: str, blob: str | None) -> None:
        """Make the real file at *path* blob *blob*, or remove it for None."""
        target = self._inside(path)
        if blob is None:
            target.unlink(missing_ok=True)
            # A folder the removal emptied goes with it, as git's own checkout takes it away.
            for folder in target.parents:
                if folder == self.project:
                    break
                try:
                    folder.rmdir()
                except OSError:  # not empty
                    break
            return
        # Written beside the real file, then renamed over it: a write cut
        # short leaves the real file whole.  History leaves out the *~ name.
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f"{target.name}.landing~")
        try:
            with open(staged, "wb") as out:
                result = subprocess.run(
                    ["git", "cat-file", "blob", blob], stdout=out, stderr=subprocess.PIPE,
                    env=_environ({"GIT_DIR": str(self.repo)}), timeout=_GIT_TIMEOUT,
                )
            if result.returncode != 0:
                raise HistoryError(f"git cat-file failed: {result.stderr.decode(errors='replace').strip()}")
            os.replace(staged, target)
        except subprocess.TimeoutExpired as exc:
            raise HistoryError(f"git cat-file timed out after {_GIT_TIMEOUT}s") from exc
        finally:
            # Gone once renamed; whatever cut the write short, nothing is left beside the real file.
            staged.unlink(missing_ok=True)

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
        try:
            result = subprocess.run(
                ["git", *args], capture_output=True, text=True, env=_environ(env), cwd=cwd,
                input=input, timeout=_GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            # The git it killed held the index's lock, and no later git could run.
            Path(f"{env.get('GIT_INDEX_FILE') or Path(env['GIT_DIR']) / 'index'}.lock").unlink(missing_ok=True)
            raise HistoryError(f"git {args[0]} timed out after {_GIT_TIMEOUT}s") from exc
        if result.returncode != 0:
            raise HistoryError(f"git {args[0]} failed: {result.stderr.strip()}")
        # Only the line end: a name may start or end with a space.
        return result.stdout.removesuffix("\n")


def _environ(env: dict[str, str]) -> dict[str, str]:
    """The pod's environment for a git with *env*: none of the pod's own git variables reach it."""
    inherited = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    return {**inherited, **env, **_HERMETIC}


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


def _blob(sha: str) -> str | None:
    """A ``diff --raw`` blob id, None for an absent file."""
    return None if sha == _ZERO else sha
