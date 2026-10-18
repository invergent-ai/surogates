"""A run's report, ``report.md`` in its run dir: its settings, what it found, and each step against the bar with the
office namespace's peaks.

The capacity is the highest step that held below the knee; the knee, the lowest step that broke the bar on the
document server's side. A run the load clients stopped is a lower bound (client budget): the capacity is at least its
last holding step.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oload.events import EventLog
from oload.ramp import StepResult

# A pod's name less what its controller adds: a Deployment's hashes, a StatefulSet's ordinal.
_POD_SUFFIX = re.compile(r"-[a-z0-9]{6,10}-[a-z0-9]{5}$|-\d+$")


@dataclass(slots=True)
class Peaks:
    """The highest each metric reached over a step's samples."""

    samples: int = 0
    containers: dict[str, dict[str, float]] = field(default_factory=dict)  # owner/container -> pods, cpu_m, mem_mi
    queues: dict[str, int] = field(default_factory=dict)
    db_connections: int | None = None
    errors: dict[str, int] = field(default_factory=dict)
    error_lines: dict[str, list[str]] = field(default_factory=dict)
    failed: Counter[str] = field(default_factory=Counter)
    failures: dict[str, str] = field(default_factory=dict)  # each failing probe's first reason


def owner(pod: str) -> str:
    """``office-docs-6c8b7f9d5c-h4j2m`` -> ``office-docs``; ``office-load-client-3`` -> ``office-load-client``."""
    return _POD_SUFFIX.sub("", pod)


def peaks(samples: list[dict[str, Any]]) -> Peaks:
    found = Peaks(samples=len(samples))
    for sample in samples:
        pods: Counter[str] = Counter()
        for name, usage in (sample.get("top") or {}).items():
            pod, _, container = name.partition("/")
            key = f"{owner(pod)}/{container}"
            pods[key] += 1
            peak = found.containers.setdefault(key, {"pods": 0, "cpu_m": 0, "mem_mi": 0.0})
            peak["cpu_m"] = max(peak["cpu_m"], usage["cpu_m"])
            peak["mem_mi"] = max(peak["mem_mi"], usage["mem_mi"])
        for key, count in pods.items():
            found.containers[key]["pods"] = max(found.containers[key]["pods"], count)
        for queue, depth in (sample.get("queues") or {}).items():
            found.queues[queue] = max(found.queues.get(queue, 0), depth)
        if sample.get("db_connections") is not None:
            found.db_connections = max(found.db_connections or 0, sample["db_connections"])
        for component, errors in (sample.get("errors") or {}).items():
            if errors is None:
                continue
            found.errors[component] = max(found.errors.get(component, 0), errors["count"])
            if errors["lines"] and not found.error_lines.get(component):
                found.error_lines[component] = errors["lines"]
        for probe, why in (sample.get("failed") or {}).items():
            found.failed[probe] += 1
            found.failures.setdefault(probe, why)
    return found


def render(run_dir: str | Path) -> str:
    """Write ``report.md`` into *run_dir* from what its run left there; its text."""
    run_dir = Path(run_dir)
    meta: dict[str, Any] = json.loads((run_dir / "run.json").read_text())
    results = sorted((StepResult.read(path.parent) for path in run_dir.glob("step-*/step.json")),
                     key=lambda r: r.started)
    lines = [f"# Office load: {meta.get('run_id', run_dir.name)}", "", *_settings(meta), ""]
    if meta.get("command") == "preflight-cap" or list(run_dir.glob("editors-*/cap.json")):
        lines += _editors(run_dir)
    if results:
        lines += ["## Result", ""]
        lines += (_client_check(results) if meta.get("command") == "preflight-clients" else _capacity(results))
        lines += _steps_table(results)
        for result in sorted(results, key=lambda r: r.n):
            lines += _step_section(result)
    elif not list(run_dir.glob("editors-*/cap.json")):
        lines += ["No step has finished yet.", ""]
    text = "\n".join(lines).rstrip() + "\n"
    (run_dir / "report.md").write_text(text)
    return text


