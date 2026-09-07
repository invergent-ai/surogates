"""Score a candidate fragment by running GAIA tasks through the harness.

The score is ``strict_pass`` and nothing else -- 1.0 or 0.0 from the
official GAIA scorer.  There is deliberately no partial credit for
"produced an answer" or "called a tool": those are what the deterministic
detectors flag, so paying for them would buy a candidate points for
emitting any string at all.  The behavioural detail goes to the reflection
LM as *side information* instead, where it can inform a rewrite without
being something to game.

Side information never contains the expected answer.  The reflection LM
reads it and then writes the prompt, so a gold answer reaching it is a
direct route to a fragment that has memorised the benchmark: it would score
well on the training tasks and mean nothing.  ``side_info`` is given the
task's level and no other task metadata, which makes the leak structurally
impossible rather than merely avoided.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gaia_bench.client import HarnessClient
from gaia_bench.dataset import Task
from gaia_bench.detectors import detect, tool_error_names
from gaia_bench.runner import RolloutResult, run_split
from gaia_bench.scorer import lenient_scorer, question_scorer

from promptgepa.harness import CandidateRejected

# Failures that say the platform is unwell rather than that the prompt is
# bad.  A provider returning nothing, a session erroring out or running past
# the wall-clock cap tells you nothing about the candidate, and scoring it
# zero corrupts the acceptance anchor for every later comparison.
INFRA_FLAGS = frozenset({"infra_error", "empty_llm_response"})
INFRA_STATUSES = frozenset({"error", "timeout"})


class PlatformUnhealthy(RuntimeError):
    """Too much of a batch failed in an infrastructure-shaped way.

    Raised rather than returning zeros: an unhealthy platform scored as a
    bad candidate is the failure mode that silently invalidates a whole
    optimisation run.
    """


def _clip(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + f"... [+{len(text) - limit} chars]"


def _final_message(result: RolloutResult) -> str:
    for event in reversed(result.events):
        if event.type == "llm.response":
            return (event.data.get("message") or {}).get("content") or ""
    return ""


def _tool_calls(result: RolloutResult) -> list[str]:
    return [
        event.data.get("name", "?")
        for event in result.events if event.type == "tool.call"
    ]


def is_infra_shaped(result: RolloutResult, flags: list[str]) -> bool:
    return (
        result.error is not None
        or result.terminal_status in INFRA_STATUSES
        or bool(set(flags) & INFRA_FLAGS)
    )


def health_gate(
    infra: int, total: int, *, where: Path,
    fraction: float = 0.25, minimum: int = 3,
) -> None:
    """Abort when a batch failed platform-side rather than prompt-side."""
    threshold = max(minimum, int(total * fraction))
    if infra >= threshold:
        raise PlatformUnhealthy(
            f"{infra}/{total} tasks failed infrastructure-shaped (threshold "
            f"{threshold}). Scoring these as a bad candidate would poison the "
            f"search -- fix the platform and resume. Traces: {where}"
        )


def side_info(
    result: RolloutResult,
    *,
    level: int,
    role: str,
    flags: list[str],
    strict: bool,
    lenient: bool,
) -> dict[str, Any]:
    """Feedback for the reflection LM.

    Takes the task's *level* and no other task metadata, so the expected
    answer cannot reach the proposer through this path.
    """
    calls = _tool_calls(result)
    return {
        "task_id": result.task_id[:8],
        "level": level,
        "role": role,
        "solved": strict,
        "right_answer_wrong_format": lenient and not strict,
        "failure_flags": flags,
        "terminal_status": result.terminal_status,
        "seconds": round(result.wall_clock_s, 1),
        "assistant_turns": sum(1 for e in result.events if e.type == "llm.response"),
        "tool_calls": calls[:40],
        "tools_that_errored": tool_error_names(result),
        "agent_answer": _clip(result.answer, 300),
        "agent_final_message": _clip(_final_message(result), 1200),
        "rollout_error": result.error,
    }


async def _rollout(
    tasks: list[Task],
    *,
    out_dir: Path,
    base_url: str,
    token: str,
    agent_id: str,
    concurrency: int,
    wall_clock_cap_s: float,
    retries: int,
) -> dict[str, RolloutResult]:
    """Run *tasks*, retrying only those that errored outright."""
    done: dict[str, RolloutResult] = {}
    pending = list(tasks)
    for attempt in range(retries + 1):
        if not pending:
            break
        async with HarnessClient(
            base_url=base_url, token=token, agent_id=agent_id
        ) as client:
            results = await run_split(
                client, pending, out_dir=str(out_dir),
                concurrency=concurrency, wall_clock_cap_s=wall_clock_cap_s,
            )
        done.update({r.task_id: r for r in results})
        # Only a hard error is worth a second run. A task that merely
        # answered wrongly is a measurement, not a mishap.
        pending = [t for t in pending if done[t.task_id].error is not None]
        if pending and attempt < retries:
            print(f"  retrying {len(pending)} errored task(s)")
    return done


def make_batch_evaluator(
    *,
    harness: Any,
    tasks_by_id: dict[str, Task],
    roles: dict[str, str],
    out_root: Path,
    base_url: str,
    token: str,
    agent_id: str,
    concurrency: int = 8,
    wall_clock_cap_s: float = 1800.0,
    retries: int = 1,
    infra_abort_fraction: float = 0.25,
    infra_abort_min: int = 3,
) -> Callable[[list[tuple[str, dict]]], list[tuple[float, dict]]]:
    """Build the ``batch_evaluator`` GEPA calls with every pending pair.

    All pairs sharing a candidate are evaluated under one worker restart,
    which is the whole reason for using the batch hook: the per-pair hook
    would restart the harness once per task.
    """
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    generation = itertools.count(1)

    def batch_evaluator(pairs: list[tuple[str, dict]]) -> list[tuple[float, dict]]:
        scored: list[tuple[float, dict] | None] = [None] * len(pairs)
        by_candidate: dict[str, list[tuple[int, dict]]] = defaultdict(list)
        for index, (candidate, example) in enumerate(pairs):
            by_candidate[candidate].append((index, example))

        for candidate, items in by_candidate.items():
            number = next(generation)
            out_dir = out_root / f"cand-{number:03d}"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "candidate.md").write_text(
                harness.render(candidate), encoding="utf-8"
            )
            tasks = [tasks_by_id[example["task_id"]] for _, example in items]
            print(f"candidate {number}: {len(tasks)} task(s)")

            try:
                with harness.running(candidate):
                    results = asyncio.run(_rollout(
                        tasks, out_dir=out_dir, base_url=base_url, token=token,
                        agent_id=agent_id, concurrency=concurrency,
                        wall_clock_cap_s=wall_clock_cap_s, retries=retries,
                    ))
            except CandidateRejected as exc:
                # A proposal that will not load is genuinely a bad candidate,
                # so it scores zero -- unlike a platform failure, this says
                # something true about the text. Stopping the run over it
                # would throw away every hour spent before it.
                print(f"candidate {number}: rejected ({exc})")
                for index, example in items:
                    scored[index] = (0.0, {
                        "task_id": example["task_id"][:8],
                        "solved": False,
                        "rejected": f"candidate did not load as a fragment: {exc}",
                    })
                continue

            infra = 0
            record = []
            for index, example in items:
                task = tasks_by_id[example["task_id"]]
                result = results[task.task_id]
                answer = result.answer or ""
                strict = bool(answer) and question_scorer(answer, task.final_answer)
                lenient = bool(answer) and lenient_scorer(answer, task.final_answer)
                flags = detect(result, task.level)
                infra += is_infra_shaped(result, flags)
                info = side_info(
                    result, level=task.level,
                    role=roles.get(task.task_id, example.get("role", "?")),
                    flags=flags, strict=strict, lenient=lenient,
                )
                scored[index] = (1.0 if strict else 0.0, info)
                record.append(info)

            health_gate(
                infra, len(items), where=out_dir,
                fraction=infra_abort_fraction, minimum=infra_abort_min,
            )

            passed = sum(1 for i, _ in items if scored[i][0] > 0)
            (out_dir / "scores.json").write_text(
                json.dumps({"passed": passed, "total": len(items),
                            "infra_shaped": infra, "tasks": record}, indent=2),
                encoding="utf-8",
            )
            print(f"candidate {number}: {passed}/{len(items)} strict pass")

        # Dropping a pair here would shift every later score onto the wrong
        # example, silently. Better to stop than to return a misaligned list.
        unscored = [i for i, entry in enumerate(scored) if entry is None]
        if unscored:
            raise RuntimeError(f"{len(unscored)} pair(s) went unscored: {unscored[:5]}")
        return scored  # type: ignore[return-value]

    return batch_evaluator
