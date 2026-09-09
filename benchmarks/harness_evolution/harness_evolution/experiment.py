"""A bounded hill-climbing loop with paired evaluation and a sealed final test."""
from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path
from statistics import mean

from harness_evolution.candidates import apply_proposal, cumulative_patch, snapshot, source_hash
from harness_evolution.config import TIERS, digest, read_json, task_key, write_json
from harness_evolution.proposer import Proposer, search_packet
from harness_evolution.runtime import Runtime, freeze_benchmarks
from harness_evolution.scoring import compare, validate_results
from harness_evolution.data import freeze_data, tree_hash


def controller_hash() -> str:
    return digest({p.name: p.read_text() for p in sorted(Path(__file__).parent.glob("*.py"))})


@contextmanager
def locked(run_dir: Path):
    with (run_dir / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another process owns this experiment") from exc
        yield


def report(run_dir: Path, state: dict) -> None:
    lines = ["# Harness evolution", "", f"Status: {state['status']}",
             f"Source commit: `{state['commit']}`", f"Frontier: `{state['frontier']}`", "",
             "| Attempt | Status | Pro delta | Standard delta |",
             "| --- | --- | --- | --- |"]
    for entry in state["journal"]:
        models = entry.get("comparison", {}).get("models", {})
        values = [f"{models[t]['delta']:+.3f}" if t in models else "—" for t in TIERS]
        lines.append(f"| {entry['attempt']} | {entry['status']} | {values[0]} | {values[1]} |")
    lines.extend(["", "Selection results are development measurements, not held-out benchmark scores.",
                  "Use final-test once after search. Repeat counts do not establish statistical significance.",
                  "", "Each attempt directory retains its hypothesis, patch, and private comparison details.", ""])
    if state.get("final_summary"):
        lines.extend(["## Final holdout", "", "One evaluation per source/tier; these estimates have sampling uncertainty.", "",
                      "| Tier | Benchmark | Tasks | Seed score | Candidate score | Delta |",
                      "| --- | --- | --- | --- | --- | --- |"])
        for tier, benchmarks in state["final_summary"].items():
            for benchmark, values in benchmarks.items():
                lines.append(f"| {tier} | {benchmark} | {values['tasks']} | {values['baseline']:.3f} | "
                             f"{values['candidate']:.3f} | {values['delta']:+.3f} |")
        lines.append("")
    (run_dir / "report.md").write_text("\n".join(lines))


def save(run_dir: Path, state: dict, allowed: list[str]) -> None:
    write_json(run_dir / "state.json", state)
    report(run_dir, state)
    (run_dir / "best.patch").write_text(cumulative_patch(
        run_dir / "seed", run_dir / state["frontier"], allowed,
    ))


def initialize(config: dict, run_dir: Path) -> dict:
    commit = snapshot(Path(config["repository"]), config.get("revision", "HEAD"), run_dir / "seed")
    for name in config["allowed_files"]:
        if not (run_dir / "seed" / name).is_file():
            raise ValueError(f"Editable file is absent from committed source: {name}")
    freeze_benchmarks(Path(config["repository"]), commit, run_dir / "evaluator")
    freeze_data(config, run_dir / "private_data")
    write_json(run_dir / "inputs.json", config)
    state = {"commit": commit, "config_hash": digest(config), "controller_hash": controller_hash(), "frontier": "seed",
             "frontier_hash": source_hash(run_dir / "seed"), "journal": [],
             "seed_hash": source_hash(run_dir / "seed"),
             "evaluator_hash": source_hash(run_dir / "evaluator"),
             "data_hash": tree_hash(run_dir / "private_data"),
             "next_attempt": 0, "status": "initialized", "elapsed_seconds": 0,
             "search_source_hash": None, "final_test_started": False}
    save(run_dir, state, config["allowed_files"])
    return state


def validate_resume(config: dict, run_dir: Path) -> dict:
    state = read_json(run_dir / "state.json")
    if state["config_hash"] != digest(config):
        raise ValueError("Experiment inputs changed; start a new experiment")
    if state["controller_hash"] != controller_hash():
        raise ValueError("Experiment controller changed; start a new experiment")
    if tree_hash(run_dir / "private_data") != state["data_hash"]:
        raise ValueError("Frozen task data or answer key changed")
    if source_hash(run_dir / state["frontier"]) != state["frontier_hash"]:
        raise ValueError("Saved frontier source changed")
    if (source_hash(run_dir / "seed") != state["seed_hash"]
            or source_hash(run_dir / "evaluator") != state["evaluator_hash"]):
        raise ValueError("Frozen baseline or evaluator changed")
    return state


def run(config: dict, run_dir: Path, *, resume=False, runtime_factory=Runtime, proposer_factory=Proposer) -> dict:
    run_dir = run_dir.resolve()
    if not resume:
        run_dir.mkdir(parents=True, exist_ok=False)
    with locked(run_dir):
        state = validate_resume(config, run_dir) if resume else initialize(config, run_dir)
        if state["final_test_started"]:
            raise ValueError("Final test has started; this search is permanently sealed")
        policy = config["policy"]
        started = time.monotonic()
        # A hard kill cannot run finally. Charge the time since its persisted
        # start conservatively, including downtime, rather than reset budget.
        if "active_since" in state:
            state["elapsed_seconds"] += max(0, time.time() - state.pop("active_since"))
        remaining = policy["max_wall_seconds"] - state["elapsed_seconds"]
        if remaining <= 0:
            state["status"] = "budget_exhausted"
            save(run_dir, state, config["allowed_files"])
            raise TimeoutError("Experiment wall-clock budget exhausted")
        runtime = runtime_factory(config, run_dir, started + remaining)
        proposer = proposer_factory(config["proposer"])
        search = [t for t in config["task_manifest"] if t["split"] == "search"]
        selection = [t for t in config["task_manifest"] if t["split"] == "selection"]
        state["status"] = "running"
        state["active_since"] = time.time()
        save(run_dir, state, config["allowed_files"])
        entry = None
        try:
            while state["next_attempt"] < policy["max_proposals"]:
                if time.monotonic() - started >= remaining or (run_dir / "STOP").exists():
                    state["status"] = "stopped"
                    break
                frontier = run_dir / state["frontier"]
                if state["search_source_hash"] != state["frontier_hash"]:
                    # A new unique directory also permits recovery from an
                    # interrupted search refresh without reading partial output.
                    label = f"search-{state['next_attempt']:03d}-{time.time_ns()}"
                    outcomes = runtime.evaluate(frontier, search, label)
                    for tier in TIERS:
                        validate_results(outcomes[tier], search)
                    write_json(run_dir / "search-results.json", outcomes)
                    state["search_source_hash"] = state["frontier_hash"]
                    save(run_dir, state, config["allowed_files"])
                else:
                    outcomes = read_json(run_dir / "search-results.json")
                index = state["next_attempt"]
                state["next_attempt"] += 1  # Count even interrupted/invalid attempts.
                entry = {"attempt": index, "status": "proposing", "accepted": False}
                state["journal"].append(entry)
                save(run_dir, state, config["allowed_files"])
                attempt = run_dir / "attempts" / f"{index:03d}"
                attempt.mkdir(parents=True)
                packet = search_packet(frontier, config["allowed_files"], search, outcomes, state["journal"][:-1])
                write_json(attempt / "proposer-input.json", packet)
                try:
                    proposal = proposer.propose(packet, index, timeout=remaining - (time.monotonic() - started))
                    if not isinstance(proposal, dict):
                        raise ValueError("Proposal must be a JSON object")
                    write_json(attempt / "proposal.json", proposal)
                    entry["hypothesis"] = proposal.get("hypothesis", "")
                    tier = proposal.get("smoke_tier", "pro")
                    smoke = [t for t in search if task_key(t) == proposal.get("smoke_task")]
                    if tier not in TIERS or not smoke:
                        raise ValueError("Smoke test must name a search task and a known tier")
                    old = validate_results(outcomes[tier], search)[task_key(smoke[0])]
                    if old["score"] >= 1:
                        raise ValueError("Smoke task must have an observed baseline failure")
                    candidate = attempt / "source"
                    patch = apply_proposal(frontier, candidate, proposal, config["allowed_files"])
                    (attempt / "candidate.patch").write_text(patch)
                except StopIteration:
                    entry["status"] = "no_more_proposals"
                    state["status"] = "completed"
                    break
                except (ValueError, SyntaxError) as exc:
                    entry.update(status="invalid_proposal", reason=str(exc))
                    save(run_dir, state, config["allowed_files"])
                    continue
                entry["status"] = "smoke"
                save(run_dir, state, config["allowed_files"])
                smoke_result = runtime.evaluate(candidate, smoke, f"{index:03d}-smoke")
                checked = validate_results(smoke_result[tier], smoke)
                if checked[task_key(smoke[0])]["score"] <= old["score"]:
                    entry["status"] = "smoke_rejected"
                    save(run_dir, state, config["allowed_files"])
                    continue
                entry["status"] = "selection"
                save(run_dir, state, config["allowed_files"])
                before, after = [], []
                for repetition in range(policy["repeats"]):
                    # Alternate order to reduce provider/time drift. Always
                    # run a fresh frontier control; do not reuse cached scores.
                    sources = [("baseline", frontier), ("candidate", candidate)]
                    if repetition % 2:
                        sources.reverse()
                    pair = {}
                    for kind, source in sources:
                        pair[kind] = runtime.evaluate(source, selection, f"{index:03d}-{repetition}-{kind}")
                    before.append(pair["baseline"])
                    after.append(pair["candidate"])
                comparison = compare(before, after, selection, policy)
                write_json(attempt / "comparison.json", comparison)
                entry.update(comparison=comparison, accepted=comparison["accepted"],
                             status="accepted" if comparison["accepted"] else "rejected")
                if comparison["accepted"]:
                    state["frontier"] = str(candidate.relative_to(run_dir))
                    state["frontier_hash"] = source_hash(candidate)
                save(run_dir, state, config["allowed_files"])
            else:
                state["status"] = "completed"
        except BaseException as exc:
            state["status"] = "interrupted"
            if entry and entry["status"] not in ("accepted", "rejected", "invalid_proposal", "smoke_rejected"):
                entry.update(status="invalid_evaluation", reason=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            state.pop("active_since", None)
            state["elapsed_seconds"] += time.monotonic() - started
            save(run_dir, state, config["allowed_files"])
        return state


def final_test(config: dict, run_dir: Path, *, runtime_factory=Runtime) -> dict:
    run_dir = run_dir.resolve()
    with locked(run_dir):
        state = validate_resume(config, run_dir)
        if state["final_test_started"]:
            raise ValueError("Final test was already started; holdout cannot be reused")
        tasks = [t for t in config["task_manifest"] if t["split"] == "holdout"]
        if not tasks:
            raise ValueError("No holdout tasks configured")
        state["final_test_started"] = True
        state["status"] = "final_test_started"
        save(run_dir, state, config["allowed_files"])
        runtime = runtime_factory(config, run_dir, time.monotonic() + config["policy"]["max_wall_seconds"])
        results = {}
        for name, source in (("baseline", run_dir / "seed"), ("candidate", run_dir / state["frontier"])):
            results[name] = runtime.evaluate(source, tasks, f"final-{name}")
            for tier in TIERS:
                validate_results(results[name][tier], tasks)
            write_json(run_dir / f"final-{name}.json", results[name])
        summary = {}
        for tier in TIERS:
            summary[tier] = {}
            for benchmark in sorted({t["benchmark"] for t in tasks}):
                rows = {name: [r for r in result[tier] if r["benchmark"] == benchmark]
                        for name, result in results.items()}
                before, after = (mean(r["score"] for r in rows[name]) for name in ("baseline", "candidate"))
                summary[tier][benchmark] = {"tasks": len(rows["baseline"]), "baseline": before,
                                            "candidate": after, "delta": after - before}
        state["final_summary"] = summary
        write_json(run_dir / "final-summary.json", summary)
        state["status"] = "final_test_completed"
        save(run_dir, state, config["allowed_files"])
        return results
