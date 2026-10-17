"""A fallback skill is hidden wherever its tool is offered.

A chat sent the office tools makes and edits documents live with office_edit, so the docx, xlsx, pptx and pdf-forms
skills, which do the same with scripts, are not in its prompt, its ``skills_list`` or its ``skill_view``. A chat
without the office tools has all four. The pdf skill merges, splits and creates PDFs, which office_edit does not: every
chat has it. Here the worker reads the skills itself; through the api, ``tests/integration/test_skills_fallback.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from surogates.harness.prompt import PromptBuilder
from surogates.orchestrator import worker as worker_module
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools import skill_visibility
from surogates.tools.builtin import skills as skills_module
from surogates.tools.builtin.skills import _skill_view_handler, _skills_list_handler
from surogates.tools.loader import ResourceLoader
from surogates.tools.skill_visibility import fallback_hidden, offered_fallback_tools

SKILLS = Path(__file__).parents[1] / "skills"
FALLBACKS = {"docx", "xlsx", "pptx", "pdf-forms"}
OFFICE_TOOLS = frozenset({"office_read", "office_edit"})

#: A web chat has the document pane the office tools work in, and is sent them; an API caller has none.
CHATS = pytest.mark.parametrize(
    ("channel", "tools"), [("web", OFFICE_TOOLS), ("api", frozenset())], ids=["with-the-office-tools", "without"],
)

TENANT = TenantContext(
    org_id=UUID("00000000-0000-0000-0000-000000000001"),
    user_id=UUID("00000000-0000-0000-0000-000000000002"),
    org_config={},
    user_preferences={},
    permissions=frozenset(),
    asset_root="/nonexistent",
)


def refusal(name: str) -> str:
    return (
        f"{name} is for chats without the office tools: this chat makes and edits documents live with office_edit "
        "(the office skill)."
    )


def catalog() -> list:
    """The built-in skills, as the repository holds them."""
    return ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")


def editing(monkeypatch, on: bool) -> None:
    monkeypatch.setattr(
        skill_visibility, "load_settings", lambda: SimpleNamespace(office=SimpleNamespace(agent_editing=on)),
    )


@pytest.fixture(autouse=True)
def the_worker_reads_the_repositorys_skills(monkeypatch):
    editing(monkeypatch, True)

    async def load_skills(self, tenant, **_):
        return catalog()

    monkeypatch.setattr(ResourceLoader, "load_skills", load_skills)
    folders = {path.parent.name: path.parent for path in SKILLS.rglob("SKILL.md")}
    monkeypatch.setattr(skills_module, "_resolve_skill_dir", lambda name, tenant, **_: folders.get(name))


def session_on(channel: str) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(), user_id=TENANT.user_id, org_id=TENANT.org_id, agent_id="agent-1", channel=channel,
        status="active", config={}, created_at=now, updated_at=now,
    )


async def prompt_skills(monkeypatch, channel: str, tools: frozenset[str]) -> set[str]:
    """The skills a session's system prompt lists, from the catalog the worker loads for it."""
    monkeypatch.setattr(worker_module, "_load_prompt_catalogs", AsyncMock(return_value=([], catalog())))
    _, skills = await worker_module._load_prompt_catalogs_entitled(
        session_config={}, tenant=TENANT, session_factory=None,
    )
    prompt = PromptBuilder(
        TENANT, skills=skills, session=session_on(channel), available_tools={"skill_view", "skills_list", *tools},
    ).build()
    return {skill.name for skill in catalog() if f"    - {skill.name}: " in prompt}


async def view(channel: str, name: str, file_path: str | None = None) -> dict:
    arguments = {"name": name} if file_path is None else {"name": name, "file_path": file_path}
    return json.loads(await _skill_view_handler(arguments, tenant=TENANT, session_config={}, channel=channel))


# -- the rule ----------------------------------------------------------------------------------


def test_the_office_tools_are_offered_where_the_agent_edits_live_in_a_chat_with_a_document_pane(monkeypatch):
    assert offered_fallback_tools("web", {}) == OFFICE_TOOLS
    assert offered_fallback_tools("web", None) == OFFICE_TOOLS
    assert offered_fallback_tools("api", {}) == frozenset()
    assert offered_fallback_tools("web", {"excluded_tools": ["office_edit"]}) == frozenset()
    editing(monkeypatch, False)
    assert offered_fallback_tools("web", {}) == frozenset()


def test_a_fallback_skill_hides_only_when_every_tool_it_stands_in_for_is_offered():
    assert fallback_hidden(["office_edit"], OFFICE_TOOLS)
    assert not fallback_hidden(["office_edit"], frozenset())
    assert not fallback_hidden(["office_edit", "web_search"], OFFICE_TOOLS)
    assert not fallback_hidden([], OFFICE_TOOLS)


# -- the prompt, skills_list and skill_view ----------------------------------------------------


@CHATS
async def test_the_prompt_lists_a_fallback_skill_only_without_its_tool(monkeypatch, channel, tools):
    listed = await prompt_skills(monkeypatch, channel, tools)
    assert listed & FALLBACKS == (set() if tools else FALLBACKS)
    assert "pdf" in listed


@CHATS
async def test_skills_list_lists_a_fallback_skill_only_without_its_tool(channel, tools):
    answer = json.loads(await _skills_list_handler({}, tenant=TENANT, session_config={}, channel=channel))
    listed = {skill["name"] for skill in answer["skills"]}
    assert listed & FALLBACKS == (set() if tools else FALLBACKS)
    assert "pdf" in listed
    assert answer["count"] == len(answer["skills"])


@CHATS
@pytest.mark.parametrize("name", sorted(FALLBACKS))
async def test_skill_view_serves_a_fallback_skill_only_without_its_tool(channel, tools, name):
    answer = await view(channel, name)
    if tools:
        assert answer == {"success": False, "error": refusal(name)}
    else:
        assert answer["success"] is True and answer["content"]


@CHATS
async def test_skill_view_serves_a_fallback_skills_files_only_without_its_tool(channel, tools):
    answer = await view(channel, "pdf-forms", "FORMS.md")
    if tools:
        assert answer == {"success": False, "error": refusal("pdf-forms")}
    else:
        assert answer["success"] is True and "fillable" in answer["content"]


@CHATS
async def test_every_chat_has_the_pdf_skill(channel, tools):
    answer = await view(channel, "pdf")
    assert answer["success"] is True
    assert "scripts/watermark.py" in answer["linked_files"]["scripts"]


async def test_without_live_editing_a_web_chat_keeps_every_fallback_skill(monkeypatch):
    editing(monkeypatch, False)
    answer = json.loads(await _skills_list_handler({}, tenant=TENANT, session_config={}, channel="web"))
    assert FALLBACKS <= {skill["name"] for skill in answer["skills"]}
    assert (await view("web", "docx"))["success"] is True
