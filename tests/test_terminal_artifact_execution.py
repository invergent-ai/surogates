"""Only executing a workspace file should classify it as a generator script."""

import pytest

from surogates.harness.loop_artifacts import _terminal_executes_file


@pytest.mark.parametrize(
    ("command", "path"),
    [
        ("python3 make_stiri_pdf.py", "make_stiri_pdf.py"),
        ("/usr/bin/python3.12 -u -B ./make.py", "make.py"),
        ("python -W ignore -X utf8 make.py output.pdf", "make.py"),
        ("python -- 'scripts/my report.py'", "scripts/my report.py"),
        ('python "scripts/my"\' report.py\'', "scripts/my report.py"),
        ("bash -eu script.sh", "script.sh"),
        ("bash -o pipefail script.sh", "script.sh"),
        ("./scripts/run.sh input.pdf > result.pdf", "scripts/run.sh"),
        ("node 'make report.js' result.pdf", "make report.js"),
        ("python make.py > output.pdf && echo done", "make.py"),
        ("python 2>/dev/null make.py", "make.py"),
        ("echo ready; python make.py | head -12", "make.py"),
        ("echo ready\npython make.py", "make.py"),
        ("LANG=C python3 make.py", "make.py"),
        ("echo ready # python ignored.py\npython make.py", "make.py"),
    ],
)
def test_recognizes_script_execution(command: str, path: str) -> None:
    assert _terminal_executes_file(command, path)


@pytest.mark.parametrize(
    ("command", "path"),
    [
        ("pdfinfo report.pdf", "report.pdf"),
        ("pdftotext -layout report.pdf - | head", "report.pdf"),
        ("python make.py report.pdf", "report.pdf"),
        ("python make.py --output report.pdf", "report.pdf"),
        ("python make.py > report.pdf", "report.pdf"),
        ("python >report.pdf make.py", "report.pdf"),
        ("python make.py 2>report.pdf", "report.pdf"),
        ("python make.py 2>&1", "1"),
        ("python -c 'print(\"report.pdf\")' report.pdf", "report.pdf"),
        ("python -c '&&' ./script.sh", "script.sh"),
        ("bash -c './script.sh' script.sh", "script.sh"),
        ("bash -n script.sh", "script.sh"),
        ("bash -o noexec script.sh", "script.sh"),
        ("python -m py_compile make.py", "make.py"),
        ("python -unknown make.py", "make.py"),
        ("python remake.py", "make.py"),
        ("python subdir/make.py", "make.py"),
        ("python make.py.backup", "make.py"),
        ('python "make".py', "make"),
        ("echo ';' ./script.sh", "script.sh"),
        ("echo ready # python make.py", "make.py"),
        ("cat <<EOF\npython make.py\nEOF", "make.py"),
        ("cat <<< 'python make.py'", "make.py"),
        ("echo $(python make.py)", "make.py"),
        ("echo `python make.py`", "make.py"),
        ("cd subdir && python make.py", "make.py"),
        ("source change_directory.sh; python make.py", "make.py"),
        ("eval 'cd subdir'; python make.py", "make.py"),
        ("if true; then python make.py; fi", "make.py"),
        ("(python make.py)", "make.py"),
        ("python 'make.py", "make.py"),
        ("python make.py >", "make.py"),
        ("python make.py", ""),
        ("", "make.py"),
        ("echo " + "x" * 65536 + "; python make.py", "make.py"),
    ],
)
def test_keeps_arguments_and_unsupported_syntax_as_deliverables(command: str, path: str) -> None:
    assert not _terminal_executes_file(command, path)


def test_production_pdf_inspection_does_not_mark_pdf_as_executed() -> None:
    command = (
        "python3 make_stiri_pdf.py && "
        "pdfinfo stirile-zilei-hotnews-2026-09-10.pdf 2>/dev/null | head -12; "
        "pdftotext -layout stirile-zilei-hotnews-2026-09-10.pdf - | "
        'grep -n "Diverse" -A3'
    )
    assert _terminal_executes_file(command, "make_stiri_pdf.py")
    assert not _terminal_executes_file(command, "stirile-zilei-hotnews-2026-09-10.pdf")
