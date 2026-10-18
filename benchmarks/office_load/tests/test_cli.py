"""The oload command line: its commands and options, the run record, the editors check, and whole runs with the load
clients and the cluster faked."""

from __future__ import annotations

import asyncio
import functools
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

import aiohttp
import pytest
from aiohttp.test_utils import unused_port

from oload import cli
from oload import ramp as ramp_module
from oload.clients import ClientDown, Fleet, Timed
from oload.events import Event, EventLog
from oload.metrics import ProbeFailed, Sampler
from oload.settings import Settings

SETTINGS = Settings(
    ds_url="http://office-docs.example.svc", command_url="http://127.0.0.1:9", jwt_secret="s" * 32,
    client_token="a-test-client-token", listen="127.0.0.1:9", public_base="http://127.0.0.1:9",
    namespace="office-load",
)


def test_help_lists_the_four_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["--help"])
    assert exited.value.code == 0
    out = capsys.readouterr().out
    for command in ("preflight-clients", "preflight-cap", "ramp", "report"):
        assert command in out


def test_the_ramp_defaults_to_the_spec_and_takes_overrides_for_smoke_runs() -> None:
    args = cli.parser().parse_args(["ramp", "--workload", "chat", "--profile", "prod", "--run-id", "prod-chat-1"])
    assert (args.workload, args.profile, args.run_id) == ("chat", "prod", "prod-chat-1")
    assert args.steps is None and args.ramp_s == 60 and args.hold_s == 600
    smoke = cli.parser().parse_args(["ramp", "--workload", "rollout", "--profile", "prod", "--run-id", "smoke-1",
                                     "--steps", "4,8", "--ramp-s", "5", "--hold-s", "120"])
    assert smoke.steps == [4, 8] and smoke.ramp_s == 5 and smoke.hold_s == 120


@pytest.mark.parametrize("argv", [
    ["ramp", "--workload", "burst", "--profile", "prod", "--run-id", "x"],
    ["ramp", "--workload", "rollout", "--run-id", "x"],
    ["ramp", "--workload", "rollout", "--profile", "prod", "--run-id", "x", "--steps", "4,0"],
    ["preflight-clients", "--pages", "1,two"],
    ["preflight-cap"],
])
def test_bad_options_are_refused(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.parser().parse_args(argv)
    assert exited.value.code == 2


def test_the_preflights_take_their_counts() -> None:
    assert cli.parser().parse_args(["preflight-clients", "--pages", "1,2,4,8"]).pages == [1, 2, 4, 8]
    assert cli.parser().parse_args(["preflight-cap", "--editors", "25,50"]).editors == [25, 50]


def test_a_run_id_must_fit_a_document_key() -> None:
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["ramp", "--workload", "rollout", "--profile", "p", "--run-id", "a/b"])


def test_the_run_record_keeps_no_secret() -> None:
    fleet = Fleet([object(), object()], pages_per_client=20)  # type: ignore[list-item]
    record = cli.run_record(SETTINGS, "ramp", run_id="r1", workload="rollout", profile="prod", fleet=fleet,
                            extra={"steps": None, "ramp_s": 60.0, "hold_s": 600.0})
    assert record["pages_per_client"] == 20 and record["clients"] == 2
    assert record["workload"] == "rollout" and record["profile"] == "prod" and record["steps"] is None
    assert record["settings"]["namespace"] == "office-load"
    assert "jwt_secret" not in record["settings"] and "client_token" not in record["settings"]
    text = json.dumps(record)
    assert SETTINGS.jwt_secret not in text and SETTINGS.client_token not in text
    assert "git_sha" in record


def test_missing_settings_end_the_command_with_a_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("OLOAD_DS_URL", "OLOAD_COMMAND_URL", "OLOAD_JWT_SECRET", "OLOAD_CLIENT_TOKEN", "OLOAD_LISTEN",
                 "OLOAD_PUBLIC_BASE", "OLOAD_NAMESPACE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(cli, "RUNS", tmp_path / "runs")
    assert cli.main(["ramp", "--workload", "rollout", "--profile", "prod", "--run-id", "r1"]) == 2
    assert "OLOAD_DS_URL" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


def test_report_renders_a_run_by_its_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"run_id": "r1", "command": "ramp", "workload": "rollout"}))
    monkeypatch.setattr(cli, "RUNS", tmp_path / "runs")
    assert cli.main(["report", "r1"]) == 0
    assert (run_dir / "report.md").is_file()
    assert "# Office load: r1" in capsys.readouterr().out
    assert cli.main(["report", "nothing-here"]) == 2


# The editors check: every editor open before any edits.

