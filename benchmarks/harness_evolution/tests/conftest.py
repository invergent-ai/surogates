import json
import subprocess
from pathlib import Path

import pytest

from harness_evolution.config import load_config


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    files = {
        "surogates/harness/prompts/guidance/test.md": "Keep working.\n",
        "surogates/harness/test.py": "def example():\n    return 1\n",
        "pyproject.toml": '[project]\nname = "fixture"\nversion = "0.0.0"\n',
        "benchmarks/claweval/README.md": "Private evaluator fixture\n",
        "benchmarks/enterpriseops_gym/README.md": "Private evaluator fixture\n",
        "benchmarks/dabstep/dabbench/splits/tasks_v1.json": json.dumps({"dev": ["1", "2"], "holdout": ["3"]}),
        "benchmarks/gaia/gaia_bench/splits/dev.txt": "gaia-dev-a\ngaia-dev-b\n",
        "benchmarks/gaia/gaia_bench/splits/holdout.txt": "gaia-holdout\n",
        "benchmarks/workspace_bench/wsbench/splits/lite_en.json": json.dumps({"dev": ["1", "2", "3"], "holdout": ["4"]}),
        "answer-key.json": '{"secret": "HIDDEN-ANSWER"}\n',
    }
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.test",
                    "commit", "-qm", "fixture"], check=True)
    return repo


@pytest.fixture
def tasks():
    return [{"benchmark": "claweval", "task_id": name, "family": "workflow", "split": split,
             "guard": name == "guard"}
            for name, split in (("search", "search"), ("select", "selection"),
                                ("guard", "selection"), ("holdout", "holdout"))]


@pytest.fixture
def config(tmp_path, repository, tasks):
    manifest = tmp_path / "tasks.json"
    manifest.write_text(json.dumps(tasks))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "repository": str(repository), "tasks": str(manifest),
        "allowed_files": ["surogates/harness/prompts/guidance/test.md", "surogates/harness/test.py"],
        "models": {"pro": {"model": "pro-model", "revision": "pinned-pro"},
                   "standard": {"model": "standard-model", "revision": "pinned-standard"}},
        "runtime": {"start": ["true"], "stop": ["true"]},
        "proposer": {"recorded": []},
        "policy": {"max_proposals": 2, "repeats": 3},
    }))
    return load_config(path)