def _settings(meta: dict[str, Any]) -> list[str]:
    said = [("Command", meta.get("command")), ("Workload", meta.get("workload")), ("Profile", meta.get("profile")),
            ("Pages per client", meta.get("pages_per_client")), ("Load clients", meta.get("clients"))]
    if meta.get("ramp_s") is not None:
        said.append(("Each step", f"ramps up over {meta['ramp_s']:g} s and holds {meta['hold_s']:g} s"))
    if meta.get("started_at") is not None:
        said.append(("Started", _when(meta["started_at"])))
    said.append(("Git", meta.get("git_sha")))
    return [f"- {name}: {value}" for name, value in said if value not in (None, "")]


def _capacity(results: list[StepResult]) -> list[str]:
    valid = [r for r in results if not r.invalid]
    failing = [r for r in valid if not r.holds and not r.lower_bound]
    knee = min(failing, key=lambda r: r.n) if failing else None
    below = [r.n for r in valid if r.holds and (knee is None or r.n < knee.n)]
    capacity = max(below) if below else None
    last = results[-1]
    held = "no step held" if capacity is None else f"{capacity} sessions"
    out = []
    if last.lower_bound:
        out.append(f"**Capacity: at least {held}: lower bound (client budget).** Step {last.n} ran out of load "
                   f"clients: {_first(last)}.")
    elif knee is not None:
        out.append(f"**Capacity: {held}.**" if capacity is not None else "**Capacity: none: the first step broke "
                   "the bar.**")
    elif last.invalid:
        out.append(f"**Capacity: at least {held}.** The run stopped at an invalid step.")
    else:
        out.append(f"**Capacity: at least {held}.** Every step held: no knee within the steps run.")
    out.append("")
    for result in (r for r in results if r.invalid):
        out += [f"Step {result.n} is invalid: {result.verdict.reason}. Nothing it measured stands.", ""]
    if knee is not None:
        out += [f"**Knee: {knee.n} sessions.** The first broken row: {_first(knee)}.", ""]
        out += _evidence(knee)
    return out


def _client_check(results: list[StepResult]) -> list[str]:
    held = [r.n for r in results if r.holds]
    broke = next((r for r in results if not r.holds), None)
    out = [f"**One load client held {max(held)} pages.**" if held else "**One load client held none of the page "
           "counts tried.**", ""]
    if broke is None:
        out += ["Every page count tried held.", ""]
    elif broke.invalid:
        out += [f"{broke.n} pages: invalid: {broke.verdict.reason}. Nothing it measured stands.", ""]
    else:
        out += [f"{broke.n} pages broke the bar: {_first(broke)}.", ""]
        out += _evidence(broke)
    return out


def _evidence(result: StepResult) -> list[str]:
    found = peaks(_samples(result))
    edits, forcesaves = _rates(result)
    out = [f"At step {result.n}:"]
    if len(result.verdict.broken) > 1:
        out.append("- every broken row: " + "; ".join(result.verdict.broken))
    if edits is not None:
        out.append(f"- the pace: {edits:.1f} edits and {forcesaves:.1f} forcesaves per minute through the hold")
    out += _metric_lines(found)
    return [*out, ""]


def _metric_lines(found: Peaks) -> list[str]:
    if not found.samples:
        return ["- no metrics were sampled"]
    out = []
    if found.containers:
        busiest = sorted(found.containers.items(), key=lambda item: -item[1]["cpu_m"])
        out.append("- peak CPU and memory: " + "; ".join(
            f"{name} {peak['cpu_m']:.0f}m, {peak['mem_mi']:.0f} MiB" for name, peak in busiest))
    if found.queues:
        queue, depth = max(found.queues.items(), key=lambda item: item[1])
        out.append(f"- RabbitMQ: the deepest queue {queue}, at {depth} message(s)")
    if found.db_connections is not None:
        out.append(f"- database connections: at most {found.db_connections}")
    if found.errors:
        counts = ", ".join(f"{count} {component} error line(s)" for component, count in found.errors.items())
        first = next((lines[0].replace("`", "'") for lines in found.error_lines.values() if lines), None)
        out.append(f"- {counts}" + (f"; the first: `{first}`" if first else ""))
    for probe, count in found.failed.items():
        out.append(f"- the {probe} probe failed in {count} of {found.samples} sample(s): {found.failures[probe]}")
    return out


