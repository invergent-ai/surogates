"""Skill catalogs merge system and per-agent bundle resources."""

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

    assert [s.name for s in skills if not s.is_expert] == ["brainstorming"]
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

    assert [s.name for s in skills if not s.is_expert] == ["foo"]
