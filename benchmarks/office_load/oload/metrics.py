"""The office namespace's metrics through a step, sampled with kubectl into the step's ``metrics.jsonl``.

Each sample holds:
- ``top``: CPU in millicores and memory in MiB per container, ``<pod>/<container>``, from ``kubectl top``;
- ``queues``: RabbitMQ's queue depths, from ``rabbitmqctl`` in its pod;
- ``errors``: docservice's and the converters' error lines since the step's start, counted, the first few kept;
- ``db_connections``: the document server database's connections, from ``pg_stat_activity`` on the CNPG primary;
- ``failed``: each probe that failed, with why. A failed probe's value is None: the step goes on regardless.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Any

EVERY_S = 15.0
PROBE_TIMEOUT_S = 10.0  # each kubectl call
KEEP_LINES = 5  # the first error lines of each component kept in a sample

# The office chart's pods: label, container.
RABBITMQ = ("app=office-rabbitmq", "rabbitmq")
LOGS: dict[str, tuple[str, str]] = {
    "docservice": ("app=office-docs", "docservice"),
    "converter": ("app=office-converter", "converter"),
}
# docservice and the converters log through log4js: "[<time>] [ERROR] ... nodeJS - <message>".
ERROR_LINE = re.compile(r"\[(?:ERROR|FATAL)\]")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

Runner = Callable[[Sequence[str], float], Awaitable[str]]


class ProbeFailed(Exception):
    """A kubectl call failed or did not answer in time."""


@dataclass(frozen=True, slots=True)
class Postgres:
    """Where the document server's database is: a CNPG cluster, whose primary is found by label."""

    namespace: str = "default"
    cluster: str = "surogate-pg"
    database: str = "docs_load"

    def __post_init__(self) -> None:
        if not IDENTIFIER.fullmatch(self.database):
            raise ValueError(f"a database name is letters, digits and underscores, not {self.database!r}")

    @property
    def selector(self) -> str:
        return f"cnpg.io/cluster={self.cluster},cnpg.io/instanceRole=primary"

    @property
    def query(self) -> str:
        return f"SELECT count(*) FROM pg_stat_activity WHERE datname = '{self.database}'"


async def run_command(argv: Sequence[str], timeout_s: float) -> str:
    """*argv*'s stdout; ProbeFailed when it exits nonzero or takes over *timeout_s*, which kills it."""
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(timeout_s):
            out, err = await process.communicate()
    except TimeoutError:
        raise ProbeFailed(f"{' '.join(argv[:4])} … did not answer within {timeout_s:g} s") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        said = err.decode(errors="replace").strip()
        raise ProbeFailed(said[:300] or f"{' '.join(argv[:4])} … exited {process.returncode}")
    return out.decode(errors="replace")


_CPU = {"n": 1e-6, "u": 1e-3, "m": 1.0, "": 1000.0}
_MEMORY = {"Ki": 1 / 1024, "Mi": 1.0, "Gi": 1024.0, "Ti": 1024.0**2, "k": 1e3 / 2**20, "K": 1e3 / 2**20,
           "M": 1e6 / 2**20, "G": 1e9 / 2**20, "T": 1e12 / 2**20, "": 1 / 2**20}
_QUANTITY = re.compile(r"(\d+(?:\.\d+)?)([A-Za-z]*)")


def _quantity(text: str, units: dict[str, float]) -> float:
    match = _QUANTITY.fullmatch(text.strip())
    if match is None or match[2] not in units:
        raise ValueError(f"not a quantity: {text!r}")
    return float(match[1]) * units[match[2]]


def cpu_millicores(text: str) -> int:
    """A CPU quantity (``250m``, ``2``, ``1500000n``) in millicores."""
    return round(_quantity(text, _CPU))


def memory_mib(text: str) -> float:
    """A memory quantity (``1342Mi``, ``2Gi``, ``1G``, bytes) in MiB."""
    return round(_quantity(text, _MEMORY), 1)


def parse_top(text: str) -> dict[str, dict[str, float]]:
    """``kubectl top pods --containers``: ``{"<pod>/<container>": {"cpu_m": …, "mem_mi": …}}``."""
    top = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 4 or fields[0] == "POD":
            continue
        pod, container, cpu, memory = fields
        top[f"{pod}/{container}"] = {"cpu_m": cpu_millicores(cpu), "mem_mi": memory_mib(memory)}
    return top


def parse_queues(text: str) -> dict[str, int]:
    """``rabbitmqctl list_queues name messages``: each queue's depth. Its banner and header lines are skipped."""
    queues = {}
    for line in text.splitlines():
        name, _, count = line.strip().rpartition("\t")
        if not name:
            name, _, count = line.strip().rpartition(" ")
        if name and count.isdigit():
            queues[name.strip()] = int(count)
    return queues


