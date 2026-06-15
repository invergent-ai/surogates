"""Tests for the session-scoped skill override layer in the loader."""

from __future__ import annotations

from surogates.tools.loader import (
    ResourceLoader,
    SkillDef,
    SKILL_SOURCE_PLATFORM,
)


def _skill(name, content, source=SKILL_SOURCE_PLATFORM, **kw):
    return SkillDef(name=name, description=f"{name} desc", content=content,
                    source=source, **kw)


def test_apply_overrides_patches_content_preserving_source():
    base = [
        _skill("browser-research", "ORIGINAL", category="web", trigger="orig"),
        _skill("other", "KEEP"),
    ]
    overrides = {
        "browser-research": {
            "content": "CANDIDATE",
            "description": "new desc",
            "trigger": "new trigger",
        }
    }
    out = {s.name: s for s in ResourceLoader._apply_overrides(base, overrides)}

    patched = out["browser-research"]
    assert patched.content == "CANDIDATE"
    assert patched.description == "new desc"
    assert patched.trigger == "new trigger"
    # source + category preserved so staging still resolves original files.
    assert patched.source == SKILL_SOURCE_PLATFORM
    assert patched.category == "web"
    # unrelated skills untouched.
    assert out["other"].content == "KEEP"


def test_apply_overrides_partial_fields_fall_back_to_base():
    base = [_skill("s", "ORIGINAL", trigger="orig-trigger")]
    overrides = {"s": {"content": "CANDIDATE"}}  # no description/trigger
    out = ResourceLoader._apply_overrides(base, overrides)[0]
    assert out.content == "CANDIDATE"
    assert out.description == "s desc"           # fell back to base
    assert out.trigger == "orig-trigger"          # fell back to base


def test_apply_overrides_ignores_when_base_missing():
    base = [_skill("present", "X")]
    overrides = {"ghost": {"content": "NEW", "description": "d", "trigger": "t"}}
    out = {s.name: s for s in ResourceLoader._apply_overrides(base, overrides)}
    assert "ghost" not in out
    assert out["present"].content == "X"


def test_apply_overrides_empty_is_noop():
    base = [_skill("s", "X")]
    assert ResourceLoader._apply_overrides(base, {}) == base
    assert ResourceLoader._apply_overrides(base, None) == base


def test_apply_overrides_skips_empty_content():
    base = [_skill("s", "ORIGINAL")]
    out = ResourceLoader._apply_overrides(base, {"s": {"content": ""}})
    # An empty candidate body is defensive-skipped: keep the original.
    assert out[0].content == "ORIGINAL"
