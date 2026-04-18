"""Tests for surogates.storage.skill_staging.SkillStager."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from surogates.storage.backend import LocalBackend
from surogates.storage.skill_staging import (
    STAGING_DIR,
    STAGING_MARKER,
    SkillStager,
    has_stageable_assets,
)


@pytest.fixture()
def backend(tmp_path: Path) -> LocalBackend:
    return LocalBackend(base_path=str(tmp_path))


@pytest.fixture()
def stager(backend: LocalBackend) -> SkillStager:
    return SkillStager(backend=backend)


# =========================================================================
# has_stageable_assets
# =========================================================================


class TestHasStageableAssets:
    def test_none(self):
        assert has_stageable_assets(None) is False

    def test_empty_dict(self):
        assert has_stageable_assets({}) is False

    def test_empty_list(self):
        assert has_stageable_assets([]) is False

    def test_dict_with_empty_values(self):
        assert has_stageable_assets({"scripts": [], "assets": []}) is False

    def test_dict_with_files(self):
        assert has_stageable_assets({"scripts": ["build.py"]}) is True

    def test_list_with_files(self):
        assert has_stageable_assets(["README.md", "scripts/build.py"]) is True


# =========================================================================
# stage_from_filesystem (platform skills)
# =========================================================================


class TestStageFromFilesystem:
    async def test_copies_all_files_preserving_layout(
        self, stager: SkillStager, tmp_path: Path, backend: LocalBackend,
    ):
        # Build a fake platform skill directory.
        skill_src = tmp_path / "src" / "pptx_builder"
        (skill_src / "scripts").mkdir(parents=True)
        (skill_src / "assets").mkdir(parents=True)
        (skill_src / "SKILL.md").write_text("---\nname: pptx_builder\n---\nbody")
        (skill_src / "scripts" / "build.py").write_text("print('hi')")
        (skill_src / "assets" / "template.pptx").write_bytes(b"\x00\x01binary")

        session_id = uuid4()
        await backend.create_bucket(f"session-{session_id}")

        staged_at = await stager.stage_from_filesystem(
            session_id=session_id,
            skill_name="pptx_builder",
            source_dir=skill_src,
        )

        # staged_at points to the LocalBackend bucket dir + .skills prefix
        assert staged_at.endswith("/.skills/pptx_builder/")
        assert f"session-{session_id}" in staged_at

        # All files are present in the session bucket.
        keys = await backend.list_keys(f"session-{session_id}", prefix=".skills/")
        assert ".skills/pptx_builder/SKILL.md" in keys
        assert ".skills/pptx_builder/scripts/build.py" in keys
        assert ".skills/pptx_builder/assets/template.pptx" in keys
        assert ".skills/pptx_builder/.staged" in keys

        # Binary content is preserved bit-for-bit.
        data = await backend.read(
            f"session-{session_id}", ".skills/pptx_builder/assets/template.pptx",
        )
        assert data == b"\x00\x01binary"

    async def test_idempotent(
        self, stager: SkillStager, tmp_path: Path, backend: LocalBackend,
    ):
        skill_src = tmp_path / "src" / "my_skill"
        skill_src.mkdir(parents=True)
        (skill_src / "SKILL.md").write_text("body")
        (skill_src / "script.py").write_text("v1")

        session_id = uuid4()
        await backend.create_bucket(f"session-{session_id}")

        first = await stager.stage_from_filesystem(
            session_id=session_id, skill_name="my_skill", source_dir=skill_src,
        )

        # Modify the source after the first stage — a second stage should
        # see the marker and short-circuit.
        (skill_src / "script.py").write_text("v2")

        second = await stager.stage_from_filesystem(
            session_id=session_id, skill_name="my_skill", source_dir=skill_src,
        )

        assert first == second
        data = await backend.read(
            f"session-{session_id}", ".skills/my_skill/script.py",
        )
        assert data == b"v1"  # still the first version

    async def test_missing_source_raises(
        self, stager: SkillStager, backend: LocalBackend,
    ):
        session_id = uuid4()
        await backend.create_bucket(f"session-{session_id}")

        with pytest.raises(FileNotFoundError):
            await stager.stage_from_filesystem(
                session_id=session_id,
                skill_name="nope",
                source_dir=Path("/nonexistent/path"),
            )


# =========================================================================
# stage_from_tenant_bucket
# =========================================================================


class TestStageFromTenantBucket:
    async def test_copies_keys_preserving_relative_paths(
        self, stager: SkillStager, backend: LocalBackend,
    ):
        tenant_bucket = "tenant-aaaaaaaa"
        await backend.create_bucket(tenant_bucket)
        src_prefix = "shared/skills/pptx_builder"
        await backend.write_text(tenant_bucket, f"{src_prefix}/SKILL.md", "body")
        await backend.write_text(tenant_bucket, f"{src_prefix}/scripts/build.py", "print('x')")
        await backend.write(tenant_bucket, f"{src_prefix}/assets/template.pptx", b"\x89PNG")

        session_id = uuid4()
        await backend.create_bucket(f"session-{session_id}")

        staged_at = await stager.stage_from_tenant_bucket(
            session_id=session_id,
            skill_name="pptx_builder",
            tenant_bucket_name=tenant_bucket,
            source_prefix=src_prefix,
        )
        assert staged_at.endswith("/.skills/pptx_builder/")

        keys = await backend.list_keys(f"session-{session_id}", prefix=".skills/")
        assert ".skills/pptx_builder/SKILL.md" in keys
        assert ".skills/pptx_builder/scripts/build.py" in keys
        assert ".skills/pptx_builder/assets/template.pptx" in keys
        assert ".skills/pptx_builder/.staged" in keys

    async def test_idempotent(self, stager: SkillStager, backend: LocalBackend):
        tenant_bucket = "tenant-bbbbbbbb"
        await backend.create_bucket(tenant_bucket)
        src_prefix = "shared/skills/stable"
        await backend.write_text(tenant_bucket, f"{src_prefix}/SKILL.md", "v1")
        await backend.write_text(tenant_bucket, f"{src_prefix}/scripts/x.py", "v1")

        session_id = uuid4()
        await backend.create_bucket(f"session-{session_id}")

        first = await stager.stage_from_tenant_bucket(
            session_id=session_id,
            skill_name="stable",
            tenant_bucket_name=tenant_bucket,
            source_prefix=src_prefix,
        )

        # Source changes after staging — marker short-circuits the copy.
        await backend.write_text(tenant_bucket, f"{src_prefix}/scripts/x.py", "v2")

        second = await stager.stage_from_tenant_bucket(
            session_id=session_id,
            skill_name="stable",
            tenant_bucket_name=tenant_bucket,
            source_prefix=src_prefix,
        )
        assert first == second
        staged = await backend.read_text(
            f"session-{session_id}", ".skills/stable/scripts/x.py",
        )
        assert staged == "v1"


# =========================================================================
# is_staged / staged_file_path
# =========================================================================


class TestIsStaged:
    async def test_false_before_staging(
        self, stager: SkillStager, backend: LocalBackend,
    ):
        session_id = uuid4()
        await backend.create_bucket(f"session-{session_id}")
        assert await stager.is_staged(session_id, "anything") is False

    async def test_true_after_staging(
        self, stager: SkillStager, tmp_path: Path, backend: LocalBackend,
    ):
        skill_src = tmp_path / "src"
        skill_src.mkdir()
        (skill_src / "SKILL.md").write_text("b")
        (skill_src / "asset.bin").write_bytes(b"x")

        session_id = uuid4()
        await backend.create_bucket(f"session-{session_id}")
        await stager.stage_from_filesystem(
            session_id=session_id, skill_name="k", source_dir=skill_src,
        )
        assert await stager.is_staged(session_id, "k") is True


class TestStagedFilePath:
    def test_joins_workspace_path_with_relative(
        self, stager: SkillStager, tmp_path: Path,
    ):
        session_id = uuid4()
        path = stager.staged_file_path(session_id, "pptx", "assets/t.pptx")
        assert path.endswith(f"/.skills/pptx/assets/t.pptx")
        assert f"session-{session_id}" in path


# =========================================================================
# Constants
# =========================================================================


def test_staging_dir_constant():
    assert STAGING_DIR == ".skills"


def test_staging_marker_constant():
    assert STAGING_MARKER == ".staged"


# =========================================================================
# API route helper integration
# =========================================================================


class _MockAppState:
    def __init__(self, storage: LocalBackend) -> None:
        self.storage = storage


class _MockApp:
    def __init__(self, storage: LocalBackend) -> None:
        self.state = _MockAppState(storage)


class _MockRequest:
    def __init__(self, storage: LocalBackend) -> None:
        self.app = _MockApp(storage)


class TestStageSkillForSessionHelper:
    """Verifies surogates.api.routes.skills._stage_skill_for_session."""

    async def test_platform_skill_is_staged(
        self, tmp_path: Path, backend: LocalBackend,
    ):
        from surogates.api.routes.skills import _stage_skill_for_session
        from surogates.tools.loader import PLATFORM_SKILLS_DIR, ResourceLoader

        # Build a platform skill tree under a temporary platform dir.
        platform_dir = tmp_path / "platform-skills"
        skill_dir = platform_dir / "pptx_builder"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: pptx_builder\n---\nbody")
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "build.py").write_text("x=1")
        (skill_dir / "assets").mkdir()
        (skill_dir / "assets" / "template.pptx").write_bytes(b"\x00\x01")

        # Patch PLATFORM_SKILLS_DIR on ResourceLoader.resolve_platform_skill_dir
        # by constructing a ResourceLoader with a custom dir.
        # _stage_skill_for_session builds its own ResourceLoader with the
        # default path — so monkey-patch PLATFORM_SKILLS_DIR.
        import surogates.tools.loader as loader_mod
        original = loader_mod.PLATFORM_SKILLS_DIR
        loader_mod.PLATFORM_SKILLS_DIR = str(platform_dir)
        try:
            session_id = uuid4()
            await backend.create_bucket(f"session-{session_id}")

            # Fake SkillDef — minimum needed fields.
            class _DummySkill:
                name = "pptx_builder"
                source = loader_mod.SKILL_SOURCE_PLATFORM

            # Tenant value is only used for user/org paths; irrelevant here
            # but must expose .org_id.
            class _DummyTenant:
                org_id = uuid4()
                user_id = uuid4()

            request = _MockRequest(backend)
            staged_at = await _stage_skill_for_session(
                request=request,  # type: ignore[arg-type]
                tenant=_DummyTenant(),  # type: ignore[arg-type]
                skill_def=_DummySkill(),
                session_id=session_id,
                linked_files=["scripts/build.py", "assets/template.pptx"],
            )

            assert staged_at is not None
            assert staged_at.endswith("/.skills/pptx_builder/")

            keys = await backend.list_keys(
                f"session-{session_id}", prefix=".skills/",
            )
            assert ".skills/pptx_builder/scripts/build.py" in keys
            assert ".skills/pptx_builder/assets/template.pptx" in keys
        finally:
            loader_mod.PLATFORM_SKILLS_DIR = original

    async def test_returns_none_when_no_stageable_assets(
        self, backend: LocalBackend,
    ):
        from surogates.api.routes.skills import _stage_skill_for_session
        from surogates.tools.loader import SKILL_SOURCE_PLATFORM

        class _DummySkill:
            name = "tiny"
            source = SKILL_SOURCE_PLATFORM

        class _DummyTenant:
            org_id = uuid4()
            user_id = uuid4()

        request = _MockRequest(backend)
        staged = await _stage_skill_for_session(
            request=request,  # type: ignore[arg-type]
            tenant=_DummyTenant(),  # type: ignore[arg-type]
            skill_def=_DummySkill(),
            session_id=uuid4(),
            linked_files=[],
        )
        assert staged is None

    async def test_preamble_content(self):
        from surogates.api.routes.skills import _staging_preamble

        preamble = _staging_preamble("pptx_builder", "/workspace/.skills/pptx_builder/")
        assert "/workspace/.skills/pptx_builder/" in preamble
        assert preamble.endswith("\n\n")
        assert "relative paths" in preamble.lower()


# =========================================================================
# Session authorization gate
# =========================================================================


class TestAuthorizeSessionForStaging:
    """Verifies ``_authorize_session_for_staging`` denies cross-tenant access.

    This is the guard that prevents any authenticated caller from pointing
    ``session_id=<someone else's session>`` at the staging endpoints and
    polluting other tenants' session buckets.
    """

    async def test_rejects_session_from_different_tenant(self):
        from uuid import UUID, uuid4

        from fastapi import HTTPException

        from surogates.api.routes.skills import _authorize_session_for_staging
        from surogates.session.store import SessionNotFoundError
        from surogates.tenant.context import TenantContext

        caller_org = UUID("00000000-0000-0000-0000-000000000001")
        other_org = UUID("00000000-0000-0000-0000-000000000009")
        session_id = uuid4()

        class _FakeSession:
            id = session_id
            org_id = other_org  # belongs to a different tenant
            agent_id = "agent-under-test"

        class _FakeStore:
            async def get_session(self, sid):
                return _FakeSession()

        class _FakeSettings:
            agent_id = "agent-under-test"

        class _State:
            session_store = _FakeStore()
            settings = _FakeSettings()

        class _App:
            state = _State()

        class _Request:
            app = _App()

        caller = TenantContext(
            org_id=caller_org,
            user_id=uuid4(),
            org_config={}, user_preferences={},
            permissions=frozenset(),
            asset_root="/tmp/doesnt-matter",
        )

        # Session exists but belongs to another org → 404 (no existence leak).
        with pytest.raises(HTTPException) as exc:
            await _authorize_session_for_staging(
                _Request(), caller, session_id,
            )
        assert exc.value.status_code == 404

    async def test_rejects_missing_session(self):
        from uuid import UUID, uuid4

        from fastapi import HTTPException

        from surogates.api.routes.skills import _authorize_session_for_staging
        from surogates.session.store import SessionNotFoundError
        from surogates.tenant.context import TenantContext

        class _RaisingStore:
            async def get_session(self, sid):
                raise SessionNotFoundError(f"missing: {sid}")

        class _FakeSettings:
            agent_id = "a"

        class _State:
            session_store = _RaisingStore()
            settings = _FakeSettings()

        class _App:
            state = _State()

        class _Request:
            app = _App()

        caller = TenantContext(
            org_id=uuid4(), user_id=uuid4(),
            org_config={}, user_preferences={},
            permissions=frozenset(),
            asset_root="/tmp/doesnt-matter",
        )
        with pytest.raises(HTTPException) as exc:
            await _authorize_session_for_staging(_Request(), caller, uuid4())
        assert exc.value.status_code == 404

    async def test_allows_session_owned_by_caller(self):
        from uuid import UUID, uuid4

        from surogates.api.routes.skills import _authorize_session_for_staging
        from surogates.tenant.context import TenantContext

        caller_org_id = UUID("00000000-0000-0000-0000-000000000011")
        session_id = uuid4()

        class _FakeSession:
            id = session_id
            org_id = caller_org_id
            agent_id = "agent-under-test"

        class _FakeStore:
            async def get_session(self, sid):
                return _FakeSession()

        class _FakeSettings:
            agent_id = "agent-under-test"

        class _State:
            session_store = _FakeStore()
            settings = _FakeSettings()

        class _App:
            state = _State()

        class _Request:
            app = _App()

        caller = TenantContext(
            org_id=caller_org_id, user_id=uuid4(),
            org_config={}, user_preferences={},
            permissions=frozenset(),
            asset_root="/tmp/doesnt-matter",
        )

        # Should not raise.
        await _authorize_session_for_staging(_Request(), caller, session_id)


# =========================================================================
# Concurrency — racing callers collapse onto a single copy
# =========================================================================


class TestConcurrentStaging:
    """Two concurrent ``stage_*`` calls for the same skill must produce one
    copy operation, not two, even without Redis.

    The in-process ``asyncio.Lock`` fallback is what we validate here;
    the Redis path uses the same ``_stage_lock`` context manager and is
    covered by the lock-key test below.
    """

    async def test_concurrent_filesystem_stage_copies_once(
        self, tmp_path: Path,
    ):
        """Two concurrent stage_from_filesystem calls → one copy.

        We wrap the backend's ``write`` so we can count how many times
        the body of the staging loop executes.  With double-checked
        locking only one caller should copy; the other should find the
        marker and short-circuit.
        """
        backend = LocalBackend(base_path=str(tmp_path / "storage"))
        stager = SkillStager(backend=backend)

        skill_src = tmp_path / "src" / "concurrent_skill"
        skill_src.mkdir(parents=True)
        (skill_src / "SKILL.md").write_text("body")
        (skill_src / "scripts").mkdir()
        (skill_src / "scripts" / "a.py").write_text("a=1")

        session_id = uuid4()
        await backend.create_bucket(f"session-{session_id}")

        # Count writes that look like content writes (ignore the marker
        # which both winners and losers would never produce twice because
        # the loser short-circuits before the marker write).
        content_writes = 0
        original_write = backend.write

        async def counting_write(bucket, key, data):
            nonlocal content_writes
            if ".skills/concurrent_skill/" in key and not key.endswith(STAGING_MARKER):
                content_writes += 1
            await original_write(bucket, key, data)

        backend.write = counting_write  # type: ignore[method-assign]

        # Launch two concurrent stage operations.
        import asyncio as _asyncio
        results = await _asyncio.gather(
            stager.stage_from_filesystem(session_id, "concurrent_skill", skill_src),
            stager.stage_from_filesystem(session_id, "concurrent_skill", skill_src),
        )

        # Both calls return the same staged_at path.
        assert results[0] == results[1]

        # Only one caller performed the copy — content files were written
        # exactly once, not twice.
        expected_files = 2  # SKILL.md + scripts/a.py
        assert content_writes == expected_files, (
            f"Expected {expected_files} content writes (single copy), "
            f"got {content_writes} — concurrent callers both wrote"
        )

    async def test_concurrent_tenant_bucket_stage_copies_once(
        self, tmp_path: Path,
    ):
        """Same as above, but for ``stage_from_tenant_bucket``."""
        backend = LocalBackend(base_path=str(tmp_path / "storage"))
        stager = SkillStager(backend=backend)

        tenant = "tenant-xyz"
        await backend.create_bucket(tenant)
        await backend.write_text(tenant, "shared/skills/s/SKILL.md", "body")
        await backend.write_text(tenant, "shared/skills/s/scripts/a.py", "x=1")

        session_id = uuid4()
        await backend.create_bucket(f"session-{session_id}")

        content_writes = 0
        original_write = backend.write

        async def counting_write(bucket, key, data):
            nonlocal content_writes
            if bucket == f"session-{session_id}" and not key.endswith(STAGING_MARKER):
                content_writes += 1
            await original_write(bucket, key, data)

        backend.write = counting_write  # type: ignore[method-assign]

        import asyncio as _asyncio
        results = await _asyncio.gather(
            stager.stage_from_tenant_bucket(
                session_id, "s", tenant, "shared/skills/s",
            ),
            stager.stage_from_tenant_bucket(
                session_id, "s", tenant, "shared/skills/s",
            ),
        )
        assert results[0] == results[1]
        assert content_writes == 2  # SKILL.md + scripts/a.py, copied once


class TestStageLockKey:
    """Lock-key formatting — the contract that serialises per (session, skill)."""

    def test_lock_key_is_scoped_by_session_and_skill(self):
        session_id = UUID("00000000-0000-0000-0000-000000000042")
        key1 = SkillStager._lock_key(session_id, "pptx")
        key2 = SkillStager._lock_key(session_id, "email")
        key3 = SkillStager._lock_key(uuid4(), "pptx")
        # Same session, different skills → different keys (parallel ok).
        assert key1 != key2
        # Different session, same skill → different keys (parallel ok).
        assert key1 != key3
        # Key includes the prefix, session, and skill name.
        assert "surogates:skill-stage:" in key1
        assert str(session_id) in key1
        assert "pptx" in key1


class TestRedisLockIsUsedWhenProvided:
    """When a Redis client is supplied, the Redis lock code path runs.

    We use a minimal fake Redis that records ``lock()`` calls; the fake's
    lock object implements ``acquire`` / ``release`` as no-ops.  This
    asserts the code path is taken without needing a real Redis.
    """

    async def test_redis_lock_called_with_correct_key(self, tmp_path: Path):
        backend = LocalBackend(base_path=str(tmp_path / "storage"))

        recorded_keys: list[str] = []

        class _FakeLock:
            async def acquire(self):
                return True

            async def release(self):
                return None

        class _FakeRedis:
            def lock(self, key, **kwargs):
                recorded_keys.append(key)
                return _FakeLock()

        stager = SkillStager(backend=backend, redis=_FakeRedis())

        skill_src = tmp_path / "src" / "k"
        skill_src.mkdir(parents=True)
        (skill_src / "SKILL.md").write_text("b")
        (skill_src / "a.py").write_text("a")

        session_id = uuid4()
        await backend.create_bucket(f"session-{session_id}")

        await stager.stage_from_filesystem(session_id, "k", skill_src)

        assert len(recorded_keys) == 1
        assert recorded_keys[0] == f"surogates:skill-stage:{session_id}:k"
