"""Derive a local answer key for the 450 withheld-answer tasks.

Upstream withholds ground truth for the default split -- official
scoring happens on the leaderboard. But the same dataset publishes every
submission's per-task grading in ``data/task_scores/*.jsonl`` (rows of
``{submission_id, task_id, score, level, agent_answer}``), including
submissions the leaderboard scored at 100%. Any row with ``score: true``
holds an answer the official scorer accepted for that task.

The key built here takes, per task, the **modal accepted answer** across
all correct rows (the most canonical surface form), overridden by the 10
public dev-split ground truths where they overlap. Coverage and source
counts are recorded per task; a task nobody has answered correctly in
public gets no key entry and is reported as ungradable rather than
guessed at.

This makes our numbers self-consistent, free, and instantly re-gradable
-- and emphatically **not leaderboard-comparable**: the reference is
scorer-accepted answers, not the hidden truth, so borderline-tolerance
cases can diverge. RESULTS.md carries that caveat; a run can always be
exported with ``dabbench export <run_id>`` for official submission.
"""
from __future__ import annotations

import collections
import json
import pathlib

KEY_PATH = pathlib.Path(__file__).parent.parent / "answer_key.json"
# Keys are derived data, freshly buildable -- but a run graded with one
# key must say which; the digest in the key file goes into RESULTS rows.
KEY_VERSION = 1


def build_key(snapshot_dir: str | None = None) -> dict:
    """Scan task_scores and write answer_key.json. Returns the key."""
    import huggingface_hub

    from dabbench.dataset import HF_DATASET, load_tasks

    root = snapshot_dir or huggingface_hub.snapshot_download(
        repo_id=HF_DATASET,
        repo_type="dataset",
        allow_patterns=["data/task_scores/*", "data/tasks/*"],
    )

    votes: dict[str, collections.Counter] = {}
    sources: dict[str, set] = {}
    score_dir = pathlib.Path(root) / "data" / "task_scores"
    for path in sorted(score_dir.glob("*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("score") is not True:
                    continue
                task_id = str(row.get("task_id"))
                answer = str(row.get("agent_answer") or "").strip()
                if not answer:
                    continue
                votes.setdefault(task_id, collections.Counter())[answer] += 1
                sources.setdefault(task_id, set()).add(
                    str(row.get("submission_id"))
                )

    entries: dict[str, dict] = {}
    for task_id, counter in votes.items():
        answer, count = counter.most_common(1)[0]
        entries[task_id] = {
            "answer": answer,
            "votes": count,
            "distinct_accepted": len(counter),
            "submissions": len(sources[task_id]),
            "source": "task_scores",
        }

    # The 10 public dev answers are authoritative wherever they overlap.
    for task in load_tasks("upstream-dev", snapshot_dir=root):
        if task.answer:
            entries[task.task_id] = {
                "answer": task.answer,
                "votes": None,
                "distinct_accepted": None,
                "submissions": None,
                "source": "upstream-dev",
            }

    all_ids = {t.task_id for t in load_tasks("all", snapshot_dir=root)}
    key = {
        "version": KEY_VERSION,
        "covered": sum(1 for t in all_ids if t in entries),
        "total": len(all_ids),
        "uncovered_task_ids": sorted(
            (t for t in all_ids if t not in entries),
            key=lambda x: (len(x), x),
        ),
        "entries": entries,
    }
    with open(KEY_PATH, "w", encoding="utf-8") as fh:
        json.dump(key, fh, ensure_ascii=False, indent=1)
    return key


def load_key() -> dict:
    if not KEY_PATH.exists():
        raise SystemExit(
            "no answer key -- run `dabbench build-key` first (downloads "
            "public task_scores and derives the key locally)"
        )
    with open(KEY_PATH, encoding="utf-8") as fh:
        return json.load(fh)
