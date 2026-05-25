"""SA-principal access to the skills list at /v1/api/skills.

Mirrors the path the Surogate Ops Work UI takes to populate the chat
slash menu: it authenticates to surogates as a per-user service
account, so every request reaches the handler with ``user_id=None`` on
the harness side.  The read endpoints are mounted at both ``/v1/`` (JWT)
and ``/v1/api/`` (service-account); the mutating endpoints stay
JWT-only — splitting the surface keeps an SA token from being able to
create/edit/delete skills.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _write_builtin(platform_dir: Path) -> None:
    """Write a single platform skill at the layout the loader walks."""
    skill_dir = platform_dir / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Built-in demo\n---\nBody\n",
        encoding="utf-8",
    )


def _write_builtin_with_root_file(platform_dir: Path) -> None:
    """Platform skill with a top-level linked doc next to SKILL.md.

    Mirrors the real ``productivity/pptx`` layout where ``editing.md``
    and ``pptxgenjs.md`` sit at the skill root rather than inside one
    of ``references/templates/scripts/assets``.
    """
    skill_dir = platform_dir / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Built-in demo\n---\nBody\n",
        encoding="utf-8",
    )
    (skill_dir / "editing.md").write_text("root-level doc body", encoding="utf-8")


@pytest_asyncio.fixture(loop_scope="session")
async def app(
    session_factory,
    redis_client,
    pg_url,
    redis_url,
    tmp_path_factory,
):
    """FastAPI app wired to test containers, with a platform skills dir.

    Mirrors the ``app`` fixture in ``test_agents_api.py`` (same wiring
    for ``session_factory``, ``redis``, storage, and credential vault),
    then points ``settings.platform_skills_dir`` at a tmp directory we
    can populate per test.
    """
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory)

    platform_dir = tmp_path_factory.mktemp("skills-sa-platform")
    settings = Settings()
    settings.platform_skills_dir = str(platform_dir)
    application.state.settings = settings
    application.state.platform_skills_dir = str(platform_dir)

    storage_root = tmp_path_factory.mktemp("skills-sa-storage")
    application.state.storage = LocalBackend(base_path=str(storage_root))
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key(),
    )
    # Stash the platform dir on the app for tests to seed per case.
    application.state._test_platform_dir = platform_dir
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        yield c


async def test_sa_token_can_list_skills_at_v1_api(
    app, client: AsyncClient, session_factory,
):
    platform_dir: Path = app.state._test_platform_dir
    _write_builtin(platform_dir)

    org_id = await create_org(session_factory)
    sa = await issue_service_account_token(
        session_factory, org_id, name="ops-chat-sa",
    )

    response = await client.get(
        "/v1/api/skills",
        headers={"Authorization": f"Bearer {sa.token}"},
    )

    assert response.status_code == 200, response.text
    names = {s["name"] for s in response.json()["skills"]}
    assert "demo-skill" in names


async def test_sa_token_cannot_create_skill_at_v1_api(
    client: AsyncClient, session_factory,
):
    org_id = await create_org(session_factory)
    sa = await issue_service_account_token(
        session_factory, org_id, name="ops-chat-sa-write",
    )

    response = await client.post(
        "/v1/api/skills",
        json={"name": "should-fail", "content": "..."},
        headers={"Authorization": f"Bearer {sa.token}"},
    )

    # write_router is not mounted at /v1/api/, so POST on this path is
    # a 405 (the GET handler exists, the POST handler does not).  Either
    # way the SA token cannot create skills via this prefix — which is
    # what we care about.
    assert response.status_code == 405


async def test_read_root_level_skill_file_returns_content(
    app, client: AsyncClient, session_factory,
):
    """Root-level linked files (e.g. ``editing.md``) must be readable.

    Regression for a 422 caused by applying the write-time
    ``validate_file_path`` validator (which requires the path to start
    with ``references/templates/scripts/assets``) to the read route,
    even though the listing endpoint advertises root-level files in
    ``linked_files``.
    """
    platform_dir: Path = app.state._test_platform_dir
    _write_builtin_with_root_file(platform_dir)

    org_id = await create_org(session_factory)
    sa = await issue_service_account_token(
        session_factory, org_id, name="ops-chat-sa-file-read",
    )

    response = await client.get(
        "/v1/api/skills/demo-skill/file",
        params={"path": "editing.md"},
        headers={"Authorization": f"Bearer {sa.token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["file_path"] == "editing.md"
    assert body["content"] == "root-level doc body"
    assert body["binary"] is False


async def test_read_skill_file_rejects_path_traversal(
    app, client: AsyncClient, session_factory,
):
    """``..`` in the path must still be refused with 422."""
    platform_dir: Path = app.state._test_platform_dir
    _write_builtin_with_root_file(platform_dir)

    org_id = await create_org(session_factory)
    sa = await issue_service_account_token(
        session_factory, org_id, name="ops-chat-sa-file-traversal",
    )

    response = await client.get(
        "/v1/api/skills/demo-skill/file",
        params={"path": "../etc/passwd"},
        headers={"Authorization": f"Bearer {sa.token}"},
    )

    assert response.status_code == 422, response.text
    assert "traversal" in response.json()["detail"].lower()


async def test_sa_token_rejected_on_v1_skills_without_api_prefix(
    app, client: AsyncClient, session_factory,
):
    """Bare SA tokens must NOT reach the JWT-only mount at ``/v1/skills``."""
    platform_dir: Path = app.state._test_platform_dir
    _write_builtin(platform_dir)

    org_id = await create_org(session_factory)
    sa = await issue_service_account_token(
        session_factory, org_id, name="ops-chat-sa-jwt-mount",
    )

    response = await client.get(
        "/v1/skills",
        headers={"Authorization": f"Bearer {sa.token}"},
    )

    # The middleware (`_tenant_context_from_token`) explicitly rejects
    # bare service-account tokens off the `/v1/api/*` allow-list with a
    # 403 so the failure message names the prefix that *would* work.
    assert response.status_code == 403
