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

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from surogates.tools.utils.checkpoint_manager import DEFAULT_EXCLUDES, _git_env

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
#: Only the repository's own config: none from the pod's home, where a
#: thread's commands can write, and none from the system.  Paths are read
#: as spelt: a file may be named ``:notes.md``.
_HERMETIC = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_LITERAL_PATHSPECS": "1"}


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
                self._main("add", "-A")
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
            env={"GIT_DIR": str(self.repo)}, cwd=self.repo, unset=("GIT_WORK_TREE",),
        )
        (self.copy / ".git").unlink()

    def snapshot(self, reason: str) -> str:
        """Commit the copy on the branch if it changed; the branch's tip."""
        self._copy("add", "-A")
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

    def _git(
        self, args: list[str], *, env: dict[str, str], cwd: Path, unset: tuple[str, ...] = (),
    ) -> str:
        full = _git_env(Path(env["GIT_DIR"]), env.get("GIT_WORK_TREE", str(cwd)))
        full.update(env, **_HERMETIC)
        for name in unset:
            full.pop(name, None)
        try:
            result = subprocess.run(
                ["git", *args], capture_output=True, text=True, env=full, cwd=cwd,
                timeout=_GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise HistoryError(f"git {args[0]} timed out after {_GIT_TIMEOUT}s") from exc
        if result.returncode != 0:
            raise HistoryError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip()
