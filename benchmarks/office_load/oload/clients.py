"""The load clients: runtime-office instances the tool opens its editor pages on, called as the harness's worker calls
runtime-office.

Each call gets a client of its own, as the harness's do, so no call waits inside the tool for another's connection.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import aiohttp
from yarl import URL

# The harness's timeouts. Opening loads the whole editor in a browser; a deck's edit can take about 292 s.
OPEN_TIMEOUT_S = 90.0
EDIT_TIMEOUT_S = 300.0
CALL_TIMEOUT_S = 60.0  # read, flush and close

LABEL = "app=office-load-client"
# runtime-office's own defaults, for a pod that does not set them.
DEFAULT_PORT = 8080
DEFAULT_MAX_PAGES = 6
TOKEN_ENV = "RUNTIME_OFFICE_TOKEN"  # runtime-office does not start without it: it marks the container
MAX_PAGES_ENV = "RUNTIME_OFFICE_MAX_PAGES"

Cause = Literal["client", "server", "timeout"]


@dataclass(frozen=True, slots=True)
class Timed[T]:
    """A call's answer, and how long the call took in seconds, its client's set-up included."""

    value: T
    seconds: float


class RuntimeOfficeError(Exception):
    """runtime-office answered with a failure: the editor, or the document server behind it, failed.

    ``cause`` is what a session's event blames. A load client's own limit is measured apart, so an editor failure
    (500, 502) counts against the document server. ``status`` is the HTTP status when an answer came; ``seconds`` how
    long the call took.
    """

    cause: Cause = "server"

    def __init__(self, message: str, *, status: int | None = None, seconds: float = 0.0,
                 cause: Cause | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.seconds = seconds
        if cause is not None:
            self.cause = cause


class ClientBusy(RuntimeOfficeError):
    """The load client has as many pages open as it holds (503)."""

    cause: Cause = "client"


class ClientDown(RuntimeOfficeError):
    """The load client could not be reached, or dropped the call."""

    cause: Cause = "client"


class CallTimedOut(RuntimeOfficeError):
    """No answer within the call's timeout. An edit may still have applied."""

    cause: Cause = "timeout"


class PageGone(RuntimeOfficeError):
    """The page is not open any more: its editor died or stopped answering (410, and 404 as the harness reads it)."""


class RequestRefused(Exception):
    """runtime-office refused the request itself, a 4xx other than 404 and 410: the tool or its token is wrong.

    It is no load failure, so not a RuntimeOfficeError: no measurement stands with it.
    """

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class FleetFull(Exception):
    """No load client has room for a session's pages: the client budget is the limit, not the document server."""

    cause: Cause = "client"


class NoClients(Exception):
    """No load client could be found ready to take pages."""


