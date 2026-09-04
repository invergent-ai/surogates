"""Build the per-episode upload plan from the vendored checkout.

The whole simulation goes into the session workspace: upstream's
``tools/`` and ``data/`` trees plus the standalone tool manager, laid
out under ``sim/``, with our ``ecsim.py`` at the workspace root. The
sandbox mounts that workspace, so after upload the agent simply runs
``python3 ecsim.py ...`` -- no service, no port, no tunnel.

Excluded on purpose: upstream's ``agent/`` loop and LLM plumbing (the
surogates harness *is* the agent loop under test), the context-manager
tokenizer (their agent's context accounting), and ``assets/`` images.
"""
from __future__ import annotations

import os
import pathlib
import posixpath
from dataclasses import dataclass

from ecbench import vendor

# ecsim.py sits inside this package; it is data as far as the benchmark
# is concerned -- uploaded, never imported by the orchestrator.
ECSIM_SOURCE = pathlib.Path(__file__).parent / "ecsim.py"

# vendored path -> workspace destination directory
_SIM_TREES = ("tools", "data")
_TOOL_MANAGER = pathlib.Path("agent") / "ecommerce_tool_manager.py"

_SKIP_DIRS = {"__pycache__", ".pytest_cache"}
MAX_FILE_BYTES = 45_000_000


@dataclass(frozen=True)
class StagedFile:
    local_path: str
    subdir: str
    name: str
    workspace_path: str
    size: int


class StagingError(Exception):
    """The checkout cannot be staged faithfully; carries the reason."""


def _stage_one(local: pathlib.Path, subdir: str) -> StagedFile:
    size = local.stat().st_size
    if size > MAX_FILE_BYTES:
        raise StagingError(
            f"{local.name} is {size / 1e6:.1f} MB, over the upload cap"
        )
    return StagedFile(
        local_path=str(local),
        subdir=subdir,
        name=local.name,
        workspace_path=posixpath.join(subdir, local.name) if subdir else local.name,
        size=size,
    )


def stage_plan() -> list[StagedFile]:
    """Every file one episode needs, workspace-relative."""
    root = vendor.home()
    plan: list[StagedFile] = []

    for tree in _SIM_TREES:
        base = root / tree
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in sorted(filenames):
                if fname.endswith(".pyc"):
                    continue
                local = pathlib.Path(dirpath) / fname
                rel = local.relative_to(root).parent
                plan.append(_stage_one(local, posixpath.join("sim", *rel.parts)))

    manager = root / _TOOL_MANAGER
    if not manager.is_file():
        raise StagingError(f"missing {_TOOL_MANAGER} in checkout")
    # Flat into sim/ so importing it never triggers upstream's agent
    # package __init__ (which pulls in their LLM client stack).
    plan.append(_stage_one(manager, "sim"))

    if not ECSIM_SOURCE.is_file():
        raise StagingError("ecsim.py missing from the ecbench package")
    plan.append(_stage_one(ECSIM_SOURCE, ""))

    if not any(s.workspace_path == "sim/tools/ecommerce_env.py" for s in plan):
        raise StagingError("checkout has no tools/ecommerce_env.py")
    return plan
