"""Regression tests for V4A patch atomicity."""

from __future__ import annotations

from pathlib import Path

from surogates.tools.builtin.file_ops import _apply_v4a_patch


def test_v4a_patch_validation_failure_leaves_all_files_unchanged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("alpha\n", encoding="utf-8")
    second.write_text("beta\n", encoding="utf-8")

    result = _apply_v4a_patch(
        """*** Begin Patch
*** Update File: first.txt
@@
-alpha
+ALPHA
*** Update File: second.txt
@@
-missing
+MISSING
*** End Patch"""
    )

    assert result["status"] == "error"
    assert "no files were modified" in " ".join(result["errors"])
    assert first.read_text(encoding="utf-8") == "alpha\n"
    assert second.read_text(encoding="utf-8") == "beta\n"
