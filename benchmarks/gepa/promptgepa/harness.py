"""Put a candidate fragment in front of the local harness worker.

``PromptLibrary`` caches fragment bodies per path for the life of the
process and is reached through a process-wide singleton, so a fragment
cannot be swapped inside a running worker.  A candidate is applied by
building a prompts tree for it and restarting the worker against that tree
through ``SUROGATES_PROMPTS_ROOT``.

The tree mirrors the shipped one by symlink and materialises exactly one
file, so the candidate body is the only difference between two evaluations.
No other fragment can drift, and no stale copy can survive.

Only the fragment *body* is optimised.  The frontmatter is re-attached
verbatim from the shipped file on every write, so a proposal cannot break
the loader's ``name``/``description`` contract and take the worker down at
boot -- a failure that would otherwise score every task zero and be read as
a very bad candidate rather than as a broken one.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import signal
import socket
import subprocess
import time
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

import httpx
import yaml

FENCE = "---"
_PR_SET_PDEATHSIG = 1


class CandidateRejected(ValueError):
    """The candidate would not load as a prompt fragment."""


class StaleWorkerError(RuntimeError):
    """Another harness worker is running and would serve sessions itself.

    Two workers share the Redis queue, so a leftover one silently answers
    part of the batch with the *shipped* prompt.  The run would still
    produce a score -- a meaningless one -- which is why this is fatal
    rather than a warning.
    """


def split_fragment(text: str) -> tuple[str, str]:
    """Split a fragment into its frontmatter block and body.

    The header keeps its fences and trailing newline so ``header + body``
    reproduces a loadable fragment.  Returns ``("", text)`` when there is
    no frontmatter.
    """
    if not text.startswith(FENCE):
        return "", text
    lines = text.splitlines(keepends=True)
    for index in range(1, len(lines)):
        if lines[index].rstrip() == FENCE:
            return "".join(lines[: index + 1]), "".join(lines[index + 1:])
    return "", text


def read_seed(prompts_root: Path, fragment: str) -> tuple[str, str]:
    """Return ``(header, body)`` of the shipped *fragment* (e.g.
    ``guidance/execution_discipline``)."""
    path = Path(prompts_root) / f"{fragment}.md"
    if not path.exists():
        raise FileNotFoundError(f"no such fragment: {path}")
    return split_fragment(path.read_text(encoding="utf-8"))


def is_worker_argv(argv: list[str]) -> bool:
    """True for a harness worker however it was launched.

    Both spellings occur on a dev box: the console script
    (``.../bin/surogates worker``) and the module form the VSCode launch
    configs use (``python -m surogates.cli.main worker``).  The second is
    wrapped in debugpy, so the entrypoint sits deep in argv rather than at
    argv[0] -- matching only the console script misses a worker that is
    very much running and very much consuming the queue.
    """
    if "worker" not in argv:
        return False
    before = argv[: argv.index("worker")]
    return any(
        part.endswith("surogates") or part == "surogates.cli.main"
        for part in before
    )


def other_workers() -> list[int]:
    """PIDs of harness workers this process did not start."""
    # ponytail: /proc scan, Linux only -- this runs on a dev box, and psutil
    # is a dependency for something a directory listing already answers.
    mine = os.getpid()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == mine:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        if is_worker_argv(argv):
            found.append(int(entry.name))
    return found


def _die_with_parent() -> None:
    """Ask the kernel to SIGKILL this child when the optimiser dies.

    ``finally`` does not run when Python takes a SIGTERM, so a killed run
    would otherwise leave a worker behind -- still consuming the queue,
    still serving the candidate prompt of an experiment that no longer
    exists. SIGKILL rather than SIGTERM because the worker drains
    in-flight sessions on SIGTERM and can outlive its parent by minutes.
    """
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)


def free_port() -> int:
    """A port the worker's health server can have to itself."""
    # ponytail: classic bind-zero-and-release, so there is a small window
    # where something else could take it. The worker fails loudly on a bind
    # collision, which is the outcome that matters.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class WorkerHarness:
    """Restarts the local worker against a prompts tree per candidate."""

    def __init__(
        self,
        *,
        repo_root: Path,
        fragment: str,
        workdir: Path,
        worker_cmd: list[str] | None = None,
        health_port: int | None = None,
        boot_timeout_s: float = 120.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.fragment = fragment
        # Absolute, always: the tree path crosses a process boundary into a
        # worker started with cwd=repo_root, so a relative one silently
        # resolves somewhere else and the worker dies on a missing fragment.
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._src = self.repo_root / "surogates" / "harness" / "prompts"
        self._rel = Path(f"{fragment}.md")
        self._header, self.seed_body = read_seed(self._src, fragment)
        self._cmd = worker_cmd or [
            str(self.repo_root / ".venv" / "bin" / "surogates"), "worker",
        ]
        # The worker's health side-car takes health_port from config, which
        # a locally running API or a second worker may already hold. Give
        # ours one of its own and readiness has an honest answer.
        self.health_port = health_port or free_port()
        self.boot_timeout_s = boot_timeout_s
        self._env = env or {}
        self.log_path = self.workdir / "worker.log"

    def render(self, body: str) -> str:
        """The full fragment text for *body*, frontmatter re-attached."""
        return self._header + body.strip() + "\n"

    def build_tree(self, body: str) -> Path:
        """Materialise a prompts tree whose only real file is the candidate."""
        tree = self.workdir / "prompts"
        if tree.exists():
            shutil.rmtree(tree)
        for src in sorted(self._src.rglob("*.md")):
            rel = src.relative_to(self._src)
            dst = tree / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if rel == self._rel:
                dst.write_text(self.render(body), encoding="utf-8")
            else:
                dst.symlink_to(src.resolve())
        if not (tree / self._rel).exists():
            raise CandidateRejected(
                f"{self.fragment} is not part of {self._src}"
            )
        self._check_loadable(tree / self._rel)
        return tree

    def _check_loadable(self, path: Path) -> None:
        header, body = split_fragment(path.read_text(encoding="utf-8"))
        if not body.strip():
            raise CandidateRejected("candidate body is empty")
        if not header:
            raise CandidateRejected("candidate lost its frontmatter")
        try:
            meta = yaml.safe_load(header.strip().strip(FENCE)) or {}
        except yaml.YAMLError as exc:
            raise CandidateRejected(f"frontmatter is not valid YAML: {exc}") from exc
        if not meta.get("name"):
            raise CandidateRejected("frontmatter has no 'name'")

    @contextmanager
    def running(self, body: str) -> Iterator[Path]:
        """Run the worker against *body* for the duration of the block."""
        tree = self.build_tree(body)
        stale = other_workers()
        if stale:
            raise StaleWorkerError(
                f"harness worker already running (pid {stale}); stop it first "
                "or its sessions will be served with the shipped prompt"
            )
        env = {
            **os.environ, **self._env,
            "SUROGATES_PROMPTS_ROOT": str(tree),
            "SUROGATES_HEALTH_PORT": str(self.health_port),
        }
        with open(self.log_path, "ab") as log:
            log.write(f"\n=== worker start {time.strftime('%F %T')} ===\n".encode())
            log.flush()
            proc = subprocess.Popen(
                self._cmd, cwd=self.repo_root, env=env,
                stdout=log, stderr=subprocess.STDOUT,
                preexec_fn=_die_with_parent,
            )
        try:
            self._await_boot(proc)
            yield tree
        finally:
            self._stop(proc)

    def _await_boot(self, proc: subprocess.Popen) -> None:
        """Wait for the worker's own readiness probe, not for a clock."""
        url = f"http://127.0.0.1:{self.health_port}/health/ready"
        deadline = time.monotonic() + self.boot_timeout_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"worker exited with code {proc.returncode} during boot; "
                    f"see {self.log_path}"
                )
            try:
                response = httpx.get(url, timeout=2.0)
                if response.status_code == 200:
                    checks = response.json().get("checks", {})
                    bad = {k: v for k, v in checks.items() if v != "ok"}
                    if bad:
                        raise RuntimeError(f"worker unhealthy at boot: {bad}")
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        self._stop(proc)
        raise RuntimeError(
            f"worker was not ready on {url} within {self.boot_timeout_s}s; "
            f"see {self.log_path}"
        )

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        # The worker drains in-flight sessions on SIGTERM, which for a GAIA
        # rollout can mean minutes. There is nothing worth draining here --
        # its candidate is already scored -- and a per-restart wait is paid
        # dozens of times in one search, so the window is short.
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
