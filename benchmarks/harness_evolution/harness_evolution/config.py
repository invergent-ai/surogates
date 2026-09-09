"""Frozen experiment inputs and a task partition shared by both model tiers."""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path, PurePosixPath
from harness_evolution.benchmarks import BENCHMARKS, EOG_MODES, check_reservations, upstream_split, valid_id

TIERS = ("pro", "standard")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    temp.replace(path)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def relative_file(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or "\\" in value
            or str(path) != value or any(p.startswith(".") for p in path.parts)):
        raise ValueError(f"Not a safe relative file: {value!r}")
    return value


def task_key(task: dict) -> str:
    return f"{task['benchmark']}:{task['task_id']}"


def validate_tasks(tasks: list[dict], *, partitioned: bool = True) -> None:
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Task manifest must be a nonempty list")
    keys, groups = set(), {}
    for task in tasks:
        if task.get("benchmark") not in BENCHMARKS:
            raise ValueError("Supported benchmarks: " + ", ".join(BENCHMARKS))
        tid = task.get("task_id")
        if not valid_id(task["benchmark"], tid):
            raise ValueError("Task IDs must be nonempty path-safe strings")
        key = task_key(task)
        if key in keys:
            raise ValueError(f"Duplicate task: {key}")
        keys.add(key)
        if not isinstance(task.get("family"), str) or not task["family"]:
            raise ValueError(f"Task {key} needs a family for regression checks")
        if partitioned:
            split = task.get("split")
            if split not in ("search", "selection", "holdout"):
                raise ValueError(f"Invalid partition for {key}")
            if task.get("sealed_holdout") and split != "holdout":
                raise ValueError(f"Sealed holdout reassigned: {key}")
            if task.get("guard") and split != "selection":
                raise ValueError("Regression guards belong in selection")
            group = task.get("group", key)
            if group in groups and groups[group] != split:
                raise ValueError(f"Related task group crosses partitions: {group}")
            groups[group] = split
        upstream = upstream_split(task)
        if not isinstance(upstream, str) or not upstream or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in upstream):
            raise ValueError("Upstream split must be a path-safe name")
        if upstream == "holdout" and (not partitioned or task["split"] != "holdout"):
            if partitioned:
                raise ValueError("Upstream holdout tasks cannot enter search or selection")
        if task["benchmark"] in ("workspace_bench", "dabstep", "gaia") and upstream not in ("dev", "holdout"):
            raise ValueError("Benchmark requires a dev/holdout upstream split")
        if task["benchmark"] == "enterpriseops_gym" and upstream not in EOG_MODES:
            raise ValueError("Unknown EnterpriseOps tool mode")
    if partitioned:
        if not {"search", "selection"} <= {t["split"] for t in tasks}:
            raise ValueError("Experiment needs search and selection tasks")
        for benchmark in {t["benchmark"] for t in tasks}:
            roles = {t["split"] for t in tasks if t["benchmark"] == benchmark}
            if "search" in roles and "selection" not in roles:
                raise ValueError(f"{benchmark} needs search and selection tasks")
            if "selection" in roles and "search" not in roles and any(
                not t.get("guard") for t in tasks if t["benchmark"] == benchmark and t["split"] == "selection"
            ):
                raise ValueError("Selection-only benchmarks must mark their tasks as guards")


