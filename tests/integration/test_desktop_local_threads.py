"""Project threads on a folder of the user's computer, through the real desktop app: each in a copy of its own, its turn landing in the folder.

The app is the echo client in a subprocess: its real link, journal, binder
and executor, the file helper's hosts in their sandbox, and the guest under
KVM that keeps the folder's history and the threads' copies.  Each turn is a
worker's real wake, its model a script.  Behind ``SUROGATE_VM_TESTS=1``:
the guest image built by ``images/guest/build.sh``, and the agent's disk.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from surogates.db.models import DeviceOperation
from surogates.devices.binding import BIND
from surogates.session.events import EventType
from tests.test_steer_loop import _final_response

from .test_desktop_file_operations import journal_dir  # noqa: F401  (a fixture)
from .test_desktop_link_client import DESKTOP, built_client, client, connected  # noqa: F401  (built_client is a fixture)
from .test_device_sessions import is_bound
from .test_devices import api, eventually, link_url, register  # noqa: F401  (api and link_url are fixtures)
from .test_local_history_landing import said
from .test_local_history_open import woken
from .test_local_threads import begin, device_card, make_local
from .test_turn_sagas import calling
from .test_workstream_overview import rows
from .test_workstream_threads import PROPOSED, call_tool, events_of

pytestmark = [
    pytest.mark.desktop,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(os.environ.get("SUROGATE_VM_TESTS") != "1", reason="boots the VM: set SUROGATE_VM_TESTS=1"),
]

#: A turn's whole wake, a landing's steps through the guest and the file helper among them.
TURN_S = 600.0


@pytest.fixture(scope="session")
def agent_disk(built_client) -> Path:  # noqa: F811
    """The disk the guest's agent boots from, with the folder's history on it."""
    disk = DESKTOP / "dist" / "agent.img"
    subprocess.run(["npm", "run", "agent-disk"], cwd=DESKTOP, check=True)
    assert disk.exists(), f"the build left no {disk}"
    return disk


@pytest.fixture()
def apart(monkeypatch):
    """A home and a runtime folder of the test's own for the app: its sockets' paths short enough, nothing of the user's."""
    home = Path(tempfile.mkdtemp(prefix="sd-home-", dir="/tmp"))
    given = os.environ.get("XDG_RUNTIME_DIR", "")
    run = Path(tempfile.mkdtemp(prefix="vm-", dir=given if given.startswith("/tmp/") else "/tmp"))
    run.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    yield
    shutil.rmtree(run, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)


async def confirmed_on(app, times: int = 1) -> list[dict]:
    """The folder and nonce the app's user confirmed, as the page would send them, once for each thread to make there."""
    await app.until(lambda events: sum(e["event"] in ("prepared", "error") for e in events) >= times, timeout=60.0)
    prepared = [e for e in app.events if e["event"] in ("prepared", "error")]
    assert all(e["event"] == "prepared" for e in prepared), prepared
    await app.until(connected, timeout=60.0)
    return prepared


async def a_thread_on_the_app(api, device: dict, prepared: dict, project=None, master=None):
    """A project's thread the routes make on the app's computer, bound there by the app's own binder, and begun."""
    if project is None:
        project, master, proposal_id = await device_card(api)
    else:
        proposal_id = (await call_tool(api, master, "propose_threads", threads=PROPOSED))["proposal_id"]
    made = await make_local(api, project, proposal_id, {
        "kind": "device", "device_id": device["id"], "folder": prepared["folder"], "nonce": prepared["nonce"],
    })
    assert made.status_code == 201, made.text
    thread_id = made.json()["thread_id"]
    await eventually(lambda: is_bound(api, thread_id), timeout=60.0)
    assert (await begin(api, project, thread_id)).status_code == 201
    return project, master, await api.app.state.session_store.get_session(UUID(thread_id))


async def bound_as(api, thread) -> dict:
    """What the app answered the thread's bind with."""
    async with api.app.state.session_factory() as db:
        return await db.scalar(select(DeviceOperation.outcome).where(
            DeviceOperation.calling_session_id == thread.id, DeviceOperation.invocation_id == BIND,
        ))


