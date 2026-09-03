"""Turn a task's data manifest into an upload plan for one session.

The dataset stores each input as ``data/<hash>_<name>`` inside the task
directory; the agent must see it under its logical name, in a ``workdir/``
folder at the workspace root -- the sandbox mounts that same workspace at
/workspace, so the agent's view is ``workdir/<name>``.

Eligibility is decided here, before any session exists. A task whose
inputs are missing from the snapshot or exceed the harness upload caps is
*skipped and reported*, never silently dropped and never half-staged: an
agent working on a partial workspace would produce a score that means
nothing.
"""
from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass

from wsbench.dataset import Task

WORKDIR = "workdir"
OUTPUT_DIR = "outputs"

# The harness rejects uploads over 50 MB; stay under it with headroom.
MAX_FILE_BYTES = 45_000_000
# Keep a whole task's staging bounded -- pathological tasks get skipped,
# not slowly uploaded for an hour.
MAX_TOTAL_BYTES = 200_000_000
MAX_FILES = 500


@dataclass(frozen=True)
class StagedFile:
    local_path: str
    subdir: str  # workspace-relative directory for the upload route
    name: str  # basename presented to the agent
    workspace_path: str  # subdir/name -- key the workspace will report
    size: int


class StagingError(Exception):
    """The task cannot be staged faithfully; carries the reason."""


def stage_plan(task: Task) -> list[StagedFile]:
    """Resolve every manifest entry to a local file and a workspace slot.

    Raises :class:`StagingError` with a human-readable reason when the
    task cannot be staged faithfully.
    """
    if not task.manifest:
        raise StagingError("empty data manifest")
    if len(task.manifest) > MAX_FILES:
        raise StagingError(
            f"{len(task.manifest)} input files exceeds cap of {MAX_FILES}"
        )

    plan: list[StagedFile] = []
    total = 0
    for entry in task.manifest:
        local = os.path.join(task.local_dir, *entry.stored_relpath.split("/"))
        if not os.path.isfile(local):
            raise StagingError(f"input file missing from snapshot: {entry.stored_relpath}")

        size = os.path.getsize(local)
        if size > MAX_FILE_BYTES:
            raise StagingError(
                f"{entry.filename} is {size / 1e6:.1f} MB, over the "
                f"{MAX_FILE_BYTES / 1e6:.0f} MB upload cap"
            )
        total += size
        if total > MAX_TOTAL_BYTES:
            raise StagingError(
                f"total input size exceeds {MAX_TOTAL_BYTES / 1e6:.0f} MB"
            )

        # A logical filename may carry subdirectories; preserve them so
        # relative references between input files keep resolving.
        logical = entry.filename.replace("\\", "/").lstrip("/")
        if ".." in logical.split("/"):
            raise StagingError(f"path traversal in manifest filename: {entry.filename}")
        directory, name = posixpath.split(logical)
        subdir = posixpath.join(WORKDIR, directory) if directory else WORKDIR

        plan.append(
            StagedFile(
                local_path=local,
                subdir=subdir,
                name=name,
                workspace_path=posixpath.join(subdir, name),
                size=size,
            )
        )
    return plan


def eligibility(task: Task) -> str | None:
    """Reason the task cannot run, or None when it can."""
    try:
        stage_plan(task)
    except StagingError as exc:
        return str(exc)
    return None
