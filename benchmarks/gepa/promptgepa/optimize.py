"""GEPA wiring and the command line.

    promptgepa sets     --runs dev-018,dev-021,dev-022,dev-023
    promptgepa optimize --runs dev-018,dev-021,dev-022,dev-023
    promptgepa apply    runs/opt-001

``optimize`` reflects on the stable failures and selects on a held-out mix
of failures and known-passing guards, then writes the winning fragment for
``apply`` to land in the tree.  It does not commit anything and it does not
claim an improvement: a candidate that wins here still owes the benchmark's
own bar of three interleaved full-dev runs before the number means
anything.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from gepa import TimeoutStopCondition
from gepa.optimize_anything import (
    EngineConfig, GEPAConfig, ReflectionConfig, optimize_anything,
)

from promptgepa.evaluate import make_batch_evaluator
from promptgepa.harness import WorkerHarness
from promptgepa.sets import derive

REPO_ROOT = Path(__file__).resolve().parents[3]
GAIA_RUNS = REPO_ROOT / "benchmarks" / "gaia" / "runs"
RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"
DEFAULT_FRAGMENT = "guidance/execution_discipline"
DEFAULT_JUDGE_MODEL = "claude-sonnet-5"

OBJECTIVE = """\
Rewrite this guidance fragment so the agent finishes hard multi-step
research tasks instead of stalling: it should act rather than narrate the
action, keep using tools until it has actually verified an answer, and end
every task with a concrete final answer in the format the task asked for.
Improve the general behaviour, never the handling of any particular task.
"""

BACKGROUND = """\
WHAT THIS TEXT IS
The candidate is the body of one markdown fragment in a production agent
harness's system prompt. The harness assembles the system prompt from many
fragments; this one is injected for every model whose identifier matches a
discipline list, on every channel, for every agent on the platform. It is
not a per-task or per-benchmark prompt. Frontmatter is re-attached
automatically -- write the body only, starting at the heading.

HOW IT IS BEING MEASURED
Scoring runs GAIA validation tasks (web research, file handling, multi-step
reasoning) through the real harness and scores the final answer with the
official GAIA scorer: 1.0 for an exact match, 0.0 otherwise. The tasks in
the training set are ones the current fragment fails on every run; the
selection set mixes further held-out failures with tasks the current
fragment already passes, so a rewrite that breaks working behaviour loses
points immediately.

The agent has tools including: terminal, read_file, search_files,
write_file, web_search, web_extract, a browser, and vision. Each task
prompt already carries GAIA's own "FINAL ANSWER:" formatting instructions
-- the fragment does not need to restate the format, but the agent does
need to actually reach a final answer.

WHAT THE FAILURE FLAGS MEAN
  no_final_answer      the session ended with no FINAL ANSWER line
  no_tool_use          the model stated an intention and called no tool
  empty_llm_response   the provider returned nothing (platform-side)
  tool_error           a tool call came back an error
A near-zero elapsed time on a tool usually means it failed instantly.

CONSTRAINTS
- Stay general. Never mention GAIA, a benchmark, a specific task, a
  specific website, or an answer to any question. Text that only helps one
  task is a defect: this fragment ships to every user of the platform.
- Keep it roughly the length of the current fragment. It shares a prompt
  budget with a dozen other fragments.
- Keep the XML-ish section tags. Other tooling and other fragments assume
  that shape.
