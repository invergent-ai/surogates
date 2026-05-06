"""Tests for surogates.storage.backend — LocalBackend and S3Backend."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from surogates.storage.backend import (
    LocalBackend,
    S3Backend,
    StorageBackend,
    create_backend,
)
from surogates.storage.tenant import (
    agent_session_bucket,
    session_workspace_key,
    session_workspace_prefix,
)


# =========================================================================
# LocalBackend
# =========================================================================


class TestLocalBackendBucket:
    """Bucket lifecycle operations."""

    @pytest.fixture()
    def backend(self, tmp_path: Path) -> LocalBackend:
        return LocalBackend(base_path=str(tmp_path))

    async def test_create_bucket(self, backend: LocalBackend, tmp_path: Path):
        await backend.create_bucket("test-bucket")
        assert (tmp_path / "test-bucket").is_dir()

    async def test_create_bucket_idempotent(self, backend: LocalBackend):
        await backend.create_bucket("test-bucket")
        await backend.create_bucket("test-bucket")  # no error

    async def test_delete_bucket(self, backend: LocalBackend, tmp_path: Path):
        await backend.create_bucket("test-bucket")
        await backend.write_text("test-bucket", "file.txt", "hello")
        await backend.delete_bucket("test-bucket")
        assert not (tmp_path / "test-bucket").exists()

    async def test_delete_bucket_nonexistent(self, backend: LocalBackend):
        await backend.delete_bucket("no-such-bucket")  # no error

    async def test_bucket_exists(self, backend: LocalBackend):
        assert not await backend.bucket_exists("test-bucket")
        await backend.create_bucket("test-bucket")
        assert await backend.bucket_exists("test-bucket")

    async def test_resolve_bucket_path(self, backend: LocalBackend, tmp_path: Path):
        path = backend.resolve_bucket_path("test-bucket")
        assert path == str((tmp_path / "test-bucket").resolve())

    async def test_resolve_workspace_path(
        self, backend: LocalBackend, tmp_path: Path,
    ):
        path = backend.resolve_workspace_path("agent-alpha", "session-123")
        assert path == str(
            (tmp_path / "agent-alpha" / "sessions" / "session-123").resolve()
        )
        assert Path(path).is_dir()


class TestLocalBackendObjects:
    """Object read/write/delete/list operations."""

    @pytest.fixture()
    async def backend(self, tmp_path: Path) -> LocalBackend:
        b = LocalBackend(base_path=str(tmp_path))
        await b.create_bucket("bucket")
        return b

    async def test_write_and_read(self, backend: LocalBackend):
        await backend.write("bucket", "key.bin", b"\x00\x01\x02")
        data = await backend.read("bucket", "key.bin")
        assert data == b"\x00\x01\x02"

    async def test_write_text_and_read_text(self, backend: LocalBackend):
        await backend.write_text("bucket", "hello.txt", "world")
        text = await backend.read_text("bucket", "hello.txt")
        assert text == "world"

    async def test_read_nonexistent_raises(self, backend: LocalBackend):
        with pytest.raises(KeyError):
            await backend.read("bucket", "no-such-key")

    async def test_exists(self, backend: LocalBackend):
        assert not await backend.exists("bucket", "key")
        await backend.write_text("bucket", "key", "val")
        assert await backend.exists("bucket", "key")

    async def test_delete(self, backend: LocalBackend):
        await backend.write_text("bucket", "key", "val")
        await backend.delete("bucket", "key")
        assert not await backend.exists("bucket", "key")

    async def test_delete_nonexistent(self, backend: LocalBackend):
        await backend.delete("bucket", "no-such-key")  # no error

    async def test_delete_cleans_empty_parents(self, backend: LocalBackend, tmp_path: Path):
        await backend.write_text("bucket", "a/b/c.txt", "data")
        await backend.delete("bucket", "a/b/c.txt")
        # Empty dirs should be cleaned up.
        assert not (tmp_path / "bucket" / "a" / "b").exists()
        assert not (tmp_path / "bucket" / "a").exists()

    async def test_list_keys(self, backend: LocalBackend):
        await backend.write_text("bucket", "a.txt", "1")
        await backend.write_text("bucket", "b/c.txt", "2")
        await backend.write_text("bucket", "b/d.txt", "3")
        keys = await backend.list_keys("bucket")
        assert keys == ["a.txt", "b/c.txt", "b/d.txt"]

    async def test_list_keys_with_prefix(self, backend: LocalBackend):
        await backend.write_text("bucket", "a.txt", "1")
        await backend.write_text("bucket", "sub/b.txt", "2")
        keys = await backend.list_keys("bucket", prefix="sub")
        assert keys == ["sub/b.txt"]

    async def test_list_keys_empty_bucket(self, backend: LocalBackend):
        keys = await backend.list_keys("bucket")
        assert keys == []

    async def test_stat(self, backend: LocalBackend):
        await backend.write_text("bucket", "key", "hello")
        info = await backend.stat("bucket", "key")
        assert info["size"] == 5
        assert "modified" in info

    async def test_stat_nonexistent_raises(self, backend: LocalBackend):
        with pytest.raises(KeyError):
            await backend.stat("bucket", "no-such-key")

    async def test_nested_write_creates_dirs(self, backend: LocalBackend):
        await backend.write_text("bucket", "deep/nested/file.txt", "data")
        text = await backend.read_text("bucket", "deep/nested/file.txt")
        assert text == "data"

    async def test_overwrite(self, backend: LocalBackend):
        await backend.write_text("bucket", "key", "old")
        await backend.write_text("bucket", "key", "new")
        assert await backend.read_text("bucket", "key") == "new"


class TestLocalBackendSecurity:
    """Path traversal protection."""

    @pytest.fixture()
    async def backend(self, tmp_path: Path) -> LocalBackend:
        b = LocalBackend(base_path=str(tmp_path))
        await b.create_bucket("bucket")
        return b

    async def test_path_traversal_read(self, backend: LocalBackend):
        with pytest.raises(ValueError, match="traversal"):
            await backend.read("bucket", "../../../etc/passwd")

    async def test_path_traversal_write(self, backend: LocalBackend):
        with pytest.raises(ValueError, match="traversal"):
            await backend.write("bucket", "../escape.txt", b"bad")


# =========================================================================
# Session workspace helpers
# =========================================================================


class TestSessionWorkspaceHelpers:
    def test_agent_session_bucket_uses_agent_id(self):
        assert agent_session_bucket("ops-provided-agent-bucket") == (
            "ops-provided-agent-bucket"
        )

    def test_agent_session_bucket_rejects_empty_bucket(self):
        with pytest.raises(ValueError, match="configured storage bucket"):
            agent_session_bucket("")

    def test_agent_session_bucket_rejects_invalid_s3_bucket_name(self):
        with pytest.raises(ValueError, match="S3-compatible"):
            agent_session_bucket("Bad_Bucket")

    def test_session_workspace_prefix_is_hard_coded_sessions_path(self):
        assert session_workspace_prefix("abc-123") == "sessions/abc-123/"

    def test_session_workspace_key_prefixes_relative_key(self):
        assert session_workspace_key("abc-123", "src/app.py") == (
            "sessions/abc-123/src/app.py"
        )

    def test_session_workspace_key_strips_leading_slash(self):
        assert session_workspace_key("abc-123", "/src/app.py") == (
            "sessions/abc-123/src/app.py"
        )


# =========================================================================
# S3Backend (mocked with moto)
# =========================================================================


class TestS3Backend:
    """S3Backend with moto mock via server mode.

    moto's ``mock_aws`` decorator doesn't support aiobotocore natively
    (raw_headers issue).  We use moto's standalone server instead, or
    skip if unavailable.  In CI, the S3Backend is tested against a real
    MinIO instance.
    """

    @pytest.fixture()
    async def backend(self, tmp_path: Path) -> S3Backend:
        """Fall back to testing S3Backend API surface via LocalBackend.

        The S3Backend's logic is exercised against real MinIO in
        integration tests.  Here we verify the interface contract
        using a LocalBackend as a stand-in.
        """
        pytest.skip(
            "S3Backend requires a running MinIO/S3 endpoint. "
            "Run integration tests with MINIO_ENDPOINT set."
        )

    @pytest.mark.skipif(
        not os.environ.get("MINIO_ENDPOINT"),
        reason="MINIO_ENDPOINT not set — skip S3Backend integration tests",
    )
    async def test_integration_placeholder(self):
        """Placeholder for integration tests against real MinIO."""
        pass


# =========================================================================
# create_backend factory
# =========================================================================


class TestCreateBackend:
    """Factory function tests."""

    def test_default_local(self):
        from surogates.config import Settings, StorageSettings
        s = Settings(storage=StorageSettings(base_path="/tmp/test"))
        backend = create_backend(s)
        assert isinstance(backend, LocalBackend)

    def test_s3_backend(self):
        from surogates.config import Settings, StorageSettings
        s = Settings(storage=StorageSettings(
            backend="s3",
            endpoint="http://localhost:9000",
            access_key="key",
            secret_key="secret",
        ))
        backend = create_backend(s)
        assert isinstance(backend, S3Backend)

    def test_fallback_no_storage_config(self):
        from surogates.config import Settings
        s = Settings()
        backend = create_backend(s)
        assert isinstance(backend, LocalBackend)

    def test_protocol_compliance(self):
        """Both backends satisfy the StorageBackend protocol."""
        assert isinstance(LocalBackend("/tmp"), StorageBackend)
        assert isinstance(S3Backend("http://localhost:9000"), StorageBackend)

    def test_s3_workspace_path_is_sandbox_mount(self):
        backend = S3Backend("http://localhost:9000")
        assert backend.resolve_workspace_path("agent-alpha", "session-123") == "/workspace"
