"""The skills API serves skills from the agent bundle and the system bundle."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from surogates.api.routes import skills as skills_routes
from surogates.api.routes.skills import list_skills, read_skill_file, view_skill
from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import session_workspace_key
from surogates.tenant.context import TenantContext

TEST_AGENT_ID = "agent-under-test"


def _make_tenant() -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/no-such-asset-root",
    )


def _skill_md(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\nBody\n"


class _FakeBundle:
    """Minimal in-memory stand-in for :class:`AgentFileBundle`."""

    def __init__(self, files: dict[str, str | bytes]) -> None:
        self._files = dict(files)

    async def list(self, prefix: str = "") -> list[str]:
        return sorted(p for p in self._files if p.startswith(prefix))

    async def read_bytes(self, path: str) -> bytes:
        if path not in self._files:
            raise LookupError(path)
        data = self._files[path]
        return data.encode() if isinstance(data, str) else data

    async def read_text(self, path: str) -> str:
        return (await self.read_bytes(path)).decode()


class _FakeBundleCache:
    """Stand-in for ``app.state.file_bundle_cache`` / ``system_bundle_cache``."""

    def __init__(self, bundle: _FakeBundle) -> None:
        self._bundle = bundle

    async def get(self, agent_id: str | None = None) -> _FakeBundle:
        return self._bundle


class _EmptyResult:
    def scalars(self) -> "_EmptyResult":
        return self

    def all(self) -> list[object]:
        return []


class _EmptyDbSession:
    async def execute(self, _stmt: object) -> _EmptyResult:
        return _EmptyResult()


class _SessionFactory:
    def __call__(self) -> "_SessionFactory":
        return self

    async def __aenter__(self) -> _EmptyDbSession:
        return _EmptyDbSession()

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.mark.asyncio
async def test_api_list_skills_surfaces_bundle_attached_skill():
    """The ``/skills`` route lists per-agent bundle skills (Layer 1)."""
    bundle = _FakeBundle(
        {
            "skills/configured-skill/SKILL.md": _skill_md(
                "configured-skill", "Loaded from the per-agent bundle",
            ),
        }
    )

    request = SimpleNamespace(
        query_params={"agent_id": TEST_AGENT_ID},
        headers={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(),
                session_factory=_SessionFactory(),
                file_bundle_cache=_FakeBundleCache(bundle),
                system_bundle_cache=None,
                slug_resolver_cache=None,
            ),
        ),
    )

    response = await list_skills(
        request=request,
        tenant=_make_tenant(),
    )

    # The built-in advisor expert is always loaded, so assert on the
    # skill under test rather than an exact total.
    names = [s.name for s in response.skills]
    assert "configured-skill" in names


STORAGE_BUCKET = "agent-test"


def _staging_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bundle: _FakeBundle | None = None,
    system_bundle: _FakeBundle | None = None,
    session_config: dict | None = None,
) -> SimpleNamespace:
    """A request whose session authorizes and whose storage is local disk."""

    async def _authorized(*_args: object) -> SimpleNamespace:
        return SimpleNamespace(config=session_config or {})

    monkeypatch.setattr(
        skills_routes, "_authorize_session_for_staging", _authorized,
    )
    return SimpleNamespace(
        query_params={"agent_id": TEST_AGENT_ID},
        headers={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(
                    storage=SimpleNamespace(bucket=STORAGE_BUCKET),
                ),
                storage=LocalBackend(base_path=str(tmp_path)),
                session_factory=_SessionFactory(),
                file_bundle_cache=(
                    _FakeBundleCache(bundle) if bundle is not None else None
                ),
                system_bundle_cache=(
                    _FakeBundleCache(system_bundle)
                    if system_bundle is not None else None
                ),
                slug_resolver_cache=None,
            ),
        ),
    )


def _xlsx_system_bundle() -> _FakeBundle:
    """The shared system bundle publishes built-ins flat at the root."""
    return _FakeBundle(
        {
            "xlsx/SKILL.md": _skill_md("xlsx", "Spreadsheets"),
            "xlsx/scripts/recalc.py": "print('recalc')",
        }
    )


@pytest.mark.asyncio
async def test_view_builtin_skill_lists_and_stages_system_bundle_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A built-in's supporting files come from the system bundle root."""
    request = _staging_request(
        tmp_path, monkeypatch, system_bundle=_xlsx_system_bundle(),
    )
    session_id = uuid4()

    detail = await view_skill(
        name="xlsx", request=request, tenant=_make_tenant(),
        session_id=session_id,
    )

    assert detail.builtin is True
    assert detail.linked_files == ["scripts/recalc.py"]
    assert detail.staged_at is not None
    keys = await request.app.state.storage.list_keys(STORAGE_BUCKET, prefix="")
    assert session_workspace_key(
        session_id, ".skills/xlsx/scripts/recalc.py",
    ) in keys


