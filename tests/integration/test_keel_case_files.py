"""Keel lists, reads and writes a case's files through ``keel-link``, and opens them in the editor for a person.

A case's files are the workspace every step session of the case shares: the
``case:<case id>`` boundary's.  Keel reaches it with no session, and a file
one side writes is the file the other reads.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

import pytest
import pytest_asyncio
from botocore.exceptions import ClientError
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import text

from surogates.config import OfficeSettings
from surogates.keel.case_files import CASE_ORG_OBJECT, case_key, case_workspace
from surogates.keel.files import case_file_value
from surogates.keel.versions import case_versions
from surogates.office import sessions as office_sessions
from surogates.office.agent import location_of
from surogates.office.sessions import open_or_reuse, record_editors
from surogates.office.tokens import read_url_token, sign
from surogates.session.attachment_ingest import workspace_root_id
from surogates.storage.backend import S3Backend
from surogates.storage.tenant import boundary_workspace_key

from .conftest import create_org, create_user
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
    queue_score,
    user_messages,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

CASE = contract_of()["case_id"]

JWT_SECRET = "j" * 32
TOKEN_SECRET = "t" * 32
OFFICE = OfficeSettings(
    public_url="https://office.example",
    internal_url="http://docs.internal",
    callback_base_url="https://api.example",
    jwt_secret=JWT_SECRET,
    token_secret=TOKEN_SECRET,
)


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


async def written_by_the_session(link_app, session, path: str, data: bytes) -> None:
    """*data* at *path* in the case's workspace, as the step session's sandbox writes it."""
    key = boundary_workspace_key(session.config, session, workspace_root_id(session), path)
    await link_app.state.storage.write(session.config["storage_bucket"], key, data)


async def open_editor(
    client, org_id, user_id, path: str = "pack.docx", access: str = "edit", case_id=CASE, *,
    theme: str = "light", auth: str | None = LINK_KEY, mentionable: list[dict] | None = None,
):
    request = {"org_id": str(org_id), "path": path, "user_id": str(user_id), "access": access, "theme": theme}
    if mentionable is not None:
        request["mentionable"] = mentionable
    return await client.post(f"/v1/keel/cases/{case_id}/editor", json=request, headers=link_headers(auth))


@pytest.fixture
def office_on(link_app):
    """A document server for keel-link to open documents in."""
    link_app.state.settings.office = OFFICE


@pytest_asyncio.fixture(loop_scope="session")
async def document_server_calls(link_app):
    """The runtime API on keel-link's state: where the document server sends its callbacks."""
    from surogates.api.app import create_app

    application = create_app()
    for name in ("session_factory", "redis", "session_store", "settings", "storage"):
        setattr(application.state, name, getattr(link_app.state, name))
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://api") as client:
        yield client


async def person(session_factory, org_id, name: str | None = None) -> tuple[UUID, str]:
    """A user of *org_id*, named *name* when given, and the display name they are shown under."""
    user_id = await create_user(session_factory, org_id)
    if name is None:
        return user_id, f"Test User {user_id}"
    async with session_factory() as db, db.begin():
        await db.execute(text("UPDATE users SET display_name = :name WHERE id = :id"), {"name": name, "id": user_id})
    return user_id, name


def permissions(config) -> dict[str, bool]:
    return {name: config["document"]["permissions"][name] for name in ("edit", "review", "comment")}


def listed_paths(answer) -> list[str]:
    return [entry["path"] for entry in answer.json()["files"]]


def refusals(*answers) -> list[tuple[int, str]]:
    return [(answer.status_code, code_of(answer)) for answer in answers]


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


async def test_every_case_file_route_needs_the_link_key(link, link_app, org, session_factory, office_on):
    assert (await upload(link, "memos/credit.docx", b"memo")).status_code == 200
    user_id, _ = await person(session_factory, org)

    for key in (None, "not-the-link-key"):
        answers = [
            await listing(link, auth=key),
            await download(link, "memos/credit.docx", auth=key),
            await upload(link, "memos/credit.docx", b"over it", auth=key),
            await open_editor(link, org, user_id, "memos/credit.docx", auth=key),
        ]
        assert [(a.status_code, code_of(a)) for a in answers] == [(401, "bad-link-key")] * 4

    assert (await download(link, "memos/credit.docx")).content == b"memo"
    location = case_workspace(link_app.state.storage, link_app.state.settings, UUID(CASE)).location("memos/credit.docx")
    assert await office_sessions.open_at(session_factory, location) is None