class RuntimeOffice:
    """One load client's HTTP API."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    def __repr__(self) -> str:
        return f"RuntimeOffice({self.base_url!r})"

    async def open(self, doc_key: str, session_id: str, config: dict[str, Any],
                   document_server_url: str) -> Timed[str]:
        """Open *doc_key* in an editor page for *session_id*; the page's id."""
        answered = await self._call("POST", "/pages", {
            "doc_key": doc_key, "session_id": session_id, "config": config, "document_server_url": document_server_url,
        }, OPEN_TIMEOUT_S)
        return Timed(str(_field(answered, "page_id")), answered.seconds)

    async def read(self, page: str, request: dict[str, Any]) -> Timed[dict[str, Any]]:
        """What *page*'s editor reads for *request*, an office_read's view: ``{"view": "slide", "target": "1473"}``."""
        answered = await self._call("POST", f"{_page_path(page)}/read", request, CALL_TIMEOUT_S)
        return Timed(_answer(answered), answered.seconds)

    async def edit(self, page: str, ops: list[Any]) -> Timed[dict[str, Any]]:
        answered = await self._call("POST", f"{_page_path(page)}/edit", {"ops": ops}, EDIT_TIMEOUT_S)
        return Timed(_answer(answered), answered.seconds)

    async def flush(self, page: str) -> Timed[bool]:
        """Whether the editor reports every change acknowledged by the document server."""
        answered = await self._call("POST", f"{_page_path(page)}/flush", {}, CALL_TIMEOUT_S)
        return Timed(bool(_field(answered, "flushed")), answered.seconds)

    async def close(self, page: str) -> None:
        """Close *page*; a page already gone is closed."""
        await self._call("DELETE", _page_path(page), None, CALL_TIMEOUT_S, closing=True)

    async def _call(self, method: str, path: str, body: dict[str, Any] | None, timeout: float, *,
                    closing: bool = False) -> Timed[dict[str, Any] | None]:
        started = time.monotonic()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
                async with http.request(method, URL(f"{self.base_url}{path}", encoded=True), json=body,
                                        headers=self._headers, allow_redirects=False) as response:
                    status, reason = response.status, response.reason or ""
                    raw = await response.read()
        # aiohttp's timeout errors are client errors too: the timeout is caught first.
        except TimeoutError as exc:
            raise CallTimedOut(f"{self.base_url} did not answer {method} {path} within {timeout:g} s",
                               seconds=time.monotonic() - started) from exc
        except aiohttp.ClientError as exc:
            raise ClientDown(f"{self.base_url} could not be reached: {exc!r}",
                             seconds=time.monotonic() - started) from exc
        seconds = time.monotonic() - started
        if closing and status in (204, 404, 410):
            return Timed(None, seconds)
        try:
            answer = json.loads(raw) if raw else None
        except ValueError:
            answer = None
        if status == 200 and isinstance(answer, dict) and not closing:
            return Timed(answer, seconds)
        said = answer.get("error") if isinstance(answer, dict) else None
        message = str(said or raw[:300].decode(errors="replace") or reason)
        if status == 503:
            raise ClientBusy(message, status=status, seconds=seconds)
        if status in (404, 410):
            raise PageGone(message, status=status, seconds=seconds)
        if 400 <= status < 500:
            raise RequestRefused(message, status=status)
        if status < 400:
            raise RuntimeOfficeError(f"not runtime-office's answer: {message}", status=status, seconds=seconds,
                                     cause="client")
        raise RuntimeOfficeError(message, status=status, seconds=seconds)


def _page_path(page: str) -> str:
    return f"/pages/{quote(page, safe='')}"


def _answer(answered: Timed[dict[str, Any] | None]) -> dict[str, Any]:
    assert answered.value is not None  # only a close answers nothing
    return answered.value


def _field(answered: Timed[dict[str, Any] | None], name: str) -> Any:
    answer = _answer(answered)
    if name not in answer:
        raise RuntimeOfficeError(f"runtime-office's answer has no {name!r}: {json.dumps(answer)[:300]}",
                                 status=200, seconds=answered.seconds, cause="client")
    return answer[name]


