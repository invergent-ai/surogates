"""Tests for HubBundleClient — async wrapper over the Surogate Hub SDK.

Plan 3 / Task 3.  Thin adapter that exposes read_bytes(path) +
list_paths(prefix) + aclose() so the FileBundleCache loader and the
AgentFileBundle don't depend on the SDK's auto-generated shape
directly.

Tests use a fake ObjectsApi so they don't need a real Hub.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class _FakeStat:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakeObjectsApi:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple] = []

    def get_object(self, user, repository, ref, path):
        self.calls.append(("get", user, repository, ref, path))
        if path not in self.objects:
            raise FileNotFoundError(path)
        return bytearray(self.objects[path])

    def list_objects(self, user, repository, ref, prefix=None):
        self.calls.append(("list", user, repository, ref, prefix))
        keys = [
            k for k in self.objects
            if prefix is None or k.startswith(prefix)
        ]
        result = MagicMock()
        result.results = [_FakeStat(k) for k in sorted(keys)]
        return result


@pytest.mark.asyncio
async def test_hub_bundle_client_read_bytes():
    from surogates.runtime.hub_client import HubBundleClient

    objects = _FakeObjectsApi()
    objects.objects["SOUL.md"] = b"# soul\nhello"
    client = HubBundleClient(
        objects_api=objects, user="acme", repository="agents",
    )
    try:
        data = await client.read_bytes("v1.0.0", "SOUL.md")
        assert data == b"# soul\nhello"
        assert objects.calls == [
            ("get", "acme", "agents", "v1.0.0", "SOUL.md"),
        ]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_hub_bundle_client_read_bytes_missing_raises_lookup_error():
    """Missing-path → LookupError so the AgentFileBundle and the
    cache can distinguish 'file does not exist in this bundle' from
    a network error.  LookupError is the same shape RuntimeConfigCache
    uses for 'agent not configured', so the runtime error taxonomy
    stays consistent."""
    from surogates.runtime.hub_client import HubBundleClient

    objects = _FakeObjectsApi()  # empty
    client = HubBundleClient(
        objects_api=objects, user="acme", repository="agents",
    )
    try:
        with pytest.raises(LookupError):
            await client.read_bytes("v1.0.0", "SOUL.md")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_hub_bundle_client_list_paths_with_prefix():
    from surogates.runtime.hub_client import HubBundleClient

    objects = _FakeObjectsApi()
    objects.objects = {
        "SOUL.md": b"",
        "skills/foo/SKILL.md": b"",
        "skills/bar/SKILL.md": b"",
        "agents/baz/AGENT.md": b"",
    }
    client = HubBundleClient(
        objects_api=objects, user="acme", repository="agents",
    )
    try:
        skills = await client.list_paths("v1.0.0", prefix="skills/")
        assert sorted(skills) == [
            "skills/bar/SKILL.md", "skills/foo/SKILL.md",
        ]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_hub_bundle_client_list_paths_empty_prefix_returns_all():
    from surogates.runtime.hub_client import HubBundleClient

    objects = _FakeObjectsApi()
    objects.objects = {"SOUL.md": b"", "skills/foo/SKILL.md": b""}
    client = HubBundleClient(
        objects_api=objects, user="acme", repository="agents",
    )
    try:
        all_paths = await client.list_paths("v1.0.0", prefix="")
        assert sorted(all_paths) == ["SOUL.md", "skills/foo/SKILL.md"]
    finally:
        await client.aclose()