def files_of(report) -> list[tuple]:
    return [(f["ref"], f["landing"], f.get("reason")) for f in report.data["files"] if f["kind"] == "file"]


def entries(folder: Path) -> dict[str, bytes | None]:
    """Every entry of *folder*, a file by its bytes; no link is followed."""
    return {str(p.relative_to(folder)): (p.read_bytes() if p.is_file() and not p.is_symlink() else None) for p in sorted(folder.rglob("*"))}


def nothing_kept(journal_dir: Path) -> bool:  # noqa: F811
    """Whether the app keeps nothing of a landing: every one recorded or put back, and forgotten."""
    return not [p for p in (journal_dir / "data" / "landings").rglob("*") if p.is_file()]


async def test_a_thread_works_in_its_copy_on_the_app_and_its_turns_land_in_the_folder_around_your_save(
    built_client, agent_disk, apart, journal_dir, api, link_url, tmp_path, monkeypatch,  # noqa: F811
):
    folder = tmp_path / "Reports"
    folder.mkdir()
    (folder / "Report.docx").write_bytes(b"PK report v1")
    device = await register(api)
    app = await client(built_client, link_url, device["token"], journal_dir / "journal.sqlite", confirm=folder)
    try:
        [prepared] = await confirmed_on(app)
        _, master, thread = await a_thread_on_the_app(api, device, prepared)
        # The app's binder bound it to a copy of the folder of its own, and said so.
        assert await bound_as(api, thread) == {"ok": {"history": {"thread": str(thread.id)}}}

        # Its turn writes two files in its copy: the folder does not change while it works, and its end lands them.
        before = entries(folder)
        seen: list[tuple] = []

        async def looked(harness) -> None:
            [copy] = (journal_dir / "data" / "history").glob(f"*/threads/{thread.id}")
            seen.append((entries(folder), (copy / "Budget.xlsx").read_text(), (copy / "Notes.md").read_text()))

        await asyncio.wait_for(woken(api, monkeypatch, thread, [
            calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"}), ("write_file", {"path": "Notes.md", "content": "Checked.\n"})),
            calling(("memory", {"action": "add", "content": "Checked the totals."})),
            _final_response("The totals are in Budget.xlsx."),
        ], during=looked), TURN_S)
        assert seen == [(before, "Total,42\n", "Checked.\n")]
        # Its chat said, while the guest made its first copy, that it was being made: nothing else was waited for.
        waits = await events_of(api, thread.id, EventType.DEVICE_WAITING, EventType.DEVICE_RESUMED)
        assert [(e.type, e.data.get("reason"), e.data.get("folder")) for e in waits] == [
            (EventType.DEVICE_WAITING.value, "copying", prepared["folder"]), (EventType.DEVICE_RESUMED.value, None, None),
        ]
        assert entries(folder) == {**before, "Budget.xlsx": b"Total,42\n", "Notes.md": b"Checked.\n"}
        [landed] = await events_of(api, master.id, EventType.WORKER_COMPLETE)
        assert files_of(landed) == [("Budget.xlsx", "landed", None), ("Notes.md", "landed", None)]
        assert nothing_kept(journal_dir)

        # You save the report while its next turn works on it: your save stands, and the thread redoes its change on it.
        async def you_save(harness) -> None:
            (folder / "Report.docx").write_bytes(b"PK report v2, by you")

        await asyncio.wait_for(woken(api, monkeypatch, thread, [
            calling(("terminal", {"command": "printf ' edited' >> Report.docx"})),
            calling(("memory", {"action": "add", "content": "Edited the report."})),
            _final_response("I added a line to the report."),
        ], said="Add a line to the report.", during=you_save), TURN_S)
        assert (folder / "Report.docx").read_bytes() == b"PK report v2, by you"
        [redo] = await events_of(api, thread.id, EventType.HISTORY_REDO)
        assert redo.data["files"] == [{"path": "Report.docx", "reason": "changed", "by": {"kind": "you"}}]
        [_, left_out] = await events_of(api, master.id, EventType.WORKER_COMPLETE)
        assert files_of(left_out) == [("Report.docx", "redoing", "changed")]
        # The redo wakes it: its change, made again on your save, lands.
        await asyncio.wait_for(woken(api, monkeypatch, thread, [
            calling(("terminal", {"command": "printf ' edited' >> Report.docx"})),
            _final_response("I added the line to your newer report."),
        ]), TURN_S)
        assert (folder / "Report.docx").read_bytes() == b"PK report v2, by you edited"
        assert set(entries(folder)) == {"Budget.xlsx", "Notes.md", "Report.docx"}
        assert nothing_kept(journal_dir)
    finally:
        await app.close()


