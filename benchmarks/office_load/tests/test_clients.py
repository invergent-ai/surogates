"""The load clients: runtime-office's HTTP API as the harness calls it, and the fleet the pages are spread over."""

from __future__ import annotations

import asyncio
import copy
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, unused_port

from oload import clients
from oload.clients import (
    CallTimedOut,
    ClientBusy,
    ClientDown,
    Fleet,
    FleetFull,
    NoClients,
    PageGone,
    RequestRefused,
    RuntimeOffice,
    RuntimeOfficeError,
    Timed,
)

TOKEN = "a-test-runtime-office-token-of-32-characters"
CONFIG = {"document": {"key": "load-r1-0-abc"}, "token": "signed"}
DS_URL = "http://office-docs.example.svc"
PODS = Path(__file__).parent / "fixtures" / "kubectl_pods.json"


@dataclass(frozen=True, slots=True)
class Seen:
    method: str
    raw_path: str
    authorization: str | None
    body: Any


class FakeRuntimeOffice:
    """Answers runtime-office's routes as it does, and records every request.

    *status* and *answer* override every answer; *hold_s* holds each request until :attr:`release` is set, at most
    that long.
    """

    def __init__(self, *, status: int = 200, answer: Any = None, raw: bytes | None = None, hold_s: float = 0.0) -> None:
        self.status = status
        self.answer = answer
        self.raw = raw
        self.hold_s = hold_s
        self.requests: list[Seen] = []
        self.in_flight = 0
        self.release = asyncio.Event()

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        body = json.loads(raw) if raw else None
        self.requests.append(Seen(request.method, request.raw_path, request.headers.get("Authorization"), body))
        if self.hold_s:
            self.in_flight += 1
            try:
                await asyncio.wait_for(self.release.wait(), self.hold_s)
            except TimeoutError:
                pass
            finally:
                self.in_flight -= 1
        if self.raw is not None:
            return web.Response(status=self.status, body=self.raw)
        if self.status != 200 or self.answer is not None:
            return web.json_response(self.answer, status=self.status)
        if request.method == "DELETE":
            return web.Response(status=204)
        if request.path == "/pages":
            return web.json_response({"page_id": f"{body['doc_key']}.{body['session_id']}"})
        if request.path.endswith("/edit"):
            return web.json_response({"ops": len(body["ops"]), "pages": 2})
        return web.json_response({"flushed": True})


def at(server: TestServer) -> RuntimeOffice:
    return RuntimeOffice(str(server.make_url("")), TOKEN)


def refused() -> RuntimeOffice:
    """A load client nothing listens at."""
    return RuntimeOffice(f"http://127.0.0.1:{unused_port()}", TOKEN)


async def call(runtime: RuntimeOffice, what: str, page: str = "load-r1-0-abc.s0") -> Any:
    if what == "open":
        return await runtime.open("load-r1-0-abc", "s0", CONFIG, DS_URL)
    if what == "edit":
        return await runtime.edit(page, [{"insert": {"end": True}, "paragraph": "m1"}])
    if what == "flush":
        return await runtime.flush(page)
    return await runtime.close(page)


CALLS = ("open", "edit", "flush", "close")


# The calls.

async def test_calls_name_runtime_office_routes_with_the_bearer_token() -> None:
    fake = FakeRuntimeOffice()
    async with TestServer(fake.app()) as server:
        runtime = at(server)
        opened = await runtime.open("load-r1-0-abc", "s0", CONFIG, DS_URL)
        edited = await runtime.edit(opened.value, [{"insert": {"end": True}, "paragraph": "m1"}])
        flushed = await runtime.flush(opened.value)
        closed = await runtime.close(opened.value)

    assert isinstance(opened, Timed) and opened.value == "load-r1-0-abc.s0"
    assert edited.value == {"ops": 1, "pages": 2}
    assert flushed.value is True
    assert closed is None
    assert all(0 <= timed.seconds < 5 for timed in (opened, edited, flushed))
    assert [(seen.method, seen.raw_path, seen.body) for seen in fake.requests] == [
        ("POST", "/pages", {"doc_key": "load-r1-0-abc", "session_id": "s0", "config": CONFIG,
                            "document_server_url": DS_URL}),
        ("POST", "/pages/load-r1-0-abc.s0/edit", {"ops": [{"insert": {"end": True}, "paragraph": "m1"}]}),
        ("POST", "/pages/load-r1-0-abc.s0/flush", {}),
        ("DELETE", "/pages/load-r1-0-abc.s0", None),
    ]
    assert {seen.authorization for seen in fake.requests} == {f"Bearer {TOKEN}"}


