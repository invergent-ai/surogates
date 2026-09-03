"""Load Workspace-Bench-Lite (EN) and partition it into dev and holdout.

The Lite drop is self-contained: ``task_lite_clean_en/<id>/data/`` holds
every input file a task depends on, so the 37.6 GB full-workspace archive
is never needed. Everything comes from the ungated HuggingFace dataset and
is cached by ``huggingface_hub``; there is no token requirement.

The split is frozen in ``wsbench/splits/lite_en.json`` (task ids only),
generated once by :func:`make_split` -- stratified over (persona,
difficulty), unlike GAIA's random split whose easier holdout muddied the
overfitting signal. Iterate on dev; touch holdout only to report a final
number.
"""
from __future__ import annotations

import ast
import csv
import json
import os
import pathlib
import random
from dataclasses import dataclass

HF_DATASET = "Workspace-Bench/Workspace-Bench-Lite"
CSV_NAME = "task_lite_clean_en_metadata_table.csv"
TASK_DIR_PREFIX = "task_lite_clean_en"
DEFAULT_SEED = 20260902
SPLITS_PATH = pathlib.Path(__file__).parent / "splits" / "lite_en.json"


@dataclass(frozen=True)
class ManifestFile:
    """One input file: logical name the task refers to, and where the
    dataset actually stores it (hash-prefixed, under the task's data/)."""

    filename: str
    stored_relpath: str


@dataclass(frozen=True)
class Task:
    task_id: str
    persona: str
    instruction: str
    difficulty: str  # easy | medium | hard
    output_files: tuple[str, ...]
    rubrics: tuple[str, ...]
    rubric_types: tuple[str, ...]
    tested_capabilities: tuple[str, ...]
    manifest: tuple[ManifestFile, ...]
    local_dir: str  # task_lite_clean_en/<id> inside the snapshot


def _parse_listish(raw: str) -> list:
    """Parse a list column that is JSON in the CSV but a Python-repr
    string in the per-task metadata.json. Accept both, so the loader does
    not depend on which artifact upstream regenerated last."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = ast.literal_eval(raw)
    if not isinstance(value, list):
        raise ValueError(f"expected a list, got {type(value).__name__}")
    return value


def row_to_task(row: dict[str, str], snapshot_dir: str) -> Task:
    task_id = str(row["absolute_id"]).strip()
    manifest = tuple(
        ManifestFile(
            filename=str(m["filename"]),
            stored_relpath=str(m["stored_relpath"]),
        )
        for m in _parse_listish(row.get("data_manifest", ""))
    )
    return Task(
        task_id=task_id,
        persona=str(row.get("persona", "")).strip(),
        instruction=str(row.get("task", "")),
        difficulty=str(row.get("task_diff", "")).strip().lower(),
        output_files=tuple(map(str, _parse_listish(row.get("output_files", "")))),
        rubrics=tuple(map(str, _parse_listish(row.get("rubrics", "")))),
        rubric_types=tuple(map(str, _parse_listish(row.get("rubric_types", "")))),
        tested_capabilities=tuple(
            map(str, _parse_listish(row.get("tested_capabilities", "")))
        ),
        manifest=manifest,
        local_dir=os.path.join(snapshot_dir, TASK_DIR_PREFIX, task_id),
    )


def download_dataset() -> str:
    """Fetch (or reuse from cache) the EN metadata + per-task data files.

    Returns the local snapshot directory. ~250 MB on first run; free
    afterwards -- ``snapshot_download`` is content-addressed.
    """
    import huggingface_hub

    return huggingface_hub.snapshot_download(
        repo_id=HF_DATASET,
        repo_type="dataset",
        allow_patterns=[CSV_NAME, f"{TASK_DIR_PREFIX}/*"],
    )


def make_split(
    rows: list[tuple[str, str, str]],
    dev_fraction: float = 0.7,
    seed: int = DEFAULT_SEED,
) -> tuple[list[str], list[str]]:
    """Partition (task_id, persona, difficulty) rows into (dev, holdout).

    Stratified: each (persona, difficulty) stratum is shuffled with the
    seed and cut at ``dev_fraction`` by largest remainder, so neither
    split skews easier or toward one persona. Input is sorted first, so
    the result does not depend on CSV row order.
    """
    strata: dict[tuple[str, str], list[str]] = {}
    for task_id, persona, difficulty in sorted(rows):
        strata.setdefault((persona, difficulty), []).append(task_id)

    total = sum(len(v) for v in strata.values())
    target_dev = round(total * dev_fraction)

    rng = random.Random(seed)
    # Deterministic stratum order, each internally shuffled.
    shuffled: list[tuple[tuple[str, str], list[str]]] = []
    for key in sorted(strata):
        ids = list(strata[key])
        rng.shuffle(ids)
        shuffled.append((key, ids))

    # Largest-remainder apportionment of the dev quota across strata.
    quotas = [len(ids) * dev_fraction for _, ids in shuffled]
    takes = [int(q) for q in quotas]
    remainders = sorted(
        range(len(quotas)),
        key=lambda i: (quotas[i] - takes[i], shuffled[i][0]),
        reverse=True,
    )
    short = target_dev - sum(takes)
    for i in remainders[:short]:
        takes[i] += 1

    dev: list[str] = []
    holdout: list[str] = []
    for (_, ids), take in zip(shuffled, takes):
        dev.extend(ids[:take])
        holdout.extend(ids[take:])
    return sorted(dev), sorted(holdout)


def frozen_split() -> dict[str, list[str]]:
    with open(SPLITS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def load_tasks(split: str = "dev", snapshot_dir: str | None = None) -> list[Task]:
    """Load EN Lite tasks for ``split`` ("dev", "holdout" or "all")."""
    root = snapshot_dir or download_dataset()
    with open(os.path.join(root, CSV_NAME), encoding="utf-8") as fh:
        tasks = [row_to_task(row, root) for row in csv.DictReader(fh)]

    if split == "all":
        return tasks
    wanted = set(frozen_split()[split])
    return [t for t in tasks if t.task_id in wanted]
