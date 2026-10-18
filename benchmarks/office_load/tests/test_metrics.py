"""The office namespace's metrics: kubectl's outputs read, and the sampler that records them through a step."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oload.metrics import (
    Postgres,
    ProbeFailed,
    Sampler,
    settle,
    cpu_millicores,
    error_lines,
    memory_mib,
    parse_queues,
    parse_top,
    run_command,
)

FIXTURES = Path(__file__).parent / "fixtures"
TOP = (FIXTURES / "kubectl_top_containers.txt").read_text()
QUEUES = (FIXTURES / "rabbitmqctl_list_queues.txt").read_text()
POD = "[pod/office-docs-6c8b7f9d5c-h4j2m/docservice]"
DOCSERVICE_LOG = f"""\
{POD} [2026-10-18T15:00:01.120] [WARN] [localhost] [load-r1-0-ab12] [agent-0] nodeJS - an error page, warned
{POD} [2026-10-18T15:00:02.310] [ERROR] [localhost] [load-r1-3-cd34] [agent-3] nodeJS - error: ETIMEDOUT
{POD} [2026-10-18T15:00:02.900] [INFO] [localhost] [load-r1-3-cd34] [agent-3] nodeJS - command {{"error":0}}
{POD} [2026-10-18T15:00:03.004] [FATAL] nodeJS - out of memory
"""


def test_kubectl_top_gives_millicores_and_mebibytes_per_container() -> None:
    top = parse_top(TOP)
    assert len(top) == 7
    assert top["office-docs-6c8b7f9d5c-h4j2m/docservice"] == {"cpu_m": 1998, "mem_mi": 2310.0}
    assert top["office-docs-6c8b7f9d5c-h4j2m/proxy"] == {"cpu_m": 41, "mem_mi": 38.0}
    assert top["office-load-client-1/runtime-office"] == {"cpu_m": 2950, "mem_mi": 9530.0}


def test_quantities_in_any_unit() -> None:
    assert cpu_millicores("250m") == 250
    assert cpu_millicores("2") == 2000
    assert cpu_millicores("1500000n") == 2
    assert cpu_millicores("1500u") == 2
    assert memory_mib("1342Mi") == 1342.0
    assert memory_mib("2Gi") == 2048.0
    assert memory_mib("512Ki") == 0.5
    assert memory_mib("1048576") == 1.0
    assert memory_mib("1G") == pytest.approx(953.7, abs=0.05)
    with pytest.raises(ValueError):
        cpu_millicores("lots")


def test_rabbitmqctl_gives_each_queue_depth() -> None:
    assert parse_queues(QUEUES) == {
        "ds.converttask": 17, "ds.convertresponse": 3, "ds.convertdead": 0, "ds.delayed": 0,
        "amq.gen-7nq2Fh0RkGdNw7mMl_ZcQw": 0,
    }
    assert parse_queues("name\tmessages\n") == {}


def test_error_lines_are_those_logged_at_error_or_fatal() -> None:
    lines = error_lines(DOCSERVICE_LOG)
    assert len(lines) == 2
    assert "ETIMEDOUT" in lines[0] and "out of memory" in lines[1]
    assert error_lines("") == []


def test_the_database_name_goes_into_sql_only_as_an_identifier() -> None:
    assert Postgres().database == "docs_load"
    with pytest.raises(ValueError):
        Postgres(database="x'; DROP TABLE y; --")


# The sampler, with kubectl faked.

class FakeKubectl:
    """Answers the sampler's kubectl calls with recorded outputs; *failing* names calls that fail, by a word in them."""

    def __init__(self, *, failing: Sequence[str] = ()) -> None:
        self.calls: list[list[str]] = []
        self.failing = failing

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> str:
        argv = list(argv)
        self.calls.append(argv)
        await asyncio.sleep(0)
        if any(word in argv for word in self.failing):
            raise ProbeFailed("error: Metrics API not available")
        if "top" in argv:
            return TOP
        if "rabbitmqctl" in argv:
            return QUEUES
        if "psql" in argv:
            return "12\n"
        if "logs" in argv:
            return DOCSERVICE_LOG if "app=office-docs" in argv else ""
        if "get" in argv:
            selector = argv[argv.index("-l") + 1]
            return "pod/surogate-pg-2\n" if "cnpg.io" in selector else "pod/office-rabbitmq-0\n"
        raise AssertionError(f"an unexpected call: {argv}")


def samples(step_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (step_dir / "metrics.jsonl").read_text().splitlines()]