def _steps_table(results: list[StepResult]) -> list[str]:
    out = ["## Steps", "",
           "| n | Verdict | Opens p95 | Flush max | Forcesave p99 | Forcesave max | Callback p95 | Callback p99 "
           "| Lost | Timed out | Client failures | Edits/min | Forcesaves/min | Leaked slots | Broken rows |",
           "|" + " --- |" * 15]
    leaked_before = {}
    total = 0
    for result in results:  # in the order they ran: each step's leaks are the running total's growth
        leaked_before[result.n], total = total, result.leaked
    for result in sorted(results, key=lambda r: r.n):
        stats = result.verdict.stats
        failures = stats.get("failures", {})
        edits, forcesaves = _rates(result)
        said = result.verdict.reason if result.invalid else "; ".join(result.verdict.broken)
        cells = [
            str(result.n), _verdict(result),
            _s(stats, "open", "p95"), _s(stats, "flush", "max"), _s(stats, "forcesave", "p99"),
            _s(stats, "forcesave", "max"), _s(stats, "callback", "p95"), _s(stats, "callback", "p99"),
            str(failures.get("lost", 0)), str(failures.get("timeout", 0)), str(failures.get("client", 0)),
            _rate(edits), _rate(forcesaves), str(result.leaked - leaked_before[result.n]),
            (said or "").replace("|", "\\|"),
        ]
        out.append("| " + " | ".join(cells) + " |")
    if total:
        out += ["", f"{total} load-client slot(s) leaked in all: pages that may live on in their pods."]
    return [*out, ""]


def _step_section(result: StepResult) -> list[str]:
    out = [f"## Step {result.n}: {_verdict(result)}", "",
           f"Started {_when(result.started)}; held from +{result.hold_from - result.started:.0f} s to "
           f"+{result.ended - result.started:.0f} s.", ""]
    if result.invalid:
        out += [f"Invalid: {result.verdict.reason}", ""]
    elif result.verdict.broken:
        out += ["Broken rows:", "", *(f"- {row}" for row in result.verdict.broken), ""]
    found = peaks(_samples(result))
    if found.containers:
        out += ["| Container | Pods | Peak CPU (m) | Peak memory (MiB) |", "| --- | --- | --- | --- |"]
        for name, peak in sorted(found.containers.items()):
            out.append(f"| {name} | {peak['pods']:.0f} | {peak['cpu_m']:.0f} | {peak['mem_mi']:.0f} |")
        out.append("")
    other = [line for line in _metric_lines(found) if not line.startswith("- peak CPU")]
    return [*out, *other, ""]


def _samples(result: StepResult) -> list[dict[str, Any]]:
    path = None if result.dir is None else result.dir / "metrics.jsonl"
    if path is None or not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _rates(result: StepResult) -> tuple[float | None, float | None]:
    """Edits and forcesaves sent per minute through the step's hold."""
    path = None if result.dir is None else result.dir / "events.jsonl"
    minutes = (result.ended - result.hold_from) / 60
    if path is None or not path.is_file() or minutes <= 0:
        return None, None
    held = [e for e in EventLog.read(path) if result.hold_from <= e.at <= result.ended]
    return (sum(e.what == "edit" for e in held) / minutes, sum(e.what == "forcesave" for e in held) / minutes)


def _editors(run_dir: Path) -> list[str]:
    checks = sorted((json.loads(path.read_text()) for path in run_dir.glob("editors-*/cap.json")),
                    key=lambda check: check["editors"])
    out = ["## Editors at once", "",
           "Each editor opened, all of them before any edited; then each made one edit and one save.", "",
           "| Editors | Opened | Flushed true | Saved their marker | First failure |",
           "| --- | --- | --- | --- | --- |"]
    for check in checks:
        first = (check.get("failures") or ["—"])[0].replace("|", "\\|")
        out.append(f"| {check['editors']} | {check['opened']} | {check['flushed']} | {check['saved']} | {first} |")
    return [*out, ""]


def _first(result: StepResult) -> str:
    return result.verdict.broken[0] if result.verdict.broken else (result.verdict.reason or "no row named")


def _verdict(result: StepResult) -> str:
    if result.invalid:
        return "invalid"
    if result.holds:
        return "holds"
    return "client-limited" if result.lower_bound else "breaks"


def _s(stats: dict[str, Any], what: str, which: str) -> str:
    value = stats.get(what, {}).get(which)
    if value is None:
        return "—"
    return "timed out" if math.isinf(value) else f"{value:.2f}"


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def _when(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
