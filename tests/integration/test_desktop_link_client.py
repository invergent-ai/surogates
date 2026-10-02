"""The desktop app's link client against the real device link: the protocol both sides speak."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from surogates.devices.operations import CANCELLED_OUTCOME, DeviceOperations, OperationRequest

from .test_devices import (  # noqa: F401  (fixtures)
    _fields,
    api,
    eventually,
    laptop_rig,
    link_url,
    outcome_of,
    request_for,
    stop,
)

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]

DESKTOP = Path(__file__).resolve().parents[2] / "desktop"
CLIENT = DESKTOP / "dist" / "testing" / "echo-client.js"


@pytest.fixture(scope="session")
def built_client() -> Path:
    if shutil.which("npm") is None:
        pytest.fail("the desktop cross-check needs npm")
    subprocess.run(["npm", "ci"], cwd=DESKTOP, check=True)
    subprocess.run(["npm", "run", "build"], cwd=DESKTOP, check=True)
    assert CLIENT.exists(), f"the build left no {CLIENT}"
    return CLIENT


class Client:
    """The echo client in a subprocess, its events read line by line."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.events: list[dict] = []
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.process.stdout is not None
        async for line in self.process.stdout:
            self.events.append(json.loads(line))

    async def until(self, check, timeout: float = 10.0) -> None:
        async def seen() -> bool:
            return check(self.events)
        await eventually(seen, timeout=timeout)

    async def close(self) -> None:
        try:
            if self.process.returncode is None:
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 10.0)
            except TimeoutError:
                # An app that ignores SIGTERM must not outlive its test.
                self.process.kill()
                await self.process.wait()
        finally:
            self._reader.cancel()


async def client(built_client: Path, url: str, token: str, journal: Path, *, hold: bool = False) -> Client:
    args = ["node", str(built_client), "--url", url, "--token", token, "--journal", str(journal)]
    process = await asyncio.create_subprocess_exec(
        *args, *(["--hold"] if hold else []), stdout=asyncio.subprocess.PIPE,
    )
    return Client(process)


def connected(events: list[dict]) -> bool:
    return any(e == {"event": "status", "status": "connected"} for e in events)


def held(rig) -> OperationRequest:
    """An operation the echo client holds until it is cancelled."""
    return OperationRequest(**{
        **_fields(request_for(rig.device_id, rig.root)),
        "kind": "run",
        "args": {"command": "true", "workdir": None, "timeout": 10},
    })


def journal_row(journal: Path, operation_id: str):
    with contextlib.closing(sqlite3.connect(journal)) as db:
        return db.execute("SELECT state, outcome FROM operations WHERE id = ?", (operation_id,)).fetchone()


async def test_the_app_answers_an_operation_the_server_sends(built_client, laptop_rig, link_url, tmp_path):
    rig = laptop_rig
    app = await client(built_client, link_url, rig.token, tmp_path / "journal.sqlite")
    try:
        await app.until(connected)
        outcome = await asyncio.wait_for(rig.ops.run(request_for(rig.device_id, rig.root)), 10.0)
        assert outcome == {"ok": "/usr/bin/sh"}
    finally:
        await app.close()


async def test_a_cancel_reaches_the_app_and_stops_its_work(built_client, laptop_rig, link_url, tmp_path):
    rig = laptop_rig
    app = await client(built_client, link_url, rig.token, tmp_path / "journal.sqlite", hold=True)
    try:
        await app.until(connected)
        waiting = asyncio.create_task(rig.ops.run(held(rig)))
        await app.until(lambda events: any(e["event"] == "op" for e in events))
        await rig.ops.cancel([rig.root])
        assert await asyncio.wait_for(waiting, 5.0) == CANCELLED_OUTCOME
        await app.until(lambda events: any(e["event"] == "cancel" for e in events))
    finally:
        await app.close()


