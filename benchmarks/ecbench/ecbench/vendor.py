"""Locate and verify the pinned E-CommerceBench checkout.

The upstream benchmark (environment engine, tools, world data) is
Apache-2.0 but lives in its own repo, so it is never vendored into this
tree. It sits in a local clone -- ``vendor/E-CommerceBench`` next to this
package by default, or wherever ``ECBENCH_HOME`` points. ``PIN`` holds
the audited upstream commit; a checkout on any other commit is refused
rather than silently simulated with a different world.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

PIN_FILE = pathlib.Path(__file__).parent.parent / "PIN"
DEFAULT_HOME = pathlib.Path(__file__).parent.parent / "vendor" / "E-CommerceBench"


def home() -> pathlib.Path:
    root = pathlib.Path(os.environ.get("ECBENCH_HOME") or DEFAULT_HOME)
    if not (root / "tools").is_dir() or not (root / "data").is_dir():
        raise SystemExit(
            f"E-CommerceBench checkout not found at {root} -- clone it "
            "first (see README, Setup)."
        )
    return root


def verify_pin() -> str:
    """Return the checkout's commit, refusing a commit other than PIN."""
    root = home()
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    pinned = PIN_FILE.read_text().strip() if PIN_FILE.exists() else ""
    if pinned and commit != pinned:
        raise SystemExit(
            f"E-CommerceBench checkout is at {commit[:12]} but PIN requires "
            f"{pinned[:12]} -- run `git -C {root} checkout {pinned}` or "
            "update PIN deliberately."
        )
    return commit
