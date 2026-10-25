"""A comment that mentions a draft step's agent reaches its session through ``keel-link``.

A person working a case @mentions the agent of a draft step in a comment in
one of the case's documents.  Keel sends the mention to the step's session:
the session gets the message a chat's mention becomes, marked as Keel's,
once per key, and wakes, a draft under review too.  The worker then posts
the acknowledgement in the comment's thread, and the agent answers there.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import AsyncClient

import surogates.harness.loop as loop_module
from surogates.config import SHARED_WORK_QUEUE_KEY, OfficeSettings, encode_queue_member
from surogates.keel.case_files import case_workspace, write_case_file
from surogates.office import sessions as office_sessions
from surogates.tools.builtin import office as office_tools
from tests.test_office_mentions import _harness

from .test_keel_step_sessions import (  # noqa: F401  (link, link_app and org are fixtures)
    LINK_KEY,
    cancel,
    code_of,
    completed_step,
    link,
    link_app,
    link_headers,
    org,
    queue_score,
    step_route,
    timeline,
    user_messages,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

DOCUMENT_SERVER = {
    "public_url": "https://office.example",
    "internal_url": "http://docs.internal",
    "callback_base_url": "https://api.example",
    "jwt_secret": "j" * 32,
    "token_secret": "t" * 32,
}
#: A document server with runtime-office beside it: the agent edits live, and answers comments.
AGENT_EDITING = OfficeSettings(
    **DOCUMENT_SERVER, runtime_office_url="http://runtime-office.test", runtime_office_token="r" * 32,
)
AUTHOR = uuid.UUID("3b8f2a52-6c1d-4e0f-9a47-0d5e6c7b8a91")
COMMENT = "@credit-analyst@agent.invalid check the rate"
MENTION_KEY = "keel:c:1:mention:1"


def mention_body(**overrides) -> dict:
    """Ana's comment in the offer, mentioning the step's agent, as Keel sends it."""
    return {
        "path": "offer.docx",
        "thread": "c1a2",
        "comment": COMMENT,
        "author_id": str(AUTHOR),
        "author_name": "Ana Popescu",
        "idempotency_key": MENTION_KEY,
        **overrides,
    }


async def mention(client: AsyncClient, session_id, *, auth: str | None = LINK_KEY, **overrides):
    return await client.post(
        step_route(session_id, "mentions"), json=mention_body(**overrides), headers=link_headers(auth),
    )


class _NamedAgents:
    """The runtime configs ops answers, each agent with the display name the editor shows it under."""

    def __init__(self, agents) -> None:
        self._agents = agents

    async def get(self, agent_id: str) -> dict:
        return {**await self._agents.get(agent_id), "display_name": "Credit Analyst"}


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def agent_editing(link_app):
    link_app.state.settings.office = AGENT_EDITING
    link_app.state.runtime_config_cache = _NamedAgents(link_app.state.runtime_config_cache)


async def draft_step(link, org, session_store, redis_client, *, key: str = "keel:c:1:0"):
    """A draft step session whose turn has ended, and which is no longer queued."""
    return await completed_step(link, org, session_store, redis_client, key=key, mode="draft")


async def test_a_mention_reaches_a_draft_step_as_the_editors_mention_does(link, org, session_store, redis_client):
    step = await draft_step(link, org, session_store, redis_client)

    sent = await mention(link, step.id)

    assert (sent.status_code, sent.json()) == (202, {})
    message = (await user_messages(session_store, step.id))[-1]
    assert message.data == {
        "content": "[Comment on offer.docx] Ana Popescu: «@Credit Analyst check the rate»",
        "synthetic": "keel_mention",
        "keel_key": MENTION_KEY,
        "source": "office_mention",
        "metadata": {
            "path": "offer.docx",
            "thread": "c1a2",
            "kind": "docx",
            "author": "Ana Popescu",
            "author_id": f"user:{AUTHOR}",
            "comment": COMMENT,
        },
    }
    assert await timeline(session_store, step.id) == [
        ("user.message", "keel_step"),
        ("session.resume", None),
        ("user.message", "keel_mention"),
    ]
    assert (await session_store.get_session(step.id)).status == "active"
    # Behind interactive work, as every wake of a step.
    assert await queue_score(redis_client, step) == 1.0


async def test_a_mention_without_an_author_name_is_someones(link, org, session_store, redis_client):
    step = await draft_step(link, org, session_store, redis_client)

    sent = await mention(link, step.id, path="./out//terms.xlsx", author_name=None)

    assert (sent.status_code, sent.json()) == (202, {})
    message = (await user_messages(session_store, step.id))[-1]
    assert message.data["content"] == "[Comment on terms.xlsx] Someone: «@Credit Analyst check the rate»"
    # The path as the case's files name it.
    assert message.data["metadata"]["path"] == "out/terms.xlsx"
    assert message.data["metadata"]["kind"] == "xlsx"
    assert message.data["metadata"]["author"] == "Someone"


async def test_the_worker_acknowledges_a_mention_in_the_comments_thread_of_the_cases_document(
    link, link_app, org, session_store, session_factory, redis_client, monkeypatch,
):
    case_id = uuid.uuid4()
    step = await completed_step(link, org, session_store, redis_client, case_id=str(case_id), mode="draft")
    state = link_app.state
    await write_case_file(state.storage, state.settings, case_id, "offer.docx", b"PK-docx")
    edits: list[tuple[str, list]] = []

    class RuntimeOffice:
        """runtime-office, as far as an acknowledgement reaches it: it opens the agent's page, and takes its edits."""

        def __init__(self, office) -> None:
            pass

        async def open(self, *, doc_key, session_id, config, document_server_url):
            return f"{doc_key}.{session_id}"

        async def edit(self, page, ops):
            edits.append((page, ops))
            return {"applied": True, "results": [{"op": 0}], "flushed": True}

    monkeypatch.setattr(office_tools, "RuntimeOffice", RuntimeOffice)
    for module in (office_tools, loop_module):
        monkeypatch.setattr(module, "load_settings", lambda: SimpleNamespace(office=AGENT_EDITING))
    before = await session_store.last_event_id(step.id)
    sent = await mention(link, step.id)
    assert sent.status_code == 202, sent.text

    # The step's worker reads the mention as a turn does, and posts the acknowledgement before the model reads it.
    worker = _harness(session_store)
    worker._storage, worker._session_factory, worker._redis = state.storage, session_factory, redis_client
    message, _ = await worker._collect_steer_messages(await session_store.get_session(step.id), before)

    # In the case's own document, through the step's agent's editor page, in the comment's thread.
    document = await office_sessions.open_at(
        session_factory, case_workspace(state.storage, state.settings, case_id).location("offer.docx"),
    )
    assert document is not None
    assert document.session_ids == (step.id,)
    assert edits == [(
        f"{document.doc_key}.{step.id}",
        [{"op": "reply", "thread": "c1a2", "body": "On it, I'll reply here when done."}],
    )]
    # Saved with the documents the turn edits, at its end; and the model reads the comment with its thread's note.
    assert worker._mention_paths == ("offer.docx",)
    assert message["content"].startswith("[A comment in offer.docx mentioned you.")
    assert message["content"].endswith("[Comment on offer.docx] Ana Popescu: «@Credit Analyst check the rate»")


