"""The skill routes leave a fallback skill out of a chat sent the tool it stands in for.

A chat with the office tools makes and edits documents live with office_edit: ``GET /v1/skills`` with its
``session_id`` leaves out the docx, xlsx, pptx and pdf-forms skills, which do the same with scripts, and
``GET /v1/skills/{name}`` and ``/file`` refuse them, saying why. A chat without the office tools has them, and so does
the web UI's slash menu, which names no session. The pdf skill is in every chat.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.tenant.credentials import CredentialVault
from surogates.tools import skill_visibility

from .conftest import create_org, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")

AGENT_ID = "default"
SKILLS = Path(__file__).parents[2] / "skills/productivity"
FALLBACKS = {"docx", "xlsx", "pptx", "pdf-forms"}


def refusal(name: str) -> str:
    return (
        f"{name} is for chats without the office tools: this chat makes and edits documents live with office_edit "
        "(the office skill)."
    )


class _FakeBundle:
    """The agent's Hub bundle, holding the repository's document skills."""

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        for name in (*FALLBACKS, "pdf"):
            self._files[f"skills/{name}/SKILL.md"] = (SKILLS / name / "SKILL.md").read_bytes()
        self._files["skills/pdf-forms/FORMS.md"] = (SKILLS / "pdf-forms/FORMS.md").read_bytes()

    async def list(self, prefix: str = "") -> list[str]:
        return sorted(p for p in self._files if p.startswith(prefix))

    async def read_bytes(self, path: str) -> bytes:
        try:
            return self._files[path]
        except KeyError as exc:
            raise LookupError(path) from exc

    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return (await self.read_bytes(path)).decode(encoding)


class _FakeBundleCache:
    def __init__(self, bundle: _FakeBundle) -> None:
        self._bundle = bundle

    async def get(self, agent_id: str) -> _FakeBundle:
        return self._bundle


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url, tmp_path_factory):
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.runtime import RuntimeConfigCache

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory, redis=redis_client)
    settings = Settings()
    settings.storage.bucket = "test-skills-fallback"
    application.state.settings = settings
    application.state.storage = LocalBackend(base_path=str(tmp_path_factory.mktemp("skills-fallback-storage")))
    application.state.credential_vault = CredentialVault(session_factory, Fernet.generate_key())

    async def _runtime_loader(agent_id: str) -> dict:
        return {
            "agent_id": agent_id, "org_id": "00000000-0000-0000-0000-000000000000", "project_id": "test-project",
            "enabled": True, "version": 1, "storage_key_prefix": "",
        }

    application.state.runtime_config_cache = RuntimeConfigCache(loader=_runtime_loader)
    application.state.file_bundle_cache = _FakeBundleCache(_FakeBundle())
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def chats(session_factory):
    """An org's token, and a web chat (sent the office tools) and an API caller's chat (not) in it."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(session_factory, org_id, name="skills-fallback")
    store = SessionStore(session_factory)
    web = await store.create_session(user_id=None, org_id=org_id, agent_id=AGENT_ID, channel="web", config={})
    api = await store.create_session(user_id=None, org_id=org_id, agent_id=AGENT_ID, channel="api", config={})
    return SimpleNamespace(headers={"Authorization": f"Bearer {issued.token}"}, web=web.id, api=api.id)


@pytest.fixture(autouse=True)
def live_editing(monkeypatch):
    monkeypatch.setattr(
        skill_visibility, "load_settings", lambda: SimpleNamespace(office=SimpleNamespace(agent_editing=True)),
    )


async def listed(client, chats, session_id=None) -> set[str]:
    query = f"agent_id={AGENT_ID}" + (f"&session_id={session_id}" if session_id else "")
    resp = await client.get(f"/v1/api/skills?{query}", headers=chats.headers)
    assert resp.status_code == 200, resp.text
    return {skill["name"] for skill in resp.json()["skills"]}


async def test_a_chat_with_the_office_tools_lists_no_fallback_skill(client, chats):
    names = await listed(client, chats, chats.web)
    assert not names & FALLBACKS
    assert "pdf" in names


async def test_a_chat_without_them_and_the_slash_menu_list_every_one(client, chats):
    for session_id in (chats.api, None):
        assert FALLBACKS | {"pdf"} <= await listed(client, chats, session_id), session_id


async def test_a_chat_with_the_office_tools_is_refused_a_fallback_skill_and_its_files(client, chats):
    for name in sorted(FALLBACKS):
        resp = await client.get(
            f"/v1/api/skills/{name}?agent_id={AGENT_ID}&session_id={chats.web}", headers=chats.headers,
        )
        assert resp.status_code == 403, (name, resp.text)
        assert resp.json()["detail"] == refusal(name)
    resp = await client.get(
        f"/v1/api/skills/pdf-forms/file?path=FORMS.md&agent_id={AGENT_ID}&session_id={chats.web}",
        headers=chats.headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == refusal("pdf-forms")


async def test_a_chat_without_them_reads_them(client, chats):
    for session_id in (chats.api, None):
        query = f"agent_id={AGENT_ID}" + (f"&session_id={session_id}" if session_id else "")
        resp = await client.get(f"/v1/api/skills/docx?{query}", headers=chats.headers)
        assert resp.status_code == 200, (session_id, resp.text)
        resp = await client.get(f"/v1/api/skills/pdf-forms/file?path=FORMS.md&{query}", headers=chats.headers)
        assert resp.status_code == 200, (session_id, resp.text)
        assert "fillable" in resp.json()["content"]


async def test_every_chat_reads_the_pdf_skill(client, chats):
    for session_id in (chats.web, chats.api):
        resp = await client.get(
            f"/v1/api/skills/pdf?agent_id={AGENT_ID}&session_id={session_id}", headers=chats.headers,
        )
        assert resp.status_code == 200, (session_id, resp.text)