async def test_the_page_id_is_one_path_segment() -> None:
    fake = FakeRuntimeOffice()
    async with TestServer(fake.app()) as server:
        await call(at(server), "edit", page="a/b c")
        await call(at(server), "close", page="a/b c")
    assert [seen.raw_path for seen in fake.requests] == ["/pages/a%2Fb%20c/edit", "/pages/a%2Fb%20c"]


async def test_flush_reads_flushed_false() -> None:
    fake = FakeRuntimeOffice(answer={"flushed": False})
    async with TestServer(fake.app()) as server:
        flushed = await call(at(server), "flush")
    assert flushed.value is False


def test_the_token_is_not_in_the_repr() -> None:
    assert TOKEN not in repr(RuntimeOffice("http://192.0.2.10:8080/", TOKEN))
    assert RuntimeOffice("http://192.0.2.10:8080/", TOKEN).base_url == "http://192.0.2.10:8080"


# What a failed call raises.

@pytest.mark.parametrize("what", CALLS)
async def test_503_is_client_busy(what: str) -> None:
    fake = FakeRuntimeOffice(status=503, answer={"error": "runtime-office has 4 documents open, its limit"})
    async with TestServer(fake.app()) as server:
        with pytest.raises(ClientBusy) as raised:
            await call(at(server), what)
    busy = raised.value
    assert isinstance(busy, RuntimeOfficeError)
    assert (busy.cause, busy.status) == ("client", 503)
    assert "4 documents open" in str(busy)
    assert 0 <= busy.seconds < 5


@pytest.mark.parametrize("what", CALLS)
async def test_a_refused_connection_is_client_down(what: str) -> None:
    with pytest.raises(ClientDown) as raised:
        await call(refused(), what)
    assert (raised.value.cause, raised.value.status) == ("client", None)
    assert isinstance(raised.value, RuntimeOfficeError)