- Markdown body only. No frontmatter, no surrounding code fence.
"""


class ReflectionError(RuntimeError):
    """The reflection LM returned nothing usable."""


def make_reflection_lm(
    base_url: str, api_key: str, model: str,
    timeout: float = 600.0, max_tokens: int = 16000, attempts: int = 3,
):
    """A sync ``(prompt) -> str`` callable, GEPA's LanguageModel protocol.

    Retried, because the proposer sits in the middle of a run measured in
    hours: one transient 5xx or one empty reply would otherwise throw away
    every rollout paid for up to that point.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"

    def once(messages: list[dict[str, Any]]) -> str:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        if response.status_code >= 400:
            raise ReflectionError(
                f"reflection call failed (HTTP {response.status_code}): "
                f"{response.text[:300]}"
            )
        choices = response.json().get("choices") or []
        content = (choices[0].get("message", {}).get("content") or "") if choices else ""
        if not content.strip():
            # Empty content with finish_reason=length is the thinking-burn
            # signature: the whole budget spent on hidden reasoning with
            # nothing visible returned. Worth retrying -- it is a sampling
            # outcome, not a deterministic refusal.
            finish = choices[0].get("finish_reason") if choices else None
            raise ReflectionError(
                f"reflection LM returned empty content (finish_reason={finish!r})"
            )
        return content

    def call(prompt: str | list[dict[str, Any]]) -> str:
        messages = (
            prompt if isinstance(prompt, list)
            else [{"role": "user", "content": prompt}]
        )
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return once(messages)
            except (ReflectionError, httpx.HTTPError) as exc:
                last = exc
                print(f"  reflection attempt {attempt + 1}/{attempts} failed: {exc}")
                if attempt + 1 < attempts:
                    time.sleep(2 ** attempt * 5)
        raise ReflectionError(f"reflection LM failed {attempts}x; last: {last}")

    return call


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set -- see benchmarks/gepa/README.md")
    return value


def _resolve_runs(spec: str) -> list[Path]:
    """Accept run ids (resolved under the GAIA runs dir) or explicit paths."""
    resolved = []
    for item in (s.strip() for s in spec.split(",") if s.strip()):
        path = Path(item)
        if not path.exists():
            path = GAIA_RUNS / item
        if not path.exists():
            raise SystemExit(f"no such run: {item} (looked in {GAIA_RUNS})")
        resolved.append(path)
    return resolved


def _next_run_dir(prefix: str) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    existing = [d.name for d in RUNS_DIR.iterdir() if d.name.startswith(prefix)]
    return RUNS_DIR / f"{prefix}-{len(existing) + 1:03d}"


def _build_sets(args: argparse.Namespace):
    flags = tuple(
        f.strip() for f in (args.train_flags or "").split(",") if f.strip()
    )
    return derive(
        _resolve_runs(args.runs),
        guard_size=args.guard_size,
        seed=args.seed,
        train_flags=flags or None,
    )


