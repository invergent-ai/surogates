"""Every data file the package reads at runtime must ship in the wheel.

The ops server installs this package as a wheel and runs its migrations,
which read ``surogates/db/observability.sql`` from the installed
location. A ``.sql`` file present in the source tree but absent from
``[tool.setuptools.package-data]`` passes every test here and then fails
the ops server's first start in production with FileNotFoundError.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "surogates"


def _package_data_globs() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["setuptools"]["package-data"]["surogates"]


def test_every_sql_file_is_shipped():
    globs = _package_data_globs()
    sql_files = sorted(p.relative_to(PACKAGE).as_posix() for p in PACKAGE.rglob("*.sql"))
    assert sql_files, "expected at least one .sql data file under surogates/"
    missing = [f for f in sql_files if not any(fnmatch.fnmatch(f, g) for g in globs)]
    assert missing == [], f"not covered by package-data: {missing}"