async def test_the_sampler_records_every_probe_into_metrics_jsonl(tmp_path: Path) -> None:
    kubectl = FakeKubectl()
    step_dir = tmp_path / "step-5"
    step_dir.mkdir()
    before = time.time()
    async with Sampler("office-load", every_s=0.05, out_dir=step_dir, run=kubectl):
        await asyncio.sleep(0.12)
    taken = samples(step_dir)
    assert len(taken) >= 3  # at the start, every 0.05 s, and at the end
    first = taken[0]
    assert first["at"] >= before
    assert first["top"]["office-docs-6c8b7f9d5c-h4j2m/docservice"] == {"cpu_m": 1998, "mem_mi": 2310.0}
    assert first["queues"]["ds.converttask"] == 17
    assert first["errors"]["docservice"]["count"] == 2
    assert "ETIMEDOUT" in first["errors"]["docservice"]["lines"][0]
    assert first["errors"]["converter"] == {"count": 0, "lines": []}
    assert first["db_connections"] == 12
    assert first["failed"] == {}

    calls = kubectl.calls
    assert ["kubectl", "-n", "office-load", "top", "pods", "--containers"] in calls
    since = datetime.fromtimestamp(before, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    logs = [call for call in calls if "logs" in call]
    assert {call[call.index("-c") + 1] for call in logs} == {"docservice", "converter"}
    # Since the step's start, every line: a selector alone gives only the last 10 of each pod.
    assert all(f"--since-time={since}" in call and "--tail=-1" in call for call in logs)
    exec_rabbitmq = next(call for call in calls if "rabbitmqctl" in call)
    assert exec_rabbitmq[:5] == ["kubectl", "-n", "office-load", "exec", "pod/office-rabbitmq-0"]
    assert exec_rabbitmq[-3:] == ["list_queues", "name", "messages"]
    primary = next(call for call in calls if "cnpg.io/cluster=surogate-pg,cnpg.io/instanceRole=primary" in call)
    assert primary[:3] == ["kubectl", "-n", "default"]
    psql = next(call for call in calls if "psql" in call)
    assert psql[:5] == ["kubectl", "-n", "default", "exec", "pod/surogate-pg-2"]
    assert psql[-1] == "SELECT count(*) FROM pg_stat_activity WHERE datname = 'docs_load'"


async def test_a_failing_kubectl_call_records_an_error_sample_and_the_step_goes_on(tmp_path: Path) -> None:
    kubectl = FakeKubectl(failing=["top", "psql"])
    async with Sampler("office-load", every_s=0.05, out_dir=tmp_path, run=kubectl):
        await asyncio.sleep(0.07)
    taken = samples(tmp_path)
    assert len(taken) >= 2
    for sample in taken:
        assert sample["top"] is None and sample["db_connections"] is None
        assert sample["failed"] == {"top": "error: Metrics API not available",
                                    "db_connections": "error: Metrics API not available"}
        assert sample["queues"]["ds.converttask"] == 17  # the others still recorded


async def test_an_unexpected_failure_is_recorded_too(tmp_path: Path) -> None:
    async def broken(argv: Sequence[str], timeout_s: float) -> str:
        raise FileNotFoundError("kubectl")

    async with Sampler("office-load", every_s=10, out_dir=tmp_path, run=broken):
        pass
    sample = samples(tmp_path)[0]
    assert set(sample["failed"]) == {"top", "queues", "errors.docservice", "errors.converter", "db_connections"}
    assert "kubectl" in sample["failed"]["top"]


async def test_samples_wait_for_the_step_dir_and_are_all_written(tmp_path: Path) -> None:
    step_dir = tmp_path / "step-5"
    async with Sampler("office-load", every_s=0.02, out_dir=step_dir, run=FakeKubectl()) as sampler:
        await asyncio.sleep(0.05)
        assert not step_dir.exists()  # the step makes its own dir: the sampler never does
        step_dir.mkdir()
        await asyncio.sleep(0.03)
    assert len(samples(step_dir)) == sampler.taken >= 3


async def test_a_step_dir_that_holds_metrics_already_is_refused(tmp_path: Path) -> None:
    (tmp_path / "metrics.jsonl").write_text("{}\n")
    with pytest.raises(FileExistsError):
        async with Sampler("office-load", out_dir=tmp_path, run=FakeKubectl()):
            pass


async def test_a_step_that_fails_takes_no_last_sample(tmp_path: Path) -> None:
    kubectl = FakeKubectl()
    with pytest.raises(RuntimeError):
        async with Sampler("office-load", every_s=10, out_dir=tmp_path, run=kubectl):
            await asyncio.sleep(0.05)  # the first sample, at the start
            raise RuntimeError("the step failed")
    assert len(samples(tmp_path)) == 1


# kubectl itself, as a subprocess.

async def test_run_command_gives_stdout() -> None:
    assert await run_command([sys.executable, "-c", "print('12')"], 10) == "12\n"


async def test_run_command_fails_with_stderr_on_a_nonzero_exit() -> None:
    with pytest.raises(ProbeFailed, match="no such pod"):
        await run_command([sys.executable, "-c", "import sys; sys.stderr.write('no such pod'); sys.exit(1)"], 10)


async def test_run_command_kills_a_call_past_its_timeout() -> None:
    started = time.monotonic()
    with pytest.raises(ProbeFailed, match=r"0\.2 s"):
        await run_command([sys.executable, "-c", "import time; time.sleep(30)"], 0.2)
    assert time.monotonic() - started < 5


def _settle_run(depths_by_call):
    """A kubectl stand-in: pod listings answer the RabbitMQ pod; queue listings answer the next depths."""
    calls = iter(depths_by_call)

    async def run(argv, timeout_s):
        if "get" in argv:
            return "pod/office-rabbitmq-0\n"
        depth = next(calls)
        return f"ds.converttask6\t{depth}\nds.convertresponse\t0\n"
    return run


async def test_settle_waits_for_the_queues_to_stay_empty():
    now = [0.0]

    async def sleep(s):
        now[0] += s

    settled, waited = await settle("ns", quiet_s=30, max_s=600, every_s=10, run=_settle_run([3, 0, 2, 0, 0, 0, 0]),
                                           clock=lambda: now[0], sleep=sleep)
    assert settled is True
    assert waited == 60  # empty from t=30, quiet through t=60; the 2 at t=20 restarted the quiet window


async def test_settle_gives_up_after_its_limit():
    now = [0.0]

    async def sleep(s):
        now[0] += s

    settled, waited = await settle("ns", quiet_s=30, max_s=50, every_s=10, run=_settle_run([5] * 20),
                                           clock=lambda: now[0], sleep=sleep)
    assert settled is False
    assert waited >= 50