def error_lines(text: str) -> list[str]:
    """The lines of a log logged at ERROR or FATAL."""
    return [line for line in text.splitlines() if ERROR_LINE.search(line)]


def _first_pod(listing: str, what: str) -> str:
    pods = listing.split()
    if not pods:
        raise ProbeFailed(f"no pod {what}")
    return pods[0]


class Sampler:
    """Samples *namespace*'s metrics every *every_s* seconds, at its start and once more at its end.

    Each sample goes to ``<out_dir>/metrics.jsonl``. The step makes *out_dir*, never the sampler: samples taken
    before it exists are written once it does. A step dir that holds metrics already is refused.
    """

    def __init__(self, namespace: str, every_s: float = EVERY_S, *, out_dir: str | Path,
                 postgres: Postgres | None = None, kubectl: str = "kubectl", run: Runner = run_command,
                 timeout_s: float = PROBE_TIMEOUT_S) -> None:
        self.namespace = namespace
        self.every_s = every_s
        self.path = Path(out_dir) / "metrics.jsonl"
        self.postgres = postgres if postgres is not None else Postgres()
        self.kubectl = kubectl
        self.run = run
        self.timeout_s = timeout_s
        self.since = 0.0
        self.taken = 0
        self._held: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Sampler:
        if self.path.exists():
            raise FileExistsError(f"{self.path} holds metrics already: a run id names one run")
        self.since = time.time()
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        assert self._task is not None
        self._task.cancel()
        await asyncio.wait([self._task])
        if exc_type is None:
            await self.sample()
        self._write()

    async def _loop(self) -> None:
        start = time.monotonic()
        while True:
            await self.sample()
            await asyncio.sleep(self.every_s - (time.monotonic() - start) % self.every_s)

    async def sample(self) -> dict[str, Any]:
        """Take one sample, every probe at once; record it."""
        at = time.time()
        failed: dict[str, str] = {}
        probes: dict[str, Callable[[], Awaitable[Any]]] = {
            "top": self._top,
            "queues": self._queues,
            **{f"errors.{component}": partial(self._errors, *where) for component, where in LOGS.items()},
            "db_connections": self._db_connections,
        }
        values = dict(zip(probes, await asyncio.gather(*(self._probe(name, probe, failed)
                                                          for name, probe in probes.items())), strict=True))
        record = {
            "at": at,
            "top": values["top"],
            "queues": values["queues"],
            "errors": {component: values[f"errors.{component}"] for component in LOGS},
            "db_connections": values["db_connections"],
            "failed": failed,
        }
        self.taken += 1
        self._held.append(record)
        self._write()
        return record

    async def _probe(self, name: str, probe: Callable[[], Awaitable[Any]], failed: dict[str, str]) -> Any:
        try:
            return await probe()
        except Exception as exc:  # a broken probe is recorded, never the step's end
            failed[name] = str(exc)[:300] or type(exc).__name__
            return None

    def _write(self) -> None:
        if not self._held or not self.path.parent.is_dir():
            return
        with self.path.open("a") as file:
            file.writelines(json.dumps(record) + "\n" for record in self._held)
        self._held = []

    async def _kubectl(self, namespace: str, *args: str) -> str:
        return await self.run([self.kubectl, "-n", namespace, *args], self.timeout_s)

    async def _top(self) -> dict[str, dict[str, float]]:
        return parse_top(await self._kubectl(self.namespace, "top", "pods", "--containers"))

    async def _queues(self) -> dict[str, int]:
        selector, container = RABBITMQ
        pod = _first_pod(await self._kubectl(self.namespace, "get", "pods", "-l", selector, "-o", "name"),
                         f"labelled {selector}")
        return parse_queues(await self._kubectl(self.namespace, "exec", pod, "-c", container, "--",
                                                "rabbitmqctl", "-q", "list_queues", "name", "messages"))

    async def _errors(self, selector: str, container: str) -> dict[str, Any]:
        since = datetime.fromtimestamp(self.since, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # A selector alone gives each pod's last 10 lines: --tail=-1 gives them all.
        lines = error_lines(await self._kubectl(self.namespace, "logs", "-l", selector, "-c", container,
                                                f"--since-time={since}", "--tail=-1", "--prefix"))
        return {"count": len(lines), "lines": [line[:300] for line in lines[:KEEP_LINES]]}

    async def _db_connections(self) -> int:
        pg = self.postgres
        pod = _first_pod(await self._kubectl(pg.namespace, "get", "pods", "-l", pg.selector, "-o", "name"),
                         f"labelled {pg.selector}")
        answer = await self._kubectl(pg.namespace, "exec", pod, "-c", "postgres", "--", "psql", "-tAc", pg.query)
        return int(answer.strip())