async def test_a_mention_wakes_a_draft_under_review_and_keeps_its_mark(link, org, session_store, redis_client):
    step = await draft_step(link, org, session_store, redis_client)
    await session_store.mark_case_step_reported(step.id, "drafted")

    sent = await mention(link, step.id)

    assert (sent.status_code, sent.json()) == (202, {})
    assert await timeline(session_store, step.id) == [
        ("user.message", "keel_step"),
        ("session.resume", None),
        ("user.message", "keel_mention"),
    ]
    session = await session_store.get_session(step.id)
    assert session.status == "active"
    # The draft is still the reviewer's: the mention gives no step back.
    assert session.config["case_step"]["reported"] == "drafted"
    assert await queue_score(redis_client, step) == 1.0


async def test_a_mention_is_written_once_per_key(link, org, session_store, redis_client):
    step = await draft_step(link, org, session_store, redis_client)
    first = await mention(link, step.id)
    # The mention's turn ran and ended before Keel's retry came.
    await session_store.update_session_status(step.id, "completed")
    await redis_client.zrem(
        SHARED_WORK_QUEUE_KEY,
        encode_queue_member(org_id=str(org), agent_id=step.agent_id, session_id=str(step.id)),
    )

    again = await mention(link, step.id)

    assert (first.status_code, first.json()) == (202, {})
    assert (again.status_code, again.json()) == (202, {})
    markers = [m.data.get("synthetic") for m in await user_messages(session_store, step.id)]
    assert markers == ["keel_step", "keel_mention"]
    # The retry neither wakes the session nor queues it again.
    assert (await session_store.get_session(step.id)).status == "completed"
    assert await queue_score(redis_client, step) is None


