"""Tests for the prompt library loader, parser, and startup validator."""

from __future__ import annotations

from pathlib import Path

import pytest

from surogates.harness.prompt_library import (
    PROMPTS_ROOT,
    REQUIRED_KEYS,
    PromptFrontmatterError,
    PromptLibrary,
    PromptNotFoundError,
    _split_frontmatter,
    default_library,
)


# ---------------------------------------------------------------------------
# _split_frontmatter
# ---------------------------------------------------------------------------


class TestSplitFrontmatter:
    def test_parses_frontmatter_and_body(self) -> None:
        meta, body = _split_frontmatter(
            "---\nname: foo\ndescription: bar\n---\nhello world\n"
        )
        assert meta == {"name": "foo", "description": "bar"}
        assert body == "hello world"

    def test_no_frontmatter_returns_empty_meta(self) -> None:
        meta, body = _split_frontmatter("hello world\n")
        assert meta == {}
        assert body == "hello world"

    def test_empty_frontmatter_block_is_accepted(self) -> None:
        meta, body = _split_frontmatter("---\n---\nhi\n")
        assert meta == {}
        assert body == "hi"

    def test_crlf_line_endings_are_normalized(self) -> None:
        meta, body = _split_frontmatter(
            "---\r\nname: foo\r\n---\r\nhi\r\n"
        )
        assert meta == {"name": "foo"}
        assert body == "hi"

    def test_missing_closing_fence_raises(self) -> None:
        with pytest.raises(PromptFrontmatterError, match="closing fence"):
            _split_frontmatter("---\nname: foo\nno closing fence\n")

    def test_unparseable_yaml_raises(self) -> None:
        # Unclosed flow-style bracket is a hard YAML syntax error.
        with pytest.raises(PromptFrontmatterError, match="valid YAML"):
            _split_frontmatter("---\nkey: [unclosed\n---\nbody\n")

    def test_non_mapping_frontmatter_raises(self) -> None:
        with pytest.raises(PromptFrontmatterError, match="mapping"):
            _split_frontmatter("---\n- just\n- a\n- list\n---\nbody\n")

    def test_body_without_newline_after_opening_fence_is_plain(self) -> None:
        # Starts with "---" but isn't actually frontmatter (no newline
        # immediately after the fence).  Treat the whole thing as body.
        meta, body = _split_frontmatter("---inline content---\n")
        assert meta == {}
        assert body == "---inline content---"


# ---------------------------------------------------------------------------
# PromptLibrary
# ---------------------------------------------------------------------------


class TestPromptLibrary:
    def _write(self, root: Path, key: str, content: str) -> Path:
        path = root / f"{key}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_get_returns_body(self, tmp_path: Path) -> None:
        self._write(tmp_path, "guidance/demo", "---\nname: demo\n---\nhello\n")
        lib = PromptLibrary(root=tmp_path)
        assert lib.get("guidance/demo") == "hello"

    def test_metadata_returns_frontmatter(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "guidance/demo",
            "---\nname: demo\napplies_when: always\n---\nbody\n",
        )
        lib = PromptLibrary(root=tmp_path)
        assert lib.metadata("guidance/demo") == {
            "name": "demo",
            "applies_when": "always",
        }

    def test_get_caches_reads(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path, "guidance/demo", "---\nname: demo\n---\nfirst\n"
        )
        lib = PromptLibrary(root=tmp_path)
        assert lib.get("guidance/demo") == "first"
        # Overwrite on disk; cached value must be returned.
        path.write_text("---\nname: demo\n---\nsecond\n", encoding="utf-8")
        assert lib.get("guidance/demo") == "first"

    def test_get_missing_raises(self, tmp_path: Path) -> None:
        lib = PromptLibrary(root=tmp_path)
        with pytest.raises(PromptNotFoundError):
            lib.get("guidance/does_not_exist")

    def test_platform_hint_returns_body(self, tmp_path: Path) -> None:
        self._write(tmp_path, "platforms/demo", "hello demo\n")
        lib = PromptLibrary(root=tmp_path)
        assert lib.platform_hint("demo") == "hello demo"

    def test_platform_hint_unknown_returns_none(self, tmp_path: Path) -> None:
        lib = PromptLibrary(root=tmp_path)
        assert lib.platform_hint("nope") is None

    def test_platform_hint_empty_channel_returns_none(self, tmp_path: Path) -> None:
        lib = PromptLibrary(root=tmp_path)
        assert lib.platform_hint("") is None

    def test_platforms_iterates_all(self, tmp_path: Path) -> None:
        self._write(tmp_path, "platforms/a", "one")
        self._write(tmp_path, "platforms/b", "two")
        lib = PromptLibrary(root=tmp_path)
        assert lib.platforms() == {"a": "one", "b": "two"}


# ---------------------------------------------------------------------------
# validate() — boot-time check
# ---------------------------------------------------------------------------


class TestValidate:
    def test_default_library_validates_clean(self) -> None:
        """The bundled package fragments must always pass validation."""
        PromptLibrary().validate()

    def test_default_library_singleton_validates(self) -> None:
        """Same check via the shared process-wide singleton."""
        default_library().validate()

    def test_validate_raises_on_missing_required(self, tmp_path: Path) -> None:
        lib = PromptLibrary(root=tmp_path)
        with pytest.raises(PromptNotFoundError):
            lib.validate()

    def test_validate_raises_on_malformed_fragment(self, tmp_path: Path) -> None:
        # Create every required fragment *except* one, which is malformed.
        for key in REQUIRED_KEYS:
            path = tmp_path / f"{key}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            if key == "guidance/memory":
                # Opening fence with no close -- typical typo.
                path.write_text("---\nname: memory\nno close\n", encoding="utf-8")
            else:
                path.write_text(f"---\nname: {key}\n---\nbody\n", encoding="utf-8")

        lib = PromptLibrary(root=tmp_path)
        with pytest.raises(PromptFrontmatterError):
            lib.validate()

    def test_validate_checks_platform_hints_too(self, tmp_path: Path) -> None:
        # All required keys present and well-formed...
        for key in REQUIRED_KEYS:
            path = tmp_path / f"{key}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"---\nname: {key}\n---\nbody\n", encoding="utf-8")
        # ...but a broken platform hint must still fail validation.
        (tmp_path / "platforms").mkdir()
        (tmp_path / "platforms" / "broken.md").write_text(
            "---\nname: broken\nno closing fence\n", encoding="utf-8"
        )

        lib = PromptLibrary(root=tmp_path)
        with pytest.raises(PromptFrontmatterError):
            lib.validate()


# ---------------------------------------------------------------------------
# Bundled fragments sanity
# ---------------------------------------------------------------------------


class TestBundledFragments:
    def test_prompts_root_exists(self) -> None:
        assert PROMPTS_ROOT.is_dir()

    def test_every_required_key_resolves(self) -> None:
        lib = PromptLibrary()
        for key in REQUIRED_KEYS:
            body = lib.get(key)
            assert body, f"required fragment {key!r} has empty body"
