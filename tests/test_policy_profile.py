"""Tests for sub-agent governance policy profiles.

Covers:
- :meth:`ResourceLoader.load_policy_profile` — file discovery and merge
- :meth:`GovernanceGate.with_profile` — composition (narrow allowed, union
  denied, egress overlay), freeze on return, non-mutation of base gate
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from surogates.governance.policy import GovernanceGate
from surogates.tenant.context import TenantContext
from surogates.tools.loader import ResourceLoader


def _make_tenant(asset_root: str) -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root=asset_root,
    )


def _loader(tmp_path: Path, platform_agents: Path | None = None) -> ResourceLoader:
    return ResourceLoader(
        platform_skills_dir=str(tmp_path / "skills"),
        platform_mcp_dir=str(tmp_path / "mcp"),
        platform_agents_dir=(
            str(platform_agents) if platform_agents is not None
            else str(tmp_path / "platform_agents_unused")
        ),
    )


# =========================================================================
# ResourceLoader.load_policy_profile
# =========================================================================


class TestLoadPolicyProfile:

    def test_loads_platform_yaml_profile(self, tmp_path: Path):
        platform_agents = tmp_path / "platform_agents"
        policies_dir = platform_agents / "policies"
        policies_dir.mkdir(parents=True)
        (policies_dir / "read_only.yaml").write_text(
            "allowed_tools:\n  - read_file\n  - search_files\n"
            "denied_tools:\n  - write_file\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path, platform_agents=platform_agents)
        tenant = _make_tenant(str(tmp_path / "assets"))

        profile = loader.load_policy_profile(tenant, "read_only")
        assert profile is not None
        assert profile["allowed_tools"] == ["read_file", "search_files"]
        assert profile["denied_tools"] == ["write_file"]

    def test_loads_json_profile(self, tmp_path: Path):
        platform_agents = tmp_path / "platform_agents"
        policies_dir = platform_agents / "policies"
        policies_dir.mkdir(parents=True)
        (policies_dir / "strict.json").write_text(
            json.dumps({
                "allowed_tools": ["read_file"],
                "denied_tools": ["terminal", "write_file"],
            }),
            encoding="utf-8",
        )

        loader = _loader(tmp_path, platform_agents=platform_agents)
        tenant = _make_tenant(str(tmp_path / "assets"))
        profile = loader.load_policy_profile(tenant, "strict")
        assert profile is not None
        assert "terminal" in profile["denied_tools"]

    def test_returns_none_when_missing(self, tmp_path: Path):
        loader = _loader(tmp_path)
        tenant = _make_tenant(str(tmp_path / "assets"))
        assert loader.load_policy_profile(tenant, "nope") is None

    def test_org_overlay_merges_with_platform(self, tmp_path: Path):
        platform_agents = tmp_path / "platform_agents"
        (platform_agents / "policies").mkdir(parents=True)
        (platform_agents / "policies" / "shared.yaml").write_text(
            "allowed_tools:\n  - platform_tool\n"
            "denied_tools:\n  - banned_by_platform\n",
            encoding="utf-8",
        )

        org_id = "00000000-0000-0000-0000-000000000001"
        org_policies = (
            tmp_path / "assets" / org_id / "shared" / "agents" / "policies"
        )
        org_policies.mkdir(parents=True)
        (org_policies / "shared.yaml").write_text(
            "allowed_tools:\n  - org_tool\n"
            "denied_tools:\n  - banned_by_org\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path, platform_agents=platform_agents)
        tenant = _make_tenant(str(tmp_path / "assets"))

        profile = loader.load_policy_profile(tenant, "shared")
        assert profile is not None
        assert set(profile["allowed_tools"]) == {"platform_tool", "org_tool"}
        assert set(profile["denied_tools"]) == {
            "banned_by_platform", "banned_by_org",
        }

    def test_egress_rules_union(self, tmp_path: Path):
        platform_agents = tmp_path / "platform_agents"
        (platform_agents / "policies").mkdir(parents=True)
        (platform_agents / "policies" / "p.yaml").write_text(
            "egress:\n"
            "  default_action: deny\n"
            "  rules:\n"
            "    - {domain: 'a.example', action: allow}\n",
            encoding="utf-8",
        )
        org_id = "00000000-0000-0000-0000-000000000001"
        org_policies = (
            tmp_path / "assets" / org_id / "shared" / "agents" / "policies"
        )
        org_policies.mkdir(parents=True)
        (org_policies / "p.yaml").write_text(
            "egress:\n"
            "  rules:\n"
            "    - {domain: 'b.example', action: allow}\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path, platform_agents=platform_agents)
        tenant = _make_tenant(str(tmp_path / "assets"))
        profile = loader.load_policy_profile(tenant, "p")
        assert profile is not None
        domains = {r["domain"] for r in profile["egress"]["rules"]}
        assert domains == {"a.example", "b.example"}
        assert profile["egress"]["default_action"] == "deny"


# =========================================================================
# GovernanceGate.with_profile — composition semantics
# =========================================================================


class TestGovernanceGateWithProfile:

    def test_profile_narrows_base_allowlist(self):
        base = GovernanceGate(
            allowed_tools={"read_file", "write_file", "terminal", "search_files"},
        )
        composed = base.with_profile({"allowed_tools": ["read_file", "search_files"]})

        assert composed.check("read_file").allowed is True
        assert composed.check("search_files").allowed is True
        # Narrowed out: these were in base but not in profile.
        assert composed.check("write_file").allowed is False
        assert composed.check("terminal").allowed is False

    def test_profile_cannot_widen_base_allowlist(self):
        """Intersection semantics: profile can never add tools beyond base."""
        base = GovernanceGate(allowed_tools={"read_file"})
        composed = base.with_profile({"allowed_tools": ["read_file", "write_file"]})

        # Base had only read_file; profile tried to add write_file — ignored.
        assert composed.check("read_file").allowed is True
        assert composed.check("write_file").allowed is False

    def test_profile_denied_is_union(self):
        base = GovernanceGate(denied_tools={"base_deny"})
        composed = base.with_profile({"denied_tools": ["profile_deny"]})

        assert composed.check("base_deny").allowed is False
        assert composed.check("profile_deny").allowed is False
        # Everything else still allowed under open policy.
        assert composed.check("random_tool").allowed is True

    def test_profile_denied_overrides_profile_allowed(self):
        """When a tool appears in both profile allowed and base denied, it's denied."""
        base = GovernanceGate(
            allowed_tools={"read_file", "write_file"},
            denied_tools={"write_file"},
        )
        composed = base.with_profile({"allowed_tools": ["read_file", "write_file"]})

        assert composed.check("read_file").allowed is True
        # Even though profile says allowed, base denied wins (union semantics).
        assert composed.check("write_file").allowed is False

    def test_profile_only_no_base_allowlist(self):
        """With open base, profile allowlist becomes the effective allowlist."""
        base = GovernanceGate()  # open policy
        composed = base.with_profile({"allowed_tools": ["read_file"]})

        assert composed.check("read_file").allowed is True
        assert composed.check("write_file").allowed is False

    def test_composed_gate_is_frozen(self):
        base = GovernanceGate(allowed_tools={"read_file"})
        composed = base.with_profile({"allowed_tools": ["read_file"]})
        assert composed.is_frozen is True

    def test_base_gate_is_not_mutated(self):
        base = GovernanceGate(allowed_tools={"read_file", "write_file"})
        assert base.is_frozen is False
        composed = base.with_profile({"allowed_tools": ["read_file"]})

        # Base still has the original allowlist and is not frozen.
        assert base.is_frozen is False
        assert base.check("write_file").allowed is True
        # Composed applied the narrowing.
        assert composed.check("write_file").allowed is False

    def test_disabled_base_stays_disabled(self):
        """Profile cannot re-enable a disabled base gate."""
        base = GovernanceGate(
            allowed_tools={"read_file"}, enabled=False,
        )
        composed = base.with_profile({"allowed_tools": ["read_file"]})
        # Disabled gate allows everything — but the profile didn't change that.
        d = composed.check("anything")
        assert d.allowed is True
        assert "disabled" in d.reason

    def test_empty_profile_preserves_base_semantics(self):
        base = GovernanceGate(allowed_tools={"read_file", "write_file"})
        composed = base.with_profile({})

        assert composed.check("read_file").allowed is True
        assert composed.check("write_file").allowed is True
        assert composed.check("terminal").allowed is False


class TestStrictestEgressDefault:
    """``_strictest_egress_default`` picks the stricter of two options.

    ``deny`` always wins under narrowing semantics so a profile can
    never loosen the base's default action; absent / misspelled values
    fall back to ``deny``.
    """

    def test_deny_beats_allow_from_base(self):
        from surogates.governance.policy import _strictest_egress_default
        assert _strictest_egress_default("deny", "allow") == "deny"

    def test_deny_beats_allow_from_profile(self):
        from surogates.governance.policy import _strictest_egress_default
        assert _strictest_egress_default("allow", "deny") == "deny"

    def test_both_allow_stays_allow(self):
        from surogates.governance.policy import _strictest_egress_default
        assert _strictest_egress_default("allow", "allow") == "allow"

    def test_missing_defaults_to_deny(self):
        from surogates.governance.policy import _strictest_egress_default
        assert _strictest_egress_default(None, None) == "deny"

    def test_misspelled_default_falls_back_to_deny(self):
        """A profile with a bogus value cannot accidentally widen."""
        from surogates.governance.policy import _strictest_egress_default
        assert _strictest_egress_default("allow", "anything") == "deny"
