"""The benchmark must never import the product packages.

Same rule as every benchmark in this tree: it is a client of the harness
HTTP API, and an in-process import would bypass the API, the session
store, the sandbox mount and the tool router -- most of what is being
measured.
"""
import pathlib
import re

PACKAGE_DIR = pathlib.Path(__file__).parent.parent / "dabbench"


def test_no_product_imports():
    offenders = []
    for path in PACKAGE_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.match(r"\s*(import|from)\s+(surogate_ops|surogates)\b", line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], "forbidden product imports:\n" + "\n".join(offenders)


def test_package_importable():
    import dabbench  # noqa: F401
