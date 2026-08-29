"""Offline evaluation of a doubter (critic) against stored GAIA trajectories.

The question this answers: if a critic model reviewed the agent's action
history before it answered, would it flag the runs that turned out wrong --
and would it leave the correct ones alone?

Both halves matter. A doubter that catches every failure by objecting to
everything costs a rerun on every task and makes the agent worse.

Runs entirely on stored traces: no agent, no browser, no benchmark. A pass
over 110 trajectories is ~722k tokens, which on deepseek-v4-flash is a few
cents -- against $73+ for a live run.

Usage:
    python experiments/doubter_eval.py dev-022 [--limit N] [--model ID]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import sys

import httpx
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = pathlib.Path.home() / ".surogate" / "config.yaml"

# Reasoning first, verdict last: guided decoding emits keys in declaration
# order, so a leading score would be produced before the analysis that is
# supposed to justify it.
SCHEMA = {
    "type": "object",
    "properties": {
        "flaws": {
            "type": "string",
            "description": (
                "Concrete flaws in the action history: a missing step, an "
                "unverified assumption, a question asked but not answered. "
                "Empty string if the history is sound."
            ),
        },
        "score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 10,
            "description": "0 = illogical and full of errors, 10 = sound and complete.",
        },
        "rerun": {
            "type": "boolean",
            "description": "True if the agent should run again.",
        },
    },
    "required": ["flaws", "score", "rerun"],
    "additionalProperties": False,
}

INSTRUCTION = """You are reviewing the actions an agent took to answer a question.

Look for a logical flaw or a missing step: a claim it never verified, a
question it answered from memory when it had a tool available, a sub-question
in the task it silently skipped, an answer that does not follow from what it
actually found.

Not every history is flawed. Many are sound, and saying so is the correct
answer -- objecting to good work costs a wasted rerun. Judge only what the
history shows.

The history ends just before the agent commits to its answer, so the absence
of a verification step at the end is expected. Do not report that as a flaw.
"""


def load_summary_endpoint() -> tuple[str, str, str]:
    cfg = yaml.safe_load(CONFIG.read_text())
    return (
        cfg["summary_llm_endpoint"].rstrip("/"),
        cfg["summary_llm_key"],
        cfg["summary_llm_model"],
    )


async def review(client: httpx.AsyncClient, model: str, question: str,
                 trajectory: str, sem: asyncio.Semaphore) -> dict | None:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": INSTRUCTION},
            {"role": "user", "content": f"Task:\n{question}\n\nAction history:\n{trajectory}"},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "doubt", "strict": True, "schema": SCHEMA},
        },
    }
    async with sem:
        for attempt in range(3):
            try:
                r = await client.post("/chat/completions", json=body, timeout=180)
                if r.status_code != 200:
                    if attempt == 2:
                        print(f"    HTTP {r.status_code}: {r.text[:160]}", file=sys.stderr)
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                content = r.json()["choices"][0]["message"]["content"]
                return json.loads(content)
            except Exception as exc:  # noqa: BLE001 - one bad trace must not kill the sweep
                if attempt == 2:
                    print(f"    {type(exc).__name__}: {exc}", file=sys.stderr)
                await asyncio.sleep(2 * (attempt + 1))
    return None


def question_of(task_dir: pathlib.Path) -> str:
    """First user message, which carries the task text."""
    events = task_dir / "events.jsonl"
    if not events.exists():
        return ""
    for line in events.open():
        e = json.loads(line)
        if e.get("type") == "user.message" and isinstance(e.get("data"), dict):
            c = e["data"].get("content") or ""
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return c[:4000]
    return ""


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--model")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    run = ROOT / "runs" / args.run_id
    outcomes = {x["task_id"]: x for x in json.loads((run / "outcomes.json").read_text())}
    base, key, model = load_summary_endpoint()
    model = args.model or model

    tasks = []
    for tid, out in outcomes.items():
        d = run / "tasks" / tid
        traj = d / "trajectory.md"
        if traj.exists():
            tasks.append((tid, bool(out.get("strict_pass")), question_of(d),
                          traj.read_text(errors="replace")))
    if args.limit:
        tasks = tasks[: args.limit]

    print(f"{len(tasks)} trajectories from {args.run_id} | model={model}")
    print(f"  {sum(1 for t in tasks if t[1])} passed / {sum(1 for t in tasks if not t[1])} failed\n")

    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(
        base_url=base, headers={"Authorization": f"Bearer {key}"}
    ) as client:
        verdicts = await asyncio.gather(*[
            review(client, model, q, tr, sem) for _, _, q, tr in tasks
        ])

    tp = fp = tn = fn = err = 0
    rows = []
    for (tid, passed, _, _), v in zip(tasks, verdicts):
        if v is None:
            err += 1
            continue
        flagged = bool(v.get("rerun"))
        if not passed and flagged:
            tp += 1
        elif passed and flagged:
            fp += 1
        elif passed and not flagged:
            tn += 1
        else:
            fn += 1
        rows.append((tid[:8], passed, flagged, v.get("score"), (v.get("flaws") or "")[:90]))

    print(f"{'task':9} {'truth':>7} {'doubter':>8} {'score':>5}  flaws")
    for r in sorted(rows, key=lambda x: (x[1], x[2])):
        print(f"{r[0]:9} {'pass' if r[1] else 'FAIL':>7} "
              f"{'rerun' if r[2] else 'ok':>8} {str(r[3]):>5}  {r[4]!r}")

    out_path = run / "doubter_verdicts.json"
    out_path.write_text(json.dumps([
        {"task_id": tid, "strict_pass": passed, "verdict": v}
        for (tid, passed, _, _), v in zip(tasks, verdicts)
    ], indent=1))
    print(f"\n  verdicts written to {out_path}")

    graded = tp + fp + tn + fn
    print(f"\n  errors: {err}   graded: {graded}")
    if graded:
        print(f"  caught {tp}/{tp+fn} real failures     ({tp/(tp+fn)*100:.0f}% detection)"
              if tp + fn else "  no failures in set")
        print(f"  flagged {fp}/{fp+tn} correct runs      ({fp/(fp+tn)*100:.0f}% false positive)"
              if fp + tn else "  no passes in set")
        print(f"  a rerun triggered here is right {tp/(tp+fp)*100:.0f}% of the time"
              if tp + fp else "  never triggered a rerun")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
