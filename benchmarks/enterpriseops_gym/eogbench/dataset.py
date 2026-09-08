"""Load EnterpriseOps-Gym task definitions.

Tasks are self-describing JSON files: gym server config (URL + seed
database), policy system prompt, user prompt, per-task tool allow-list,
and the hidden SQL verifiers. The public repo ships a per-domain sample
under ``data/revised/``; the full 1,150-task set lives on HuggingFace
(``ServiceNow-AI/EnterpriseOps-Gym``) and drops into the same layout --
point ``EOG_TASKS_DIR`` at any directory of ``<domain>/task_*.json``.
"""
from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass
from typing import Any

from eogbench import vendor


@dataclass(frozen=True)
class Task:
    task_id: str  # "<domain>/<filename stem>"
    domain: str
    system_prompt: str
    user_prompt: str
    selected_tools: tuple[str, ...]
    restricted_tools: tuple[str, ...]
    gym_name: str
    gym_url: str
    mcp_endpoint: str
    seed_database_file: str
    verifiers: tuple[dict[str, Any], ...]
    reset_database: bool


def tasks_dir() -> pathlib.Path:
    override = os.environ.get("EOG_TASKS_DIR")
    if override:
        return pathlib.Path(override)
    return vendor.home() / "data" / "revised"


def _parse(path: pathlib.Path, domain: str) -> Task:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    gyms = list(raw.get("gym_servers_config") or [])
    if len(gyms) != 1:
        raise ValueError(
            f"{path.name}: expected exactly one gym server, got {len(gyms)} "
            "(multi-gym hybrid tasks are out of scope for now)"
        )
    gym = gyms[0]
    return Task(
        task_id=f"{domain}/{path.stem.removeprefix('task_')}",
        domain=domain,
        system_prompt=str(raw.get("system_prompt") or ""),
        user_prompt=str(raw.get("user_prompt") or ""),
        selected_tools=tuple(raw.get("selected_tools") or []),
        restricted_tools=tuple(raw.get("restricted_tools") or []),
        gym_name=str(gym.get("mcp_server_name") or ""),
        gym_url=str(gym.get("mcp_server_url") or ""),
        mcp_endpoint=str(raw.get("mcp_endpoint") or "/mcp"),
        seed_database_file=str(gym.get("seed_database_file") or ""),
        verifiers=tuple(raw.get("verifiers") or []),
        reset_database=bool(raw.get("reset_database_between_runs", True)),
    )


def load_tasks(domains: tuple[str, ...] | None = None) -> list[Task]:
    """Every task under the tasks dir, sorted; skipped files are errors.

    A task that cannot be parsed is a hard failure, not a silent skip --
    the run's denominator must be knowable.
    """
    root = tasks_dir()
    if not root.is_dir():
        raise SystemExit(f"tasks directory not found: {root}")
    tasks: list[Task] = []
    for domain_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if domains and domain_dir.name not in domains:
            continue
        for path in sorted(domain_dir.glob("task_*.json")):
            tasks.append(_parse(path, domain_dir.name))
    if not tasks:
        raise SystemExit(f"no task_*.json files under {root}")
    return tasks
