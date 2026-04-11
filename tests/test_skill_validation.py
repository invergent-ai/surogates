"""Tests for surogates.tools.builtin.skill_validation."""

from __future__ import annotations

from surogates.tools.builtin.skill_validation import (
    validate_name,
    validate_category,
    validate_frontmatter,
    validate_content_size,
    validate_file_path,
    validate_file_size,
)


class TestValidateName:
    def test_valid(self):
        assert validate_name("my-skill") is None
        assert validate_name("my_skill.v2") is None
        assert validate_name("a") is None

    def test_empty(self):
        assert validate_name("") is not None

    def test_too_long(self):
        assert validate_name("x" * 65) is not None

    def test_invalid_chars(self):
        assert validate_name("My Skill") is not None
        assert validate_name("my/skill") is not None

    def test_starts_with_hyphen(self):
        assert validate_name("-skill") is not None


class TestValidateCategory:
    def test_valid(self):
        assert validate_category("devops") is None
        assert validate_category("data-science") is None

    def test_none_ok(self):
        assert validate_category(None) is None

    def test_empty_ok(self):
        assert validate_category("") is None

    def test_slashes_rejected(self):
        assert validate_category("a/b") is not None


class TestValidateFrontmatter:
    def test_valid(self):
        content = "---\nname: test\ndescription: A test\n---\n\n# Content here"
        assert validate_frontmatter(content) is None

    def test_empty(self):
        assert validate_frontmatter("") is not None

    def test_no_frontmatter(self):
        assert validate_frontmatter("# Just markdown") is not None

    def test_unclosed_frontmatter(self):
        assert validate_frontmatter("---\nname: test\n") is not None

    def test_missing_name(self):
        content = "---\ndescription: A test\n---\n\n# Content"
        assert validate_frontmatter(content) is not None

    def test_missing_description(self):
        content = "---\nname: test\n---\n\n# Content"
        assert validate_frontmatter(content) is not None

    def test_no_body(self):
        content = "---\nname: test\ndescription: A test\n---\n"
        assert validate_frontmatter(content) is not None


class TestValidateContentSize:
    def test_within_limit(self):
        assert validate_content_size("x" * 100) is None

    def test_exceeds_limit(self):
        assert validate_content_size("x" * 100_001) is not None


class TestValidateFilePath:
    def test_valid(self):
        assert validate_file_path("references/api.md") is None
        assert validate_file_path("templates/config.yaml") is None
        assert validate_file_path("scripts/run.sh") is None
        assert validate_file_path("assets/logo.png") is None

    def test_empty(self):
        assert validate_file_path("") is not None

    def test_traversal(self):
        assert validate_file_path("../escape.txt") is not None

    def test_wrong_subdir(self):
        assert validate_file_path("src/code.py") is not None

    def test_just_directory(self):
        assert validate_file_path("references") is not None


class TestValidateFileSize:
    def test_within_limit(self):
        assert validate_file_size("x" * 1000) is None

    def test_exceeds_limit(self):
        assert validate_file_size("x" * 1_048_577) is not None