async def test_the_platforms_own_folders_are_not_a_cases_files(link, link_app, org, session_store):
    session = await step_session(link, org, session_store)
    for path in ("_history/memos/credit.docx", "_artifacts/a1/meta.json", ".threads/t1/README.md", "_whiteboard/canvas.json"):
        await written_by_the_session(link_app, session, path, b"the platform's")
    await written_by_the_session(link_app, session, "memos/credit.docx", b"memo")

    assert listed_paths(await listing(link)) == ["memos/credit.docx"]
    for path in ("_history/memos/credit.docx", "_artifacts/a1/meta.json", ".threads/t1/README.md", "./_whiteboard/canvas.json"):
        assert refusals(await download(link, path), await upload(link, path, b"over it")) == [(422, "invalid-path")] * 2, path
    assert await link_app.state.storage.read(
        session.config["storage_bucket"],
        boundary_workspace_key(session.config, session, workspace_root_id(session), "_history/memos/credit.docx"),
    ) == b"the platform's"


async def test_the_case_files_list_leaves_out_hidden_paths(link, link_app, org, session_store):
    # A step session stages its skills under .skills/ in the case's workspace.
    session = await step_session(link, org, session_store)
    await written_by_the_session(link_app, session, ".skills/credit/SKILL.md", b"a skill")
    await written_by_the_session(link_app, session, "docs/offer.docx", b"offer")

    listed = await listing(link)

    assert listed.status_code == 200, listed.text
    assert listed_paths(listed) == ["docs/offer.docx"]


async def test_a_hidden_path_is_refused(link, link_app, org, session_store):
    session = await step_session(link, org, session_store)
    await written_by_the_session(link_app, session, ".skills/credit/SKILL.md", b"a skill")

    for path in (".skills/credit/SKILL.md", "docs/.cache/x"):
        assert refusals(await download(link, path), await upload(link, path, b"over it")) == [(422, "invalid-path")] * 2, path

    def key(path: str) -> str:
        return boundary_workspace_key(session.config, session, workspace_root_id(session), path)

    assert await link_app.state.storage.read(session.config["storage_bucket"], key(".skills/credit/SKILL.md")) == b"a skill"
    with pytest.raises(KeyError):
        await link_app.state.storage.read(session.config["storage_bucket"], key("docs/.cache/x"))


async def test_a_path_a_steps_output_reads_as_another_file_is_refused(link, link_app, org, session_store):
    # A step's output takes a leading root/ or workspace/ off the path it names.
    session = await step_session(link, org, session_store)
    await written_by_the_session(link_app, session, "root/payslip.pdf", b"payslip")

    for path in ("root/payslip.pdf", "workspace/payslip.pdf"):
        assert refusals(await download(link, path), await upload(link, path, b"x")) == [(422, "invalid-path")] * 2, path
    assert listed_paths(await listing(link)) == []


async def test_encoded_backslashed_and_overlong_paths_are_refused(link):
    for query in ("%2e%2e%2fx.docx", "%2E%2E/x.docx", "memos/%2e%2e/%2e%2e/x.docx", "memo%00.docx"):
        answers = [
            await link.get(f"{content_route(CASE)}?path={query}", headers=link_headers(LINK_KEY)),
            await link.put(f"{content_route(CASE)}?path={query}", content=b"x", headers=link_headers(LINK_KEY)),
        ]
        assert refusals(*answers) == [(422, "invalid-path")] * 2, query
    # Longer than the 1024 bytes an S3 store takes as a key, in folders a disk would take.
    overlong = "/".join(["a" * 200] * 6)
    for path in ("..\\x.docx", "memos\\credit.docx", overlong):
        assert refusals(await download(link, path), await upload(link, path, b"x")) == [(422, "invalid-path")] * 2, path
    assert (await listing(link)).json() == {"files": []}


