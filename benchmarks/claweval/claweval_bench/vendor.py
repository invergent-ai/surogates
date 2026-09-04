"""Locate and verify the pinned claw-eval checkout.

The upstream benchmark (tasks, mock services, graders) is MIT-licensed but
large, so it is never vendored into this repo. It lives in a local clone --
``vendor/claw-eval`` next to this package by default, or wherever
``CLAWEVAL_HOME`` points -- installed editable into the benchmark venv so
``import claw_eval`` resolves. ``PIN`` holds the audited upstream commit;
a checkout on any other commit is refused rather than silently graded with
different rubrics.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

PIN_FILE = pathlib.Path(__file__).parent.parent / "PIN"
DEFAULT_HOME = pathlib.Path(__file__).parent.parent / "vendor" / "claw-eval"


def home() -> pathlib.Path:
    root = pathlib.Path(os.environ.get("CLAWEVAL_HOME") or DEFAULT_HOME)
    if not (root / "tasks").is_dir():
        raise SystemExit(
            f"claw-eval checkout not found at {root} -- clone it and install "
            "it into this venv first (see README, Setup)."
        )
    return root


def tasks_dir() -> pathlib.Path:
    return home() / "tasks"


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
            f"claw-eval checkout is at {commit[:12]} but PIN requires "
            f"{pinned[:12]} -- run `git -C {root} checkout {pinned}` or "
            "update PIN deliberately."
        )
    return commit
