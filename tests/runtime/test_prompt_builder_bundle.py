"""Tests for PromptBuilder threading bundle content through.

Plan 3 / Task 12.  PromptBuilder takes pre-loaded soul_md_content /
agent_md_content strings (not the bundle itself) so build() stays
sync.  harness_factory does the async pre-load via load_soul_md /
load_agent_md before constructing the builder.

This design avoids cascading async through PromptBuilder.build()
and every callsite while still routing all SOUL.md / AGENT.md reads
through the per-session, Hub-backed bundle.
"""

from __future__ import annotations

import inspect


def test_prompt_builder_init_accepts_bundle_content_kwargs():
    from surogates.harness.prompt import PromptBuilder

    sig = inspect.signature(PromptBuilder.__init__)
    assert "soul_md_content" in sig.parameters
    assert "agent_md_content" in sig.parameters


def test_prompt_builder_uses_preloaded_soul_when_provided():
    """Source-level regression: _context_files_section reads from
    self._soul_md_content (the pre-loaded bundle content) before
    falling back to the legacy disk path."""
    import surogates.harness.prompt as p

    src = inspect.getsource(p)
    assert "self._soul_md_content" in src
    assert "self._agent_md_content" in src


def test_prompt_builder_falls_back_to_disk_when_content_none():
    """Helm-mode and bundle-less workers pass None; the builder
    falls back to load_soul_md_from_disk so the legacy code path
    keeps working until Plan 9 retires it."""
    import surogates.harness.prompt as p

    src = inspect.getsource(p)
    # The disk fallback must remain in the section builder.
    assert "load_soul_md_from_disk" in src
