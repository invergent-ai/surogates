"""What a project's chat does not have, because it is a thread's work.

A document written to the user's request is a thread's, so the chat has no
skill that makes one: they are not in its prompt or its ``skills_list``,
and ``skill_view`` of one is refused with words that send the work to a
thread.  The project's threads, and every other chat, keep them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest

from surogates.harness.api_client import HarnessAPIClient
from surogates.harness.prompt import PromptBuilder
from surogates.orchestrator import worker as worker_module
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.builtin.skills import _skill_view_handler, _skills_list_handler
from surogates.tools.loader import ResourceLoader, SkillDef
from surogates.workstreams import master_config, thread_config

pytestmark = pytest.mark.asyncio

#: The catalog's skills that make a document.
DOCUMENT_SKILLS = ["docx", "xlsx", "pptx", "pdf", "doc-coauthoring"]
#: Skills that make none: reading a PDF, and a schedule.
OTHER_SKILLS = ["pdf-reading", "schedule"]

PROJECT = SimpleNamespace(id=uuid4(), name="Quarterly report", instructions="Use euros.", thread_tier=None)
MASTER = master_config(PROJECT.id, name=PROJECT.name, goal=None, instructions=PROJECT.instructions)
MASTER_ON_A_COMPUTER = {
    **MASTER,
    "execution": {"kind": "device", "device_id": str(uuid4()), "device_name": "A laptop"},
    "workspace_path": "/nonexistent/Quarterly report",
}
THREAD = thread_config(PROJECT, title="Draft the NDA")

MASTERS = pytest.mark.parametrize("config", [MASTER, MASTER_ON_A_COMPUTER], ids=["in-the-cloud", "on-a-computer"])
OTHERS = pytest.mark.parametrize("config", [THREAD, {}], ids=["its-thread", "a-chat"])
MODES = pytest.mark.parametrize("through_the_api", [True, False], ids=["through-the-api", "in-the-worker"])

TENANT = TenantContext(
    org_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_id=UUID("00000000-0000-0000-0000-000000000002"),
    org_config={},
    user_preferences={},
    permissions=frozenset(),
    asset_root="/tmp/test",
)


def refusal(name: str) -> str:
    return (
        f"The {name} skill makes a document, and in a project a document is a thread's work. "
        "Propose a thread for it with propose_threads, or start one with start_thread where this project allows."
    )


def session_with(config: dict, *, parent: bool = False) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(), user_id=TENANT.user_id, org_id=TENANT.org_id, agent_id="agent-1", channel="web",
        status="active", parent_id=uuid4() if parent else None, config=dict(config),
        created_at=now, updated_at=now,
    )


def catalog() -> list[SkillDef]:
    return [
        SkillDef(
            name=name, description=f"Works with {name}", content=f"# {name}\nThe {name} playbook.",
            source="org_db", category="productivity",
        )
        for name in DOCUMENT_SKILLS + OTHER_SKILLS
    ]


class TheApi:
    """The api's skill routes, as the harness client reaches them."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request.url.path)
        if request.url.path == "/v1/skills":
            return httpx.Response(200, json={"skills": [
                {"name": s.name, "description": s.description, "type": "skill", "category": s.category}
                for s in catalog()
            ], "total": len(catalog())})
        name = request.url.path.removeprefix("/v1/skills/")
        [skill] = [s for s in catalog() if s.name == name]
        return httpx.Response(200, json={"name": name, "type": "skill", "content": skill.content})

    def client(self) -> HarnessAPIClient:
        client = HarnessAPIClient("http://api.internal:8000", "token", session_id=str(uuid4()))
        client._client = httpx.AsyncClient(
            base_url="http://api.internal:8000", transport=httpx.MockTransport(self.answer),
        )
        return client