async def test_a_file_over_the_limit_is_not_read(link, link_app):
    assert (await upload(link, "big.pdf", b"123456789")).status_code == 200
    link_app.state.settings.keel.max_file_bytes = 8

    refused = await download(link, "big.pdf")

    assert (refused.status_code, code_of(refused)) == (413, "too-large")


class _RefusingStore:
    """A store that refuses every call, as one does once its key has lost its rights."""

    def __getattr__(self, operation: str):
        async def refuse(*args, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, operation)

        return refuse


async def test_a_store_that_refuses_is_unavailable_and_logs_no_path(link, link_app, caplog):
    link_app.state.storage = _RefusingStore()

    with caplog.at_level(logging.WARNING, logger="surogates.api.routes.keel"):
        answers = [
            await listing(link),
            await download(link, "memos/credit.docx"),
            await upload(link, "memos/credit.docx", b"memo"),
        ]

    assert refusals(*answers) == [(503, "unavailable")] * 3
    logged = [record.getMessage() for record in caplog.records if record.name == "surogates.api.routes.keel"]
    assert len(logged) == 3 and all(CASE in line and "ClientError" in line for line in logged)
    assert not any("credit" in line for line in logged)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def kept_keys(link_app, versions) -> list[str]:
    return sorted(await link_app.state.storage.list_keys(versions.bucket, versions.prefix))


async def test_a_file_keel_writes_is_kept_by_its_hash(link, link_app):
    versions = case_versions(link_app.state.settings, UUID(CASE))
    storage = link_app.state.storage
    first, second = b"first version", b"second version"

    assert (await upload(link, "in/a.pdf", first)).status_code == 200
    assert await storage.read(versions.bucket, versions.key(sha(first))) == first

    # The same bytes at another path are the same version.
    assert (await upload(link, "in/b.pdf", first)).status_code == 200
    assert await kept_keys(link_app, versions) == [versions.key(sha(first))]

    # A file written over is gone from the workspace, and kept.
    assert (await upload(link, "in/a.pdf", second)).status_code == 200
    assert await kept_keys(link_app, versions) == sorted([versions.key(sha(first)), versions.key(sha(second))])
    assert await storage.read(versions.bucket, versions.key(sha(first))) == first

    # Never listed as a case file, and never in the workspace the sandbox mounts.
    assert sorted(listed_paths(await listing(link))) == ["in/a.pdf", "in/b.pdf"]
    assert not any(key.startswith(case_key(link_app.state.settings, UUID(CASE), "")) for key in await kept_keys(link_app, versions))


async def test_a_file_keel_reads_is_kept_by_its_hash(link, link_app, org, session_store):
    session = await step_session(link, org, session_store)
    versions = case_versions(link_app.state.settings, UUID(CASE))
    data = b"written by the session"
    await written_by_the_session(link_app, session, "memos/credit.docx", data)

    read = await download(link, "memos/credit.docx")

    assert read.status_code == 200, read.text
    digest = read.headers["x-content-sha256"]
    assert digest == sha(data)
    assert await link_app.state.storage.read(versions.bucket, versions.key(digest)) == data

    again = await download(link, "memos/credit.docx")

    assert again.headers["x-content-sha256"] == digest
    assert await kept_keys(link_app, versions) == [versions.key(digest)]