@pytest.mark.asyncio
async def test_read_builtin_skill_file_from_system_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    request = _staging_request(
        tmp_path, monkeypatch, system_bundle=_xlsx_system_bundle(),
    )

    result = await read_skill_file(
        name="xlsx", path="scripts/recalc.py", request=request,
        tenant=_make_tenant(),
    )

    assert result == {
        "file_path": "scripts/recalc.py",
        "content": "print('recalc')",
        "binary": False,
    }


@pytest.mark.asyncio
async def test_read_binary_builtin_skill_file_stages_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    system_bundle = _xlsx_system_bundle()
    system_bundle._files["xlsx/assets/template.xlsx"] = b"\xff\xfe"
    request = _staging_request(tmp_path, monkeypatch, system_bundle=system_bundle)
    session_id = uuid4()

    result = await read_skill_file(
        name="xlsx", path="assets/template.xlsx", request=request,
        tenant=_make_tenant(), session_id=session_id,
    )

    assert result["staged_at"] is not None
    keys = await request.app.state.storage.list_keys(STORAGE_BUCKET, prefix="")
    assert session_workspace_key(
        session_id, ".skills/xlsx/assets/template.xlsx",
    ) in keys


@pytest.mark.asyncio
async def test_agent_bundle_skill_files_still_come_from_skills_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    bundle = _FakeBundle(
        {
            "skills/proc/SKILL.md": _skill_md("proc", "Per-agent skill"),
            "skills/proc/references/notes.md": "notes",
        }
    )
    request = _staging_request(
        tmp_path, monkeypatch,
        bundle=bundle, system_bundle=_xlsx_system_bundle(),
    )
    session_id = uuid4()

    detail = await view_skill(
        name="proc", request=request, tenant=_make_tenant(),
        session_id=session_id,
    )
    result = await read_skill_file(
        name="proc", path="references/notes.md", request=request,
        tenant=_make_tenant(),
    )

    assert detail.builtin is False
    assert detail.linked_files == ["references/notes.md"]
    assert detail.staged_at is not None
    keys = await request.app.state.storage.list_keys(STORAGE_BUCKET, prefix="")
    assert session_workspace_key(
        session_id, ".skills/proc/references/notes.md",
    ) in keys
    assert result["content"] == "notes"


ON_A_COMPUTER = {"execution": {"kind": "device", "device_id": str(uuid4())}, "workspace_path": "/home/me/notes"}


@pytest.mark.asyncio
async def test_a_local_folder_chats_skill_is_not_staged_in_the_cloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    system_bundle = _xlsx_system_bundle()
    system_bundle._files["xlsx/assets/template.xlsx"] = b"\xff\xfe"
    request = _staging_request(tmp_path, monkeypatch, system_bundle=system_bundle, session_config=ON_A_COMPUTER)

    detail = await view_skill(name="xlsx", request=request, tenant=_make_tenant(), session_id=uuid4())
    binary = await read_skill_file(
        name="xlsx", path="assets/template.xlsx", request=request, tenant=_make_tenant(), session_id=uuid4(),
    )

    # Its tool call puts them in its folder (surogates.tools.builtin.skills).
    assert detail.linked_files == ["assets/template.xlsx", "scripts/recalc.py"]
    assert detail.staged_at is None
    assert binary == {
        "file_path": "assets/template.xlsx", "content": "[Binary file]", "binary": True,
        "hint": 'Call skill_view("xlsx") to put this skill\'s files in the folder.',
    }
    assert await request.app.state.storage.list_keys(STORAGE_BUCKET, prefix="") == []


@pytest.mark.asyncio
async def test_a_skill_file_is_read_as_stored_for_a_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    system_bundle = _xlsx_system_bundle()
    system_bundle._files["xlsx/assets/template.xlsx"] = b"\xff\xfe"
    request = _staging_request(tmp_path, monkeypatch, system_bundle=system_bundle)

    raw = await read_skill_file(
        name="xlsx", path="assets/template.xlsx", request=request, tenant=_make_tenant(), raw=True,
    )

    assert raw.body == b"\xff\xfe"
    assert raw.media_type == "application/octet-stream"