class SlowOffice:
    """A load client whose opens take *open_s*, the first of them failing; it records when each edit came."""

    def __init__(self, open_s: list[float]) -> None:
        self.open_s = open_s
        self.opens = 0
        self.settled: list[float] = []
        self.edits: list[float] = []

    async def open(self, *args: Any) -> Timed[str]:
        index, self.opens = self.opens, self.opens + 1
        await asyncio.sleep(self.open_s[index])
        self.settled.append(asyncio.get_running_loop().time())
        if index == 0:
            raise ClientDown("gone", seconds=self.open_s[index])
        return Timed(f"page-{index}", self.open_s[index])

    async def edit(self, page: str, ops: list[Any]) -> Timed[dict[str, Any]]:
        self.edits.append(asyncio.get_running_loop().time())
        return Timed({"applied": True}, 0.01)


async def test_editors_edit_only_once_every_editor_opened_or_failed_to() -> None:
    office = SlowOffice([0.15, 0.01, 0.05])
    gate = cli.OpenGate()
    gated = cli.Gated(office, gate)  # type: ignore[arg-type]
    gate.expect(3)

    async def editor() -> None:
        try:
            opened = await gated.open("key", "session", {}, "http://ds")
        except ClientDown:
            return
        await gated.edit(opened.value, [])

    await asyncio.gather(editor(), editor(), editor())
    assert len(office.edits) == 2
    assert min(office.edits) >= max(office.settled)  # after the slowest open, though it failed


async def test_the_gate_gives_way_after_its_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "GATE_TIMEOUT_S", 0.05)
    gate = cli.OpenGate()
    gate.expect(2)  # one editor never opens: its place in the fleet failed
    gate.settled()
    await asyncio.wait_for(gate.wait(), 1)


def test_the_editors_outcome_counts_each_editor_once() -> None:
    def ev(session: int, what: str, ok: bool = True, cause: str | None = None, detail: str | None = None) -> Event:
        return Event(session, "docx", what, ok, 0.1, cause, 0.0, detail)  # type: ignore[arg-type]

    events = [ev(0, "open"), ev(1, "open"), ev(2, "open", False, "server", "connection limit"),
              ev(0, "edit"), ev(1, "edit"), ev(0, "flush"), ev(1, "flush", False, "server"),
              ev(0, "forcesave"), ev(0, "callback"), ev(0, "marker")]
    outcome = cli.editors_outcome(3, events)
    assert (outcome["editors"], outcome["opened"], outcome["flushed"], outcome["saved"]) == (3, 2, 1, 1)
    assert not outcome["holds"]
    assert any("connection limit" in failure for failure in outcome["failures"])


# Whole runs, with the load clients and the cluster faked.

