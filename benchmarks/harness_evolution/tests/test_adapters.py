"""Exercise the adapter boundary without model calls or benchmark services."""
import argparse
import ast
import json
import sys
import types
from pathlib import Path

import httpx
import pytest

from harness_evolution import benchmark_driver, compose_runtime
from harness_evolution.candidates import source_hash
from harness_evolution.config import read_json, write_json
from harness_evolution.proposer import Proposer
from harness_evolution.scoring import InvalidEvaluation


def parser_from_benchmark(benchmark, package):
    # Use the actual CLI parser to catch adapter/argument drift, without
    # importing that benchmark's separate grader dependency graph.
    path = Path(__file__).resolve().parents[2] / benchmark / package / "cli.py"
    tree = ast.parse(path.read_text())
    definition = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_parser")
    namespace = {"argparse": argparse, "pathlib": __import__("pathlib"), "DEFAULT_CONCURRENCY": 3}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["build_parser"]


@pytest.fixture
def driver_fixture(tmp_path, monkeypatch):
    def install(benchmark, *, model="deployed-model", error=None):
        package = "wsbench" if benchmark == "workspace_bench" else "claweval_bench"
        cli = types.ModuleType(package + ".cli")
        cli.build_parser = parser_from_benchmark(benchmark, package)
        calls = []
        output = tmp_path / "private-evaluation" / "artifacts"
        outcome = {"benchmark": benchmark, "task_id": "100", "terminal_status": "completed",
                   "scores": {"safety": 1, "completion": 1}, "passed_rubrics": 3, "total_rubrics": 4,
                   "error": error}

        async def run(args):
            calls.append(args)
            assert args.tasks == "100"
            assert cli.RUNS_DIR / args.run_id == output
            if benchmark == "workspace_bench":
                assert cli.load_tasks(args.split) == ["pinned-task-fixture"]
            task_dir = output / "tasks/100"
            write_json(task_dir / "meta.json", {"wall_clock_s": 2, "session_id": "session-100"})
            (task_dir / "events.jsonl").write_text(json.dumps({"type": "llm.request", "data": {"model": model}}) + "\n")
            if benchmark == "claweval":
                write_json(output / "outcomes.json", [outcome])
            return 0

        async def judge(args):
            calls.append(args)
            assert cli.load_tasks(args.split) == ["pinned-task-fixture"]
            assert not (output / "outcomes.json").exists()
            write_json(output / "outcomes.json", [outcome])
            return 0

        cli._cmd_run, cli._cmd_judge = run, judge
        module = types.ModuleType(package)
        module.cli = cli
        if benchmark == "workspace_bench":
            dataset = types.ModuleType(package + ".dataset")
            dataset.HF_DATASET, dataset.CSV_NAME, dataset.TASK_DIR_PREFIX = "fixture-dataset", "tasks.csv", "tasks"

            def load_tasks(split, snapshot_dir=None):
                assert snapshot_dir == str(tmp_path / "pinned-data")
                return ["pinned-task-fixture"]

            def download(**kwargs):
                assert kwargs["revision"] == "a" * 40
                assert kwargs["repo_id"] == dataset.HF_DATASET
                return str(tmp_path / "pinned-data")

            dataset.load_tasks = load_tasks
            module.dataset = dataset
            hub = types.ModuleType("huggingface_hub")
            hub.snapshot_download = download
            monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
            monkeypatch.setitem(sys.modules, package + ".dataset", dataset)
        monkeypatch.setitem(sys.modules, package, module)
        monkeypatch.setitem(sys.modules, package + ".cli", cli)
        monkeypatch.setattr(sys, "path", list(sys.path))
        # Restore all driver-written environment variables after the test.
        for name in ("WSBENCH_BASE_URL", "WSBENCH_AGENT_ID", "CLAWEVAL_BASE_URL", "CLAWEVAL_AGENT_ID",
                     "CLAWEVAL_OPS_BASE_URL", "CLAWEVAL_PROJECT_ID"):
            monkeypatch.setenv(name, "unused")
        monkeypatch.setenv("CLAWEVAL_JUDGE_BASE_URL", "http://judge.test")
        request = {"benchmark": benchmark, "tier": "standard", "output_dir": str(output),
                   "benchmark_root": str(tmp_path / "evaluator"), "task_timeout_s": 30,
                   "dataset_revision": "a" * 40,
                   "tasks": [{"benchmark": benchmark, "task_id": "100"}],
                   "runtime": {"base_url": "http://127.0.0.1:8000", "ops_base_url": "http://127.0.0.1:8888",
                               "agents": {"pro": "p", "standard": "s"}, "project_id": "project",
                               "models": {"standard": {"model": "canonical-model", "served_model": "deployed-model"}}}}
        return request, calls
    return install


