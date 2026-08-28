"""The benchmark must never import the product packages.

It now lives inside the surogates repo, so ``import surogates`` would resolve
against the very tree under measurement -- which is exactly what must not
happen. The benchmark is a client: it exercises the harness over its HTTP API,
the same surface real callers use. An in-process import would bypass the API,
the session store and the tool router, i.e. most of what is being measured,
and would silently turn a harness benchmark into a model benchmark.
"""
import pathlib
import re

FORBIDDEN = ("surogate_ops", "surogates")
PACKAGE_DIR = pathlib.Path(__file__).parent.parent / "gaia_bench"


def test_no_product_imports():
    offenders = []
    for path in PACKAGE_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.match(r"\s*(import|from)\s+(surogate_ops|surogates)\b", line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], "forbidden product imports:\n" + "\n".join(offenders)


def test_package_importable():
    import gaia_bench  # noqa: F401
