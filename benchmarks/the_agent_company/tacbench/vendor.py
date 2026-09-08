"""Locate and verify the pinned TheAgentCompany checkout.

The upstream repo carries the 175 task definitions (``task.md``,
``checkpoints.md``, ``evaluator.py`` per task) and the service-stack
setup under ``servers/``. ``PIN`` holds the audited upstream commit;
evaluators at any other commit grade differently, so a mismatch is
refused.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

PIN_FILE = pathlib.Path(__file__).parent.parent / "PIN"
DEFAULT_HOME = pathlib.Path(__file__).parent.parent / "vendor" / "TheAgentCompany"


def home() -> pathlib.Path:
    root = pathlib.Path(os.environ.get("TAC_HOME") or DEFAULT_HOME)
    if not (root / "workspaces" / "tasks").is_dir():
        raise SystemExit(
            f"TheAgentCompany checkout not found at {root} -- clone it "
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
            f"TheAgentCompany checkout is at {commit[:12]} but PIN requires "
            f"{pinned[:12]} -- run `git -C {root} checkout {pinned}` or "
            "update PIN deliberately."
        )
    return commit