class _RefusesVersions:
    """A store that takes the workspace's files and refuses to keep a version of one."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def write(self, bucket: str, key: str, data: bytes) -> None:
        self._refuse(key)
        await self._inner.write(bucket, key, data)

    async def upload(self, bucket: str, key: str, *args, **kwargs) -> None:
        self._refuse(key)
        await self._inner.upload(bucket, key, *args, **kwargs)

    @staticmethod
    def _refuse(key: str) -> None:
        if "/versions/" in key:
            raise ClientError({"Error": {"Code": "SlowDown", "Message": "slow down secret-key"}}, "PutObject")


async def test_a_hash_is_never_handed_out_unkept(link, link_app, org, session_store, caplog):
    session = await step_session(link, org, session_store)
    working = link_app.state.storage
    await written_by_the_session(link_app, session, "memos/d.pdf", b"written by the session")
    link_app.state.storage = _RefusesVersions(working)

    with caplog.at_level(logging.INFO, logger="surogates.api.routes.keel"):
        written = await upload(link, "in/c.pdf", b"offered to Keel")
        read = await download(link, "memos/d.pdf")

    assert refusals(written, read) == [(503, "unavailable")] * 2
    assert "x-content-sha256" not in read.headers
    logged = [record.getMessage() for record in caplog.records if record.name == "surogates.api.routes.keel"]
    assert logged and all(CASE in line and "ClientError" in line for line in logged)
    assert not any("c.pdf" in line or "d.pdf" in line or "secret-key" in line for line in logged)

    # The refused write left no file Keel could read as written.
    link_app.state.storage = working
    assert refusals(await download(link, "in/c.pdf")) == [(404, "unknown-file")]


class _RefusingS3Client:
    """An S3 client whose every request the store refuses, as it does once the key has lost its rights."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def __getattr__(self, operation: str):
        async def refuse(**kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
                operation,
            )

        return refuse


async def test_an_s3_store_that_refuses_the_editors_look_is_unavailable(
    link, link_app, org, session_factory, office_on, monkeypatch, caplog,
):
    # The S3 backend itself, over a client the store refuses: a refused look is no missing file.
    storage = S3Backend("https://s3.example")
    monkeypatch.setattr(storage._session, "client", lambda service_name, **kwargs: _RefusingS3Client())
    link_app.state.storage = storage
    user_id, _ = await person(session_factory, org)

    with caplog.at_level(logging.WARNING, logger="surogates.api.routes.keel"):
        refused = await open_editor(link, org, user_id, "memos/credit.docx")

    assert refusals(refused) == [(503, "unavailable")]
    logged = [record.getMessage() for record in caplog.records if record.name == "surogates.api.routes.keel"]
    assert len(logged) == 1 and CASE in logged[0] and "ClientError" in logged[0]
    assert "credit" not in logged[0]
    location = case_workspace(storage, link_app.state.settings, UUID(CASE)).location("memos/credit.docx")
    assert await office_sessions.open_at(session_factory, location) is None


# ---------------------------------------------------------------------------
# A case's document in the editor
# ---------------------------------------------------------------------------


async def test_a_person_opens_a_case_document_with_the_access_keel_gives(link, link_app, org, session_factory, office_on):
    assert (await upload(link, "pack.docx", b"PK-pack")).status_code == 200
    commenter, commenter_name = await person(session_factory, org)
    reader, reader_name = await person(session_factory, org)

    opened = await open_editor(link, org, commenter, access="comment")

    assert opened.status_code == 200, opened.text
    answer = opened.json()
    # Keel named no agent to mention: the editor offers nobody.
    assert set(answer) == {"document_server_url", "config", "mentionable_users"}
    assert answer["mentionable_users"] == []
    assert answer["document_server_url"] == "https://office.example"
    config = answer["config"]
    assert config["editorConfig"]["user"] == {"id": f"user:{commenter}", "name": commenter_name}
    assert config["editorConfig"]["mode"] == "edit"
    assert permissions(config) == {"edit": False, "review": False, "comment": True}
    assert config["editorConfig"]["customization"]["uiTheme"] == "theme-light"
    assert jwt.decode(config["token"], JWT_SECRET, algorithms=["HS256"])["document"]["key"] == config["document"]["key"]
    location = case_workspace(link_app.state.storage, link_app.state.settings, UUID(CASE)).location("pack.docx")
    document = await office_sessions.open_at(session_factory, location)
    assert document is not None
    assert (document.id, document.case_id, document.session_ids) == (
        read_url_token(TOKEN_SECRET, config["document"]["url"].rsplit("/", 1)[1], "file"), UUID(CASE), (),
    )

    viewed = await open_editor(link, org, reader, access="view", theme="dark")

    assert viewed.status_code == 200, viewed.text
    config = viewed.json()["config"]
    assert config["editorConfig"]["user"] == {"id": f"user:{reader}", "name": reader_name}
    assert config["editorConfig"]["mode"] == "view"
    assert permissions(config) == {"edit": False, "review": False, "comment": False}
    assert config["editorConfig"]["customization"]["uiTheme"] == "theme-dark"