@pytest.mark.parametrize("benchmark,score", [("claweval", 1), ("workspace_bench", 0.75)])
def test_driver_uses_existing_parser_and_grades_saved_rollouts(driver_fixture, benchmark, score, monkeypatch):
    request, calls = driver_fixture(benchmark)
    rows = benchmark_driver.run(request)
    assert rows[0]["score"] == score
    assert rows[0]["seconds"] == 2
    assert rows[0]["observed_model"] == "deployed-model"
    assert rows[0]["session_id"] == "session-100"
    assert calls[0].wall_clock_cap == 30
    assert [c.command for c in calls] == (["run", "judge"] if benchmark == "workspace_bench" else ["run"])
    if benchmark == "workspace_bench":
        assert calls[0].concurrency == calls[1].concurrency == 1
    with pytest.raises(ValueError, match="already exists"):
        benchmark_driver.run(request)


def test_driver_rejects_wrong_deployed_model(driver_fixture):
    request, _ = driver_fixture("workspace_bench", model="wrong-tier")
    with pytest.raises(InvalidEvaluation, match="model"):
        benchmark_driver.run(request)


def test_driver_rejects_mutable_dataset_revision(driver_fixture):
    request, _ = driver_fixture("workspace_bench")
    request["dataset_revision"] = "main"
    with pytest.raises(InvalidEvaluation, match="pinned"):
        benchmark_driver.run(request)


def test_driver_rejects_infrastructure_failure(driver_fixture):
    request, _ = driver_fixture("claweval", error="provider unavailable")
    with pytest.raises(InvalidEvaluation, match="Rollout"):
        benchmark_driver.run(request)


def test_proposer_http_contract_and_deadline(monkeypatch):
    monkeypatch.setenv("TEST_PROPOSER_URL", "https://proposer.test/v1/")
    monkeypatch.setenv("TEST_PROPOSER_KEY", "fixture-key")
    expected = {"hypothesis": "Verify outputs", "files": {"a.md": "Verify."}}
    captured = {}

    def respond(request):
        body = json.loads(request.content)
        assert request.url == "https://proposer.test/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer fixture-key"
        assert body["model"] == "proposer-model"
        assert "tools" not in body
        assert json.loads(body["messages"][1]["content"]) == {"files": {"a.md": "Check."}}
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + json.dumps(expected) + "\n```"}}]})

    client = httpx.Client

    def make_client(**kwargs):
        captured.update(kwargs)
        return client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("harness_evolution.proposer.httpx.Client", make_client)
    proposer = Proposer({"base_url_env": "TEST_PROPOSER_URL", "api_key_env": "TEST_PROPOSER_KEY", "model": "proposer-model"})
    assert proposer.propose({"files": {"a.md": "Check."}}, 0, timeout=4) == expected
    assert captured["timeout"] == 4


def test_compose_hook_passes_bindings_and_removes_only_generated_project(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("x = 1\n")
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n")
    monkeypatch.setenv("EVOLVE_COMPOSE_FILE", str(compose))
    for name, value in (("PRO_AGENT_ID", "p"), ("STANDARD_AGENT_ID", "s"), ("PROJECT_ID", "project")):
        monkeypatch.setenv("EVOLVE_" + name, value)
    request = {"runtime_id": "evolve-" + "a" * 32, "source_dir": str(source), "source_hash": source_hash(source),
               "benchmark_profiles": {"enterpriseops_gym": "gym-only"},
               "models": {"pro": {"model": "pro-model", "revision": "pinned"},
                          "standard": {"model": "standard-model", "revision": "pinned"}}}
    monkeypatch.setenv("EVOLVE_BENCHMARK_AGENTS_JSON", json.dumps({"enterpriseops_gym": {"pro": "ep", "standard": "es"}}))
    monkeypatch.setenv("EVOLVE_EOG_SERVICES_JSON", json.dumps({"csm": {"service": "gym-csm", "port": 8001}}))
    request_path, receipt = tmp_path / "request.json", tmp_path / "receipt.json"
    write_json(request_path, request)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[3] == request["runtime_id"]
        assert json.loads(kwargs["env"]["EVOLVE_MODEL_BINDINGS_JSON"]) == request["models"]
        assert kwargs["env"]["EVOLVE_SOURCE_DIR"] == str(source)
        assert json.loads(kwargs["env"]["EVOLVE_BENCHMARK_PROFILES_JSON"]) == request["benchmark_profiles"]

    monkeypatch.setattr(compose_runtime.subprocess, "run", run)
    monkeypatch.setattr(compose_runtime.subprocess, "check_output", lambda *args, **kwargs: "127.0.0.1:18000\n")
    compose_runtime.run("start", request_path, receipt)
    assert read_json(receipt)["agents"] == {"pro": "p", "standard": "s"}
    assert read_json(receipt)["models"] == request["models"]
    assert read_json(receipt)["gym_urls"] == {"csm": "http://127.0.0.1:18000"}
    assert read_json(receipt)["benchmark_agents"]["enterpriseops_gym"]["standard"] == "es"
    compose_runtime.run("stop", request_path, receipt)
    assert calls[-1][-3:] == ["down", "--volumes", "--remove-orphans"]
    request["runtime_id"] = "production"
    write_json(request_path, request)
    with pytest.raises(ValueError, match="non-experiment"):
        compose_runtime.run("stop", request_path, receipt)
    assert len(calls) == 2
