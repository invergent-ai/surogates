"""Offline planning, bounded search, resume and one final held-out test."""
from __future__ import annotations

import argparse
import os
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from harness_evolution.config import load_config, partition, read_json, task_key, write_json
from harness_evolution.experiment import final_test, run
from harness_evolution.scoring import InvalidEvaluation, normalize
from harness_evolution.benchmarks import BENCHMARKS, artifact_name, reserved_splits


def catalog(run_specs: list[str], repository: Path) -> list[dict]:
    rows, scores = {}, defaultdict(list)
    for spec in run_specs:
        benchmark, location = spec.split("=", 1)
        if benchmark not in BENCHMARKS:
            raise ValueError(f"Unsupported benchmark: {benchmark}")
        directory = Path(location)
        reservations = reserved_splits(repository, benchmark)
        run_config = read_json(directory / "run-config.json") if (directory / "run-config.json").exists() else {}
        for outcome in read_json(directory / "outcomes.json"):
            tid = str(outcome["task_id"])
            meta_file = directory / "tasks" / artifact_name(benchmark, tid) / "meta.json"
            meta = read_json(meta_file) if meta_file.exists() else {}
            try:
                result = normalize(benchmark, outcome, meta)
            except InvalidEvaluation:
                # Infrastructure failures are not task-difficulty strata.
                continue
            key = task_key(result)
            sealed = tid in reservations.get("holdout", set())
            upstream = "holdout" if sealed else ("dev" if reservations else "general")
            if benchmark == "enterpriseops_gym":
                upstream = run_config.get("mode")
                if not upstream:
                    raise ValueError("EnterpriseOps catalog needs run-config.json with the task mode")
            rows[key] = {"benchmark": benchmark, "task_id": tid,
                         "family": outcome.get("persona") or outcome.get("category") or outcome.get("domain") or str(outcome.get("level", "general")),
                         "difficulty": outcome.get("difficulty", outcome.get("level", "unknown")),
                         "upstream_split": upstream,
                         "sealed_holdout": sealed}
            if benchmark == "enterpriseops_gym":
                rows[key]["group"] = "enterpriseops_gym:" + tid
            scores[key].append(result["score"])
    for key, row in rows.items():
        values = scores[key]
        row["failure_mode"] = "unstable" if len(set(values)) > 1 else ("passed" if values[0] == 1 else "incomplete")
    return [rows[key] for key in sorted(rows)]


