"""Run benchmark CLIs in their own interpreters and private run dirs.

The controller launches this file with the benchmark's venv, keeping the
benchmark dependency graphs and their vendored graders independent.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from functools import partial
from dataclasses import replace
from pathlib import Path

from harness_evolution.config import read_json, write_json
from harness_evolution.benchmarks import PREFIXES, artifact_name, upstream_split
from harness_evolution.runtime import check_endpoint
from harness_evolution.scoring import InvalidEvaluation, normalize, validate_results


def run(request: dict) -> list[dict]:
    name = request["benchmark"]
    out = Path(request["output_dir"])
    if out.exists():
        raise ValueError("Benchmark output directory already exists")
    benchmark_root = Path(request["benchmark_root"])
    sys.path.insert(0, str(benchmark_root / name))
    tasks = request["tasks"]
    upstream = {upstream_split(t) for t in tasks}
    if len(upstream) != 1:
        raise ValueError("One upstream split per driver invocation is required")
    split = upstream.pop()
    ids = ",".join(t["task_id"] for t in tasks)
    runtime = request["runtime"]
    prefix = PREFIXES[name]
    os.environ[f"{prefix}_BASE_URL"] = runtime["base_url"]
    agents = runtime.get("benchmark_agents", {}).get(name, runtime["agents"])
    os.environ[f"{prefix}_AGENT_ID"] = agents[request["tier"]]
    revision = request.get("dataset_revision", "")
    if name != "claweval":
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise InvalidEvaluation(f"{name} dataset needs a pinned 40-character commit")
        os.environ[f"{prefix}_DATASET_REVISION"] = revision
    if name == "claweval":
        os.environ["CLAWEVAL_OPS_BASE_URL"] = runtime["ops_base_url"]
        os.environ["CLAWEVAL_PROJECT_ID"] = runtime["project_id"]
        if not os.environ.get("CLAWEVAL_JUDGE_BASE_URL"):
            raise InvalidEvaluation("Claw selection requires a configured grader model")
        from claweval_bench import cli
        cli.RUNS_DIR = out.parent
        args = cli.build_parser().parse_args([
            "run", "--split", split, "--tasks", ids, "--run-id", out.name,
            "--wall-clock-cap", str(request["task_timeout_s"]),
        ])
        if asyncio.run(cli._cmd_run(args)):
            raise InvalidEvaluation("Claw runner failed")
    elif name == "workspace_bench":
        from wsbench import cli
        from wsbench import dataset
        from huggingface_hub import snapshot_download

        revision = request.get("dataset_revision", "")
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise InvalidEvaluation("Workspace dataset needs a pinned 40-character commit")
        data_root = snapshot_download(repo_id=dataset.HF_DATASET, repo_type="dataset", revision=revision,
                                      allow_patterns=[dataset.CSV_NAME, f"{dataset.TASK_DIR_PREFIX}/*"])
        # Both unchanged CLI phases consume the exact same dataset snapshot.
        cli.load_tasks = partial(dataset.load_tasks, snapshot_dir=data_root)
        cli.RUNS_DIR = out.parent
        args = cli.build_parser().parse_args([
            "run", "--split", split, "--tasks", ids, "--run-id", out.name,
            "--concurrency", "1", "--wall-clock-cap", str(request["task_timeout_s"]),
        ])
        if asyncio.run(cli._cmd_run(args)):
            raise InvalidEvaluation("Workspace runner failed")
        args = cli.build_parser().parse_args(["judge", out.name, "--split", split, "--concurrency", "1"])
        if asyncio.run(cli._cmd_judge(args)):
            raise InvalidEvaluation("Workspace judge failed")
    elif name == "enterpriseops_gym":
        from eogbench import cli
        data = request["benchmark_data"]
        if data["mode"] != split:
            raise InvalidEvaluation("EnterpriseOps task mode does not match frozen data")
        os.environ["EOG_TASKS_DIR"] = data["tasks_dir"]
        os.environ["EOG_SEED_ROOT"] = data["seed_root"]
        os.environ["EOG_OPS_BASE_URL"] = runtime["ops_base_url"]
        os.environ["EOG_PROJECT_ID"] = runtime["project_id"]
        os.environ.pop("EOG_GYM_URL", None)
        domains = sorted({t["task_id"].split("/")[0] for t in tasks})
        urls = runtime.get("gym_urls", {})
        for domain in domains:
            check_endpoint(urls.get(domain, ""))
        load_tasks = cli.load_tasks
        cli.load_tasks = lambda *args, **kwargs: [replace(t, gym_url=urls[t.domain]) for t in load_tasks(*args, **kwargs)]
        cli.RUNS_DIR = out.parent
        args = cli.build_parser().parse_args([
            "run", "--domains", ",".join(domains), "--tasks", ids, "--run-id", out.name,
            "--wall-clock-cap", str(request["task_timeout_s"]),
        ])
        if asyncio.run(cli._cmd_run(args)):
            raise InvalidEvaluation("EnterpriseOps runner failed")
        write_json(out / "run-config.json", {"mode": split, "dataset_revision": revision})
    else:
        if name == "dabstep":
            from dabbench import cli
            key = read_json(Path(request["benchmark_data"]["answer_key"]))
            cli.load_key = lambda: key
        elif name == "gaia":
            from gaia_bench import cli
        else:
            raise InvalidEvaluation(f"Unknown benchmark {name}")
        cli.RUNS_DIR = out.parent
        args = cli.build_parser().parse_args([
            "run", "--split", split, "--tasks", ids, "--run-id", out.name,
            "--concurrency", "1", "--wall-clock-cap", str(request["task_timeout_s"]),
        ])
        if asyncio.run(cli._cmd_run(args)):
            raise InvalidEvaluation(f"{name} runner failed")
        if name == "dabstep":
            args = cli.build_parser().parse_args(["score", out.name, "--split", split])
            if cli._cmd_score(args):
                raise InvalidEvaluation("DABstep scorer failed")
    outcomes = read_json(out / "outcomes.json")
    rows = []
    for outcome in outcomes:
        task_dir = out / "tasks" / artifact_name(name, str(outcome["task_id"]))
        meta = read_json(task_dir / "meta.json")
        row = normalize(name, outcome, meta)
        row["trace_path"] = str(task_dir / "events.jsonl")
        expected_model = runtime["models"][request["tier"]].get("served_model", runtime["models"][request["tier"]]["model"])
        events = [json.loads(line) for line in Path(row["trace_path"]).read_text().splitlines() if line.strip()]
        observed = {e["data"].get("model") for e in events if e.get("type") == "llm.request"}
        if observed != {expected_model}:
            raise InvalidEvaluation("Observed LLM model does not match the pinned tier binding")
        row["observed_model"] = expected_model
        row["session_id"] = meta.get("session_id")
        rows.append(row)
    validate_results(rows, tasks)
    return rows


def main() -> None:
    request = read_json(Path(sys.argv[1]))
    write_json(Path(sys.argv[2]), run(request))


if __name__ == "__main__":
    main()
