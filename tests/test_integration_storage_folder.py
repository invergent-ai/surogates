"""The storage folder the integration suite's conftest makes for a run.

Each run used to leave its ``surogates-tenant-assets-*`` folder in the
temp folder, one for the controller and one for each worker, and a test
host ran out of file slots under them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _run_with_conftest(tmp_path: Path, **env: str) -> str:
    """Import the conftest in a process of its own whose temp folder is
    *tmp_path*, as a pytest run imports it, and end that process: the
    storage folder the run used."""
    inherited = {
        name: value
        for name, value in os.environ.items()
        if name != "SUROGATES_STORAGE_BASE_PATH"
    }
    run = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, tests.integration.conftest; "
            "print(os.environ['SUROGATES_STORAGE_BASE_PATH'])",
        ],
        cwd=_REPO,
        env={**inherited, "TMPDIR": str(tmp_path), **env},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    return run.stdout.strip().splitlines()[-1]


def test_a_run_removes_the_storage_folder_it_made(tmp_path: Path) -> None:
    used = Path(_run_with_conftest(tmp_path))

    assert used.parent == tmp_path
    assert used.name.startswith("surogates-tenant-assets-")
    assert list(tmp_path.iterdir()) == []


def test_a_run_makes_no_folder_and_removes_none_where_one_is_named(
    tmp_path: Path,
) -> None:
    named = tmp_path / "named"
    named.mkdir()

    used = _run_with_conftest(tmp_path, SUROGATES_STORAGE_BASE_PATH=str(named))

    assert used == str(named)
    assert list(tmp_path.iterdir()) == [named]