def tool_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """The OLOAD_* names for a tool listening on a free local port; its base URL."""
    port = unused_port()
    base = f"http://127.0.0.1:{port}"
    for name, value in {"OLOAD_DS_URL": "http://office-docs.example.svc", "OLOAD_COMMAND_URL": "http://127.0.0.1:9",
                        "OLOAD_JWT_SECRET": "s" * 32, "OLOAD_CLIENT_TOKEN": "a-test-client-token",
                        "OLOAD_LISTEN": f"127.0.0.1:{port}", "OLOAD_PUBLIC_BASE": base,
                        "OLOAD_NAMESPACE": "office-load"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(cli, "RUNS", tmp_path / "runs")

    async def no_cluster(argv: Sequence[str], timeout_s: float) -> str:
        raise ProbeFailed("no cluster here")

    monkeypatch.setattr(cli, "Sampler", functools.partial(Sampler, run=no_cluster))
    return base


def fake_fleet(monkeypatch: pytest.MonkeyPatch, clients: list[Any], pages: int) -> None:
    async def discover(settings: Settings, pages_per_client: int | None) -> Fleet:
        return Fleet(clients, pages_per_client or pages)

    monkeypatch.setattr(cli, "_discover", discover)


class ServedSession:
    """A session that fetches a file from the tool, to show it is served through the step, and logs an open."""

    statuses: ClassVar[list[int]] = []

    def __init__(self, index: int, workload: str, fleet: Fleet, settings: Settings, state: Any, log: EventLog,
                 rng: Any, *, run_id: str, pace: Any = None) -> None:
        self.index, self.fleet, self.settings, self.log = index, fleet, settings, log

    async def run(self, until: float, *, turns: int | None = None) -> None:
        client = self.fleet.place(1)
        try:
            async with aiohttp.ClientSession() as http, http.get(f"{self.settings.public_base}/files/none") as answer:
                ServedSession.statuses.append(answer.status)
            self.log.add(Event(self.index, "docx", "open", True, 0.1, None, time.time()))
            await asyncio.sleep(max(0.0, until - time.monotonic()))
        finally:
            self.fleet.release(client)


def test_a_smoke_ramp_serves_the_tool_records_the_run_and_reports(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    tool_env(monkeypatch, tmp_path)
    fake_fleet(monkeypatch, [object(), object()], 4)
    ServedSession.statuses = []
    monkeypatch.setattr(ramp_module, "VirtualSession", ServedSession)

    assert cli.main(["ramp", "--workload", "rollout", "--profile", "smoke", "--run-id", "smoke-1", "--steps", "2,4",
                     "--ramp-s", "0", "--hold-s", "0.1"]) == 0

    assert ServedSession.statuses == [404] * 6  # the tool answered, for a document it does not serve
    run_dir = tmp_path / "runs" / "smoke-1"
    record = json.loads((run_dir / "run.json").read_text())
    assert (record["command"], record["workload"], record["profile"]) == ("ramp", "rollout", "smoke")
    assert (record["clients"], record["pages_per_client"], record["steps"]) == (2, 4, [2, 4])
    assert "a-test-client-token" not in (run_dir / "run.json").read_text()
    for n in (2, 4):
        assert (run_dir / f"step-{n}" / "step.json").is_file()
        sampled = (run_dir / f"step-{n}" / "metrics.jsonl").read_text().splitlines()
        assert sampled and json.loads(sampled[0])["failed"]["top"] == "no cluster here"
    report = (run_dir / "report.md").read_text()
    assert "**Capacity: at least 4 sessions.**" in report
    assert "the top probe failed" in report
    assert cli.main(["ramp", "--workload", "rollout", "--profile", "smoke", "--run-id", "smoke-1"]) == 2  # one run


class OpeningOffice:
    """A load client whose opens take a while; it records when each open ended and each edit came."""

    def __init__(self, open_s: float) -> None:
        self.open_s = open_s
        self.opened: list[float] = []
        self.edits: list[float] = []

    async def open(self, *args: Any) -> Timed[str]:
        await asyncio.sleep(self.open_s)
        self.opened.append(time.monotonic())
        return Timed("page", self.open_s)

    async def edit(self, page: str, ops: list[Any]) -> Timed[dict[str, Any]]:
        self.edits.append(time.monotonic())
        return Timed({"applied": True}, 0.01)


class EditorSession:
    """An editor as the check drives it: open, one edit, then its save found."""

    def __init__(self, index: int, workload: str, fleet: Fleet, settings: Settings, state: Any, log: EventLog,
                 rng: Any, *, run_id: str, pace: Any = None) -> None:
        assert pace is not None and pace.edits_in_turn() == 1
        self.index, self.fleet, self.log = index, fleet, log

    async def run(self, until: float, *, turns: int | None = None) -> None:
        assert turns == 1
        client = self.fleet.place(1)
        try:
            opened = await client.open("key", "session", {}, "http://ds")
            await client.edit(opened.value, [])
            for what in ("open", "edit", "flush", "forcesave", "callback", "marker"):
                self.log.add(Event(self.index, "docx", what, True, 0.1, None, time.time()))  # type: ignore[arg-type]
        finally:
            self.fleet.release(client)


def test_the_editors_check_opens_every_editor_before_any_edits(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    tool_env(monkeypatch, tmp_path)
    offices = [OpeningOffice(0.02), OpeningOffice(0.2)]
    fake_fleet(monkeypatch, offices, 4)
    monkeypatch.setattr(cli, "VirtualSession", EditorSession)

    assert cli.main(["preflight-cap", "--editors", "3,5", "--run-id", "cap-1"]) == 0

    opened = sorted(t for office in offices for t in office.opened)
    edits = sorted(t for office in offices for t in office.edits)
    assert len(opened) == len(edits) == 8
    assert edits[0] >= opened[2] and edits[3] >= opened[7]  # each count's edits after its every open
    run_dir = tmp_path / "runs" / "cap-1"
    for n in (3, 5):
        check = json.loads((run_dir / f"editors-{n}" / "cap.json").read_text())
        assert (check["editors"], check["opened"], check["flushed"], check["saved"], check["holds"]) == (
            n, n, n, n, True)
        assert (run_dir / f"editors-{n}" / "metrics.jsonl").is_file()
    assert "| 5 | 5 | 5 | 5 |" in (run_dir / "report.md").read_text()
    assert cli.main(["preflight-cap", "--editors", "9"]) == 2  # more than the fleet holds
