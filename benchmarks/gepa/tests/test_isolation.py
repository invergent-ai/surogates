"""The optimiser drives the harness, it does not link against it.

Same rule as the GAIA benchmark next door, for the same reason: the tree
under measurement must not be importable into the thing measuring it. The
optimiser needs to write a prompt file and start a process, which is
filesystem and subprocess work, not an import.
"""
import pathlib
import re

FORBIDDEN = ("surogate_ops", "surogates")
PACKAGE_DIR = pathlib.Path(__file__).parent.parent / "promptgepa"


def test_no_product_imports():
    offenders = []
    for path in PACKAGE_DIR.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"\s*(import|from)\s+(surogate_ops|surogates)\b", line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], "forbidden product imports:\n" + "\n".join(offenders)


def test_package_importable():
    import promptgepa.evaluate  # noqa: F401
    import promptgepa.harness  # noqa: F401
    import promptgepa.optimize  # noqa: F401
    import promptgepa.sets  # noqa: F401
