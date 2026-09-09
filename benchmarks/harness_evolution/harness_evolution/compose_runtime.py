"""Deployment hook for an operator-owned, disposable Docker Compose stack.

The Compose file must mount EVOLVE_SOURCE_DIR read-only in every harness
service, provide an api service on port 8000 bound to loopback, and use
project-scoped queues/database/storage. It must not mount the experiment
directory or the evaluator. Model agents are provisioned by its init job.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from harness_evolution.candidates import source_hash
from harness_evolution.config import read_json, write_json
from harness_evolution.runtime import check_endpoint


def run(action: str, request_path: Path, receipt_path: Path) -> None:
    request = read_json(request_path)
    name = request["runtime_id"]
    if not re.fullmatch(r"evolve-[a-f0-9]{32}", name):
        raise ValueError("Refusing to operate on a non-experiment Compose project")
    compose = Path(os.environ["EVOLVE_COMPOSE_FILE"]).resolve()
    if not compose.is_file():
        raise ValueError("EVOLVE_COMPOSE_FILE must point to the dedicated experiment stack")
    env = {**os.environ, "EVOLVE_SOURCE_DIR": request["source_dir"],
           "EVOLVE_RUNTIME_ID": name, "EVOLVE_SOURCE_HASH": request["source_hash"],
           "EVOLVE_MODEL_BINDINGS_JSON": json.dumps(request["models"]),
           "EVOLVE_BENCHMARK_PROFILES_JSON": json.dumps(request.get("benchmark_profiles", {}))}
    cmd = ["docker", "compose", "--project-name", name, "--file", str(compose)]
    if action == "stop":
        subprocess.run(cmd + ["down", "--volumes", "--remove-orphans"], env=env, check=True)
        return
    if action != "start":
        raise ValueError("Expected start or stop")
    if source_hash(Path(request["source_dir"])) != request["source_hash"]:
        raise ValueError("Candidate source hash mismatch")
    # Resolve required bindings before starting anything.
    agents = {tier: os.environ[f"EVOLVE_{tier.upper()}_AGENT_ID"] for tier in ("pro", "standard")}
    project = os.environ["EVOLVE_PROJECT_ID"]
    profiles = request.get("benchmark_profiles", {})
    benchmark_agents = json.loads(os.environ.get("EVOLVE_BENCHMARK_AGENTS_JSON", "{}"))
    for benchmark in profiles:
        bindings = benchmark_agents.get(benchmark, {})
        if set(bindings) != {"pro", "standard"} or len(set(bindings.values())) != 2 or not all(bindings.values()):
            raise ValueError("Each benchmark profile needs distinct Pro and Standard agent IDs")
    services = json.loads(os.environ.get("EVOLVE_EOG_SERVICES_JSON", "{}"))
    if "enterpriseops_gym" in profiles and not services:
        raise ValueError("EnterpriseOps requires domain-to-Compose-service/port bindings")
    for binding in services.values():
        if not isinstance(binding, dict) or not isinstance(binding.get("port"), int) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", binding.get("service", "")):
            raise ValueError("Invalid EnterpriseOps Compose service binding")
    subprocess.run(cmd + ["up", "--detach", "--wait", "--wait-timeout", "480"], env=env, check=True)

    def endpoint(service: str, port: int) -> str:
        address = subprocess.check_output(cmd + ["port", service, str(port)], env=env, text=True).strip()
        if "\n" in address:
            raise ValueError("Publish one loopback address per experiment service")
        url = "http://" + address
        check_endpoint(url)
        return url

    api = endpoint("api", 8000)
    ops = endpoint("ops", 8888)
    gym_urls = {domain: endpoint(binding["service"], binding["port"]) for domain, binding in services.items()}
    write_json(receipt_path, {"runtime_id": name, "source_hash": request["source_hash"],
                             "models": request["models"], "agents": agents,
                             "project_id": project, "base_url": api, "ops_base_url": ops,
                             "benchmark_profiles": profiles, "benchmark_agents": benchmark_agents,
                             "gym_urls": gym_urls})


if __name__ == "__main__":
    run(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