async def test_two_threads_on_one_folder_each_land_and_each_names_the_file_the_other_changed(
    built_client, agent_disk, apart, journal_dir, api, link_url, tmp_path, monkeypatch,  # noqa: F811
):
    folder = tmp_path / "Reports"
    folder.mkdir()
    (folder / "Report.docx").write_bytes(b"PK report v1")
    device = await register(api)
    app = await client(built_client, link_url, device["token"], journal_dir / "journal.sqlite", confirm=folder, times=2)
    try:
        first, second = await confirmed_on(app, times=2)
        project, master, one = await a_thread_on_the_app(api, device, first)
        _, _, two = await a_thread_on_the_app(api, device, second, project, master)
        assert [await bound_as(api, thread) for thread in (one, two)] == [
            {"ok": {"history": {"thread": str(thread.id)}}} for thread in (one, two)
        ]
        # The second's turn begins from the folder as it is: it writes the budget and its notes. Meanwhile the first lands
        # the budget, and its next turn, begun from the folder as that landing left it, writes notes of its own; the
        # second lands before the first's next turn ends.
        wrote, landed = asyncio.Event(), asyncio.Event()

        async def until_the_second_lands(harness) -> None:
            wrote.set()
            await landed.wait()

        later: list[asyncio.Task] = []

        async def meanwhile(harness) -> None:
            await woken(api, monkeypatch, one, [
                calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"})), _final_response("Budget.xlsx has the totals."),
            ])
            assert (folder / "Budget.xlsx").read_text() == "Total,42\n"
            later.append(asyncio.create_task(woken(api, monkeypatch, one, [
                calling(("write_file", {"path": "Notes.md", "content": "One's notes\n"})),
                calling(("memory", {"action": "add", "content": "Noted."})),
                _final_response("The notes are in Notes.md."),
            ], said="Note the totals.", during=until_the_second_lands)))
            await asyncio.wait_for(wrote.wait(), TURN_S)

        await asyncio.wait_for(woken(api, monkeypatch, two, [
            calling(("write_file", {"path": "Budget.xlsx", "content": "Total,43\n"}), ("write_file", {"path": "Notes.md", "content": "Two's notes\n"})),
            calling(("memory", {"action": "add", "content": "Fixed the total."})),
            _final_response("Fixed the total and noted it."),
        ], during=meanwhile), TURN_S)
        landed.set()
        await asyncio.wait_for(later[0], TURN_S)
        # Each landed what the other had not changed, and named the file the other had: its change to it is redone.
        assert ((folder / "Budget.xlsx").read_text(), (folder / "Notes.md").read_text()) == ("Total,42\n", "Two's notes\n")
        by = {thread.id: {"kind": "thread", "id": str(thread.id), "title": "Check the totals"} for thread in (one, two)}
        [redo_two] = await events_of(api, two.id, EventType.HISTORY_REDO)
        assert redo_two.data["files"] == [{"path": "Budget.xlsx", "reason": "changed", "by": by[one.id]}]
        [redo_one] = await events_of(api, one.id, EventType.HISTORY_REDO)
        assert redo_one.data["files"] == [{"path": "Notes.md", "reason": "changed", "by": by[two.id]}]
        reports = {r.data["worker_id"]: files_of(r) for r in (await events_of(api, master.id, EventType.WORKER_COMPLETE))[-2:]}
        assert reports == {
            str(two.id): [("Budget.xlsx", "redoing", "changed"), ("Notes.md", "landed", None)],
            str(one.id): [("Notes.md", "redoing", "changed")],
        }
        # Each redoes its change on the other's file, and both threads' changes reach the folder.
        await asyncio.wait_for(woken(api, monkeypatch, two, [
            calling(("terminal", {"command": "printf 'Checked by two\\n' >> Budget.xlsx"})), _final_response("Redone."),
        ]), TURN_S)
        await asyncio.wait_for(woken(api, monkeypatch, one, [
            calling(("terminal", {"command": "printf \"One's notes\\n\" >> Notes.md"})), _final_response("Redone."),
        ]), TURN_S)
        assert entries(folder) == {
            "Budget.xlsx": b"Total,42\nChecked by two\n", "Notes.md": b"Two's notes\nOne's notes\n", "Report.docx": b"PK report v1",
        }
        assert nothing_kept(journal_dir)
    finally:
        await app.close()


