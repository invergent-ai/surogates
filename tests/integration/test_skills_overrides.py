"""Integration tests for session-scoped skill overrides on the skills API.

A service-account session may carry a ``skill_overrides`` map in its
``config`` (attached at prompt-submission time by the ops SkillOpt
worker).  ``GET /v1/api/skills`` and ``GET /v1/api/skills/{name}`` resolve
that map as the highest-precedence layer when the request names the
session, so the candidate ``SKILL.md`` body is served instead of the
published one — while supporting files still stage from the original
bundle source.

These tests wire a fake Hub bundle + ``RuntimeConfigCache`` rather than
the retired ``platform_skills_dir`` path (which no longer exists on
``Settings``).
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import agent_session_bucket, session_workspace_key
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")

AGENT_ID = "default"
STORAGE_BUCKET = "test-skill-overrides"


# ---------------------------------------------------------------------------
# Fake Hub bundle + cache
# ---------------------------------------------------------------------------


class _FakeBundle:
    """In-memory stand-in for an ``AgentFileBundle``.

    Holds ``path -> bytes`` and exposes the same ``list`` / ``read_text``
    / ``read_bytes`` surface the loader and stager use.
    """

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def add(self, path: str, data: str | bytes) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._files[path] = data

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def agent_bundle() -> _FakeBundle:
    return _FakeBundle()


@pytest_asyncio.fixture(loop_scope="session")
async def app(
    session_factory, redis_client, pg_url, redis_url, agent_bundle, tmp_path_factory,
):
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
    settings.storage.bucket = STORAGE_BUCKET
    application.state.settings = settings

    storage_root = tmp_path_factory.mktemp("skill-overrides-storage")
    application.state.storage = LocalBackend(base_path=str(storage_root))
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key(),
    )

    async def _runtime_loader(agent_id: str) -> dict:
        return {
            "agent_id": agent_id,
            "org_id": "00000000-0000-0000-0000-000000000000",
            "project_id": "test-project",
            "enabled": True,
            "version": 1,
            "storage_key_prefix": "",
        }

    application.state.runtime_config_cache = RuntimeConfigCache(loader=_runtime_loader)
    application.state.file_bundle_cache = _FakeBundleCache(agent_bundle)
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def sa(session_factory):
    """One org + service-account token, reused across a test's session rows."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(session_factory, org_id, name="skillopt")
    return SimpleNamespace(
        org_id=org_id,
        token=issued.token,
        headers={"Authorization": f"Bearer {issued.token}"},
    )


@pytest_asyncio.fixture(loop_scope="session")
def seed_platform_skill(agent_bundle):
    """Write a platform skill (SKILL.md + optional extra files) into the bundle."""

    def _seed(name, *, body, description="seed desc", files=None):
        frontmatter = f"---\nname: {name}\ndescription: {description}\n---\n{body}\n"
        agent_bundle.add(f"skills/{name}/SKILL.md", frontmatter)
        for rel, content in (files or {}).items():
            agent_bundle.add(f"skills/{name}/{rel}", content)

    return _seed


@pytest_asyncio.fixture(loop_scope="session")
def make_session_with_overrides(session_factory, sa):
    """Create an api-channel session in the SA's org carrying skill_overrides."""

    async def _make(*, skill, content, description=None, run_id=None, candidate_id=None):
        store = SessionStore(session_factory)
        override: dict = {"content": content, "source": "skillopt", "type": "skill"}
        if description is not None:
            override["description"] = description
        if run_id is not None:
            override["run_id"] = run_id
        if candidate_id is not None:
            override["candidate_id"] = candidate_id
        session = await store.create_session(
            user_id=None,
            org_id=sa.org_id,
            agent_id=AGENT_ID,
            channel="api",
            config={"skill_overrides": {skill: override}, "storage_key_prefix": ""},
        )
        return session.id

    return _make


@pytest_asyncio.fixture(loop_scope="session")
def read_staged_file(app):
    async def _read(session_id, skill_name, rel_path):
        key = session_workspace_key(session_id, f".skills/{skill_name}/{rel_path}")
        return await app.state.storage.read_text(
            agent_session_bucket(STORAGE_BUCKET), key,
        )

    return _read


# ---------------------------------------------------------------------------
# view_skill
# ---------------------------------------------------------------------------


async def test_view_skill_returns_override_content(
    client, sa, seed_platform_skill, make_session_with_overrides,
):
    seed_platform_skill("browser-research", body="ORIGINAL BODY")
    sid = await make_session_with_overrides(
        skill="browser-research", content="CANDIDATE BODY",
    )

    resp = await client.get(
        f"/v1/api/skills/browser-research?agent_id={AGENT_ID}&session_id={sid}",
        headers=sa.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "CANDIDATE BODY" in body["content"]
    assert "ORIGINAL BODY" not in body["content"]


async def test_view_skill_without_session_is_unchanged(
    client, sa, seed_platform_skill,
):
    seed_platform_skill("plain-skill", body="ORIGINAL BODY")
    resp = await client.get(
        f"/v1/api/skills/plain-skill?agent_id={AGENT_ID}",
        headers=sa.headers,
    )
    assert resp.status_code == 200, resp.text
    assert "ORIGINAL BODY" in resp.json()["content"]


async def test_override_stages_original_supporting_files(
    client, sa, seed_platform_skill, make_session_with_overrides, read_staged_file,
):
    seed_platform_skill(
        "research-staged", body="ORIGINAL",
        files={"scripts/run.py": "print('original script')"},
    )
    sid = await make_session_with_overrides(
        skill="research-staged", content="CANDIDATE",
    )
    resp = await client.get(
        f"/v1/api/skills/research-staged?agent_id={AGENT_ID}&session_id={sid}",
        headers=sa.headers,
    )
    assert resp.status_code == 200, resp.text
    assert "CANDIDATE" in resp.json()["content"]
    # The staged script is the ORIGINAL, untouched by the override.
    staged = await read_staged_file(sid, "research-staged", "scripts/run.py")
    assert "original script" in staged


# ---------------------------------------------------------------------------
# list_skills
# ---------------------------------------------------------------------------


async def test_list_skills_reflects_override_description(
    client, sa, seed_platform_skill, make_session_with_overrides,
):
    seed_platform_skill("listed-skill", body="ORIGINAL", description="old desc")
    sid = await make_session_with_overrides(
        skill="listed-skill", content="CANDIDATE", description="new desc",
    )
    resp = await client.get(
        f"/v1/api/skills?agent_id={AGENT_ID}&session_id={sid}",
        headers=sa.headers,
    )
    assert resp.status_code == 200, resp.text
    by_name = {s["name"]: s for s in resp.json()["skills"]}
    assert by_name["listed-skill"]["description"] == "new desc"


async def test_list_skills_without_session_keeps_original_description(
    client, sa, seed_platform_skill,
):
    seed_platform_skill("catalog-skill", body="ORIGINAL", description="old desc")
    resp = await client.get(
        f"/v1/api/skills?agent_id={AGENT_ID}",
        headers=sa.headers,
    )
    assert resp.status_code == 200, resp.text
    by_name = {s["name"]: s for s in resp.json()["skills"]}
    assert by_name["catalog-skill"]["description"] == "old desc"
