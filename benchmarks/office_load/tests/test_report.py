"""A run's report, rendered from what its steps left in the run dir."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from oload.bar import Verdict
from oload.events import Event, EventLog
from oload.ramp import StepResult
from oload.report import render

T0 = 1_760_000_000.0
STATS: dict[str, Any] = {
    "open": {"n": 5, "p50": 4.1, "p95": 6.2, "p99": 6.2, "max": 6.2},
    "flush": {"n": 40, "p50": 0.2, "p95": 0.4, "p99": 0.9, "max": 1.3},
    "forcesave": {"n": 40, "p50": 0.3, "p95": 0.8, "p99": 1.1, "max": 1.2},
    "callback": {"n": 40, "p50": 2.0, "p95": 4.5, "p99": 7.75, "max": 8.0},
    "events": 200,
    "failures": {"client": 0, "server": 0, "lost": 0, "timeout": 0, "tool": 0},
}


def write_run(run_dir: Path, *, command: str = "ramp") -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": run_dir.name, "command": command, "workload": "rollout", "profile": "prod",
        "pages_per_client": 20, "clients": 4, "ramp_s": 60, "hold_s": 600, "git_sha": "fd12b6c47abc",
        "started_at": T0, "settings": {"namespace": "office-load"},
    }))


def write_step(run_dir: Path, n: int, order: int, *, holds: bool = True, broken: list[str] | None = None,
               client_limited: bool = False, invalid: bool = False, reason: str | None = None, leaked: int = 0,
               events: list[Event] | None = None, metrics: list[dict[str, Any]] | None = None) -> None:
    out = run_dir / f"step-{n}"
    out.mkdir()
    started = T0 + order * 1000
    verdict = Verdict(holds, broken or [], client_limited, STATS, invalid, reason)
    StepResult(n, "rollout", verdict, started, started + 60, started + 660, leaked, out).write()
    log = EventLog(out / "events.jsonl")
    for event in events or []:
        log.add(event)
    if metrics is not None:
        (out / "metrics.jsonl").write_text("".join(json.dumps(sample) + "\n" for sample in metrics))


def sample(at: float, docservice_cpu: int, *, converter_cpu: int = 900, depth: int = 0, connections: int = 10,
           errors: int = 0) -> dict[str, Any]:
    return {
        "at": at,
        "top": {
            "office-docs-6c8b7f9d5c-h4j2m/docservice": {"cpu_m": docservice_cpu, "mem_mi": 2310.0},
            "office-converter-5f7d9c8b6d-2kq8x/converter": {"cpu_m": converter_cpu, "mem_mi": 1342.0},
            "office-converter-5f7d9c8b6d-9wzrt/converter": {"cpu_m": converter_cpu - 100, "mem_mi": 1400.0},
            "office-load-client-0/runtime-office": {"cpu_m": 3120, "mem_mi": 9876.0},
        },
        "queues": {"ds.converttask": depth, "ds.convertresponse": 0},
        "errors": {"docservice": {"count": errors, "lines": ["[ERROR] nodeJS - sendServerRequest error: ETIMEDOUT"]
                                  if errors else []},
                   "converter": {"count": 0, "lines": []}},
        "db_connections": connections,
        "failed": {},
    }


def event(what: str, at: float, *, ok: bool = True, cause: str | None = None) -> Event:
    return Event(0, "docx", what, ok, 0.5, cause, at)  # type: ignore[arg-type]


def test_a_run_stopped_by_the_client_budget_is_a_lower_bound(tmp_path: Path) -> None:
    run_dir = tmp_path / "prod-rollout-1"
    write_run(run_dir)
    write_step(run_dir, 5, 0, metrics=[sample(T0 + 10, 800)])
    write_step(run_dir, 10, 1, leaked=1)
    write_step(run_dir, 20, 2, holds=False, client_limited=True, leaked=1,
               broken=["fleet: room for 19 rollout session(s), not 20 (1 slot(s) leaked)"])

    text = render(run_dir)

    assert (run_dir / "report.md").read_text() == text
    assert "# Office load: prod-rollout-1" in text
    assert "rollout" in text and "prod" in text and "fd12b6c47abc" in text
    assert "| n | Verdict |" in text
    rows = [line for line in text.splitlines() if line.startswith("| 5 |") or line.startswith("| 10 |")
            or line.startswith("| 20 |")]
    assert len(rows) == 3
    assert "holds" in rows[0] and "6.20" in rows[0] and "7.75" in rows[0]
    assert "client-limited" in rows[2] and "room for 19" in rows[2]
    assert "**Capacity: at least 10 sessions" in text
    assert "lower bound (client budget)" in text
    assert "Knee" not in text
    # Leaked slots per step: the run's running total less the step's before.
    assert rows[1].split("|")[-3].strip() == "1" and rows[2].split("|")[-3].strip() == "0"
    # The step's peaks.
    assert "office-docs/docservice" in text and "800" in text


def test_the_knee_comes_with_its_evidence_and_its_pace(tmp_path: Path) -> None:
    run_dir = tmp_path / "prod-rollout-2"
    write_run(run_dir)
    write_step(run_dir, 5, 0)
    write_step(run_dir, 10, 1, holds=False, broken=["lost saves: 3 failed (3 marker lost)", "callback: p95 12.00 s"])
    write_step(run_dir, 7, 2)
    knee_start = T0 + 3 * 1000
    hold_from = knee_start + 60
    # The knee's hold: 600 s, 30 edits and 20 forcesaves; the ramp-up's 4 edits are not counted.
    knee_events = ([event("edit", knee_start + 5) for _ in range(4)]
                   + [event("edit", hold_from + 10 * i) for i in range(30)]
                   + [event("forcesave", hold_from + 20 * i) for i in range(20)])
    write_step(run_dir, 8, 3, holds=False, events=knee_events,
               broken=["callback: p95 10.50 s, over 10 s", "flush: 1 acknowledged after 10 s"],
               metrics=[sample(knee_start + 15, 1500, depth=4, connections=20),
                        sample(knee_start + 30, 1999, converter_cpu=1990, depth=31, connections=26, errors=3),
                        sample(knee_start + 45, 1700, depth=2, connections=22, errors=5)])

    text = render(run_dir)

    assert "**Capacity: 7 sessions.**" in text
    assert "**Knee: 8 sessions.**" in text
    assert "callback: p95 10.50 s, over 10 s" in text.split("## Steps")[0]  # the first broken row, up top
    assert "3.0 edits and 2.0 forcesaves per minute" in text
    evidence = text.split("## Steps")[0]
    assert "1999" in evidence  # docservice's peak CPU
    assert "1990" in evidence  # the converters'
    assert "31" in evidence  # the deepest queue
    assert "26" in evidence  # the most database connections
    assert "5 docservice error line(s)" in evidence and "ETIMEDOUT" in evidence
    assert "lower bound" not in text
    assert [line.split("|")[1].strip() for line in text.splitlines() if line.startswith("| ")
            and line.split("|")[1].strip().isdigit()][:4] == ["5", "7", "8", "10"]


def test_an_invalid_step_is_named_with_its_reason(tmp_path: Path) -> None:
    run_dir = tmp_path / "smoke-1"
    write_run(run_dir)
    write_step(run_dir, 5, 0)
    write_step(run_dir, 10, 1, holds=False, invalid=True,
               reason="2 request(s) of the tool's own refused; the first: 401 bad token")
    text = render(run_dir)
    assert "invalid" in text
    assert "step 10" in text.lower() and "401 bad token" in text
    assert "**Capacity: at least 5 sessions" in text


def test_a_run_whose_every_step_held_has_no_knee(tmp_path: Path) -> None:
    run_dir = tmp_path / "smoke-2"
    write_run(run_dir)
    write_step(run_dir, 4, 0)
    text = render(run_dir)
    assert "**Capacity: at least 4 sessions" in text and "no knee" in text


def test_the_client_check_reports_the_pages_one_client_held(tmp_path: Path) -> None:
    run_dir = tmp_path / "clients-1"
    write_run(run_dir, command="preflight-clients")
    write_step(run_dir, 1, 0)
    write_step(run_dir, 2, 1)
    write_step(run_dir, 4, 2, holds=False, client_limited=True, broken=["open: 2 failed (2 open client)"])
    text = render(run_dir)
    assert "**One load client held 2 pages.**" in text
    assert "4 pages broke the bar: open: 2 failed (2 open client)" in text


def test_the_editors_check_reports_each_count(tmp_path: Path) -> None:
    run_dir = tmp_path / "cap-1"
    write_run(run_dir, command="preflight-cap")
    for n, flushed, saved in ((25, 25, 25), (50, 50, 43)):
        out = run_dir / f"editors-{n}"
        out.mkdir()
        (out / "cap.json").write_text(json.dumps({
            "editors": n, "opened": n, "flushed": flushed, "saved": saved, "holds": saved == n,
            "failures": [] if saved == n else ["session 21 (docx) marker lost: turn 0's save lacks load-21"],
        }))
    text = render(run_dir)
    assert "| 25 | 25 | 25 | 25 |" in text
    assert "| 50 | 50 | 50 | 43 |" in text and "turn 0's save lacks load-21" in text