def skill_kwargs(monkeypatch, config: dict, *, through_the_api: bool, api: TheApi | None = None) -> dict:
    """What the harness hands a skill tool: the session's config, and the api's client or the worker's loader."""
    if through_the_api:
        return {"api_client": (api or TheApi()).client(), "tenant": TENANT, "session_config": config}

    async def load_skills(self, tenant, **_):
        return catalog()

    monkeypatch.setattr(ResourceLoader, "load_skills", load_skills)
    return {"tenant": TENANT, "session_config": config}


async def prompt_skills(monkeypatch, config: dict) -> set[str]:
    """The skills a session's system prompt lists, from the catalog the worker loads for it."""
    monkeypatch.setattr(worker_module, "_load_prompt_catalogs", AsyncMock(return_value=([], catalog())))
    _, skills = await worker_module._load_prompt_catalogs_entitled(
        session_config=config, tenant=TENANT, session_factory=None,
    )
    prompt = PromptBuilder(
        TENANT, skills=skills, session=session_with(config), available_tools={"skill_view", "skills_list"},
    ).build()
    return {name for name in DOCUMENT_SKILLS + OTHER_SKILLS if f"- {name}: Works with {name}" in prompt}


async def listed(monkeypatch, config: dict, *, through_the_api: bool) -> set[str]:
    kwargs = skill_kwargs(monkeypatch, config, through_the_api=through_the_api)
    return {s["name"] for s in json.loads(await _skills_list_handler({}, **kwargs))["skills"]}


# -- the skills that make a document ------------------------------------------------------------


@MASTERS
async def test_a_project_chats_prompt_lists_no_skill_that_makes_a_document(monkeypatch, config):
    assert await prompt_skills(monkeypatch, config) == set(OTHER_SKILLS)


@OTHERS
async def test_a_thread_and_any_other_chat_see_the_document_skills_in_their_prompt(monkeypatch, config):
    assert await prompt_skills(monkeypatch, config) == set(DOCUMENT_SKILLS + OTHER_SKILLS)


@MODES
@MASTERS
async def test_a_project_chats_skills_list_has_no_skill_that_makes_a_document(monkeypatch, config, through_the_api):
    assert await listed(monkeypatch, config, through_the_api=through_the_api) == set(OTHER_SKILLS)


@MODES
@OTHERS
async def test_a_thread_and_any_other_chat_list_the_document_skills(monkeypatch, config, through_the_api):
    assert await listed(monkeypatch, config, through_the_api=through_the_api) == set(DOCUMENT_SKILLS + OTHER_SKILLS)


@MODES
@MASTERS
@pytest.mark.parametrize("name", DOCUMENT_SKILLS)
async def test_a_project_chat_is_refused_a_skill_that_makes_a_document(monkeypatch, config, through_the_api, name):
    api = TheApi()
    kwargs = skill_kwargs(monkeypatch, config, through_the_api=through_the_api, api=api)
    for arguments in ({"name": name}, {"name": name, "file_path": "scripts/build.py"}):
        viewed = json.loads(await _skill_view_handler(arguments, **kwargs))
        assert viewed == {"success": False, "error": refusal(name)}
    # Nothing of the skill was asked for, so nothing of it was staged for the chat either.
    assert api.asked == []


@MODES
@MASTERS
async def test_a_project_chat_still_views_a_skill_that_makes_no_document(monkeypatch, config, through_the_api):
    kwargs = skill_kwargs(monkeypatch, config, through_the_api=through_the_api)
    viewed = json.loads(await _skill_view_handler({"name": "pdf-reading"}, **kwargs))
    assert "The pdf-reading playbook." in viewed["content"]


@MODES
@OTHERS
@pytest.mark.parametrize("name", DOCUMENT_SKILLS)
async def test_a_thread_and_any_other_chat_view_a_skill_that_makes_a_document(monkeypatch, config, through_the_api, name):
    kwargs = skill_kwargs(monkeypatch, config, through_the_api=through_the_api)
    viewed = json.loads(await _skill_view_handler({"name": name}, **kwargs))
    assert f"The {name} playbook." in viewed["content"]
