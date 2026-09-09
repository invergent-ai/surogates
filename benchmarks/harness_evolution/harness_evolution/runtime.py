"""Trusted deployment hooks and isolated, unchanged benchmark subprocesses.

Hooks are operator-authored argv arrays, never proposed code. A start hook
deploys the supplied source into a dedicated stack and writes a receipt.
Only that product source is mounted in the stack; evaluation files stay
with this controller. Receipts are deployment attestations, not a sandbox.
"""
from __future__ import annotations

import io
import ipaddress
import os
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from harness_evolution.candidates import source_hash
from harness_evolution.config import TIERS, read_json, write_json
from harness_evolution.scoring import InvalidEvaluation, validate_results
from harness_evolution.benchmarks import BENCHMARKS, upstream_split
from harness_evolution.data import tree_hash


def command(argv: list[str], values: dict[str, str], log: Path, timeout: float, *, env=None) -> None:
    args = []
    for item in argv:
        for key, value in values.items():
            item = item.replace("{" + key + "}", str(value))
        args.append(item)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as output:
        process = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True, cwd=log.parent)
        try:
            code = process.wait(timeout=max(0.1, timeout))
            if code:
                raise InvalidEvaluation(f"Command exited {code}; see {log}")
        finally:
            # A hook must hand persistent services to its container scheduler.
            # No subprocess descendants may leak into the next candidate.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def check_endpoint(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise InvalidEvaluation("Runtime endpoints must be credential-free HTTP URLs")
    host = parsed.hostname
    if host == "localhost":
        return
    try:
        addr = ipaddress.ip_address(host)
    except ValueError as exc:
        raise InvalidEvaluation("Use a loopback or private IP for the experiment stack") from exc
    if not (addr.is_loopback or addr.is_private) or addr.is_unspecified:
        raise InvalidEvaluation("Public runtime endpoints are not allowed")


def freeze_benchmarks(repository: Path, commit: str, destination: Path) -> None:
    archive = subprocess.check_output([
        "git", "-C", str(repository), "archive", commit,
        *["benchmarks/" + name for name in BENCHMARKS],
    ])
    destination.mkdir(exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        if any(not (m.isfile() or m.isdir()) for m in tar.getmembers()):
            raise ValueError("Benchmark archive contains links or special files")
        tar.extractall(destination, filter="data")


class Runtime:
    def __init__(self, config: dict, run_dir: Path, deadline: float):
        self.config, self.run_dir, self.deadline = config, run_dir, deadline
        self.evaluator_hash = source_hash(run_dir / "evaluator")
        self.data_hash = tree_hash(run_dir / "private_data")

    def _remaining(self) -> float:
        if (self.run_dir / "STOP").exists():
            raise InterruptedError("Experiment stopped at an evaluation boundary")
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("Experiment wall-clock budget exhausted")
        return left

    def evaluate(self, source: Path, tasks: list[dict], label: str) -> dict:
        if source_hash(self.run_dir / "evaluator") != self.evaluator_hash:
            raise InvalidEvaluation("Frozen evaluator changed during the experiment")
        if tree_hash(self.run_dir / "private_data") != self.data_hash:
            raise InvalidEvaluation("Frozen task data or answer key changed")
        root = self.run_dir / "evaluations" / label
        root.mkdir(parents=True, exist_ok=False)
        runtime_id = "evolve-" + uuid.uuid4().hex
        fingerprint = source_hash(source)
        request = {"runtime_id": runtime_id, "source_dir": str(source),
                   "source_hash": fingerprint, "models": self.config["models"],
                   "benchmark_profiles": self.config.get("benchmark_profiles", {})}
        start_request, receipt_path = root / "start.json", root / "runtime.json"
        write_json(start_request, request)
        values = {"request": str(start_request), "receipt": str(receipt_path),
                  "python": sys.executable, "repository": self.config["repository"]}
        runtime = self.config["runtime"]
        hook_env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                    "PYTHONDONTWRITEBYTECODE": "1"}
        results = {}
        try:
            command(runtime["start"], values, root / "start.log",
                    min(runtime.get("boot_timeout_s", 600), self._remaining()), env=hook_env)
            receipt = read_json(receipt_path)
            for field in ("runtime_id", "source_hash", "models"):
                if receipt.get(field) != request[field]:
                    raise InvalidEvaluation(f"Runtime receipt mismatch: {field}")
            for field in ("base_url", "ops_base_url"):
                check_endpoint(receipt.get(field, ""))
            if not receipt.get("project_id") or set(receipt.get("agents", {})) != set(TIERS):
                raise InvalidEvaluation("Runtime receipt needs an experiment project and two agents")
            if len(set(receipt["agents"].values())) != 2 or not all(receipt["agents"].values()):
                raise InvalidEvaluation("Standard and Pro need distinct agent IDs")
            if request["benchmark_profiles"]:
                if receipt.get("benchmark_profiles") != request["benchmark_profiles"]:
                    raise InvalidEvaluation("Runtime must attest the declared benchmark tool profiles")
                for benchmark in request["benchmark_profiles"]:
                    agents = receipt.get("benchmark_agents", {}).get(benchmark, {})
                    if set(agents) != set(TIERS) or len(set(agents.values())) != 2 or not all(agents.values()):
                        raise InvalidEvaluation("Each declared benchmark profile needs distinct tier agents")
            data = read_json(self.run_dir / "private_data/bindings.json")
            for tier in TIERS:
                rows = []
                groups = sorted({(t["benchmark"], upstream_split(t)) for t in tasks})
                for benchmark, split in groups:
                    subset = [t for t in tasks if t["benchmark"] == benchmark and
                              upstream_split(t) == split]
                    job = root / tier / f"{benchmark}-{split}"
                    request_file, result_file = job / "request.json", job / "results.json"
                    write_json(request_file, {
                        "benchmark": benchmark, "tier": tier, "tasks": subset,
                        "benchmark_root": str(self.run_dir / "evaluator" / "benchmarks"),
                        "output_dir": str(job / "artifacts"), "runtime": receipt,
                        "dataset_revision": self.config.get("dataset_revisions", {}).get(benchmark),
                        "benchmark_data": data.get(benchmark, {}),
                        "task_timeout_s": runtime.get("task_timeout_s", 1800),
                    })
                    python = self.config.get("benchmark_pythons", {}).get(benchmark)
                    if not python:
                        raise ValueError(f"Configure benchmark_pythons.{benchmark}")
                    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                           "PYTHONDONTWRITEBYTECODE": "1"}
                    command([python, "-m", "harness_evolution.benchmark_driver", str(request_file), str(result_file)],
                            {}, job / "driver.log", self._remaining(), env=env)
                    batch = read_json(result_file)
                    validate_results(batch, subset)
                    rows.extend(batch)
                validate_results(rows, tasks)
                results[tier] = rows
            if source_hash(source) != fingerprint:
                raise InvalidEvaluation("Runtime modified the immutable candidate source")
            if source_hash(self.run_dir / "evaluator") != self.evaluator_hash:
                raise InvalidEvaluation("Frozen evaluator changed during evaluation")
            if tree_hash(self.run_dir / "private_data") != self.data_hash:
                raise InvalidEvaluation("Frozen task data or answer key changed during evaluation")
            write_json(root / "results.json", results)
            return results
        finally:
            # Cleanup runs even after a partially failed start. Hooks must be
            # idempotent and use runtime_id when no receipt was written.
            command(runtime["stop"], values, root / "stop.log", runtime.get("stop_timeout_s", 120), env=hook_env)