async def test_two_people_share_the_document_and_the_listing_names_who_is_in_it(
    link, link_app, org, session_factory, office_on, document_server_calls,
):
    assert (await upload(link, "pack.docx", b"PK-pack")).status_code == 200
    editor, editor_name = await person(session_factory, org, "Zoe Ionescu")
    commenter, commenter_name = await person(session_factory, org, "Ana Pop")

    editing = await open_editor(link, org, editor, access="edit")
    commenting = await open_editor(link, org, commenter, access="comment")

    assert (editing.status_code, commenting.status_code) == (200, 200), (editing.text, commenting.text)
    edit_config, comment_config = editing.json()["config"], commenting.json()["config"]
    assert edit_config["editorConfig"]["mode"] == "edit"
    assert permissions(edit_config) == {"edit": True, "review": True, "comment": True}
    key = edit_config["document"]["key"]
    assert comment_config["document"]["key"] == key
    [pack] = (await listing(link)).json()["files"]
    assert pack["editing"] == []

    # The document server reports who joined, at the callback the config gave it, in the order they joined.
    callback = urlsplit(edit_config["editorConfig"]["callbackUrl"]).path

    async def joined(*users: str) -> None:
        report = {"key": key, "status": 1, "users": list(users)}
        reported = await document_server_calls.post(callback, json={"token": sign(JWT_SECRET, report)})
        assert reported.json() == {"error": 0}, reported.text

    await joined(f"user:{editor}", f"user:{commenter}")

    [pack] = (await listing(link)).json()["files"]
    assert [editor_name, commenter_name] != sorted([editor_name, commenter_name])
    assert pack["editing"] == sorted([editor_name, commenter_name])

    # Editors the runtime has no name for: a person, and an agent.
    await joined(f"user:{editor}", f"user:{uuid.uuid4()}", "agent:intake-agent", f"user:{commenter}")

    [pack] = (await listing(link)).json()["files"]
    assert pack["editing"] == ["An agent", "Ana Pop", "Someone", "Zoe Ionescu"]


async def test_a_document_the_editor_cannot_edit_opens_read_only(link, org, session_factory, office_on):
    # Older formats are converted to be viewed: whatever Keel allows, nobody edits them.
    assert (await upload(link, "letters/old.doc", b"legacy")).status_code == 200
    user_id, _ = await person(session_factory, org)

    for access in ("edit", "comment"):
        opened = await open_editor(link, org, user_id, "letters/old.doc", access=access)

        assert opened.status_code == 200, opened.text
        config = opened.json()["config"]
        assert config["editorConfig"]["mode"] == "view", access
        assert permissions(config) == {"edit": False, "review": False, "comment": False}, access


async def test_the_case_editor_refuses_what_it_cannot_open(link, link_app, org, session_factory, office_on):
    assert (await upload(link, "pack.docx", b"PK-pack")).status_code == 200
    assert (await upload(link, "notes.txt", b"notes")).status_code == 200
    user_id, _ = await person(session_factory, org)
    stranger = await create_user(session_factory, await create_org(session_factory))

    elsewhere = await open_editor(link, org, stranger)
    nobody = await open_editor(link, org, uuid.uuid4())
    not_a_document = await open_editor(link, org, user_id, "notes.txt")
    missing = await open_editor(link, org, user_id, "memos/none.docx")
    read_only_chat_form = await open_editor(link, org, user_id, access="read")
    not_a_case = await open_editor(link, org, user_id, case_id="nope")

    assert refusals(elsewhere, nobody, not_a_document, missing, read_only_chat_form, not_a_case) == [
        (404, "unknown-user"),
        (404, "unknown-user"),
        (415, "not-a-document"),
        (404, "unknown-file"),
        (422, "invalid-request"),
        (422, "invalid-request"),
    ]
    for path in ("../x.docx", "/etc/x.docx", "_history/pack.docx", ".skills/pack.docx", "root/pack.docx", "memos\\pack.docx", ""):
        assert refusals(await open_editor(link, org, user_id, path)) == [(422, "invalid-path")], path

    link_app.state.settings.office = OfficeSettings()
    off = await open_editor(link, org, user_id)

    assert refusals(off) == [(503, "office-off")]
    location = case_workspace(link_app.state.storage, link_app.state.settings, UUID(CASE)).location("pack.docx")
    assert await office_sessions.open_at(session_factory, location) is None


