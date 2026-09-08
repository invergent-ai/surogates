"""The benchmark must never import the product packages."""
import pathlib
import re

PACKAGE_DIR = pathlib.Path(__file__).parent.parent / "eogbench"


def test_no_product_imports():
    offenders = []
    for path in PACKAGE_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.match(r"\s*(import|from)\s+(surogate_ops|surogates)\b", line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], "forbidden product imports:\n" + "\n".join(offenders)


def test_package_importable():
    import eogbench  # noqa: F401
