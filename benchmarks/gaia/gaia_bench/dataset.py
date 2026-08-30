"""Load the GAIA validation split and partition it into dev and holdout.

The split is derived from a fixed seed and sorted input, so it is stable
across runs and machines. Iterating on dev while reporting on holdout is
what keeps a published number from being fit to the questions it reports on.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass

HF_DATASET = "gaia-benchmark/GAIA"
HF_CONFIG = "2023_all"
DEFAULT_SEED = 20260726


#: Attachment types no configured tool can turn into text. A task whose
#: answer is only in the audio is unanswerable here, and running it buys
#: nothing: measured 0/2 on dev-022, and one of the two invented an answer
#: from the question's wording rather than reporting that it could not
#: listen. Excluding them keeps the money and removes a fabrication prompt.
#:
#: This is a capability boundary, not a difficulty filter. Widening it to
#: anything the agent merely finds hard would make the score meaningless.
UNSUPPORTED_ATTACHMENT_SUFFIXES: tuple[str, ...] = (
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma",
)


def needs_unsupported_capability(task: "Task") -> bool:
    """True when the task's attachment needs a capability we do not have."""
    name = (task.file_name or task.file_path or "").lower()
    return name.endswith(UNSUPPORTED_ATTACHMENT_SUFFIXES)


@dataclass(frozen=True)
class Task:
    task_id: str
    question: str
    level: int
    final_answer: str
    file_name: str
    file_path: str


def make_split(
    task_ids: list[str],
    dev_size: int = 110,
    seed: int = DEFAULT_SEED,
) -> tuple[list[str], list[str]]:
    """Partition task ids into (dev, holdout).

    Input is sorted before shuffling so the result does not depend on the
    order the dataset happened to yield rows in.
    """
    ordered = sorted(task_ids)
    rng = random.Random(seed)
    rng.shuffle(ordered)
    return ordered[:dev_size], ordered[dev_size:]


HF_ATTACHMENT_DIR = "2023/validation"


def resolve_attachment(task: Task, hf_token: str | None = None) -> str | None:
    """Return a readable local path to the task's attachment, or None.

    GAIA's ``file_path`` column is a path INSIDE the HuggingFace repo
    (``2023/validation/<uuid>.xlsx``), not a file on disk. Handing it
    straight to ``open()`` raises FileNotFoundError, which the runner
    records as ``infra_error`` -- so every attachment task silently
    becomes infrastructure noise instead of a measured result. That is
    a quarter of the dev split.

    Files are pulled through ``hf_hub_download``, which caches, so a
    repeated run costs nothing.
    """
    if not task.file_name:
        return None

    # A caller may already have a real local file (fixtures, manual runs).
    if task.file_path and os.path.exists(task.file_path):
        return task.file_path

    import huggingface_hub

    remote = task.file_path or f"{HF_ATTACHMENT_DIR}/{task.file_name}"
    if not remote.startswith(HF_ATTACHMENT_DIR):
        remote = f"{HF_ATTACHMENT_DIR}/{os.path.basename(remote)}"

    return huggingface_hub.hf_hub_download(
        repo_id=HF_DATASET,
        repo_type="dataset",
        filename=remote,
        token=hf_token or os.environ.get("HF_TOKEN"),
    )


def load_tasks(split: str = "all", hf_token: str | None = None) -> list[Task]:
    """Load GAIA validation tasks for the given split.

    ``split`` is "dev", "holdout", or "all". Requires an HF token whose
    account has accepted the GAIA terms; the dataset is gated.
    """
    from datasets import load_dataset

    token = hf_token or os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError(
            "GAIA is a gated dataset. Set HF_TOKEN to a token whose account "
            "has accepted the terms at "
            "https://huggingface.co/datasets/gaia-benchmark/GAIA"
        )

    ds = load_dataset(HF_DATASET, HF_CONFIG, split="validation", token=token)
    tasks = [
        Task(
            task_id=row["task_id"],
            question=row["Question"],
            level=int(row["Level"]),
            final_answer=row["Final answer"],
            file_name=row.get("file_name") or "",
            file_path=row.get("file_path") or "",
        )
        for row in ds
    ]

    if split == "all":
        return tasks

    dev_ids, holdout_ids = make_split([t.task_id for t in tasks])
    wanted = set(dev_ids if split == "dev" else holdout_ids)
    return [t for t in tasks if t.task_id in wanted]
