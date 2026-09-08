"""Locate and verify the pinned agents-last-exam checkout.

The upstream repo carries the ~150 public task definitions (task cards
and grader scripts); the task *data* (inputs + references) is a
separate gated HuggingFace archive extracted into ``task-data/`` (or
wherever ``ALE_DATA_DIR`` points). ``PIN`` holds the audited upstream
commit; graders at any other commit score differently, so a mismatch is
refused.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

PIN_FILE = pathlib.Path(__file__).parent.parent / "PIN"
DEFAULT_HOME = pathlib.Path(__file__).parent.parent / "vendor" / "agents-last-exam"
DEFAULT_DATA = pathlib.Path(__file__).parent.parent / "task-data"


def home() -> pathlib.Path:
    root = pathlib.Path(os.environ.get("ALE_HOME") or DEFAULT_HOME)
    if not (root / "tasks").is_dir():
        raise SystemExit(
            f"agents-last-exam checkout not found at {root} -- clone it "
            "first (see README, Setup)."
        )
    return root


def data_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("ALE_DATA_DIR") or DEFAULT_DATA)


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
            f"agents-last-exam checkout is at {commit[:12]} but PIN requires "
            f"{pinned[:12]} -- run `git -C {root} checkout {pinned}` or "
            "update PIN deliberately."
        )
    return commit
