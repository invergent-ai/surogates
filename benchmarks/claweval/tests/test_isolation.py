"""The benchmark is a client: it must never import the platform.

Measuring the harness through the same HTTP surface real callers use is
the only way the numbers mean anything. (Importing the vendored
``claw_eval`` package is fine -- that is the benchmark, not the platform.)
"""
import pathlib

FORBIDDEN = ("import surogates", "from surogates", "import surogate_ops",
             "from surogate_ops")

SRC = pathlib.Path(__file__).parent.parent / "claweval_bench"


def test_no_platform_imports():
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, offenders
