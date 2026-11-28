"""Load EnterpriseOps-Gym task definitions.

Tasks are self-describing JSON files: gym server config (URL + seed
database), policy system prompt, user prompt, per-task tool allow-list,
and the hidden SQL verifiers. The public repo ships a per-domain sample
under ``data/revised/``; a public subset of the full benchmark lives on
HuggingFace. ``import-tasks`` converts a pinned release into this layout.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

from eogbench import vendor


@dataclass(frozen=True)
class Gym:
    """One entry of a multi-gym (hybrid) task's ``gym_servers_config``."""
    name: str  # mcp_server_name: the key verifiers name in ``gym_name``
    url: str
    seed_database_file: str
    context: dict[str, Any] = field(default_factory=dict)
    auth_config: dict[str, Any] = field(default_factory=dict)
    user_info: dict[str, Any] = field(default_factory=dict)


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
    context: dict[str, Any] = field(default_factory=dict)
    auth_config: dict[str, Any] = field(default_factory=dict)
    user_info: dict[str, Any] = field(default_factory=dict)
    # Multi-gym (hybrid) tasks only: every gym in upstream's order (the
    # gym_* fields above then repeat the first one). Empty for a single-gym
    # task, whose one gym is the gym_* fields -- that path is unchanged.
    gyms: tuple[Gym, ...] = ()


def tasks_dir() -> pathlib.Path:
    override = os.environ.get("EOG_TASKS_DIR")
    if override:
        return pathlib.Path(override)
    return vendor.home() / "data" / "revised"


def structured(value, kind):
    """HF columns may contain JSON strings, lists, or Arrow/numpy arrays."""
    if isinstance(value, str):
        # An empty string is an absent value: 102 oracle rows carry user_info == "" (they were rejected as
        # "Expecting value", 28 of them dev-split ids).
        value = json.loads(value) if value.strip() else None
    if hasattr(value, "tolist"):
        value = value.tolist()
    if value is None:
        return kind()
    if not isinstance(value, kind):
        raise ValueError(f"Expected a {kind.__name__} field")
    return value


def _seed_path(gym: dict) -> pathlib.PurePosixPath:
    seed = pathlib.PurePosixPath(str(gym.get("seed_database_file") or ""))
    if str(seed) == "." or seed.is_absolute() or ".." in seed.parts or "\\" in str(seed):
        raise ValueError("Seed database must be a nonempty relative path")
    return seed


def _parse_gyms(gyms: list, verifiers: list) -> tuple[Gym, ...]:
    """A hybrid task's gyms, upstream's ``gym_servers_config`` semantics
    (benchmark/executor.py): every gym is seeded and connected; a tool name
    belongs to the first gym listing it; each verifier runs against the
    database of the gym its ``gym_name`` names."""
    if any(not isinstance(g, dict) for g in gyms):
        raise ValueError("Gym configuration must be an object")
    names = [str(g.get("mcp_server_name") or "") for g in gyms]
    if not all(names) or len(set(names)) != len(names):
        raise ValueError("Multi-gym task needs distinct, nonempty gym names")
    # Upstream skips a verifier whose gym_name is absent from the config and
    # sends one without gym_name to the first gym's client with no database
    # id; neither occurs in the public data, so both are refused here.
    if any(v.get("gym_name") not in names for v in verifiers):
        raise ValueError("Verifier refers to a gym outside the task's gym_servers_config")
    out = []
    for g in gyms:
        _seed_path(g)
        out.append(Gym(
            name=str(g.get("mcp_server_name") or ""),
            url=str(g.get("mcp_server_url") or ""),
            seed_database_file=str(g.get("seed_database_file") or ""),
            context=structured(g.get("context"), dict),
            auth_config=structured(g.get("auth_config"), dict),
            user_info=structured(g.get("user_info"), dict),
        ))
    return tuple(out)


def parse_record(raw: dict, domain: str, stem: str) -> Task:
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", v) for v in (domain, stem)):
        raise ValueError("Task domain and ID must be path-safe")
    gyms = structured(raw.get("gym_servers_config"), list)
    if not gyms:
        raise ValueError(f"{domain}/{stem}: expected at least one gym server, got 0")
    gym = gyms[0]
    if not isinstance(gym, dict):
        raise ValueError("Gym configuration must be an object")
    selected = structured(raw.get("selected_tools"), list)
    restricted = structured(raw.get("restricted_tools"), list)
    if not selected or any(not isinstance(t, str) or not t for t in selected + restricted):
        raise ValueError("Task needs an explicit nonempty selected_tools list")
    if not set(selected) - set(restricted):
        raise ValueError("Task has no allowed tools after restrictions")
    verifiers = structured(raw.get("verifiers"), list)
    if not verifiers or any(not isinstance(v, dict) or v.get("verifier_type") != "database_state" for v in verifiers):
        raise ValueError("Only nonempty database_state verifier sets are supported")
    multi = _parse_gyms(gyms, verifiers) if len(gyms) > 1 else ()
    if not multi and any(v.get("gym_name") not in (None, "", gym.get("mcp_server_name")) for v in verifiers):
        raise ValueError("Verifier refers to a different gym")
    _seed_path(gym)
    return Task(
        task_id=f"{domain}/{stem.removeprefix('task_')}",
        domain=domain,
        system_prompt=str(raw.get("system_prompt") or ""),
        user_prompt=str(raw.get("user_prompt") or ""),
        selected_tools=tuple(selected),
        restricted_tools=tuple(restricted),
        gym_name=str(gym.get("mcp_server_name") or ""),
        gym_url=str(gym.get("mcp_server_url") or ""),
        mcp_endpoint=str(raw.get("mcp_endpoint") or "/mcp"),
        seed_database_file=str(gym.get("seed_database_file") or ""),
        verifiers=tuple(verifiers),
        reset_database=bool(raw.get("reset_database_between_runs", True)),
        context=structured(gym.get("context"), dict),
        auth_config=structured(gym.get("auth_config"), dict),
        user_info=structured(gym.get("user_info"), dict),
        gyms=multi,
    )


def _parse(path: pathlib.Path, domain: str) -> Task:
    with open(path, encoding="utf-8") as fh:
        return parse_record(json.load(fh), domain, path.stem)


def load_tasks(domains: tuple[str, ...] | None = None, task_ids: tuple[str, ...] | None = None) -> list[Task]:
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
            if task_ids is not None and f"{domain_dir.name}/{path.stem.removeprefix('task_')}" not in task_ids:
                continue
            tasks.append(_parse(path, domain_dir.name))
    if not tasks:
        raise SystemExit(f"no task_*.json files under {root}")
    return tasks