async def test_an_app_that_quit_mid_operation_reports_it_interrupted_and_the_cancellation_stands(
    built_client, laptop_rig, link_url, session_factory, tmp_path,
):
    rig = laptop_rig
    journal = tmp_path / "journal.sqlite"
    app = await client(built_client, link_url, rig.token, journal, hold=True)
    try:
        await app.until(connected)
        first = asyncio.create_task(rig.ops.run(held(rig)))
        await app.until(lambda events: any(e["event"] == "op" for e in events))
        operation_id = next(e["id"] for e in app.events if e["event"] == "op")
    finally:
        await app.close()  # the app quits with the operation started

    await rig.ops.cancel([rig.root])
    assert await asyncio.wait_for(first, 5.0) == CANCELLED_OUTCOME
    again = await client(built_client, link_url, rig.token, journal, hold=True)
    try:
        await again.until(connected)

        # Its journal answers the cut-off operation "interrupted"; the server
        # takes that as a duplicate of the cancellation, acknowledges it, and keeps it.
        await again.until(lambda events: {"event": "ack", "id": operation_id} in events)
        assert await outcome_of(session_factory, rig.root) == CANCELLED_OUTCOME
    finally:
        await again.close()

    # The journal's file is locked while the app runs: read it once the app has quit.
    state, outcome = journal_row(journal, operation_id)
    assert state == "acknowledged"
    assert json.loads(outcome)["error"]["type"] == "interrupted"


async def test_an_app_that_quit_mid_operation_is_sent_it_again_and_never_runs_it_again(
    built_client, laptop_rig, link_url, tmp_path, monkeypatch,
):
    rig = laptop_rig
    journal = tmp_path / "journal.sqlite"
    app = await client(built_client, link_url, rig.token, journal, hold=True)
    waiting = None
    try:
        await app.until(connected)
        waiting = asyncio.create_task(rig.ops.run(held(rig)))
        await app.until(lambda events: any(e["event"] == "op" for e in events))
        operation_id = next(e["id"] for e in app.events if e["event"] == "op")
    except BaseException:
        if waiting is not None:
            await stop(waiting)
        raise
    finally:
        await app.close()  # the app quits with the operation started, and nothing cancels it

    # After a welcome the server looks for the app's open operations while it reads the app's
    # first reply, and which one is first is down to timing. Hold the record of any reply
    # until the server's look has found the operation: the repeat is always sent.
    sent_again = asyncio.Event()
    order: list[str] = []
    real_pending, real_complete = DeviceOperations.pending, DeviceOperations.complete

    async def pending(self, *args, **kwargs):
        found = await real_pending(self, *args, **kwargs)
        if any(str(op.id) == operation_id for op in found) and not sent_again.is_set():
            order.append("found")
            sent_again.set()
        return found

    async def complete(self, device_id, generation, id_, *args, **kwargs):
        if str(id_) == operation_id:
            await asyncio.wait_for(sent_again.wait(), 5.0)
        result = await real_complete(self, device_id, generation, id_, *args, **kwargs)
        if str(id_) == operation_id:
            order.append("recorded")
        return result

    monkeypatch.setattr(DeviceOperations, "pending", pending)
    monkeypatch.setattr(DeviceOperations, "complete", complete)

    again = None
    try:
        again = await client(built_client, link_url, rig.token, journal, hold=True)
        await again.until(connected)
        outcome = await asyncio.wait_for(waiting, 10.0)
        # The app answers twice from its journal: on welcome, with what its restart recovered,
        # and to the repeat. Each is acknowledged, so two acks mean the repeat was handled.
        await again.until(lambda events: events.count({"event": "ack", "id": operation_id}) == 2)
        assert not any(e["event"] == "op" for e in again.events)
    finally:
        await stop(waiting)
        if again is not None:
            await again.close()

    assert order[:2] == ["found", "recorded"]  # the gate was hit, and held
    # The server holds what the app's journal recorded, nothing the app ran.
    state, recorded = journal_row(journal, operation_id)
    assert state == "acknowledged"
    assert outcome == json.loads(recorded)
    assert outcome["error"]["type"] == "interrupted"
