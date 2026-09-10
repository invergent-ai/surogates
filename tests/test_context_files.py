"""Project context loading from real filesystem layouts and repository hierarchies."""

from __future__ import annotations

from pathlib import Path


from surogates.harness.context_files import (
    load_project_context,
)


class TestLoadProjectContext:

    def test_agents_md_priority_over_claude_md(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text("AGENTS rules")
        (tmp_path / "CLAUDE.md").write_text("CLAUDE rules")
        result = load_project_context(str(tmp_path))
        assert "AGENTS rules" in result


    def test_walks_up_to_git_root(self, tmp_path: Path):
        # Create a git root with AGENTS.md.
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("Root instructions")
        subdir = tmp_path / "src" / "app"
        subdir.mkdir(parents=True)
        result = load_project_context(str(subdir))
        assert result is not None
        assert "Root instructions" in result