def partition(catalog: list[dict], seed: int = 0, holdout_fraction: float = 0.2) -> list[dict]:
    """Group variants before allocating strata; retain all sealed holdouts.

    Baseline-derived failure_mode and difficulty are optional strata. The
    catalog contains identifiers/descriptors only, never answers or rubrics.
    """
    validate_tasks(catalog, partitioned=False)
    if not 0 <= holdout_fraction < 1:
        raise ValueError("holdout_fraction must be in [0, 1)")
    groups = defaultdict(list)
    for task in catalog:
        groups[task.get("group", task_key(task))].append(task)
    strata = defaultdict(list)
    result = []
    for group in groups.values():
        if any(t.get("sealed_holdout") or t.get("upstream_split") == "holdout" for t in group):
            result.extend({**t, "split": "holdout", "sealed_holdout": True} for t in group)
        else:
            first = group[0]
            stratum = (first["benchmark"], str(first.get("difficulty", "")), str(first.get("failure_mode", "")))
            strata[stratum].append(group)
    rng = random.Random(seed)
    counts = defaultdict(lambda: defaultdict(int))
    for stratum, buckets in sorted(strata.items()):
        rng.shuffle(buckets)
        for group in buckets:
            benchmark = stratum[0]
            total = sum(counts[benchmark].values()) + len(group)
            targets = {"search": (1 - holdout_fraction) / 2,
                       "selection": (1 - holdout_fraction) / 2,
                       "holdout": holdout_fraction}
            role = max(targets, key=lambda r: targets[r] * total - counts[benchmark][r])
            counts[benchmark][role] += len(group)
            result.extend({**t, "split": role} for t in group)
    result.sort(key=task_key)
    validate_tasks(result)
    return result


def load_config(path: Path) -> dict:
    config = read_json(path)
    base = path.resolve().parent
    for key in ("repository", "tasks"):
        config[key] = str((base / config[key]).resolve())
    config["task_manifest"] = read_json(Path(config["tasks"]))
    validate_tasks(config["task_manifest"])
    check_reservations(Path(config["repository"]), config["task_manifest"])
    profiles = config.get("benchmark_profiles", {})
    if not isinstance(profiles, dict) or any(name not in BENCHMARKS or not isinstance(value, str) or not value for name, value in profiles.items()):
        raise ValueError("benchmark_profiles must map supported benchmarks to nonempty profile names")
    for options in config.get("benchmark_data", {}).values():
        for key in ("tasks_dir", "seed_root", "answer_key"):
            if options.get(key):
                options[key] = str((base / options[key]).resolve())
    if set(config.get("models", {})) != set(TIERS):
        raise ValueError("Configure exactly pro and standard")
    for model in config["models"].values():
        if not model.get("model") or not model.get("revision"):
            raise ValueError("Each model needs a model ID and a pinned revision")
    allowed = config.get("allowed_files", [])
    if not allowed or len(set(allowed)) != len(allowed):
        raise ValueError("allowed_files must be a nonempty unique list")
    for name in allowed:
        relative_file(name)
        if not name.startswith(("surogates/harness/", "surogates/tools/")) or not name.endswith((".py", ".md")):
            raise ValueError("Editable files must be harness/tool Python or Markdown")
    policy = {"max_proposals": 10, "repeats": 3, "min_pro_gain": 0.01,
              "max_standard_regression": 0.0, "max_group_regression": 0.0,
              "max_seconds_ratio": 1.25, "max_wall_seconds": 28800,
              **config.get("policy", {})}
    for name, value in policy.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid policy value: {name}")
    for name in ("max_proposals", "repeats", "max_wall_seconds"):
        if not isinstance(policy[name], int) or policy[name] < 1:
            raise ValueError(f"{name} must be a positive integer")
    if policy["repeats"] < 2:
        raise ValueError("At least two paired repetitions are required")
    config["policy"] = policy
    config["benchmark_pythons"] = {name: str((base / value).absolute())
                                     for name, value in config.get("benchmark_pythons", {}).items()}
    proposer = config.get("proposer", {})
    if "recorded" in proposer:
        proposer["recorded"] = [str((base / name).resolve()) for name in proposer["recorded"]]
        # Resume may not silently substitute different recorded proposals.
        proposer["recorded_hashes"] = {name: digest(read_json(Path(name))) for name in proposer["recorded"]}
    config["proposer"] = proposer
    runtime = config.get("runtime", {})
    for name in ("start", "stop"):
        if not isinstance(runtime.get(name), list) or not runtime[name] or not all(isinstance(a, str) for a in runtime[name]):
            raise ValueError(f"runtime.{name} must be an argv list")
    return config