async def test_a_place_the_app_set_aside_for_another_folder_at_the_path_reaches_the_threads_report_once_and_stands_on_its_row(
    built_client, agent_disk, apart, journal_dir, api, link_url, tmp_path, monkeypatch,  # noqa: F811
):
    folder = tmp_path / "Reports"
    folder.mkdir()
    (folder / "Report.docx").write_bytes(b"PK report v1")
    device = await register(api)
    journal = journal_dir / "journal.sqlite"
    app = await client(built_client, link_url, device["token"], journal, confirm=folder)
    try:
        [prepared] = await confirmed_on(app)
        project, master, one = await a_thread_on_the_app(api, device, prepared)
        await asyncio.wait_for(woken(api, monkeypatch, one, [
            calling(("write_file", {"path": "Notes.md", "content": "notes\n"})), _final_response("Noted."),
        ]), TURN_S)
    finally:
        await app.close()
    # Another folder at the path, as its user moves the first away and makes a new one; and a thread on it, from an app
    # started again, whose turn's open sets the old folder's place aside, its history and the first thread's copy in it.
    folder.rename(tmp_path / "Reports.old")
    folder.mkdir()
    (folder / "Other.md").write_text("the new folder's\n")
    app = await client(built_client, link_url, device["token"], journal, confirm=folder)
    try:
        [prepared] = await confirmed_on(app)
        _, _, two = await a_thread_on_the_app(api, device, prepared, project, master)
        await asyncio.wait_for(woken(api, monkeypatch, two, [_final_response("Done.")]), TURN_S)
        [aside] = [p.name for p in (journal_dir / "data" / "history").iterdir() if ".was-" in p.name and not p.name.endswith(".json")]
        assert all((journal_dir / "data" / "history" / aside / kept).is_dir() for kept in ("history.git", f"clones/{one.id}"))
        at = datetime.fromtimestamp(int(aside.rsplit("-", 1)[1]) / 1000, UTC)
        # Its report says so, once: the next says nothing of it.
        await asyncio.wait_for(woken(api, monkeypatch, two, [_final_response("Done.")], said="Go on."), TURN_S)
        first, second = [r for r in await events_of(api, master.id, EventType.WORKER_COMPLETE) if r.data["worker_id"] == str(two.id)]
        place = {"kind": "place", "at": f"{at:%Y-%m-%dT%H:%M:%S}Z"}
        assert (first.data["set_aside"], second.data.get("set_aside")) == ({"kept": [place], "snapshots": 0, "gone": []}, None)
        assert (
            "\nSet aside on its computer, as what it kept at its folder's path was another folder's history or could not be read: "
            f"the earlier history at its folder's path, with its threads' copies as they were at {at:%Y-%m-%d %H:%M} UTC\n"
        ) in said(first)
        assert "set aside" not in said(second).lower()
        # Its first copy was said to be made while it was, and only then: the open that named the place says it has one.
        waits = await events_of(api, two.id, EventType.DEVICE_WAITING, EventType.DEVICE_RESUMED)
        assert [(e.type, e.data.get("reason")) for e in waits] == [(EventType.DEVICE_WAITING.value, "copying"), (EventType.DEVICE_RESUMED.value, None)]
        # Its row says what is kept, and the first thread's says nothing of it.
        found = {row["id"]: row["set_aside"] for row in await rows(api, project)}
        assert (found[str(two.id)], found[str(one.id)]) == ({"kept": [place], "snapshots": 0, "gone": []}, None)
    finally:
        await app.close()
