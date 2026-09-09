import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness_evolution.candidates import source_hash
from harness_evolution.config import read_json, write_json
from harness_evolution.runtime import Runtime, check_endpoint, command
from harness_evolution.scoring import InvalidEvaluation


@pytest.fixture(autouse=True)
def evaluator(tmp_path):
    (tmp_path / "evaluator").mkdir()
    write_json(tmp_path / "private_data/bindings.json", {})


@pytest.mark.parametrize("url", ["https://cloud.surogate.ai", "http://8.8.8.8", "http://0.0.0.0:8000", "http://user:pass@localhost", "file:///tmp/x"])
def test_runtime_refuses_public_or_invalid_endpoints(url):
    with pytest.raises(InvalidEvaluation):
        check_endpoint(url)


def test_private_runtime_endpoints():
    for url in ("http://127.0.0.1:8000", "http://localhost:8000", "http://10.20.0.2:8000"):
        check_endpoint(url)


def test_command_has_no_shell_interpolation(tmp_path):
    target = tmp_path / "out.json"
    script = "import json,sys;open(sys.argv[1],'w').write(json.dumps(sys.argv[2:]))"
    literal = "$(touch SHOULD_NOT_EXIST) `echo bad`; a b"
    command([sys.executable, "-c", script, str(target), "{value}"], {"value": literal}, tmp_path / "log", 10)
    assert read_json(target) == [literal]
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


def test_command_timeout_cleans_up(tmp_path):
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        command([sys.executable, "-c", "import time;time.sleep(30)"], {}, tmp_path / "log", 0.1)
    assert time.monotonic() - start < 8


def test_failed_receipt_still_stops_runtime(config, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "test.py").write_text("x=1\n")
    config["runtime"] = {
        "start": [sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('{}')", "{receipt}"],
        "stop": [sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).with_name('stopped').touch()", "{request}"],
    }
    runtime = Runtime(config, tmp_path, time.monotonic() + 10)
    with pytest.raises(InvalidEvaluation, match="receipt mismatch"):
        runtime.evaluate(source, config["task_manifest"][:1], "bad")
    assert (tmp_path / "evaluations/bad/stopped").exists()


def test_complete_runtime_contract_invokes_driver_for_both_tiers(config, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "test.py").write_text("x=1\n")
    config["benchmark_pythons"] = {"claweval": sys.executable}
    calls = []

    def fake_command(argv, values, log, timeout, **kwargs):
        calls.append(log.name)
        if log.name == "start.log":
            request = read_json(Path(values["request"]))
            write_json(Path(values["receipt"]), {**request, "base_url": "http://localhost:8100", "ops_base_url": "http://localhost:8888",
                                                 "project_id": "test", "agents": {"pro": "p", "standard": "s"}})
        elif log.name == "driver.log":
            request = read_json(Path(argv[-2]))
            write_json(Path(argv[-1]), [{"benchmark": t["benchmark"], "task_id": t["task_id"], "score": 1, "passed": True, "seconds": 1, "safety_score": 1}
                                       for t in request["tasks"]])

    monkeypatch.setattr("harness_evolution.runtime.command", fake_command)
    results = Runtime(config, tmp_path, time.monotonic() + 10).evaluate(source, config["task_manifest"][:1], "ok")
    assert set(results) == {"pro", "standard"}
    assert calls == ["start.log", "driver.log", "driver.log", "stop.log"]


def test_product_imports_are_absent():
    import ast
    root = Path(__file__).resolve().parents[1] / "harness_evolution"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(name.split(".")[0] in ("surogates", "surogate_ops") for name in names), path
