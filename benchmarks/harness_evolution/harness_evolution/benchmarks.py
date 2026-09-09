"""Benchmark identities, artifact conventions, and existing holdout reservations."""
from __future__ import annotations

import json
import re
from pathlib import Path

BENCHMARKS = ("claweval", "workspace_bench", "enterpriseops_gym", "dabstep", "gaia")
PREFIXES = dict(zip(BENCHMARKS, ("CLAWEVAL", "WSBENCH", "EOG", "DABSTEP", "GAIA")))
EOG_MODES = ("oracle", "plus_5_tools", "plus_10_tools", "plus_15_tools")


def upstream_split(task: dict) -> str:
    return task.get("upstream_split", {"claweval": "general", "enterpriseops_gym": "oracle"}.get(task["benchmark"], "dev"))


def valid_id(benchmark: str, value) -> bool:
    pattern = r"(?:calendar|csm|drive|email|hr|hybrid|itsm|teams)/[A-Za-z0-9_-]+" if benchmark == "enterpriseops_gym" else r"[A-Za-z0-9_-]+"
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def artifact_name(benchmark: str, task_id: str) -> str:
    if not valid_id(benchmark, task_id):
        raise ValueError("Invalid task artifact ID")
    return task_id.replace("/", "__") if benchmark == "enterpriseops_gym" else task_id


def reserved_splits(repository: Path, benchmark: str) -> dict[str, set[str]]:
    if benchmark == "gaia":
        root = repository / "benchmarks/gaia/gaia_bench/splits"
        return {name: set((root / f"{name}.txt").read_text().splitlines()) for name in ("dev", "holdout")}
    paths = {"workspace_bench": "workspace_bench/wsbench/splits/lite_en.json",
             "dabstep": "dabstep/dabbench/splits/tasks_v1.json"}
    if benchmark not in paths:
        return {}
    raw = json.loads((repository / "benchmarks" / paths[benchmark]).read_text())
    return {name: set(raw[name]) for name in ("dev", "holdout")}


def check_reservations(repository: Path, tasks: list[dict]) -> None:
    for benchmark in {t["benchmark"] for t in tasks}:
        splits = reserved_splits(repository, benchmark)
        if not splits:
            continue
        for task in (t for t in tasks if t["benchmark"] == benchmark):
            tid = task["task_id"]
            expected = "holdout" if tid in splits["holdout"] else "dev"
            if tid not in splits[expected]:
                raise ValueError(f"Unknown {benchmark} task ID in frozen splits")
            if expected == "holdout" and task["split"] != "holdout":
                raise ValueError(f"Existing {benchmark} holdout cannot enter development")
            if upstream_split(task) != expected:
                raise ValueError(f"{benchmark} upstream split does not match the frozen split")
