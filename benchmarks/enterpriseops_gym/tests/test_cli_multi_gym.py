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
    assert sorted(started) == sorted({g.url for g in task.gyms}) == sorted(stopped)
    assert len(closed) == len(started)
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