def cmd_sets(args: argparse.Namespace) -> int:
    print(_build_sets(args).summary())
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    from gaia_bench.dataset import load_tasks

    # Everything the run needs, checked before anything is created or spent:
    # discovering a missing key an hour in costs an hour.
    for name in ("SUROGATES_SA_TOKEN", "GAIA_AGENT_ID",
                 "GAIA_JUDGE_BASE_URL", "GAIA_JUDGE_KEY"):
        _require(name)

    sets = _build_sets(args)
    print(sets.summary(), "\n")

    run_dir = (Path(args.run_dir).resolve() if args.run_dir
               else _next_run_dir("opt"))
    run_dir.mkdir(parents=True, exist_ok=True)

    tasks_by_id = {t.task_id: t for t in load_tasks(args.split)}
    missing = [t for t in sets.train + sets.val if t not in tasks_by_id]
    if missing:
        raise SystemExit(f"{len(missing)} task(s) not in split {args.split}: {missing[:5]}")

    roles = {t: "train" for t in sets.train}
    roles.update({t: "guard" for t in sets.guard})
    roles.update({t: "val" for t in sets.val if t not in roles})

    harness = WorkerHarness(
        repo_root=REPO_ROOT,
        fragment=args.fragment,
        workdir=run_dir / "harness",
        health_port=args.health_port,
    )
    evaluator = make_batch_evaluator(
        harness=harness,
        tasks_by_id=tasks_by_id,
        roles=roles,
        out_root=run_dir / "candidates",
        base_url=os.environ.get("GAIA_BASE_URL", "http://localhost:8000"),
        token=_require("SUROGATES_SA_TOKEN"),
        agent_id=_require("GAIA_AGENT_ID"),
        concurrency=args.concurrency,
        wall_clock_cap_s=args.wall_clock_cap,
    )

    # Sized off the selection set, not off one proposal: every candidate is
    # scored on the whole valset, so a budget that forgets to multiply stops
    # the search after a single rewrite and reports the seed as the winner.
    budget = args.budget or args.proposals * len(sets.val)
    print(
        f"fragment    {args.fragment}\n"
        f"budget      {budget} rollouts (~{args.proposals} proposals "
        f"x {len(sets.val)} val tasks)\n"
        f"run dir     {run_dir}\n"
    )

    result = optimize_anything(
        seed_candidate=harness.seed_body,
        batch_evaluator=evaluator,
        dataset=[{"task_id": t, "role": "train"} for t in sets.train],
        valset=[{"task_id": t, "role": roles[t]} for t in sets.val],
        objective=OBJECTIVE,
        background=BACKGROUND,
        config=GEPAConfig(
            engine=EngineConfig(
                run_dir=str(run_dir / "gepa"),
                seed=args.seed,
                max_metric_calls=budget,
                # There is no resume in this release, and a search runs for
                # hours against a local stack that can die under it. The
                # disk cache is keyed (candidate, example), so relaunching
                # with the same --run-dir replays what was already scored
                # instead of paying for it twice. Cache hits do not consume
                # max_metric_calls, hence the proposal cap and the timeout.
                cache_evaluation=not args.no_cache,
                cache_evaluation_storage="disk",
                max_candidate_proposals=args.proposals * 3,
                # Fan-out lives inside the batch evaluator, which restarts
                # the worker per candidate; a second layer of threads here
                # would race that restart.
                parallel=False,
                max_workers=1,
                display_progress_bar=False,
            ),
            reflection=ReflectionConfig(
                reflection_lm=make_reflection_lm(
                    base_url=_require("GAIA_JUDGE_BASE_URL"),
                    api_key=_require("GAIA_JUDGE_KEY"),
                    model=os.environ.get("GAIA_JUDGE_MODEL", DEFAULT_JUDGE_MODEL),
                ),
                reflection_minibatch_size=args.minibatch,
            ),
            stop_callbacks=[TimeoutStopCondition(args.timeout_h * 3600)],
        ),
    )

    best = result.best_candidate
    if isinstance(best, dict):
        best = next(iter(best.values()))
    (run_dir / "best_body.md").write_text(best, encoding="utf-8")
    (run_dir / "best_fragment.md").write_text(harness.render(best), encoding="utf-8")
    # candidates[0] is always the seed, so its val score is the baseline the
    # winner has to beat -- on the same 24 tasks, in the same session.
    val_scores = list(result.val_aggregate_scores or [])
    seed_score = val_scores[0] if val_scores else None
    best_score = val_scores[result.best_idx] if val_scores else None

    (run_dir / "result.json").write_text(json.dumps({
        "fragment": args.fragment,
        "runs": list(sets.runs),
        "train": list(sets.train),
        "val": list(sets.val),
        "guard": list(sets.guard),
        "budget": budget,
        "num_candidates": result.num_candidates,
        "total_metric_calls": result.total_metric_calls,
        "best_idx": result.best_idx,
        "seed_val_score": seed_score,
        "best_val_score": best_score,
        "val_aggregate_scores": val_scores,
        "val_subscores": [list(s) for s in (result.val_subscores or [])],
    }, indent=2), encoding="utf-8")

    print(
        f"\n{result.num_candidates} candidate(s), "
        f"{result.total_metric_calls} rollouts\n"
        f"val score:     {seed_score} (seed) -> {best_score} (best) "
        f"over {len(sets.val)} tasks\n"
        f"best fragment: {run_dir / 'best_fragment.md'}\n\n"
        "This is a search result, not a measured improvement. Before "
        "believing it:\n"
        "  promptgepa apply " + str(run_dir) + "\n"
        "  then 3 candidate and 3 seed full-dev runs, interleaved in one "
        "session\n  (identical config lost 9 points across two days once -- "
        "see RESULTS.md),\n  then holdout once."
    )
    if result.num_candidates <= 1:
        print(
            "\nWARNING: only one candidate was proposed. The budget was too "
            "small, or\nthe reflection LM failed -- check the log before "
            "reading anything into this."
        )
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    source = run_dir / "best_fragment.md"
    if not source.exists():
        raise SystemExit(f"no best_fragment.md in {run_dir}")
    fragment = args.fragment
    if (run_dir / "result.json").exists():
        fragment = json.loads((run_dir / "result.json").read_text())["fragment"]
    target = REPO_ROOT / "surogates" / "harness" / "prompts" / f"{fragment}.md"
    shutil.copyfile(source, target)
    print(f"{source} -> {target}\nreview the diff before committing: git diff -- {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="promptgepa")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--runs", required=True,
                       help="Comma-separated GAIA run ids or paths (>=2)")
        p.add_argument("--guard-size", type=int, default=12,
                       help="Known-passing tasks mixed into the selection set")
        p.add_argument("--train-flags", default=None,
                       help="Only train on stable failures carrying these flags "
                            "(default: any flag)")
        p.add_argument("--seed", type=int, default=0)

    sets_p = sub.add_parser("sets", help="Show the derived task sets and stop")
    common(sets_p)

    opt = sub.add_parser("optimize", help="Run GEPA over one prompt fragment")
    common(opt)
    opt.add_argument("--fragment", default=DEFAULT_FRAGMENT)
    opt.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    opt.add_argument("--proposals", type=int, default=18,
                     help="Target number of proposals; sizes the budget")
    opt.add_argument("--budget", type=int, default=None,
                     help="Explicit rollout cap, overriding --proposals")
    opt.add_argument("--minibatch", type=int, default=4)
    # 4 is the GAIA benchmark's own default and what its recorded runs
    # used. 8 OOM-killed this box: agent sessions hold headless browsers,
    # and the local stack shares the machine with them.
    opt.add_argument("--concurrency", type=int, default=4)
    opt.add_argument("--wall-clock-cap", type=float, default=1800.0)
    opt.add_argument("--timeout-h", type=float, default=12.0)
    opt.add_argument("--health-port", type=int, default=None,
                     help="Worker health port to poll for readiness "
                          "(default: a free one, so it cannot collide)")
    opt.add_argument("--run-dir", default=None,
                     help="Reuse a previous run dir to resume: cached "
                          "(candidate, example) scores are replayed free")
    opt.add_argument("--no-cache", action="store_true",
                     help="Disable the on-disk evaluation cache")

    apply_p = sub.add_parser("apply", help="Copy a run's best fragment into the tree")
    apply_p.add_argument("run_dir")
    apply_p.add_argument("--fragment", default=DEFAULT_FRAGMENT)

    return parser


def _unwind_on_sigterm(signum: int, frame: Any) -> None:
    """Turn SIGTERM into an exception so context managers get to run.

    Python's default SIGTERM handling exits without unwinding, which would
    skip the worker shutdown. The kernel-level PDEATHSIG in
    :mod:`promptgepa.harness` is the backstop; this is the tidy path.
    """
    raise KeyboardInterrupt(f"signal {signum}")


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _unwind_on_sigterm)
    args = build_parser().parse_args(argv)
    if args.command == "sets":
        return cmd_sets(args)
    if args.command == "apply":
        return cmd_apply(args)
    return cmd_optimize(args)


if __name__ == "__main__":
    sys.exit(main())
