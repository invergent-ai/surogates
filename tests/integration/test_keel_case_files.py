"""Keel lists, reads and writes a case's files through ``keel-link``.

A case's files are the workspace every step session of the case shares: the
``case:<case id>`` boundary's.  Keel reaches it with no session, and a file
one side writes is the file the other reads.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from uuid import UUID

import pytest

from surogates.keel.files import case_file_value
from surogates.office.agent import location_of
from surogates.office.sessions import open_or_reuse, record_editors
from surogates.session.attachment_ingest import workspace_root_id
from surogates.storage.tenant import boundary_workspace_key

from .test_keel_step_sessions import (  # noqa: F401  (link, link_app and org are fixtures)
    LINK_KEY,
    body,
    code_of,
    contract_of,
    link,
    link_app,
    link_headers,
    org,
    post,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

CASE = contract_of()["case_id"]


def files_route(case_id) -> str:
    return f"/v1/keel/cases/{case_id}/files"


def content_route(case_id) -> str:
    return f"{files_route(case_id)}/content"


async def listing(client, case_id=CASE, *, auth: str | None = LINK_KEY):
    return await client.get(files_route(case_id), headers=link_headers(auth))


async def download(client, path: str, case_id=CASE, *, auth: str | None = LINK_KEY):
    return await client.get(content_route(case_id), params={"path": path}, headers=link_headers(auth))


async def upload(client, path: str, data: bytes, case_id=CASE, *, auth: str | None = LINK_KEY):
    return await client.put(content_route(case_id), params={"path": path}, content=data, headers=link_headers(auth))


async def step_session(link, org, session_store):
    """The step session Keel starts for the case."""
    created = await post(link, body(org))
    assert created.status_code == 201, created.text
    return await session_store.get_session(UUID(created.json()["session_id"]))


async def test_a_file_a_step_session_wrote_is_listed_and_read_back(link, link_app, org, session_store):
    session = await step_session(link, org, session_store)
    key = boundary_workspace_key(session.config, session, workspace_root_id(session), "memos/credit.docx")
    await link_app.state.storage.write(session.config["storage_bucket"], key, b"memo")

    listed = await listing(link)

    assert listed.status_code == 200, listed.text
    [memo] = [entry for entry in listed.json()["files"] if entry["path"] == "memos/credit.docx"]
    assert {k: memo[k] for k in ("path", "name", "size", "editing")} == {
        "path": "memos/credit.docx", "name": "credit.docx", "size": 4, "editing": [],
    }
    assert datetime.fromisoformat(memo["modified_at"]).tzinfo is not None

    read = await download(link, "memos/credit.docx")

    assert read.status_code == 200, read.text
    assert read.content == b"memo"
    assert read.headers["content-type"] == "application/octet-stream"
    assert read.headers["x-content-sha256"] == hashlib.sha256(b"memo").hexdigest()


async def test_an_upload_lands_where_the_cases_sessions_read_it(link, link_app, org, session_store):
    session = await step_session(link, org, session_store)
    data = b"%PDF-1.7 payslip"

    put = await upload(link, "uploads/t1/payslip.pdf", data)

    assert put.status_code == 200, put.text
    answer = put.json()
    assert answer == {
        "name": "payslip.pdf",
        "uri": f"case-file://{CASE}/uploads/t1/payslip.pdf",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
    seen = await case_file_value(link_app.state.storage, session, CASE, "uploads/t1/payslip.pdf")
    assert (seen["uri"], seen["sha256"]) == (answer["uri"], answer["sha256"])


async def test_an_upload_replaces_the_file_at_its_path(link):
    assert (await upload(link, "letters/offer.docx", b"first")).status_code == 200

    replaced = await upload(link, "letters/offer.docx", b"second")

    assert replaced.status_code == 200, replaced.text
    assert (await download(link, "letters/offer.docx")).content == b"second"
    [offer] = [entry for entry in (await listing(link)).json()["files"] if entry["path"] == "letters/offer.docx"]
    assert offer["size"] == len(b"second")


async def test_an_upload_over_an_open_document_is_refused(link, link_app, org, session_store):
    session = await step_session(link, org, session_store)
    assert (await upload(link, "memos/credit.docx", b"as written")).status_code == 200
    person = f"user:{uuid.uuid4()}"
    document = await open_or_reuse(
        link_app.state.session_factory,
        location=location_of(session, "memos/credit.docx"),
        path="memos/credit.docx",
        session_id=session.id,
        user=(person, "Ana Pop"),
    )
    await record_editors(link_app.state.session_factory, document.id, [person])

    refused = await upload(link, "memos/credit.docx", b"over it")

    assert refused.status_code == 409, refused.text
    assert code_of(refused) == "file-open"
    assert (await download(link, "memos/credit.docx")).content == b"as written"
    [memo] = [entry for entry in (await listing(link)).json()["files"] if entry["path"] == "memos/credit.docx"]
    assert memo["editing"] == ["Ana Pop"]


async def test_paths_outside_the_workspace_are_refused(link):
    for path in ("../x.docx", "/etc/passwd", "memos/../../x.docx", ""):
        answers = [await download(link, path), await upload(link, path, b"x")]
        assert [(a.status_code, code_of(a)) for a in answers] == [(422, "invalid-path")] * 2, path

    answers = [await listing(link, "nope"), await download(link, "a.docx", "nope"), await upload(link, "a.docx", b"x", "nope")]
    assert [(a.status_code, code_of(a)) for a in answers] == [(422, "invalid-request")] * 3

    missing = await download(link, "memos/none.docx")
    assert (missing.status_code, code_of(missing)) == (404, "unknown-file")
    assert (await listing(link)).json() == {"files": []}


async def test_an_upload_over_the_limit_is_too_large(link, link_app):
    link_app.state.settings.keel.max_file_bytes = 8

    refused = await upload(link, "big.pdf", b"123456789")

    assert (refused.status_code, code_of(refused)) == (413, "too-large")
    assert (await listing(link)).json() == {"files": []}
    taken = await upload(link, "big.pdf", b"12345678")
    assert taken.status_code == 200, taken.text
    assert (await download(link, "big.pdf")).content == b"12345678"


async def test_a_case_without_a_workspace_lists_no_files(link):
    answer = await listing(link, uuid.uuid4())

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"files": []}


async def test_every_case_file_route_needs_the_link_key(link):
    assert (await upload(link, "memos/credit.docx", b"memo")).status_code == 200

    for key in (None, "not-the-link-key"):
        answers = [
            await listing(link, auth=key),
            await download(link, "memos/credit.docx", auth=key),
            await upload(link, "memos/credit.docx", b"over it", auth=key),
        ]
        assert [(a.status_code, code_of(a)) for a in answers] == [(401, "bad-link-key")] * 3

    assert (await download(link, "memos/credit.docx")).content == b"memo"
