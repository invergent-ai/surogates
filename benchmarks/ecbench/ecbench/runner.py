"""Drive one ECBench episode through one agent session.

A fresh session per episode, always: the session workspace holds the
simulation state, so sharing one would share the store.

Flow: create session -> upload the staged simulation -> send the
operator prompt -> stream events to a terminal state (same reconnect
discipline as the sibling benchmarks: 300 s server-side stream cap,
failed sessions never close their own stream -- poll status) -> download
the state artifacts (``final_state.json``, ``calls.jsonl``,
``sim_state.pkl``). Scoring happens later, offline, from those files --
and can recompute the settlement locally from the pickle, so an agent
that forgets to run ``finalize`` still gets scored.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ecbench.client import Event
from ecbench.staging import stage_plan

_TERMINAL_STATUSES = {"completed", "archived", "failed"}

# Episode artifacts to pull back, in priority order.
ARTIFACTS = ("final_state.json", "calls.jsonl", "sim_state.pkl")

PROMPT_TEMPLATE = """You are the sole proprietor of a new online store on \
an e-commerce platform, and your goal is to end the simulated period with \
the highest possible total assets (bank balance + platform wallet + \
pending settlement). You start with a stake of {balance:.0f} and the \
episode covers {max_days} simulated days.

The entire business runs through a simulator in your workspace. Drive it \
from your terminal:

1. `python3 ecsim.py init --max-days {max_days}` -- start the episode \
(run this exactly once; if it reports missing pandas/numpy, run \
`pip install pandas numpy` and retry).
2. `python3 ecsim.py tools` -- list the store tools and what they do.
3. `python3 ecsim.py status` -- current day, time and balances.
4. `python3 ecsim.py call '[{{"tool_name": "...", "tool_args": \
{{...}}}}]'` -- act. Batches run in order; time advances as tools \
consume simulated minutes, and one `wait_for_next_day` per batch moves \
to the next day.
5. When the episode reports it is over: `python3 ecsim.py finalize` -- \
settles everything and writes final_state.json. Always finish with this.

Business notes: research the market before stocking; negotiate with \
suppliers through the chatbox tool (some suppliers are fraudulent -- \
verify before paying); mind cash flow, rent is charged daily and \
bankruptcy ends the episode. Work day by day until day {max_days} is \
done, then finalize. Do the work through the simulator -- narrating a \
plan without tool calls achieves nothing."""


@dataclass
class EpisodeResult:
    episode: int
    session_id: str
    events: list[Event] = field(default_factory=list)
    wall_clock_s: float = 0.0
    terminal_status: str = ""
    error: str | None = None
    artifacts: list[str] = field(default_factory=list)
    collect_notes: list[str] = field(default_factory=list)


def build_prompt(max_days: int, balance: float) -> str:
    return PROMPT_TEMPLATE.format(max_days=max_days, balance=balance)


async def _collect_artifacts(
    client: Any, session_id: str, episode_dir: str
) -> tuple[list[str], list[str]]:
    got: list[str] = []
    notes: list[str] = []
    try:
        tree = {f["path"]: f["size"] for f in await client.get_workspace_tree(session_id)}
    except Exception as exc:  # noqa: BLE001 - collection is best-effort
        return got, [f"workspace tree failed: {exc}"]

    for name in ARTIFACTS:
        if name not in tree:
            notes.append(f"{name} not present in workspace")
            continue
        try:
            blob = await client.download_file(session_id, name)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"download failed for {name}: {exc}")
            continue
        os.makedirs(episode_dir, exist_ok=True)
        with open(os.path.join(episode_dir, name), "wb") as fh:
            fh.write(blob)
        got.append(name)
    return got, notes


async def run_episode(
    client: Any,
    episode: int,
    episode_dir: str,
    max_days: int,
    balance: float = 100000.0,
    wall_clock_cap_s: float = 14400.0,
) -> EpisodeResult:
    started = time.monotonic()
    session_id = ""
    events: list[Event] = []
    status = ""
    error: str | None = None
    artifacts: list[str] = []
    notes: list[str] = []

    try:
        plan = stage_plan()
        session_id = await client.create_session()
        for staged in plan:
            await client.upload_file(
                session_id, staged.local_path, staged.name, subdir=staged.subdir
            )

        await client.send_message(session_id, build_prompt(max_days, balance))

        cursor = 0
        while True:
            try:
                async for ev in client.stream_events(session_id, after=cursor):
                    events.append(ev)
                    cursor = max(cursor, ev.id)
            except httpx.TransportError:
                # Reconnect from the cursor; the status poll below is the
                # real liveness check.
                pass

            status = await client.get_session_status(session_id)
            if status in _TERMINAL_STATUSES:
                break
            if time.monotonic() - started > wall_clock_cap_s:
                status = "timeout"
                break
            await asyncio.sleep(0)

        # Collect whatever exists even after a failure or timeout: the
        # pickle lets the scorer settle the store as-is.
        artifacts, notes = await _collect_artifacts(client, session_id, episode_dir)

    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        error = f"{type(exc).__name__}: {exc}"
        status = status or "error"

    return EpisodeResult(
        episode=episode,
        session_id=session_id,
        events=events,
        wall_clock_s=time.monotonic() - started,
        terminal_status=status,
        error=error,
        artifacts=artifacts,
        collect_notes=notes,
    )


def write_trace(episode_dir: str, result: EpisodeResult) -> None:
    os.makedirs(episode_dir, exist_ok=True)
    with open(os.path.join(episode_dir, "events.jsonl"), "w", encoding="utf-8") as fh:
        for ev in result.events:
            fh.write(json.dumps(
                {"id": ev.id, "type": ev.type, "data": ev.data}, default=str
            ) + "\n")
    with open(os.path.join(episode_dir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "episode": result.episode,
            "session_id": result.session_id,
            "wall_clock_s": result.wall_clock_s,
            "terminal_status": result.terminal_status,
            "error": result.error,
            "artifacts": result.artifacts,
            "collect_notes": result.collect_notes,
        }, fh, indent=2)


async def run_episodes(
    client: Any,
    out_dir: str,
    episodes: int,
    max_days: int,
    balance: float = 100000.0,
    concurrency: int = 1,
    wall_clock_cap_s: float = 14400.0,
) -> list[EpisodeResult]:
    """Run episodes, persisting each trace as it completes.

    Episodes share nothing (per-session workspaces), so they *can* run
    in parallel -- but each one is a long, provider-heavy session, and
    the dev-001 workspace-bench run showed the tier throttling under
    sustained load, so the default is sequential.
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(index: int) -> EpisodeResult:
        async with sem:
            episode_dir = os.path.join(out_dir, "episodes", f"{index:02d}")
            result = await run_episode(
                client, index, episode_dir,
                max_days=max_days, balance=balance,
                wall_clock_cap_s=wall_clock_cap_s,
            )
            write_trace(episode_dir, result)
            return result

    return list(await asyncio.gather(*(one(i + 1) for i in range(episodes))))
