"""Load DABstep tasks and context, and partition the 450 into dev/holdout.

Everything comes from the ungated HuggingFace dataset ``adyen/DABstep``
(CC-BY-4.0) and is cached by ``huggingface_hub``. Two upstream task
files: ``all.jsonl`` (450 tasks, ground truth withheld) and ``dev.jsonl``
(10 tasks with public answers -- used for smokes and as authoritative
overrides in the derived answer key, see ``answers.py``).

The frozen split partitions the 450 into dev 100 / holdout 350,
stratified by level (easy/hard), seed-fixed -- iterate on dev, touch
holdout only to report a final number. ``tests/test_dataset.py``
re-derives it from a committed fixture so silent drift fails the suite.
"""
from __future__ import annotations

import json
import os
import pathlib
import random
from dataclasses import dataclass

HF_DATASET = "adyen/DABstep"
DEFAULT_SEED = 20260906
SPLITS_PATH = pathlib.Path(__file__).parent / "splits" / "tasks_v1.json"

# The shared context corpus, staged into every session workspace.
CONTEXT_FILES = (
    "data/context/acquirer_countries.csv",
    "data/context/fees.json",
    "data/context/manual.md",
    "data/context/merchant_category_codes.csv",
    "data/context/merchant_data.json",
    "data/context/payments-readme.md",
    "data/context/payments.csv",
)


@dataclass(frozen=True)
class Task:
    task_id: str
    question: str
    guidelines: str
    level: str  # easy | hard
    answer: str  # "" for the withheld default split


def download_dataset() -> str:
    """Fetch (or reuse from cache) tasks + context. ~25 MB first time."""
    import huggingface_hub

    return huggingface_hub.snapshot_download(
        repo_id=HF_DATASET,
        repo_type="dataset",
        allow_patterns=["data/tasks/*", "data/context/*"],
    )


def _read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _to_task(row: dict) -> Task:
    return Task(
        task_id=str(row["task_id"]),
        question=str(row.get("question") or ""),
        guidelines=str(row.get("guidelines") or ""),
        level=str(row.get("level") or "").strip().lower(),
        answer=str(row.get("answer") or ""),
    )


def make_split(
    rows: list[tuple[str, str]],
    dev_size: int = 100,
    seed: int = DEFAULT_SEED,
) -> tuple[list[str], list[str]]:
    """Partition (task_id, level) rows into (dev, holdout).

    Stratified by level with largest-remainder apportionment, sorted
    input, seeded shuffle -- same recipe as the sibling benchmarks, so
    neither split skews easier.
    """
    strata: dict[str, list[str]] = {}
    for task_id, level in sorted(rows):
        strata.setdefault(level, []).append(task_id)

    total = sum(len(v) for v in strata.values())
    fraction = dev_size / total
    rng = random.Random(seed)

    shuffled: list[tuple[str, list[str]]] = []
    for level in sorted(strata):
        ids = list(strata[level])
        rng.shuffle(ids)
        shuffled.append((level, ids))

    quotas = [len(ids) * fraction for _, ids in shuffled]
    takes = [int(q) for q in quotas]
    remainders = sorted(
        range(len(quotas)),
        key=lambda i: (quotas[i] - takes[i], shuffled[i][0]),
        reverse=True,
    )
    for i in remainders[: dev_size - sum(takes)]:
        takes[i] += 1

    dev: list[str] = []
    holdout: list[str] = []
    for (_, ids), take in zip(shuffled, takes):
        dev.extend(ids[:take])
        holdout.extend(ids[take:])
    return sorted(dev, key=_id_key), sorted(holdout, key=_id_key)


def _id_key(task_id: str):
    return (len(task_id), task_id)


def frozen_split() -> dict[str, list[str]]:
    with open(SPLITS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def load_tasks(split: str = "dev", snapshot_dir: str | None = None) -> list[Task]:
    """Load tasks for ``split``.

    "dev" / "holdout" / "all" partition the 450 withheld-answer tasks;
    "upstream-dev" is the 10-task public-answer split (smokes, key
    overrides).
    """
    root = snapshot_dir or download_dataset()
    if split == "upstream-dev":
        return [_to_task(r) for r in _read_jsonl(os.path.join(root, "data/tasks/dev.jsonl"))]

    tasks = [_to_task(r) for r in _read_jsonl(os.path.join(root, "data/tasks/all.jsonl"))]
    if split == "all":
        return tasks
    wanted = set(frozen_split()[split])
    return [t for t in tasks if t.task_id in wanted]


def context_paths(snapshot_dir: str | None = None) -> list[str]:
    """Absolute paths of the 7 context files, verified present."""
    root = snapshot_dir or download_dataset()
    paths = []
    for rel in CONTEXT_FILES:
        p = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(p):
            raise SystemExit(f"context file missing from snapshot: {rel}")
        paths.append(p)
    return paths