async def test_a_client_that_drops_the_call_is_client_down() -> None:
    async def hang_up(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(hang_up, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ClientDown) as raised:
            await call(RuntimeOffice(f"http://127.0.0.1:{port}", TOKEN), "edit")
    finally:
        server.close()
        await server.wait_closed()
    assert raised.value.cause == "client"


@pytest.mark.parametrize(("what", "timeout"), [
    ("open", "OPEN_TIMEOUT_S"), ("edit", "EDIT_TIMEOUT_S"), ("flush", "CALL_TIMEOUT_S"), ("close", "CALL_TIMEOUT_S"),
])
async def test_no_answer_within_the_calls_timeout_is_a_timeout(
    what: str, timeout: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clients, timeout, 0.2)
    fake = FakeRuntimeOffice(hold_s=5)
    async with TestServer(fake.app()) as server:
        with pytest.raises(CallTimedOut) as raised:
            await call(at(server), what)
        fake.release.set()
    assert (raised.value.cause, raised.value.status) == ("timeout", None)
    assert 0.2 <= raised.value.seconds < 3


def test_the_timeouts_are_the_harness_ones() -> None:
    assert (clients.OPEN_TIMEOUT_S, clients.EDIT_TIMEOUT_S, clients.CALL_TIMEOUT_S) == (90.0, 300.0, 60.0)


@pytest.mark.parametrize("status", [404, 410])
@pytest.mark.parametrize("what", ["edit", "flush"])
async def test_a_page_not_open_is_page_gone(what: str, status: int) -> None:
    fake = FakeRuntimeOffice(status=status, answer={"error": "the editor stopped answering"})
    async with TestServer(fake.app()) as server:
        with pytest.raises(PageGone) as raised:
            await call(at(server), what)
    assert (raised.value.cause, raised.value.status) == ("server", status)
    assert "stopped answering" in str(raised.value)


@pytest.mark.parametrize("status", [404, 410])
async def test_closing_a_page_not_open_is_fine(status: int) -> None:
    fake = FakeRuntimeOffice(status=status, answer={"error": "no page load-r1-0-abc.s0 is open"})
    async with TestServer(fake.app()) as server:
        assert await call(at(server), "close") is None


@pytest.mark.parametrize(("status", "said"), [
    (502, "the editor stopped answering as the edit was committed"),
    (500, "internal error"),
])
@pytest.mark.parametrize("what", CALLS)
async def test_an_editor_failure_is_the_servers(what: str, status: int, said: str) -> None:
    fake = FakeRuntimeOffice(status=status, answer={"error": said})
    async with TestServer(fake.app()) as server:
        with pytest.raises(RuntimeOfficeError) as raised:
            await call(at(server), what)
    assert type(raised.value) is RuntimeOfficeError
    assert (raised.value.cause, raised.value.status, str(raised.value)) == ("server", status, said)


@pytest.mark.parametrize("status", [400, 401, 405, 413])
async def test_a_refused_request_is_no_load_failure(status: int) -> None:
    fake = FakeRuntimeOffice(status=status, answer={"error": "unauthorized"})
    async with TestServer(fake.app()) as server:
        with pytest.raises(RequestRefused) as raised:
            await call(at(server), "edit")
    assert not isinstance(raised.value, RuntimeOfficeError)
    assert raised.value.status == status and "unauthorized" in str(raised.value)


@pytest.mark.parametrize(("what", "answer"), [
    ("open", {"id": "load-r1-0-abc.s0"}),
    ("flush", {"ok": True}),
    ("edit", None),
])
async def test_an_answer_runtime_office_does_not_give_is_the_clients(what: str, answer: Any) -> None:
    fake = FakeRuntimeOffice(answer=answer) if answer is not None else FakeRuntimeOffice(raw=b"<html>hi</html>")
    async with TestServer(fake.app()) as server:
        with pytest.raises(RuntimeOfficeError) as raised:
            await call(at(server), what)
    assert (raised.value.cause, raised.value.status) == ("client", 200)


async def test_each_call_has_a_client_of_its_own() -> None:
    # More calls at once than one aiohttp connection pool lets through (100): none waits inside the tool.
    fake = FakeRuntimeOffice(hold_s=10)
    async with TestServer(fake.app()) as server:
        runtime = at(server)
        edits = [asyncio.create_task(call(runtime, "edit")) for _ in range(150)]
        async with asyncio.timeout(8):
            while fake.in_flight < 150:
                await asyncio.sleep(0.02)
        fake.release.set()
        done = await asyncio.gather(*edits)
    assert len(done) == 150 and all(edited.value == {"ops": 1, "pages": 2} for edited in done)


# The fleet.

def fleet(clients_: int = 3, pages_per_client: int = 4) -> Fleet:
    return Fleet([RuntimeOffice(f"http://192.0.2.{10 + i}:8080", TOKEN) for i in range(clients_)], pages_per_client)


def test_place_spreads_pages_round_robin() -> None:
    spread = fleet()
    placed = [spread.place(1) for _ in range(10)]
    assert [spread.clients.index(client) for client in placed] == [0, 1, 2, 0, 1, 2, 0, 1, 2, 0]
    assert spread.placed == (4, 3, 3)
    assert spread.capacity == 12


def test_past_the_fleets_pages_is_fleet_full() -> None:
    spread = fleet()
    for _ in range(12):
        spread.place(1)
    assert spread.placed == (4, 4, 4)
    with pytest.raises(FleetFull) as raised:
        spread.place(1)
    assert raised.value.cause == "client"
    assert "12" in str(raised.value)
    assert spread.placed == (4, 4, 4)


def test_a_sessions_pages_go_on_one_client_with_room() -> None:
    spread = fleet(pages_per_client=3)
    assert [spread.clients.index(spread.place(2)) for _ in range(3)] == [0, 1, 2]
    # A page free on each client, none with room for two.
    with pytest.raises(FleetFull):
        spread.place(2)
    assert spread.clients.index(spread.place(1)) == 0
    assert spread.placed == (3, 2, 2)


def test_place_skips_a_client_without_room() -> None:
    spread = fleet(pages_per_client=2)
    first = spread.place(2)
    assert spread.clients.index(first) == 0
    assert [spread.clients.index(spread.place(1)) for _ in range(4)] == [1, 2, 1, 2]


def test_released_pages_make_room_again() -> None:
    spread = fleet(clients_=2, pages_per_client=1)
    first, second = spread.place(1), spread.place(1)
    spread.release(first, 1)
    assert spread.placed == (0, 1)
    assert spread.place(1) is first
    spread.release(second)
    with pytest.raises(ValueError):
        spread.release(second)
    with pytest.raises(ValueError):
        spread.release(RuntimeOffice("http://192.0.2.99:8080", TOKEN))


def test_more_pages_than_a_client_holds_is_an_error() -> None:
    with pytest.raises(ValueError):
        fleet(pages_per_client=1).place(2)
    with pytest.raises(ValueError):
        fleet().place(0)
    with pytest.raises(ValueError):
        fleet(pages_per_client=0)


# Discovery.

def pods() -> dict[str, Any]:
    return json.loads(PODS.read_text())


def test_discovery_reads_the_ready_pods_of_a_recorded_listing() -> None:
    found = Fleet.from_pods(pods(), TOKEN)
    # -2 is not ready yet, -3 is not scheduled and -4 is going away.
    assert [client.base_url for client in found.clients] == ["http://192.0.2.10:8080", "http://192.0.2.11:8080"]
    # As many pages as each pod holds: its RUNTIME_OFFICE_MAX_PAGES.
    assert found.pages_per_client == 4
    assert found.placed == (0, 0)
    assert all(client._headers == {"Authorization": f"Bearer {TOKEN}"} for client in found.clients)


def test_discovery_orders_the_pods_by_ordinal_and_reads_their_port() -> None:
    listing = pods()
    template = listing["items"][0]
    listing["items"] = []
    for ordinal, port in ((10, "8081"), (2, "8080"), (1, None)):
        pod = copy.deepcopy(template)
        pod["metadata"]["name"] = f"office-load-client-{ordinal}"
        pod["status"]["podIP"] = f"192.0.2.{ordinal + 100}"
        env = pod["spec"]["containers"][0]["env"]
        pod["spec"]["containers"][0]["env"] = [item for item in env if item["name"] != "PORT"]
        if port is not None:
            pod["spec"]["containers"][0]["env"].append({"name": "PORT", "value": port})
        listing["items"].append(pod)
    found = Fleet.from_pods(listing, TOKEN)
    assert [client.base_url for client in found.clients] == [
        "http://192.0.2.101:8080", "http://192.0.2.102:8080", "http://192.0.2.110:8081",
    ]


def test_discovery_finds_the_runtime_office_container_beside_another() -> None:
    listing = pods()
    for pod in listing["items"]:
        pod["spec"]["containers"].insert(0, {"name": "sidecar", "env": [{"name": "PORT", "value": "15001"}]})
    found = Fleet.from_pods(listing, TOKEN)
    assert found.clients[0].base_url == "http://192.0.2.10:8080"


def test_discovery_takes_the_smallest_pool_and_runtime_offices_default() -> None:
    listing = pods()
    container = listing["items"][1]["spec"]["containers"][0]
    container["env"] = [item for item in container["env"] if item["name"] != "RUNTIME_OFFICE_MAX_PAGES"]
    # A pod that sets no pool size holds runtime-office's default of 6.
    assert Fleet.from_pods(listing, TOKEN).pages_per_client == 4
    for item in listing["items"][0]["spec"]["containers"][0]["env"]:
        if item["name"] == "RUNTIME_OFFICE_MAX_PAGES":
            item["value"] = "8"
    assert Fleet.from_pods(listing, TOKEN).pages_per_client == 6


def test_discovery_takes_fewer_pages_than_the_pods_hold_but_not_more() -> None:
    assert Fleet.from_pods(pods(), TOKEN, pages_per_client=3).pages_per_client == 3
    with pytest.raises(ValueError) as raised:
        Fleet.from_pods(pods(), TOKEN, pages_per_client=5)
    assert "4" in str(raised.value)


def test_discovery_with_no_ready_pod_is_no_clients() -> None:
    listing = pods()
    listing["items"] = listing["items"][2:]
    with pytest.raises(NoClients):
        Fleet.from_pods(listing, TOKEN)


def fake_kubectl(tmp_path: Path, *, exit_code: int = 0) -> Path:
    """A kubectl that records its arguments and prints the recorded listing."""
    script = tmp_path / "kubectl"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > '{tmp_path / 'args'}'\n"
        + (f"cat '{PODS}'\n" if exit_code == 0 else f"echo 'error: You must be logged in' >&2\nexit {exit_code}\n")
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


async def test_discover_lists_the_labelled_pods_with_kubectl(tmp_path: Path) -> None:
    found = await Fleet.discover("office-load", TOKEN, kubectl=str(fake_kubectl(tmp_path)))
    assert (tmp_path / "args").read_text().split("\n")[:-1] == [
        "-n", "office-load", "get", "pods", "-l", "app=office-load-client", "-o", "json",
    ]
    assert [client.base_url for client in found.clients] == ["http://192.0.2.10:8080", "http://192.0.2.11:8080"]
    assert found.pages_per_client == 4


async def test_discover_says_why_kubectl_failed(tmp_path: Path) -> None:
    with pytest.raises(NoClients) as raised:
        await Fleet.discover("office-load", TOKEN, kubectl=str(fake_kubectl(tmp_path, exit_code=1)))
    assert "You must be logged in" in str(raised.value)