class Fleet:
    """The load clients, and how many pages the tool has placed on each."""

    def __init__(self, clients: Sequence[RuntimeOffice], pages_per_client: int) -> None:
        if pages_per_client < 1:
            raise ValueError(f"a client holds at least one page, not {pages_per_client}")
        self.clients = tuple(clients)
        self.pages_per_client = pages_per_client
        self._placed = [0] * len(self.clients)
        self._next = 0

    @property
    def capacity(self) -> int:
        return len(self.clients) * self.pages_per_client

    @property
    def placed(self) -> tuple[int, ...]:
        """The pages placed on each client, in :attr:`clients` order."""
        return tuple(self._placed)

    def place(self, pages: int = 1) -> RuntimeOffice:
        """A client for one session's *pages*, all on it: the next client round-robin with room for them."""
        if not 1 <= pages <= self.pages_per_client:
            raise ValueError(f"a session's pages go on one client, which holds {self.pages_per_client}, not {pages}")
        count = len(self.clients)
        for step in range(count):
            index = (self._next + step) % count
            if self._placed[index] + pages <= self.pages_per_client:
                self._placed[index] += pages
                self._next = (index + 1) % count
                return self.clients[index]
        raise FleetFull(f"no load client has room for {pages} more page(s): the fleet holds {self.capacity} "
                        f"({count} clients of {self.pages_per_client})")

    def release(self, client: RuntimeOffice, pages: int = 1) -> None:
        """Give back *pages* placed on *client*, once closed."""
        index = next((i for i, placed in enumerate(self.clients) if placed is client), None)
        if index is None:
            raise ValueError(f"{client!r} is not in the fleet")
        if not 1 <= pages <= self._placed[index]:
            raise ValueError(f"{client!r} has {self._placed[index]} page(s) placed, not {pages} to release")
        self._placed[index] -= pages

    @classmethod
    def from_pods(cls, pods: dict[str, Any], token: str, *, pages_per_client: int | None = None) -> Fleet:
        """The fleet of the ready pods in a ``kubectl get pods -o json`` listing, in ordinal order.

        Each pod holds its ``RUNTIME_OFFICE_MAX_PAGES``. *pages_per_client* defaults to the smallest any pod holds; more
        would be refused by the pods (503), so it is an error.
        """
        ready = sorted((pod for pod in pods.get("items", []) if _ready(pod)), key=_ordinal)
        if not ready:
            raise NoClients(f"no {LABEL} pod is ready")
        clients, holds = [], []
        for pod in ready:
            container = _runtime_office(pod)
            ip = pod["status"]["podIP"]
            host = f"[{ip}]" if ":" in ip else ip
            clients.append(RuntimeOffice(f"http://{host}:{_env_int(container, 'PORT', DEFAULT_PORT)}", token))
            holds.append(_env_int(container, MAX_PAGES_ENV, DEFAULT_MAX_PAGES))
        held = min(holds)
        if pages_per_client is not None and pages_per_client > held:
            raise ValueError(f"the load clients hold {held} pages each, fewer than {pages_per_client}")
        return cls(clients, held if pages_per_client is None else pages_per_client)

    @classmethod
    async def discover(cls, namespace: str, token: str, *, pages_per_client: int | None = None,
                       kubectl: str = "kubectl") -> Fleet:
        """The fleet of the ready ``app=office-load-client`` pods in *namespace*; kubectl reads ``KUBECONFIG``."""
        process = await asyncio.create_subprocess_exec(
            kubectl, "-n", namespace, "get", "pods", "-l", LABEL, "-o", "json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        if process.returncode != 0:
            raise NoClients(f"kubectl could not list the load clients: {err.decode(errors='replace').strip()}")
        return cls.from_pods(json.loads(out), token, pages_per_client=pages_per_client)


def _ready(pod: dict[str, Any]) -> bool:
    """Running, Ready and not going away."""
    status = pod.get("status", {})
    return (
        "deletionTimestamp" not in pod.get("metadata", {})
        and status.get("phase") == "Running"
        and bool(status.get("podIP"))
        and any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
    )


def _ordinal(pod: dict[str, Any]) -> list[int | str]:
    """A StatefulSet pod's name, sorted with its numbers as numbers: -2 before -10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", pod["metadata"]["name"])]


def _runtime_office(pod: dict[str, Any]) -> dict[str, Any]:
    for container in pod["spec"]["containers"]:
        if any(item.get("name") == TOKEN_ENV for item in container.get("env", [])):
            return container
    raise NoClients(f"pod {pod['metadata']['name']} has no container setting {TOKEN_ENV}")


def _env_int(container: dict[str, Any], name: str, default: int) -> int:
    for item in container.get("env", []):
        if item.get("name") == name:
            if "value" not in item:
                raise NoClients(f"container {container.get('name')} sets {name} from elsewhere; it cannot be read here")
            return int(item["value"])
    return default
