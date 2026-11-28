"""eogbench run: a multi-gym (hybrid) task gets one proxy and one address per gym, for that task only."""
import argparse
from types import SimpleNamespace

import pytest

from eogbench import cli
from eogbench.dataset import parse_record
from test_dataset import _hybrid_json


def _args(scoring="upstream"):
    return argparse.Namespace(wall_clock_cap=60.0, scoring=scoring)


@pytest.mark.asyncio
async def test_each_gym_gets_its_own_proxy_and_all_are_stopped(monkeypatch):
    monkeypatch.delenv("EOG_ADAPTER_PUBLIC_URL", raising=False)
    started, stopped, closed, seen = [], [], [], {}

    class Proxy:
        def __init__(self, url):
            self.url, self.port, self.local_url = url, 9000 + len(started), f"http://127.0.0.1:{9000 + len(started)}"

        def start(self):
            started.append(self.url)
            return self

        def stop(self):
            stopped.append(self.url)

    def expose(base, port, public_url=None):
        return SimpleNamespace(public_base=None, close=lambda: closed.append(port))

    async def run_task(client, registrar, tunnel_url, proxy, task, wall_clock_cap_s, gym_proxies, scoring):
        seen.update(gym_proxies=gym_proxies, scoring=scoring)
        return cli.RolloutResult(task_id=task.task_id, session_id="s", scoring=scoring)

    monkeypatch.setattr(cli, "GymProxy", Proxy)
    monkeypatch.setattr(cli, "expose_adapter", expose)
    monkeypatch.setattr(cli, "run_task", run_task)
    task = parse_record(_hybrid_json(), "hybrid", "task_h")
    result = await cli._run_multi_gym_task(None, None, "http://localhost:8000", task, _args("all-verifiers"))
    assert result.scoring == "all-verifiers" and seen["scoring"] == "all-verifiers"
    assert sorted(started) == sorted(g.url for g in task.gyms) == sorted(stopped)
    assert len(closed) == len(started) == len(task.gyms)
    assert set(seen["gym_proxies"]) == {g.name for g in task.gyms}
    for g in task.gyms:
        base, proxy = seen["gym_proxies"][g.name]
        assert proxy.url == g.url and base == proxy.local_url


@pytest.mark.asyncio
async def test_a_single_public_address_cannot_front_a_multi_gym_task(monkeypatch):
    monkeypatch.setenv("EOG_ADAPTER_PUBLIC_URL", "https://adapter.example")

    def never(*a, **k):
        raise AssertionError("no proxy may start")

    monkeypatch.setattr(cli, "GymProxy", never)
    task = parse_record(_hybrid_json(), "hybrid", "task_h")
    result = await cli._run_multi_gym_task(None, None, "http://localhost:8000", task, _args())
    assert result.passed is None and result.terminal_status == "env_error" and "one per gym" in result.error


def test_the_cli_offers_both_scoring_rules():
    parser = cli.build_parser()
    args = parser.parse_args(["run"])
    assert args.scoring == "upstream"
    assert parser.parse_args(["run", "--scoring", "all-verifiers"]).scoring == "all-verifiers"
    from eogbench.runner import SCORING_RULES

    run = next(a for a in parser._subparsers._group_actions[0].choices["run"]._actions if a.dest == "scoring")
    assert tuple(run.choices) == SCORING_RULES  # build_parser stays self-contained (harness_evolution reads it alone)


def _fakes(monkeypatch, *, fail_expose_at=None):
    started, stopped = [], []

    class Proxy:
        def __init__(self, url):
            self.url, self.port = url, 9000 + len(started)
            self.local_url = f"http://127.0.0.1:{self.port}"

        def start(self):
            started.append(self)
            return self

        def stop(self):
            stopped.append(self)

    def expose(base, port, public_url=None):
        if fail_expose_at is not None and len(started) - 1 == fail_expose_at:
            raise RuntimeError("tunnel refused")
        return SimpleNamespace(public_base=None, close=lambda: None)

    monkeypatch.delenv("EOG_ADAPTER_PUBLIC_URL", raising=False)
    monkeypatch.setattr(cli, "GymProxy", Proxy)
    monkeypatch.setattr(cli, "expose_adapter", expose)
    return started, stopped


@pytest.mark.asyncio
async def test_two_gyms_on_one_url_still_get_a_proxy_each(monkeypatch):
    started, stopped = _fakes(monkeypatch)
    seen = {}

    async def run_task(client, registrar, tunnel_url, proxy, task, wall_clock_cap_s, gym_proxies, scoring):
        seen.update(gym_proxies)
        return cli.RolloutResult(task_id=task.task_id, session_id="s")

    monkeypatch.setattr(cli, "run_task", run_task)
    raw = _hybrid_json()
    raw["gym_servers_config"][1]["mcp_server_url"] = raw["gym_servers_config"][0]["mcp_server_url"]
    task = parse_record(raw, "hybrid", "task_h")
    await cli._run_multi_gym_task(None, None, "http://localhost:8000", task, _args())
    assert len(started) == 2 and len({id(p) for _, p in seen.values()}) == 2 and len(stopped) == 2


@pytest.mark.asyncio
async def test_a_setup_failure_is_an_infrastructure_row_and_stops_what_started(monkeypatch):
    started, stopped = _fakes(monkeypatch, fail_expose_at=1)

    async def run_task(*a, **k):
        raise AssertionError("no rollout after a failed setup")

    monkeypatch.setattr(cli, "run_task", run_task)
    task = parse_record(_hybrid_json(), "hybrid", "task_h")
    result = await cli._run_multi_gym_task(None, None, "http://localhost:8000", task, _args())
    assert result.passed is None and result.terminal_status == "env_error" and "tunnel refused" in result.error
    assert len(started) == 2 and len(stopped) == 2


def test_the_outcome_counts_the_results_the_verdict_read():
    results = [{"name": "a", "passed": False}, {"name": "b", "passed": True}, {"name": "a", "passed": True}]
    up = cli._outcome({"task_id": "t", "passed": True, "verifier_results": results, "scoring": "upstream"}, "csm")
    assert (up.verifiers_total, up.verifiers_passed, up.failed_verifiers) == (2, 2, [])
    old = cli._outcome({"task_id": "t", "passed": False, "verifier_results": results,
                        "scoring": "all-verifiers"}, "csm")
    assert (old.verifiers_total, old.verifiers_passed, old.failed_verifiers) == (3, 2, ["a"])
