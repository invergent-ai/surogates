"""Tests for the multi-bundle skill loader.

Layer 1 of :meth:`ResourceLoader.load_skills` is now the merge of two
optional bundles:

* the shared ``platform/system-skills`` bundle whose root IS the
  catalog (``<name>/SKILL.md``),
* the per-agent Hub bundle whose ``skills/<name>/SKILL.md`` subtree
  holds org-attached skills.

Per-agent shadows system on name collision because the per-agent bundle
is the LAST argument to ``_merge`` (last-wins-by-name semantics).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from surogates.tenant.context import TenantContext
from surogates.tools.loader import (
    SKILL_SOURCE_PLATFORM,
    ResourceLoader,
)


class _FakeBundle:
    """Minimal in-memory stand-in for :class:`AgentFileBundle`.

    Exposes the ``list(prefix)`` / ``read_text(path)`` surface the
    loader uses.  ``list`` returns sorted matches so tests are
    deterministic without relying on Hub's pagination order.
    """

    def __init__(self, files: dict[str, str]) -> None:
        self._files = dict(files)

    async def list(self, prefix: str = "") -> list[str]:
        return sorted(p for p in self._files if p.startswith(prefix))

    async def read_text(self, path: str) -> str:
        if path not in self._files:
            raise LookupError(path)
        return self._files[path]


def _skill_md(name: str, body: str) -> str:
    return (
        f"---\nname: {name}\ndescription: {body}\n---\n{body}\n"
    )


def _tenant() -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/no-such-asset-root",
    )


# ---------------------------------------------------------------------------
# _load_skills_from_bundle root_prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_skills_from_bundle_root_prefix_empty() -> None:
    """System-bundle layout: skills live at the repo root."""

    bundle = _FakeBundle(
        {
            "brainstorming/SKILL.md": _skill_md("brainstorming", "system"),
            "executing-plans/SKILL.md": _skill_md(
                "executing-plans", "system",
            ),
        }
    )
    loader = ResourceLoader()

    skills = await loader._load_skills_from_bundle(
        bundle, source=SKILL_SOURCE_PLATFORM, root_prefix="",
    )

    assert sorted(s.name for s in skills) == [
        "brainstorming",
        "executing-plans",
    ]
    assert all(s.source == SKILL_SOURCE_PLATFORM for s in skills)


@pytest.mark.asyncio
async def test_load_skills_from_bundle_root_prefix_default_is_skills() -> None:
    """Per-agent bundle layout: skills live under ``skills/``."""

    bundle = _FakeBundle(
        {
            "SOUL.md": "ignored — not under skills/",
            "agents/foo/AGENT.md": "ignored — not under skills/",
            "skills/foo/SKILL.md": _skill_md("foo", "agent-attached"),
            "skills/bar/SKILL.md": _skill_md("bar", "agent-attached"),
        }
    )
    loader = ResourceLoader()

    skills = await loader._load_skills_from_bundle(
        bundle, source=SKILL_SOURCE_PLATFORM,
    )

    assert sorted(s.name for s in skills) == ["bar", "foo"]


@pytest.mark.asyncio
async def test_load_skills_from_bundle_handles_nested_path() -> None:
    """Skill loader uses the LAST path segment when frontmatter has no
    name, so ``skills/cat/foo/SKILL.md`` should still surface as
    ``foo``."""

    bundle = _FakeBundle(
        {
            "skills/cat/foo/SKILL.md": "no frontmatter — body only\n",
        }
    )
    loader = ResourceLoader()

    skills = await loader._load_skills_from_bundle(
        bundle, source=SKILL_SOURCE_PLATFORM,
    )

    assert [s.name for s in skills] == ["foo"]


@pytest.mark.asyncio
async def test_load_skills_from_bundle_swallows_list_exception() -> None:
    """A Hub failure on ``list`` MUST NOT propagate — Layer 1 falls
    back to empty so the agent's session still boots with whatever
    higher-precedence layers can provide."""

    class _BrokenBundle:
        async def list(self, prefix: str = "") -> list[str]:
            raise RuntimeError("hub list failed")

        async def read_text(self, path: str) -> str:  # pragma: no cover
            raise AssertionError("should not be reached")

    loader = ResourceLoader()
    skills = await loader._load_skills_from_bundle(
        _BrokenBundle(), source=SKILL_SOURCE_PLATFORM, root_prefix="",
    )
    assert skills == []


@pytest.mark.asyncio
async def test_load_skills_from_bundle_skips_directory_marker() -> None:
    """Some Hub backends include the bare prefix path in ``list``
    results (e.g. ``skills/`` itself).  The loader must skip entries
    whose post-prefix component is empty."""

    bundle = _FakeBundle(
        {
            "skills/": "",
            "skills/foo/SKILL.md": _skill_md("foo", "agent-attached"),
        }
    )
    loader = ResourceLoader()

    skills = await loader._load_skills_from_bundle(
        bundle, source=SKILL_SOURCE_PLATFORM,
    )

    assert [s.name for s in skills] == ["foo"]


# ---------------------------------------------------------------------------
# load_skills(bundle=..., system_bundle=...) — Layer 1 merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_skills_merges_system_then_per_agent() -> None:
    """Layer 1 is ``_merge(system, per_agent)``.  Last-arg-wins-by-name
    semantics mean an org-attached skill with the same name as a
    system one shadows the system version — matching the spec's
    override rule (admin can shadow, cannot remove)."""

    system = _FakeBundle(
        {
            "brainstorming/SKILL.md": _skill_md("brainstorming", "system"),
            "writing-plans/SKILL.md": _skill_md("writing-plans", "system"),
        }
    )
    per_agent = _FakeBundle(
        {
            # Same name as a system skill — should win.
            "skills/brainstorming/SKILL.md": _skill_md(
                "brainstorming", "agent-override",
            ),
            # Brand-new skill — only the per-agent bundle has it.
            "skills/extra/SKILL.md": _skill_md("extra", "agent-only"),
        }
    )
    loader = ResourceLoader()

    skills = await loader.load_skills(
        _tenant(),
        db_session=None,
        bundle=per_agent,
        system_bundle=system,
    )

    by_name = {s.name: s.description for s in skills}
    assert by_name["brainstorming"] == "agent-override"
    assert by_name["writing-plans"] == "system"
    assert by_name["extra"] == "agent-only"


@pytest.mark.asyncio
async def test_load_skills_system_bundle_only() -> None:
    """Agents that have not had a per-agent bundle published yet still
    see system skills — Layer 1 collapses to the system bundle alone."""

    system = _FakeBundle(
        {"brainstorming/SKILL.md": _skill_md("brainstorming", "system")},
    )
    loader = ResourceLoader()

    skills = await loader.load_skills(
        _tenant(),
        db_session=None,
        bundle=None,
        system_bundle=system,
    )

    assert [s.name for s in skills] == ["brainstorming"]
    assert all(s.source == SKILL_SOURCE_PLATFORM for s in skills)


@pytest.mark.asyncio
async def test_builtin_flag_distinguishes_system_from_per_agent() -> None:
    """``builtin`` marks ONLY framework system skills (Layer 1a).

    Both layers share ``source="platform"``, so ``source`` cannot tell a
    framework built-in from a tenant-attached per-agent skill — the slash
    menu's "hide built-ins" must key on ``builtin`` instead.  On a name
    collision the per-agent override wins and is NOT a built-in.
    """

    system = _FakeBundle(
        {
            "brainstorming/SKILL.md": _skill_md("brainstorming", "system"),
            "writing-plans/SKILL.md": _skill_md("writing-plans", "system"),
        }
    )
    per_agent = _FakeBundle(
        {
            "skills/writing-plans/SKILL.md": _skill_md(
                "writing-plans", "agent-override",
            ),
            "skills/extra/SKILL.md": _skill_md("extra", "agent-only"),
        }
    )
    loader = ResourceLoader()

    skills = await loader.load_skills(
        _tenant(),
        db_session=None,
        bundle=per_agent,
        system_bundle=system,
    )

    by_name = {s.name: s for s in skills}
    # System-only skill: a genuine built-in.
    assert by_name["brainstorming"].builtin is True
    # Org-attached only: NOT a built-in (even though source is platform).
    assert by_name["extra"].builtin is False
    assert by_name["extra"].source == SKILL_SOURCE_PLATFORM
    # Override: per-agent shadows system, so it stops being a built-in.
    assert by_name["writing-plans"].description == "agent-override"
    assert by_name["writing-plans"].builtin is False


@pytest.mark.asyncio
async def test_load_skills_per_agent_only() -> None:
    """The flip side: no system bundle published yet means the older
    behaviour (per-agent only) is preserved verbatim."""

    per_agent = _FakeBundle(
        {"skills/foo/SKILL.md": _skill_md("foo", "agent-attached")},
    )
    loader = ResourceLoader()

    skills = await loader.load_skills(
        _tenant(),
        db_session=None,
        bundle=per_agent,
        system_bundle=None,
    )

    assert [s.name for s in skills] == ["foo"]


@pytest.mark.asyncio
async def test_load_skills_no_bundles_returns_empty() -> None:
    """Boot path: when neither bundle is wired the loader yields an
    empty Layer 1 rather than crashing.  Layers 2-4 are independent
    and exercised in ``test_loader_agents.py``."""

    loader = ResourceLoader()

    skills = await loader.load_skills(
        _tenant(),
        db_session=None,
        bundle=None,
        system_bundle=None,
    )

    assert skills == []


@pytest.mark.asyncio
async def test_load_skills_system_bundle_kwarg_is_keyword_only_default_none() -> None:
    """``system_bundle`` defaults to ``None`` so older call sites that
    only pass ``bundle=`` keep working — important for the test suite
    and any code path that hasn't been updated yet."""

    per_agent = _FakeBundle(
        {"skills/foo/SKILL.md": _skill_md("foo", "agent-attached")},
    )
    loader = ResourceLoader()

    # No ``system_bundle`` kwarg at all — must not raise and must
    # produce the same result as passing ``system_bundle=None``.
    skills = await loader.load_skills(
        _tenant(), db_session=None, bundle=per_agent,
    )

    assert [s.name for s in skills] == ["foo"]