async def test_a_step_without_office_tools_takes_no_mention(link, link_app, org, session_store, redis_client):
    complete = await completed_step(link, org, session_store, redis_client, key="keel:c:1:0")
    draft = await draft_step(link, org, session_store, redis_client, key="keel:c:1:1")

    # A complete step's agent edits no document.  Its path and kind are checked first.
    answers = [
        await mention(link, complete.id),
        await mention(link, complete.id, path="../x.docx"),
        await mention(link, complete.id, path="notes.txt"),
    ]
    assert [(a.status_code, code_of(a)) for a in answers] == [
        (409, "no-office"), (422, "invalid-path"), (415, "not-a-document"),
    ]

    # Without runtime-office the agent edits no document live, and answers no comment.
    link_app.state.settings.office = OfficeSettings(**DOCUMENT_SERVER)
    refused = await mention(link, draft.id)

    assert (refused.status_code, code_of(refused)) == (409, "no-office")
    for step in (complete, draft):
        assert await timeline(session_store, step.id) == [("user.message", "keel_step")]
        assert (await session_store.get_session(step.id)).status == "completed"
        assert await queue_score(redis_client, step) is None


async def test_a_step_that_is_over_takes_no_mention(link, org, session_store, redis_client):
    step = await draft_step(link, org, session_store, redis_client)
    first = await mention(link, step.id)
    await cancel(link, step.id)
    await redis_client.zrem(
        SHARED_WORK_QUEUE_KEY,
        encode_queue_member(org_id=str(org), agent_id=step.agent_id, session_id=str(step.id)),
    )

    refused = await mention(link, step.id, idempotency_key="keel:c:1:mention:2")
    # Keel's retry of a mention the step took before it ended answers as the first call did.
    again = await mention(link, step.id)

    assert first.status_code == 202, first.text
    assert (refused.status_code, code_of(refused)) == (409, "step-finished")
    assert (again.status_code, again.json()) == (202, {})
    assert await timeline(session_store, step.id) == [
        ("user.message", "keel_step"),
        ("session.resume", None),
        ("user.message", "keel_mention"),
        ("session.pause", None),
    ]
    assert (await session_store.get_session(step.id)).status == "paused"
    assert await queue_score(redis_client, step) is None


async def test_a_mention_out_of_its_shape_is_refused(link, org, session_store, redis_client):
    step = await draft_step(link, org, session_store, redis_client)

    paths = [
        await mention(link, step.id, path="../x.docx"),
        await mention(link, step.id, path="/offer.docx"),
        await mention(link, step.id, path="notes.txt"),
        # A kind the editor only converts to show: the agent edits none.
        await mention(link, step.id, path="offer.doc"),
    ]
    assert [(a.status_code, code_of(a)) for a in paths] == [
        (422, "invalid-path"), (422, "invalid-path"), (415, "not-a-document"), (415, "not-a-document"),
    ]

    bodies = [
        await mention(link, step.id, thread="a b"),
        await mention(link, step.id, thread='c1" or another'),
        await mention(link, step.id, thread="x" * 65),
        await mention(link, step.id, author_id="not-a-uuid"),
        await mention(link, step.id, comment=""),
        await mention(link, step.id, comment="x" * 20001),
        await mention(link, step.id, author_name=""),
        await mention(link, step.id, author_name="   "),
        await mention(link, step.id, author_name="Ana\nPopescu"),
        await mention(link, step.id, author_name="a" * 201),
        await mention(link, step.id, idempotency_key=""),
        await mention(link, step.id, idempotency_key="k" * 201),
        await mention(link, step.id, path=""),
        await mention(link, step.id, path="a" * 4092 + ".docx"),
        # The body is read before the path.
        await mention(link, step.id, path="../x.docx", thread="a b"),
        await mention(link, step.id, priority="high"),
    ]
    assert [(a.status_code, code_of(a)) for a in bodies] == [(422, "invalid-request")] * 16
    assert await timeline(session_store, step.id) == [("user.message", "keel_step")]
    assert (await session_store.get_session(step.id)).status == "completed"
    assert await queue_score(redis_client, step) is None