async def test_a_document_whose_last_save_is_landing_is_closing(link, org, session_factory, office_on, monkeypatch):
    monkeypatch.setattr(office_sessions, "CLOSING_WAIT_S", 0.2)
    monkeypatch.setattr(office_sessions, "CLOSING_POLL_S", 0.05)
    assert (await upload(link, "pack.docx", b"PK-pack")).status_code == 200
    user_id, _ = await person(session_factory, org)
    first = (await open_editor(link, org, user_id)).json()["config"]
    row_id = read_url_token(TOKEN_SECRET, first["document"]["url"].rsplit("/", 1)[1], "file")
    await office_sessions.set_pending(session_factory, row_id, bucket="b", key="k", close=True)

    closing = await open_editor(link, org, user_id)

    assert refusals(closing) == [(409, "document-closing")]


# ---------------------------------------------------------------------------
# The step's agent to mention, and the case's organisation
# ---------------------------------------------------------------------------

#: The agent Keel names to mention in a case's document, by its own name for it.
LOAN_AGENT = {"id": "loan-agent", "name": "Credit analyst"}


async def test_the_editor_offers_the_agents_keel_names(link, link_app, org, session_factory):
    assert (await upload(link, "pack.docx", b"PK-pack")).status_code == 200
    assert (await upload(link, "letters/old.doc", b"legacy")).status_code == 200
    user_id, _ = await person(session_factory, org)
    link_app.state.settings.office = OFFICE.model_copy(update={"runtime_office_url": "http://runtime-office.test"})

    offered = await open_editor(link, org, user_id, mentionable=[LOAN_AGENT])
    commenting = await open_editor(link, org, user_id, access="comment", mentionable=[LOAN_AGENT])
    unnamed = await open_editor(link, org, user_id)
    converted = await open_editor(link, org, user_id, "letters/old.doc", mentionable=[LOAN_AGENT])
    viewing = await open_editor(link, org, user_id, access="view", mentionable=[LOAN_AGENT])

    assert [a.status_code for a in (offered, commenting, unnamed, converted, viewing)] == [200] * 5
    agent = {"id": "agent:loan-agent", "name": "Credit analyst", "email": "credit-analyst@agent.invalid"}
    assert offered.json()["mentionable_users"] == [agent]
    assert commenting.json()["mentionable_users"] == [agent]
    assert unnamed.json()["mentionable_users"] == []
    # The editor only shows a converted document, and a viewer comments on nothing: neither mentions the agent.
    assert converted.json()["mentionable_users"] == []
    assert viewing.json()["mentionable_users"] == []

    # Without agent editing, no agent answers a comment: none is offered.
    link_app.state.settings.office = OFFICE
    no_agent_editing = await open_editor(link, org, user_id, mentionable=[LOAN_AGENT])

    assert no_agent_editing.status_code == 200, no_agent_editing.text
    assert no_agent_editing.json()["mentionable_users"] == []


async def test_the_editor_refuses_mentionable_agents_out_of_their_shape(link, org, session_factory, office_on):
    assert (await upload(link, "pack.docx", b"PK-pack")).status_code == 200
    user_id, _ = await person(session_factory, org)

    for mentionable in (
        [{"id": "", "name": "Credit analyst"}],
        [{"id": "a" * 201, "name": "Credit analyst"}],
        [{"id": "loan-agent", "name": ""}],
        [{"id": "loan-agent", "name": "n" * 201}],
        [{"id": "loan-agent", "name": "Credit\nanalyst"}],
        [LOAN_AGENT] * 21,
    ):
        answer = await open_editor(link, org, user_id, mentionable=mentionable)
        assert refusals(answer) == [(422, "invalid-request")], mentionable


