"""Write-safety tests for the patch tool's replace mode.

Every case here is a way ``_apply_replace`` could persist bytes the caller
never asked to change.  The tool reports success on write, so a corruption
that lints clean is invisible to the agent -- these tests are the only thing
standing between a near-miss ``old_string`` and a silently rewritten file.
"""

from __future__ import annotations

from pathlib import Path

from surogates.tools.builtin.file_ops import _apply_replace


def _write(tmp_path: Path, content: str) -> str:
    target = tmp_path / "sample.py"
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    return str(target)


def test_empty_old_string_is_rejected(tmp_path: Path) -> None:
    """``content.replace("", x)`` interleaves x between every character."""
    original = "class A:\n    x = 1\n"
    path = _write(tmp_path, original)

    result = _apply_replace(path, "", "Z", replace_all=True)

    assert "error" in result
    assert Path(path).read_text(encoding="utf-8") == original


def test_whitespace_only_old_string_is_rejected(tmp_path: Path) -> None:
    """A bare indent matches every indented line in the file."""
    original = "class A:\n    x = 1\n    y = 2\n"
    path = _write(tmp_path, original)

    result = _apply_replace(path, "    ", "\t", replace_all=True)

    assert "error" in result
    assert Path(path).read_text(encoding="utf-8") == original


def test_identical_old_and_new_string_is_rejected(tmp_path: Path) -> None:
    """A no-op edit currently reports a fabricated whitespace-change diff."""
    original = "class A:\n    x = 1\n"
    path = _write(tmp_path, original)

    result = _apply_replace(path, "x = 1", "x = 1", replace_all=False)

    assert "error" in result
    assert "diff" not in result


# --- Scoping: a fuzzy match must only ever rewrite the lines it matched ---
#
# Each case below differs from the file by whitespace alone, so it misses the
# exact-match branch and reaches the fuzzy path.  The edit must land, and every
# byte outside the matched window must survive untouched.


def test_whitespace_run_mismatch_does_not_flatten_the_file(tmp_path: Path) -> None:
    original = (
        "class A:\n"
        "    FOO   = 1\n"
        "    BARBAZ = 2\n"
        "    def m(self):\n"
        "        return self.FOO\n"
    )
    path = _write(tmp_path, original)

    result = _apply_replace(
        path, "    FOO = 1\n    BARBAZ = 2", "    FOO = 9\n    BARBAZ = 2",
        replace_all=False,
    )

    assert result.get("status") == "ok", result
    assert Path(path).read_text(encoding="utf-8") == (
        "class A:\n"
        "    FOO = 9\n"
        "    BARBAZ = 2\n"
        "    def m(self):\n"
        "        return self.FOO\n"
    )


def test_indent_mismatch_does_not_dedent_the_file(tmp_path: Path) -> None:
    original = "if 1:\n    def f():\n        a = 1\n        b = 2\n"
    path = _write(tmp_path, original)

    # Model supplies the block dedented to column 0; the file has it at 4/8.
    result = _apply_replace(
        path, "def f():\n    a = 1", "    def f():\n        a = 9",
        replace_all=False,
    )

    assert result.get("status") == "ok", result
    assert Path(path).read_text(encoding="utf-8") == (
        "if 1:\n    def f():\n        a = 9\n        b = 2\n"
    )


def test_crlf_file_keeps_its_line_endings(tmp_path: Path) -> None:
    original = "alpha = 1\r\nbeta  = 2\r\ngamma = 3\r\n"
    path = _write(tmp_path, original)

    result = _apply_replace(path, "beta = 2", "beta = 9", replace_all=False)

    assert result.get("status") == "ok", result
    with open(path, encoding="utf-8", newline="") as fh:
        written = fh.read()
    assert written == "alpha = 1\r\nbeta = 9\r\ngamma = 3\r\n"


def test_tab_indented_file_keeps_its_tabs(tmp_path: Path) -> None:
    original = "func main() {\n\tx := 1\n\ty := 2\n}\n"
    path = _write(tmp_path, original)

    result = _apply_replace(path, "    x := 1", "\tx := 9", replace_all=False)

    assert result.get("status") == "ok", result
    assert Path(path).read_text(encoding="utf-8") == (
        "func main() {\n\tx := 9\n\ty := 2\n}\n"
    )


def test_trailing_whitespace_elsewhere_survives(tmp_path: Path) -> None:
    original = "keep = 1   \ntarget  = 2\nalso = 3\t\n"
    path = _write(tmp_path, original)

    result = _apply_replace(path, "target = 2", "target = 9", replace_all=False)

    assert result.get("status") == "ok", result
    assert Path(path).read_text(encoding="utf-8") == (
        "keep = 1   \ntarget = 9\nalso = 3\t\n"
    )


def test_case_mismatch_is_rejected_rather_than_guessed(tmp_path: Path) -> None:
    """Case-insensitive matching edits a different symbol than the one asked for."""
    original = "Foo = 1\nfoo = 2\n"
    path = _write(tmp_path, original)

    result = _apply_replace(path, "FOO = 1", "FOO = 9", replace_all=False)

    assert "error" in result
    assert Path(path).read_text(encoding="utf-8") == original


def test_ambiguous_fuzzy_match_is_rejected(tmp_path: Path) -> None:
    # Neither occurrence matches exactly, and both normalize to the target.
    original = "a = 1\nx  = 0\nb = 2\nx   = 0\n"
    path = _write(tmp_path, original)

    result = _apply_replace(path, "x = 0", "x = 5", replace_all=False)

    assert "error" in result
    assert Path(path).read_text(encoding="utf-8") == original


def test_exact_match_path_is_unchanged(tmp_path: Path) -> None:
    """The overwhelmingly common path must behave exactly as before."""
    original = "alpha = 1\nbeta = 2\n"
    path = _write(tmp_path, original)

    result = _apply_replace(path, "beta = 2", "beta = 9", replace_all=False)

    assert result.get("status") == "ok", result
    assert Path(path).read_text(encoding="utf-8") == "alpha = 1\nbeta = 9\n"


def test_replace_all_stays_exact_match_only(tmp_path: Path) -> None:
    """replace_all over a fuzzy match cannot know how many it would hit."""
    original = "x  = 0\ny = 1\nx = 0\n"
    path = _write(tmp_path, original)

    result = _apply_replace(path, "x = 0", "x = 5", replace_all=True)

    # Exact match exists once; only that occurrence changes.
    assert result.get("status") == "ok", result
    assert Path(path).read_text(encoding="utf-8") == "x  = 0\ny = 1\nx = 5\n"

