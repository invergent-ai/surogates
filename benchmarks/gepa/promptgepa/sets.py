"""Derive optimiser task sets from prior GAIA runs.

One run cannot tell a real failure from a coin flip.  The GAIA benchmark's
own README records identical tasks flipping 7/10 -> 4/10 between runs hours
apart, and four local runs of the 110-task dev split agree on only 78 of
them.  Every set here is therefore defined over two or more runs and keeps
only the tasks that agreed every time:

    stable_fail   failed in every run   the training signal -- the seed
                                        scores 0 on these, which is what
                                        reflection needs to learn from
    stable_pass   passed in every run   the regression guard
    flipper       disagreed             excluded from every set

Flippers are dropped rather than down-weighted.  They are the mechanism by
which noise is accepted as progress: against a set of coin flips a candidate
that changes nothing still "improves" some of the time.

Splitting the stable failures into train and val is what keeps the search
honest.  GEPA reflects on ``train`` and selects on ``val``, so a candidate
that only memorised the training tasks does not survive selection.  The
guard tasks sit inside ``val`` on purpose: a regression then costs the
candidate during the search rather than being discovered afterwards.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

# A capability the harness does not have is not something prompt text can
# fix, so those tasks never enter a set.  They stay failures in the score.
UNFIXABLE_FLAGS: tuple[str, ...] = ("unsupported_capability",)


class NoSignalError(ValueError):
    """The runs contain no stable failures to learn from.

    Raised rather than returning an empty training set: GEPA given nothing
    to improve reports the seed back as "best" after burning the budget,
    which reads like a result and is not one.
    """


@dataclass(frozen=True)
class TaskSets:
    """Task ids partitioned for one optimisation run."""

    train: tuple[str, ...]
    val: tuple[str, ...]
    guard: tuple[str, ...]
    stable_fail: tuple[str, ...]
    stable_pass: tuple[str, ...]
    flippers: tuple[str, ...]
    runs: tuple[str, ...]
    flag_counts: dict[str, int]

    def summary(self) -> str:
        val_fail = [t for t in self.val if t not in set(self.guard)]
        flags = ", ".join(
            f"{name} x{n}" for name, n in
            sorted(self.flag_counts.items(), key=lambda kv: -kv[1])
        ) or "none"
        return "\n".join([
            f"runs        {len(self.runs)}: {', '.join(self.runs)}",
            f"stable pass {len(self.stable_pass):>3}",
            f"stable fail {len(self.stable_fail):>3}   flags: {flags}",
            f"flippers    {len(self.flippers):>3}   (excluded)",
            "",
            f"train       {len(self.train):>3}   stable failures, reflected on",
            f"val         {len(self.val):>3}   selected on "
            f"({len(val_fail)} held-out failures + {len(self.guard)} guards)",
        ])


def load_outcomes(run_dir: Path) -> dict[str, dict]:
    """Read one run's ``outcomes.json`` as ``{task_id: outcome}``."""
    path = Path(run_dir) / "outcomes.json"
    if not path.exists():
        raise FileNotFoundError(f"no outcomes.json in {run_dir}")
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    return {row["task_id"]: row for row in rows}


def derive(
    run_dirs: list[Path],
    *,
    guard_size: int = 12,
    seed: int = 0,
    train_flags: tuple[str, ...] | None = None,
    exclude_flags: tuple[str, ...] = UNFIXABLE_FLAGS,
) -> TaskSets:
    """Partition the tasks common to *run_dirs* into optimiser sets.

    ``train_flags`` chooses which stable failures are trained on.  ``None``
    takes every failure carrying any behavioural flag -- the ones a
    deterministic detector could explain, and so the ones prompt text has a
    route to.  Passing explicit flags narrows that to one failure class
    (e.g. ``("no_tool_use",)``), which is how this is pointed at a different
    problem later.  Whatever is not trained on becomes held-out validation.
    """
    if len(run_dirs) < 2:
        raise ValueError(
            f"need >=2 runs to tell a stable failure from a flip, got {len(run_dirs)}"
        )

    runs = {Path(d).name: load_outcomes(Path(d)) for d in run_dirs}
    common = sorted(set.intersection(*(set(r) for r in runs.values())))
    if not common:
        raise ValueError("the runs share no task ids")

    excluded = set(exclude_flags)

    def flags_of(task_id: str) -> set[str]:
        return {f for r in runs.values() for f in r[task_id].get("flags") or ()}

    eligible = [t for t in common if not (flags_of(t) & excluded)]

    stable_fail, stable_pass, flippers = [], [], []
    for task_id in eligible:
        passes = [runs[r][task_id]["strict_pass"] for r in runs]
        (stable_pass if all(passes) else
         stable_fail if not any(passes) else flippers).append(task_id)

    if not stable_fail:
        raise NoSignalError(
            f"no task failed in all {len(runs)} runs -- nothing for the "
            "optimiser to improve on. Widen the run set or lower the bar."
        )

    wanted = set(train_flags) if train_flags else None

    def trains_on(task_id: str) -> bool:
        found = flags_of(task_id)
        return bool(found & wanted) if wanted else bool(found)

    train = [t for t in stable_fail if trains_on(t)]
    if not train:
        seen = sorted({f for t in stable_fail for f in flags_of(t)})
        raise NoSignalError(
            f"no stable failure matches train_flags={train_flags!r}. "
            f"Flags present on stable failures: {seen or 'none'}"
        )

    val_fail = [t for t in stable_fail if t not in set(train)]
    guard = sorted(random.Random(seed).sample(
        stable_pass, min(guard_size, len(stable_pass))
    ))

    counts: dict[str, int] = {}
    for task_id in stable_fail:
        for flag in flags_of(task_id):
            counts[flag] = counts.get(flag, 0) + 1

    return TaskSets(
        train=tuple(train),
        val=tuple(sorted(val_fail + guard)),
        guard=tuple(guard),
        stable_fail=tuple(stable_fail),
        stable_pass=tuple(stable_pass),
        flippers=tuple(flippers),
        runs=tuple(runs.keys()),
        flag_counts=counts,
    )