async def test_a_case_of_another_org_does_not_open(link, org, session_factory, office_on):
    assert (await upload(link, "pack.docx", b"PK-pack")).status_code == 200
    user_id, _ = await person(session_factory, org)
    other = await create_org(session_factory)
    outsider, _ = await person(session_factory, other)

    first = await open_editor(link, org, user_id)
    elsewhere = await open_editor(link, other, outsider)
    again = await open_editor(link, org, user_id)

    assert first.status_code == 200, first.text
    # Told apart from no file at all by nothing.
    assert refusals(elsewhere) == [(404, "unknown-file")]
    assert elsewhere.json() == (await open_editor(link, other, outsider, "memos/none.docx")).json()
    assert again.status_code == 200, again.text


async def test_a_step_session_records_its_cases_org(link, org, session_factory, session_store, office_on):
    assert (await upload(link, "pack.docx", b"PK-pack")).status_code == 200
    user_id, _ = await person(session_factory, org)
    other = await create_org(session_factory)
    outsider, _ = await person(session_factory, other)
    await step_session(link, org, session_store)

    elsewhere = await open_editor(link, other, outsider)

    assert refusals(elsewhere) == [(404, "unknown-file")]
    assert (await open_editor(link, org, user_id)).status_code == 200


class _RefusingTheOrgRecord:
    """The test's store, refusing every read of the case's org record, as a store does once a key has lost its rights there."""

    def __init__(self, storage) -> None:
        self._storage = storage

    def __getattr__(self, operation: str):
        return getattr(self._storage, operation)

    async def read(self, bucket: str, key: str) -> bytes:
        if key.endswith(f"{CASE}/{CASE_ORG_OBJECT}"):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": f"Access Denied: {key}"}}, "GetObject")
        return await self._storage.read(bucket, key)


async def test_an_unreadable_org_record_is_unavailable(link, link_app, org, session_factory, office_on, caplog):
    assert (await upload(link, "pack.docx", b"PK-pack")).status_code == 200
    user_id, _ = await person(session_factory, org)
    link_app.state.storage = _RefusingTheOrgRecord(link_app.state.storage)
    caplog.clear()

    with caplog.at_level(logging.DEBUG):
        refused = await open_editor(link, org, user_id)

    assert refusals(refused) == [(503, "unavailable")]
    logged = [record.getMessage() for record in caplog.records if record.name == "surogates.api.routes.keel"]
    assert len(logged) == 1 and CASE in logged[0] and "ClientError" in logged[0]
    assert str(org) not in caplog.text
    assert CASE_ORG_OBJECT not in caplog.text and "pack" not in caplog.text
    location = case_workspace(link_app.state.storage, link_app.state.settings, UUID(CASE)).location("pack.docx")
    assert await office_sessions.open_at(session_factory, location) is None


async def test_a_step_session_starts_when_the_org_cannot_be_recorded(
    link, link_app, org, session_store, redis_client, caplog,
):
    link_app.state.storage = _RefusingTheOrgRecord(link_app.state.storage)

    with caplog.at_level(logging.DEBUG):
        session = await step_session(link, org, session_store)

    # Started as any step: its kickoff written, and queued.
    assert len(await user_messages(session_store, session.id)) == 1
    assert await queue_score(redis_client, session) is not None
    logged = [record.getMessage() for record in caplog.records if record.name == "surogates.api.routes.keel"]
    assert len(logged) == 1 and CASE in logged[0] and "ClientError" in logged[0]
    assert str(org) not in caplog.text and CASE_ORG_OBJECT not in caplog.text


async def test_the_case_files_never_list_the_org_record(link, org, session_factory, office_on):
    assert (await upload(link, "pack.docx", b"PK-pack")).status_code == 200
    user_id, _ = await person(session_factory, org)

    assert (await open_editor(link, org, user_id)).status_code == 200

    assert listed_paths(await listing(link)) == ["pack.docx"]
    assert refusals(await download(link, CASE_ORG_OBJECT)) == [(404, "unknown-file")]
