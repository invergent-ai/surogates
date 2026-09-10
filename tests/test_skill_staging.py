"""Skill files stage consistently under concurrent requests."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from surogates.storage.backend import LocalBackend
from surogates.storage.skill_staging import (
    STAGING_MARKER,
    SkillStager,
)
from surogates.storage.tenant import session_workspace_key


STORAGE_BUCKET = "agent-test"


@pytest.fixture()
def backend(tmp_path: Path) -> LocalBackend:
    return LocalBackend(base_path=str(tmp_path))


@pytest.fixture()
def stager(backend: LocalBackend) -> SkillStager:
    return SkillStager(backend=backend, storage_bucket=STORAGE_BUCKET)


class TestStageFromObjectStore:
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
        await backend.create_bucket(STORAGE_BUCKET)

        staged_at = await stager.stage_from_object_store(
            session_id=session_id,
            skill_name="pptx_builder",
            source_bucket=tenant_bucket,
            source_prefix=src_prefix,
        )
        assert staged_at.endswith("/.skills/pptx_builder/")

        keys = await backend.list_keys(
            STORAGE_BUCKET, prefix=f"{session_id}/.skills/",
        )
        assert session_workspace_key(session_id, ".skills/pptx_builder/SKILL.md") in keys
        assert session_workspace_key(session_id, ".skills/pptx_builder/scripts/build.py") in keys
        assert session_workspace_key(session_id, ".skills/pptx_builder/assets/template.pptx") in keys
        assert session_workspace_key(session_id, ".skills/pptx_builder/.staged") in keys

    async def test_idempotent(self, stager: SkillStager, backend: LocalBackend):
        tenant_bucket = "tenant-bbbbbbbb"
        await backend.create_bucket(tenant_bucket)
        src_prefix = "shared/skills/stable"
        await backend.write_text(tenant_bucket, f"{src_prefix}/SKILL.md", "v1")
        await backend.write_text(tenant_bucket, f"{src_prefix}/scripts/x.py", "v1")

        session_id = uuid4()
        await backend.create_bucket(STORAGE_BUCKET)

        first = await stager.stage_from_object_store(
            session_id=session_id,
            skill_name="stable",
            source_bucket=tenant_bucket,
            source_prefix=src_prefix,
        )

        # Source changes after staging — marker short-circuits the copy.
        await backend.write_text(tenant_bucket, f"{src_prefix}/scripts/x.py", "v2")

        second = await stager.stage_from_object_store(
            session_id=session_id,
            skill_name="stable",
            source_bucket=tenant_bucket,
            source_prefix=src_prefix,
        )
        assert first == second
        staged = await backend.read_text(
            STORAGE_BUCKET,
            session_workspace_key(session_id, ".skills/stable/scripts/x.py"),
        )
        assert staged == "v1"


class TestConcurrentStaging:
    """Concurrent requests for the same skill copy each source file once."""

    async def test_concurrent_tenant_bucket_stage_copies_once(
        self, tmp_path: Path,
    ):
        backend = LocalBackend(base_path=str(tmp_path / "storage"))
        stager = SkillStager(backend=backend, storage_bucket=STORAGE_BUCKET)

        tenant = "tenant-xyz"
        await backend.create_bucket(tenant)
        await backend.write_text(tenant, "shared/skills/s/SKILL.md", "body")
        await backend.write_text(tenant, "shared/skills/s/scripts/a.py", "x=1")

        session_id = uuid4()
        await backend.create_bucket(STORAGE_BUCKET)

        content_writes = 0
        original_write = backend.write

        async def counting_write(bucket, key, data):
            nonlocal content_writes
            if bucket == STORAGE_BUCKET and not key.endswith(STAGING_MARKER):
                content_writes += 1
            await original_write(bucket, key, data)

        backend.write = counting_write  # type: ignore[method-assign]

        import asyncio as _asyncio
        results = await _asyncio.gather(
            stager.stage_from_object_store(
                session_id, "s", tenant, "shared/skills/s",
            ),
            stager.stage_from_object_store(
                session_id, "s", tenant, "shared/skills/s",
            ),
        )
        assert results[0] == results[1]
        assert content_writes == 2  # SKILL.md + scripts/a.py, copied once
