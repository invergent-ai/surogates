"""Locate and verify the pinned EnterpriseOps-Gym checkout.

The upstream benchmark (task JSONs, gym MCP client, SQL verifier
engine) lives in a local clone -- ``vendor/EnterpriseOps-Gym`` next to
this package by default, or wherever ``EOG_HOME`` points. ``PIN`` holds
the audited upstream commit; a checkout on any other commit is refused
rather than silently verified with different SQL.

The vendored code is used via ``pythonpath()`` (sys.path insertion),
never installed: only ``benchmark/mcp_client.py`` and
``benchmark/verifier.py`` are imported, and installing upstream's full
package would drag in its provider and ray extras.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

PIN_FILE = pathlib.Path(__file__).parent.parent / "PIN"
DEFAULT_HOME = pathlib.Path(__file__).parent.parent / "vendor" / "EnterpriseOps-Gym"


def home() -> pathlib.Path:
    root = pathlib.Path(os.environ.get("EOG_HOME") or DEFAULT_HOME)
    if not (root / "benchmark").is_dir() or not (root / "data").is_dir():
        raise SystemExit(
            f"EnterpriseOps-Gym checkout not found at {root} -- clone it "
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
            f"EnterpriseOps-Gym checkout is at {commit[:12]} but PIN requires "
            f"{pinned[:12]} -- run `git -C {root} checkout {pinned}` or "
            "update PIN deliberately."
        )
    return commit


def pythonpath() -> pathlib.Path:
    """Make ``benchmark.*`` importable from the vendored checkout."""
    root = home()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root