def preflight(config: dict, *, needs_proposer=True) -> list[str]:
    missing = []
    required_env = {"SUROGATES_SA_TOKEN"}
    for benchmark in {t["benchmark"] for t in config["task_manifest"]}:
        python = config.get("benchmark_pythons", {}).get(benchmark)
        if not python or not Path(python).is_file():
            missing.append(f"benchmark_pythons.{benchmark}: interpreter missing")
        if benchmark != "claweval":
            revision = config.get("dataset_revisions", {}).get(benchmark, "")
            if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
                missing.append(f"dataset_revisions.{benchmark} needs a pinned 40-character commit")
        if benchmark == "workspace_bench":
            required_env.update(("WSBENCH_JUDGE_BASE_URL", "WSBENCH_JUDGE_KEY", "WSBENCH_JUDGE_MODEL"))
        elif benchmark == "claweval":
            required_env.update(("CLAWEVAL_HOME", "CLAWEVAL_JUDGE_BASE_URL", "CLAWEVAL_JUDGE_KEY", "CLAWEVAL_JUDGE_MODEL"))
            if not os.environ.get("CLAWEVAL_OPS_TOKEN") and not (
                os.environ.get("CLAWEVAL_OPS_USER") and os.environ.get("CLAWEVAL_OPS_PASSWORD")
            ):
                missing.append("Claw requires test Ops authentication: CLAWEVAL_OPS_TOKEN or CLAWEVAL_OPS_USER/PASSWORD")
        elif benchmark == "enterpriseops_gym":
            required_env.add("EOG_HOME")
            if not os.environ.get("EOG_OPS_TOKEN") and not (os.environ.get("EOG_OPS_USER") and os.environ.get("EOG_OPS_PASSWORD")):
                missing.append("EnterpriseOps requires test Ops authentication: EOG_OPS_TOKEN or EOG_OPS_USER/PASSWORD")
            for field in ("tasks_dir", "seed_root"):
                path = config.get("benchmark_data", {}).get(benchmark, {}).get(field)
                if not path or not Path(path).is_dir():
                    missing.append(f"benchmark_data.{benchmark}.{field}: directory missing")
            if benchmark not in config.get("benchmark_profiles", {}):
                missing.append("benchmark_profiles.enterpriseops_gym must declare a gym-only tool profile")
        elif benchmark == "dabstep":
            path = config.get("benchmark_data", {}).get(benchmark, {}).get("answer_key")
            if not path or not Path(path).is_file():
                missing.append("benchmark_data.dabstep.answer_key: private reference key missing")
        elif benchmark == "gaia":
            required_env.add("HF_TOKEN")
    for name in sorted(required_env):
        if not os.environ.get(name):
            missing.append(f"benchmark environment variable missing: {name}")
    proposer = config.get("proposer", {})
    if needs_proposer and "recorded" in proposer:
        for path in proposer["recorded"]:
            if not Path(path).is_file():
                missing.append(f"recorded proposal missing: {path}")
    elif needs_proposer:
        for key in ("base_url_env", "api_key_env"):
            if not proposer.get(key) or not os.environ.get(proposer[key]):
                missing.append(f"proposer environment variable missing: {proposer.get(key, key)}")
        if not proposer.get("model") or proposer["model"].startswith("REPLACE_"):
            missing.append("proposer.model needs a deployed model ID")
    for tier, model in config["models"].items():
        for field in ("model", "served_model", "revision"):
            if model.get(field, "").startswith("REPLACE_"):
                missing.append(f"models.{tier}.{field} needs a concrete value")
    if any("harness_evolution.compose_runtime" in arg for arg in config["runtime"]["start"]):
        runtime_vars = ["EVOLVE_COMPOSE_FILE", "EVOLVE_PRO_AGENT_ID", "EVOLVE_STANDARD_AGENT_ID", "EVOLVE_PROJECT_ID"]
        if config.get("benchmark_profiles"):
            runtime_vars.append("EVOLVE_BENCHMARK_AGENTS_JSON")
        if any(t["benchmark"] == "enterpriseops_gym" for t in config["task_manifest"]):
            runtime_vars.append("EVOLVE_EOG_SERVICES_JSON")
        for name in runtime_vars:
            if not os.environ.get(name):
                missing.append(f"runtime environment variable missing: {name}")
        if not shutil.which("docker"):
            missing.append("Docker executable missing")
    return missing


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="harness-evolve")
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("catalog", help="Extract task descriptors from saved benchmark outcomes")
    collect.add_argument("--run", action="append", required=True, metavar="BENCHMARK=RUN_DIR")
    collect.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[3])
    collect.add_argument("--output", type=Path, required=True)
    split = sub.add_parser("split", help="Freeze a grouped, stratified search/selection/holdout manifest")
    split.add_argument("catalog", type=Path)
    split.add_argument("--output", type=Path, required=True)
    split.add_argument("--seed", type=int, default=0)
    split.add_argument("--holdout-fraction", type=float, default=0.2)
    for name in ("plan", "run", "final-test"):
        command = sub.add_parser(name)
        command.add_argument("config", type=Path)
        if name != "plan":
            command.add_argument("--run-dir", type=Path, required=True)
        if name == "run":
            command.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command in ("catalog", "split"):
            if args.output.exists():
                raise ValueError("Output already exists; task partitions must not be overwritten")
            data = catalog(args.run, args.repository) if args.command == "catalog" else partition(
                read_json(args.catalog), args.seed, args.holdout_fraction,
            )
            write_json(args.output, data)
            print(f"Wrote {len(data)} tasks to {args.output}")
            return 0
        config = load_config(args.config)
        missing = preflight(config, needs_proposer=args.command != "final-test")
        if args.command == "plan":
            counts = Counter(t["split"] for t in config["task_manifest"])
            policy = config["policy"]
            maximum = 2 * counts["search"] + policy["max_proposals"] * (
                2 + 4 * policy["repeats"] * counts["selection"] + 2 * counts["search"])
            print(f"Tasks: {dict(counts)}")
            print(f"Up to {maximum} task rollouts during search, including paired controls and repetitions")
            print(f"Final test: {4 * counts['holdout']} additional task rollouts")
            print("Editable files: " + ", ".join(config["allowed_files"]))
            print("Live preflight: " + ("ready" if not missing else "configuration incomplete"))
            for item in missing:
                print("- " + item)
            return 0
        if missing:
            raise ValueError("Live preflight failed:\n" + "\n".join(missing))
        if args.command == "run":
            state = run(config, args.run_dir, resume=args.resume)
            print(f"{state['status']}; report: {args.run_dir / 'report.md'}")
        else:
            final_test(config, args.run_dir)
            print(f"Final test recorded in {args.run_dir}; search is sealed")
        return 0
    except (ValueError, OSError, InvalidEvaluation, TimeoutError) as exc:
        parser.exit(2, f"harness-evolve: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
