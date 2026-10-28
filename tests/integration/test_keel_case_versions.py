"""Keel reads, checks and deletes the versions of a case's files through ``keel-link``.

A version is a file as it was when its digest was handed to Keel, kept by its
hash beside the case's workspace.  A version can be read by its hash, Keel
asks which of the hashes it holds are gone, and drops them all once the
case's retention ends.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from botocore.exceptions import ClientError

from surogates.keel.files import case_file_value
from surogates.keel.versions import MAX_ASKED

from .test_keel_case_files import (  # noqa: F401  (link, link_app and org are fixtures)
    CASE,
    LINK_KEY,
    download,
    link,
    link_app,
    link_headers,
    listed_paths,
    listing,
    org,
    refusals,
    step_session,
    upload,
    written_by_the_session,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

OTHER_CASE = str(uuid.uuid4())


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def version_route(case_id, digest: str) -> str:
    return f"/v1/keel/cases/{case_id}/versions/{digest}"


def missing_route(case_id) -> str:
    return f"/v1/keel/cases/{case_id}/versions/missing"


def versions_route(case_id) -> str:
    return f"/v1/keel/cases/{case_id}/versions"


async def version(client, digest: str, case_id=CASE, *, auth: str | None = LINK_KEY):
    return await client.get(version_route(case_id, digest), headers=link_headers(auth))


async def missing(client, hashes, case_id=CASE, *, auth: str | None = LINK_KEY):
    return await client.post(missing_route(case_id), json={"sha256": hashes}, headers=link_headers(auth))


async def drop(client, case_id=CASE, *, auth: str | None = LINK_KEY):
    return await client.delete(versions_route(case_id), headers=link_headers(auth))


async def test_keel_reads_a_version_by_its_hash(link, link_app, org, session_store):
    older, newer = b"the first draft", b"the second draft, longer"
    assert (await upload(link, "in/a.pdf", older)).status_code == 200
    assert (await upload(link, "in/a.pdf", newer)).status_code == 200

    read = await version(link, sha(older))

    assert read.status_code == 200, read.text
    assert read.content == older
    assert read.headers["x-content-sha256"] == sha(older)
    assert read.headers["content-length"] == str(len(older))
    assert read.headers["content-type"] == "application/octet-stream"
    # The file at the path is the newer one: the older bytes are only kept.
    assert (await download(link, "in/a.pdf")).content == newer

    # An output file is kept the moment its value is made.
    session = await step_session(link, org, session_store)
    output = b"%PDF-1.7 the step's output"
    await written_by_the_session(link_app, session, "out/o.pdf", output)
    value = await case_file_value(link_app.state.storage, session, CASE, "out/o.pdf")
    assert value["sha256"] == sha(output)

    kept = await version(link, sha(output))

    assert kept.status_code == 200, kept.text
    assert kept.content == output


async def test_keel_learns_which_versions_are_gone(link, link_app):
    first, second = b"kept one", b"kept two"
    for data in (first, second):
        assert (await upload(link, f"in/{sha(data)[:8]}.pdf", data)).status_code == 200
    gone, also_gone = sha(b"never kept"), sha(b"nor this")

    answer = await missing(link, [gone, sha(first), gone, also_gone, sha(second)])

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"missing": [gone, also_gone]}

    # A case nothing was ever written to has no versions at all, and no bucket may be there yet.
    nothing = await missing(link, [sha(first), sha(second)], OTHER_CASE)

    assert nothing.json() == {"missing": [sha(first), sha(second)]}


async def test_deleting_a_cases_versions_leaves_its_files_and_other_cases(link, link_app):
    one, two, other = b"case one", b"case two", b"another case"
    assert (await upload(link, "in/one.pdf", one)).status_code == 200
    assert (await upload(link, "in/two.pdf", two)).status_code == 200
    assert (await upload(link, "in/other.pdf", other, OTHER_CASE)).status_code == 200
    files = listed_paths(await listing(link))

    dropped = await drop(link)

    assert (dropped.status_code, dropped.json()) == (200, {"deleted": 2})
    gone = await version(link, sha(one))
    assert refusals(gone) == [(404, "unknown-version")]
    assert listed_paths(await listing(link)) == files
    assert (await version(link, sha(other), OTHER_CASE)).content == other

    assert (await drop(link)).json() == {"deleted": 0}
    assert (await drop(link, str(uuid.uuid4()))).json() == {"deleted": 0}
    # The files stay readable; a read keeps the version of what it reads again.
    assert (await download(link, "in/one.pdf")).content == one


class _FailingStore:
    """A store that answers as it should, except for the calls that read or drop versions."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        if name in {"stat", "download", "list_entries", "list_keys", "delete_prefix"}:
            async def refuse(*args, **kwargs):
                raise ClientError({"Error": {"Code": "SlowDown", "Message": "slow down"}}, name)

            return refuse
        return getattr(self._inner, name)


@pytest.mark.parametrize(("method", "route", "body"), [
    ("get", version_route(CASE, "a" * 64), None),
    ("post", missing_route(CASE), {"sha256": ["a" * 64]}),
    ("delete", versions_route(CASE), None),
], ids=["read", "missing", "delete"])
async def test_keel_link_refuses_a_version_without_its_key(link, method, route, body):
    answered = await link.request(method, route, json=body, headers=link_headers(None))

    assert answered.status_code == 401


@pytest.mark.parametrize("hash_", ["A" * 64, "a" * 63, "g" * 64], ids=["upper-case", "short", "not-hex"])
async def test_keel_link_refuses_a_hash_that_is_not_a_sha256(link, hash_):
    assert refusals(await version(link, hash_), await missing(link, [hash_])) == [(422, "invalid-request")] * 2


async def test_keel_link_refuses_the_asks_it_cannot_read(link):
    answers = [
        await link.post(missing_route(CASE), json={}, headers=link_headers(LINK_KEY)),
        await missing(link, []),
        await missing(link, ["a" * 64] * (MAX_ASKED + 1)),
        await missing(link, ["a" * 64, "b" * 63]),
        await version(link, "a" * 64, "not-a-uuid"),
        await missing(link, ["a" * 64], "not-a-uuid"),
        await drop(link, "not-a-uuid"),
    ]

    assert refusals(*answers) == [(422, "invalid-request")] * 7
    assert refusals(await version(link, "a" * 64)) == [(404, "unknown-version")]
    assert (await missing(link, ["a" * 64] * MAX_ASKED)).json() == {"missing": ["a" * 64]}


async def test_keel_link_does_not_read_a_version_over_the_limit(link, link_app):
    data = b"12345"
    assert (await upload(link, "in/a.pdf", data)).status_code == 200
    link_app.state.settings.keel.max_file_bytes = 4

    assert refusals(await version(link, sha(data))) == [(413, "too-large")]


async def test_a_store_that_refuses_makes_the_versions_unavailable(link, link_app):
    data = b"kept"
    assert (await upload(link, "in/a.pdf", data)).status_code == 200
    link_app.state.storage = _FailingStore(link_app.state.storage)

    answers = [await version(link, sha(data)), await missing(link, [sha(data)]), await drop(link)]

    assert refusals(*answers) == [(503, "unavailable")] * 3

