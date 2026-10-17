"""The guide an office call carries is fetched from the office skill's files, and a missing one is not asked for again
at every call."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from surogates.harness.api_client import HarnessAPIClient
from surogates.office import guide
from surogates.office.guide import RETRY_AFTER_S, guide_for

OFFICE_SKILL = Path(__file__).parents[1] / "skills/productivity/office"
API = "http://api.internal:8000"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(guide, "monotonic", lambda: now[0])
    guide._unfetchable.clear()
    yield now
    guide._unfetchable.clear()


def api(answer) -> tuple[HarnessAPIClient, list[httpx.Request]]:
    """The worker's client, scoped to a chat, over an API that answers with *answer*; and what it was asked."""
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return answer(request)

    client = HarnessAPIClient(API, "token", session_id=str(uuid4()), agent_id="writer")
    client._client = httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handler))
    return client, asked


def the_skill(request: httpx.Request) -> httpx.Response:
    path = request.url.params.get("path")
    if request.url.path != "/v1/skills/office/file" or not path:
        return httpx.Response(404, json={"detail": "Skill 'office' not found."})
    return httpx.Response(200, content=(OFFICE_SKILL / path).read_bytes())


def not_found(request: httpx.Request) -> httpx.Response:
    # A system bundle older than the references' names: the file route knows the skill but not the file.
    return httpx.Response(404, json={"detail": f"File '{request.url.params.get('path')}' not found in skill 'office'."})


async def test_the_guide_is_the_skills_rules_and_the_types_reference():
    client, asked = api(the_skill)
    found = await guide_for("xlsx", client)
    assert found["rules"].startswith("# Working on documents live with the user")
    assert found["reference"] == (OFFICE_SKILL / "references/xlsx.md").read_text().strip()
    # The files as the skill stores them, under the chat's own entitlement.
    assert sorted(request.url.params["path"] for request in asked) == ["SKILL.md", "references/xlsx.md"]
    assert {request.url.path for request in asked} == {"/v1/skills/office/file"}


async def test_a_guide_the_api_has_not_is_asked_for_once_in_a_while(clock, caplog):
    client, asked = api(not_found)
    with caplog.at_level(logging.WARNING, logger=guide.__name__):
        assert await guide_for("docx", client) is None
    # Only the two files: never the skill's own view, which stages the skill into the chat's workspace.
    assert {request.url.path for request in asked} == {"/v1/skills/office/file"}
    assert sorted(request.url.params["path"] for request in asked) == ["SKILL.md", "references/docx.md"]
    # An answer the API gives is one line, not a traceback.
    assert [(record.levelname, record.exc_info) for record in caplog.records] == [("WARNING", None)]
    assert "404" in caplog.records[0].getMessage()

    # Every call on the type in the next few minutes, in any chat of this worker, goes without asking again.
    clock[0] += RETRY_AFTER_S - 1
    other, asked_again = api(the_skill)
    assert await guide_for("docx", other) is None
    assert len(asked) == 2 and asked_again == [] and len(caplog.records) == 1
    # Another type is asked for as ever.
    assert (await guide_for("pdf", other))["reference"].startswith("#")

    clock[0] += 2
    assert (await guide_for("docx", other))["reference"].startswith("# office_edit for Word documents")


async def test_a_failure_of_another_kind_keeps_its_traceback(caplog):
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client, _ = api(broken)
    with caplog.at_level(logging.WARNING, logger=guide.__name__):
        assert await guide_for("pptx", client) is None
    assert len(caplog.records) == 1 and caplog.records[0].exc_info is not None
