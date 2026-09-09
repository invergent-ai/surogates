"""Materialize committed code and bounded proposals without editing the checkout."""
from __future__ import annotations

import ast
import difflib
import io
import shutil
import subprocess
import tarfile
from pathlib import Path

from harness_evolution.config import digest, relative_file


def snapshot(repository: Path, revision: str, destination: Path) -> str:
    commit = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "--verify", f"{revision}^{{commit}}"], text=True,
    ).strip()
    # Only product source is available in the runtime snapshot. Benchmarks,
    # answer keys, local configs, credentials and uncommitted work stay out.
    archive = subprocess.check_output(
        ["git", "-C", str(repository), "archive", commit, "surogates", "pyproject.toml"],
    )
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        if any(not (m.isfile() or m.isdir()) for m in tar.getmembers()):
            raise ValueError("Source archive contains links or special files")
        tar.extractall(destination, filter="data")
    return commit


def source_hash(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Source snapshot must be a regular directory")
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in root.rglob("*")):
        raise ValueError("Source snapshot contains links or special files")
    return digest({str(p.relative_to(root)): p.read_bytes().hex()
                   for p in sorted(root.rglob("*")) if p.is_file()})


def apply_proposal(parent: Path, destination: Path, proposal: dict, allowed: list[str]) -> str:
    if not isinstance(proposal.get("hypothesis"), str) or not proposal["hypothesis"].strip():
        raise ValueError("Proposal needs a hypothesis")
    files = proposal.get("files")
    if not isinstance(files, dict) or not files or not set(files) <= set(allowed):
        raise ValueError("Proposal changed files outside the allow-list or has no files")
    patches = []
    for name, content in files.items():
        relative_file(name)
        original = parent / name
        if not original.is_file() or original.is_symlink():
            raise ValueError(f"Only existing regular files can change: {name}")
        if not isinstance(content, str) or not content.strip() or len(content) > 200_000:
            raise ValueError("Proposed file must be nonempty text under 200K characters")
        if name.endswith(".py"):
            ast.parse(content, filename=name)
        previous = original.read_text(encoding="utf-8")
        if not content.endswith("\n"):
            content += "\n"
        if not previous.endswith("\n"):
            raise ValueError(f"Patch export requires newline-terminated source: {name}")
        patches.extend(difflib.unified_diff(
            previous.splitlines(keepends=True), content.splitlines(keepends=True),
            fromfile=f"a/{name}", tofile=f"b/{name}",
        ))
    if not patches:
        raise ValueError("Proposal made no changes")
    shutil.copytree(parent, destination)
    for name, content in files.items():
        (destination / name).write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    return "".join(patches)


def cumulative_patch(seed: Path, frontier: Path, allowed: list[str]) -> str:
    return "".join(line for name in allowed for line in difflib.unified_diff(
        (seed / name).read_text().splitlines(keepends=True),
        (frontier / name).read_text().splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}",
    ))
