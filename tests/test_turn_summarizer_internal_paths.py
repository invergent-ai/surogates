"""Runtime cache files must not appear as turn-summary deliverables."""

import pytest

from surogates.harness.turn_summarizer import _is_internal_workspace_path


@pytest.mark.parametrize(
    "path",
    [
        "__pycache__/make_stiri_pdf.cpython-312.pyc",
        "reports/__pycache__/make_stiri_pdf.cpython-312.pyc",
        "reports/__pycache__/cached-output.json",
        "make_stiri_pdf.pyc",
        "reports/make_stiri_pdf.pyo",
        "reports/MAKE_STIRI_PDF.PYC",
    ],
)
def test_python_runtime_files_are_internal(path: str) -> None:
    assert _is_internal_workspace_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "stirile-zilei-hotnews-2026-09-10.pdf",
        "reports/stirile-zilei-hotnews-2026-09-10.pdf",
        "make_stiri_pdf.py",
        "_posts/report.md",
        "_config.yml",
        "__init__.py",
        "__pycache__-guide.md",
        "reports/bytecode.pyc.md",
    ],
)
def test_user_outputs_and_underscore_files_remain_candidates(path: str) -> None:
    assert not _is_internal_workspace_path(path)


@pytest.mark.parametrize(
    "path",
    [
        ".cache/report.pdf",
        "reports/.cache/report.pdf",
        "uploads/input.pdf",
        "_artifacts/report/v1.json",
        "_whiteboard/canvas.json",
    ],
)
def test_existing_internal_paths_remain_excluded(path: str) -> None:
    assert _is_internal_workspace_path(path)
