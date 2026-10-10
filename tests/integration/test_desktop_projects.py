"""The web client's projects source against the agent's real project routes, called as Surogate
Desktop calls it: every answer through the shell's own checks, which refuse a whole answer for
one field they cannot use."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from surogates.sandbox.pool import SandboxPool
from surogates.scheduled.schedule import parse_schedule
from surogates.scheduled.store import ScheduledSessionStore

from .test_desktop_link_client import built_client  # noqa: F401  (a fixture)
from .test_devices import api, link_url  # noqa: F401  (fixtures)
from .test_durable_landings import edited, ends, stored
from .test_file_history import copies  # noqa: F401  (a fixture: the api's copies in the test's own folder)
from .test_local_threads import answered_by_the_journal
from .test_thread_copies import a_thread
from .test_workstream_threads import threads_in_every_state, turn_ends
from .test_workstreams import create, master_of

pytestmark = [pytest.mark.desktop, pytest.mark.asyncio(loop_scope="session")]

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts" / "desktop-projects-check.mjs"


async def checked(api, link_url: str, project: dict, *more: str) -> dict:
    """What the shell took of each answer the page's source gave it for *project*, through its own checks."""
    origin = link_url.split("/api/")[0].replace("ws://", "http://", 1)
    check = await asyncio.create_subprocess_exec(
        "node", "--experimental-strip-types", str(CHECK), "--origin", origin, "--token", api.token, "--project", project["id"],
        *more, cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(check.communicate(), 60)
    assert check.returncode == 0, err.decode()
    return json.loads(out)


async def test_the_desktop_takes_every_answer_of_a_real_project(built_client, api, link_url, session_factory):
    project = await create(api, goal="The board's Q3 report")
    master = await master_of(api, project)
    # A thread on the user's computer, which is not connected, and a file it made there:
    # the first, so that the rows' first idle thread is still the one the check reads.
    local = await answered_by_the_journal(api, project, master)
    await turn_ends(api, local, files=["Totals.md"])
    made = await threads_in_every_state(api, master)
    # A file the user added, and a routine the master made.
    uploaded = await api.client.post(
        f"/v1/sessions/{master.id}/workspace/upload", files={"file": ("brief.pdf", b"%PDF-1.7 brief")}, headers=api.auth(),
    )
    assert uploaded.status_code == 201, uploaded.text
    await ScheduledSessionStore(session_factory).create(
        org_id=master.org_id, user_id=master.user_id, agent_id=master.agent_id, name="Weekly cash report",
        prompt="Report the cash.", schedule=parse_schedule("0 8 * * 1"), source="cron", created_from_session_id=master.id,
    )
    seen = await checked(api, link_url, project)

    assert [(listed["id"], listed["waiting"], listed["working"]) for listed in seen["listed"]] == [(project["id"], 4, 2)]
    computer = {"kind": "device", "deviceId": local.config["execution"]["device_id"], "deviceName": "Flavius's ThinkPad", "online": False}
    assert [row["place"] for row in seen["threads"] if row["id"] == str(local.id)] == [computer]
    assert (seen["opened"]["masterSessionId"], seen["opened"]["goal"]) == (project["master_session_id"], "The board's Q3 report")
    assert {row["title"]: (row["id"], row["group"], row["reason"]) for row in seen["threads"]} == {
        title: (str(made[title].id), *state) for title, state in {
            "Check the revenue figures": ("waiting", "question"),
            "Send the draft to finance": ("waiting", "approval"),
            "Pick the year": ("waiting", "question"),
            "Convert the old reports": ("waiting", "failed"),
            "Draft the summary": ("working", None),
            "Tidy the shared folder": ("working", "computer"),
            "Collect the sales data": ("idle", None),
            "Book the room": ("resolved", None),
            "Book the review meeting": ("resolved", None),
        }.items()
    } | {"Check the totals": (str(local.id), "idle", None)}
    idle = str(made["Collect the sales data"].id)
    assert [row["id"] for row in seen["one"]] == [idle]
    assert (seen["resolved"]["id"], seen["resolved"]["group"]) == (idle, "resolved")
    assert (seen["reopened"]["id"], seen["reopened"]["group"]) == (idle, "idle")
    assert (seen["renamed"]["name"], seen["renamed"]["threadTier"]) == ("Q3 report", "pro")
    assert sorted((entry["path"], entry["origin"], entry["size"], entry["place"]) for entry in seen["library"]) == [
        ("Totals.md", "produced", None, computer), ("brief.pdf", "added", 14, {"kind": "cloud"}),
    ]
    assert [(routine["name"], routine["scheduleDisplay"]) for routine in seen["routines"]] == [("Weekly cash report", "0 8 * * 1")]
    # The stream says it is ready: the shell reads the project whole.
    assert seen["heard"] == [None]


async def test_the_desktop_takes_every_answer_of_a_files_history(built_client, api, link_url, tmp_path):
    project = await create(api)
    thread = await a_thread(api, "Draft A", await master_of(api, project))
    pods = stored(api, thread, tmp_path)
    pool = SandboxPool(pods)
    for command in ("echo one >> Report.docx", "echo two >> Report.docx"):
        await edited(pool, thread, command)
        await ends(api, pool, thread)
    seen = (await checked(api, link_url, project, "--path", "Report.docx"))["history"]
    # Newest first, and last the file as you uploaded it; each as the shell's checks left it.
    assert [(v["path"], v["by"], v["change"], v["merged"], v["available"]) for v in seen["versions"]] == [
        ("Report.docx", {"kind": "thread", "threadId": str(thread.id), "title": "Draft A"}, "changed", True, True),
        ("Report.docx", {"kind": "thread", "threadId": str(thread.id), "title": "Draft A"}, "changed", True, True),
        ("Report.docx", {"kind": "you"}, "added", True, True),
    ]
    assert [v["landingId"] is not None for v in seen["versions"]] == [True, True, False]
    assert seen["none"] == []
