"""The ``guidance/browser`` fragment is gated on the presence of any
``browser_*`` tool.  When "Live browser support" is off the worker drops
those tools from the session's effective set, so the guidance must drop
out of the system prompt too (no point telling the agent how to drive a
browser it can't reach)."""

from __future__ import annotations

from uuid import UUID

from surogates.harness.prompt import PromptBuilder, default_library
from surogates.tenant import TenantContext


def _tenant() -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test",
    )


class _Session:
    model = "gpt-4o"
    config: dict = {}


def _guidance(tools: set[str]) -> str:
    pb = PromptBuilder(_tenant(), available_tools=tools, session=_Session())
    return pb._tool_guidance_section()


def test_browser_guidance_present_when_browser_tools_available():
    frag = default_library().get("guidance/browser")
    assert frag in _guidance({"browser_navigate", "browser_click"})


def test_browser_guidance_absent_when_browser_tools_removed():
    frag = default_library().get("guidance/browser")
    # No browser_* tools (browser support disabled) ⇒ no browser guidance.
    assert frag not in _guidance({"web_search"})
