"""Tests for AgentFileBundle.

Frozen handle on (agent_id, hub_ref, version)
with a small read-only surface the harness's prompt builder and
skill loader call.

The bundle is constructed by the FileBundleCache;
callers never instantiate it directly.  Frozen so a careless
harness mutation can't swap the underlying ref mid-session.
"""

from __future__ import annotations

import dataclasses

import pytest


class _FakeHubClient:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def read_bytes(self, ref, path):
        if path not in self.files:
            raise LookupError(path)
        return self.files[path]

    async def list_paths(self, ref, *, prefix=""):
        return sorted(
            p for p in self.files if p.startswith(prefix)
        )


def test_agent_file_bundle_is_frozen():
    from surogates.runtime.bundle_accessor import AgentFileBundle

    assert dataclasses.is_dataclass(AgentFileBundle)
    assert AgentFileBundle.__dataclass_params__.frozen is True


@pytest.mark.asyncio
async def test_agent_file_bundle_read_text_strips_utf8_bom():
    """A surprising number of legacy SOUL.md files in this codebase
    have a UTF-8 BOM (notepad/PowerShell saves them that way).  The
    bundle accessor strips it so downstream prompt rendering
    doesn't get a stray U+FEFF character."""
    from surogates.runtime.bundle_accessor import AgentFileBundle

    client = _FakeHubClient()
    client.files["SOUL.md"] = b"\xef\xbb\xbf# soul"
    bundle = AgentFileBundle(
        agent_id="a-1", hub_ref="acme/agents", version="v1",
        client=client,
    )
    assert await bundle.read_text("SOUL.md") == "# soul"


@pytest.mark.asyncio
async def test_agent_file_bundle_list_returns_paths_under_prefix():
    from surogates.runtime.bundle_accessor import AgentFileBundle

    client = _FakeHubClient()
    client.files = {
        "SOUL.md": b"",
        "skills/foo/SKILL.md": b"",
        "skills/bar/SKILL.md": b"",
    }
    bundle = AgentFileBundle(
        agent_id="a-1", hub_ref="acme/agents", version="v1",
        client=client,
    )
    assert await bundle.list("skills/") == [
        "skills/bar/SKILL.md", "skills/foo/SKILL.md",
    ]


def test_bundle_spec_parses_owner_repo():
    from surogates.runtime.bundle_accessor import _BundleSpec

    spec = _BundleSpec.parse("acme/agent-bundles")
    assert spec.user == "acme"
    assert spec.repository == "agent-bundles"


def test_bundle_spec_rejects_missing_slash():
    from surogates.runtime.bundle_accessor import _BundleSpec

    with pytest.raises(ValueError, match="owner/repo"):
        _BundleSpec.parse("just-a-name")


def test_bundle_spec_rejects_empty_segments():
    from surogates.runtime.bundle_accessor import _BundleSpec

    with pytest.raises(ValueError):
        _BundleSpec.parse("/repo")
    with pytest.raises(ValueError):
        _BundleSpec.parse("owner/")
