"""Drive one EnterpriseOps-Gym task through one agent session.

Sequential by construction (like benchmarks/claweval): the MCP
registration is agent-scoped and the gym proxy injects one database id
at a time. Per task:

1. **Seed.** ``create_database_from_file`` on the local gym server (the
   vendored upstream helper) makes a fresh task database; the proxy
   starts injecting its id.
2. **Expose.** The tunnel (started once per run) fronts the proxy; the
   ops registrar creates the MCP row and attaches it to the agent --
   fresh name per task so no proxy-side cache can leak tools across
   tasks.
3. **Roll out.** One session: the task's policy system prompt plus the
   user prompt in the first message, streamed to a terminal state with
   the sibling benchmarks' reconnect discipline.
4. **Verify.** The vendored ``VerifierEngine`` runs the task's hidden
   SQL against the same database, directly on the local gym URL -- the
   agent's writes and the verifier read one state. Task success = the
   verdict of upstream's ``BenchmarkExecutor._run_verifiers`` (``verdict``,
   scoring ``upstream``): one result per verifier NAME, the last of a
   repeated name wins, every kept result passed; a verifier whose query
   the gym rejects (HTTP 400, SQL error) is a fail, as upstream records
   it. ``all-verifiers`` (every verifier passed, a rejected query =
   infra) is the rule eogbench used before, kept to reproduce
   them.
5. **Teardown.** Detach + delete the MCP row, delete the database --
   even on failure.

Multi-gym (hybrid) tasks follow upstream's ``gym_servers_config`` path
(benchmark/executor.py + orchestrators/base.py): every gym gets its own
fresh database, its own scoped proxy and its own MCP row (distinct
names), all attached to the attempt's agent; a tool name belongs to the
FIRST gym (config order) whose tools/list has it, as upstream's tool
merge keeps the first occurrence; each verifier runs against the
database of the gym its ``gym_name`` names; task success as above.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from eogbench import vendor
from eogbench.client import Event
from eogbench.dataset import Gym, Task

_TERMINAL_STATUSES = {"completed", "archived", "failed"}

SCORING_RULES = ("upstream", "all-verifiers")

# The gym answered the verifier's SQL with HTTP 400 and an error in the query itself (no such table / column, a
# syntax error, an ambiguous column): a fault of the task's query, the same in every attempt. Anything else stays
# infra (re-run): transport faults (timeouts, refused connections, 5xx) and the database's own faults (locked,
# unable to open, I/O), where upstream records any verifier error as a fail.
_QUERY_REJECTED = re.compile(
    r"^SQL query failed: SQL API call failed \(HTTP 400\):.*"
    r"(no such table|no such column|syntax error|ambiguous column name)", re.S)


def query_rejected(outcome: dict[str, Any]) -> bool:
    return bool(_QUERY_REJECTED.search(str(outcome.get("error") or "")))


def kept_results(verifier_results: list[dict[str, Any]], scoring: str = "upstream") -> list[dict[str, Any]]:
    """The results a verdict reads: upstream keeps one per verifier NAME (or ``verifier_<i+1>``), the last of a
    repeated name; all-verifiers keeps every result."""
    if scoring == "all-verifiers":
        return list(verifier_results)
    if scoring != "upstream":
        raise ValueError(f"unknown scoring rule {scoring!r}")
    by_name: dict[str, dict[str, Any]] = {}
    for i, v in enumerate(verifier_results):
        by_name[v.get("name") or f"verifier_{i + 1}"] = v
    return list(by_name.values())


def verdict(verifier_results: list[dict[str, Any]], scoring: str = "upstream") -> bool | None:
    """Task success from the per-verifier results (in task order, every verifier present).

    upstream: ``verification_results[verifier.name or f"verifier_{i+1}"] = result`` then
    ``all(v["passed"] for v in verification_results.values())`` (benchmark/executor.py) -- a repeated name keeps
    only its LAST result. all-verifiers: every verifier passed (eogbench's earlier rule)."""
    if not verifier_results:
        return None
    return all(bool(v.get("passed")) for v in kept_results(verifier_results, scoring))


PROMPT_TEMPLATE = """{system_prompt}

---

{user_prompt}"""


@dataclass
class RolloutResult:
    task_id: str
    session_id: str
    database_id: str = ""
    events: list[Event] = field(default_factory=list)
    wall_clock_s: float = 0.0
    terminal_status: str = ""
    error: str | None = None
    verifier_results: list[dict[str, Any]] = field(default_factory=list)
    verify_error: str | None = None
    database_ids: dict[str, str] = field(default_factory=dict)  # multi-gym: gym name -> database
    scoring: str = "upstream"

    @property
    def passed(self) -> bool | None:
        if self.verify_error or not self.verifier_results:
            return None
        return verdict(self.verifier_results, self.scoring)


def build_prompt(task: Task) -> str:
    return PROMPT_TEMPLATE.format(
        system_prompt=task.system_prompt.strip(),
        user_prompt=task.user_prompt.strip(),
    )


def _seed(gym_url: str, seed_database_file: str) -> str:
    vendor.pythonpath()
    from benchmark.mcp_client import create_database_from_file  # type: ignore

    seed = seed_database_file
    if seed and not os.path.isabs(seed):
        seed = str(pathlib.Path(os.environ.get("EOG_SEED_ROOT") or vendor.home()) / seed)
    database_id = create_database_from_file(gym_url, seed)
    if not database_id:
        raise RuntimeError(
            f"gym at {gym_url} refused to create a database "
            f"(seed: {seed_database_file or 'server default'})"
        )
    return database_id


def _drop(gym_url: str, database_id: str) -> None:
    vendor.pythonpath()
    from benchmark.mcp_client import delete_database  # type: ignore

    if not delete_database(gym_url, database_id):
        raise RuntimeError("Gym did not confirm task database deletion")


def seed_database(task: Task) -> str:
    """Create the task's fresh database on the local gym server."""
    return _seed(task.gym_url, task.seed_database_file)


def drop_database(task: Task, database_id: str) -> None:
    _drop(task.gym_url, database_id)


def seed_gym(gym: Gym) -> str:
    """Multi-gym: one gym's fresh database (upstream seeds every gym per run)."""
    return _seed(gym.url, gym.seed_database_file)


def drop_gym(gym: Gym, database_id: str) -> None:
    _drop(gym.url, database_id)


async def gym_tool_names(gym: Gym, database_id: str, endpoint: str,
                         tries: int = 4, wait_s: float = 20.0) -> list[str]:
    """The gym's full tools/list, through the vendored client (as upstream's
    executor discovers tools). Retried: a gym seeding several databases at
    once stalls past the client's 30 s timeout (six attempts seeding the
    teams gym together gave 2 connect failures)."""
    vendor.pythonpath()
    from benchmark.mcp_client import MCPClient  # type: ignore

    for attempt in range(tries):
        client = MCPClient(base_url=gym.url, mcp_endpoint=endpoint, database_id=database_id,
                           context=gym.context, auth_config=gym.auth_config or None)
        if await client.connect():
            tools = await client.list_tools()
            if tools:
                return [str(t.get("name")) for t in tools]
        if attempt + 1 < tries:
            await asyncio.sleep(wait_s)
    raise RuntimeError(f"gym {gym.name} at {gym.url}: MCP connect / tools/list failed {tries}x")


def owned_tools(task: Task, listed: dict[str, list[str]]) -> dict[str, frozenset[str]]:
    """Upstream's tool merge: tools/list of every gym in config order, a
    name already taken by an earlier gym is skipped (first occurrence wins),
    then selected_tools keeps, restricted_tools drops. Returns, per gym, the
    task tools that gym serves."""
    allowed = frozenset(task.selected_tools) - frozenset(task.restricted_tools)
    taken: set[str] = set()
    out = {}
    for gym in task.gyms:
        own = [t for t in listed[gym.name] if t not in taken]
        taken.update(own)
        out[gym.name] = frozenset(own) & allowed
    return out


async def verify_gyms(task: Task, database_ids: dict[str, str],
                      scoring: str = "upstream") -> list[dict[str, Any]]:
    """Multi-gym: each verifier against the database (and context) of the gym
    its gym_name names, through one VerifierEngine holding a client per gym
    (upstream's BenchmarkExecutor._run_verifiers)."""
    vendor.pythonpath()
    from benchmark.mcp_client import MCPClient  # type: ignore
    from benchmark.models import VerifierConfig  # type: ignore
    from benchmark.verifier import VerifierEngine  # type: ignore

    by_name = {g.name: g for g in task.gyms}
    clients = {g.name: MCPClient(base_url=g.url, mcp_endpoint=task.mcp_endpoint,
                                 database_id=database_ids[g.name], context=g.context,
                                 auth_config=g.auth_config or None)
               for g in task.gyms}
    engine = VerifierEngine(mcp_clients=clients, llm_client=None)
    results = []
    for spec in task.verifiers:
        config = VerifierConfig(
            verifier_type=str(spec.get("verifier_type") or ""),
            validation_config=dict(spec.get("validation_config") or {}),
            name=spec.get("name"),
            description=spec.get("description"),
            gym_name=spec.get("gym_name"),
        )
        gym = by_name[config.gym_name]
        outcome = await engine.execute_verifier(
            config,
            model_response={},
            database_id=database_ids[gym.name],
            context=gym.context,
            gym_name=gym.name,
        )
        rejected = scoring == "upstream" and query_rejected(outcome)
        if not rejected and (outcome.get("error") or not isinstance(outcome.get("passed"), bool)):
            # infra; the message names the verifier and the gym's answer
            raise RuntimeError(f"Verifier infrastructure failed or returned no boolean verdict "
                               f"({gym.name}: {config.name!r}: {str(outcome.get('error'))[:300]})")
        results.append({
            "name": spec.get("name"),
            "gym": gym.name,
            "passed": False if rejected else bool(outcome.get("passed")),
            "detail": {k: v for k, v in outcome.items() if k != "passed"},
            **({"query_rejected": True} if rejected else {}),
        })
    return results


async def verify(task: Task, database_id: str, scoring: str = "upstream") -> list[dict[str, Any]]:
    """Run the task's hidden SQL verifiers against the final state."""
    vendor.pythonpath()
    from benchmark.mcp_client import MCPClient  # type: ignore
    from benchmark.models import VerifierConfig  # type: ignore
    from benchmark.verifier import VerifierEngine  # type: ignore

    client = MCPClient(
        base_url=task.gym_url,
        mcp_endpoint=task.mcp_endpoint,
        database_id=database_id,
        context=task.context,
        auth_config=task.auth_config or None,
    )
    # llm_client=None: every public task verifies via pure-SQL
    # database_state; a response_check task would fail loudly here
    # rather than pass silently.
    engine = VerifierEngine(
        mcp_clients={task.gym_name: client}, llm_client=None
    )
    results = []
    for spec in task.verifiers:
        config = VerifierConfig(
            verifier_type=str(spec.get("verifier_type") or ""),
            validation_config=dict(spec.get("validation_config") or {}),
            name=spec.get("name"),
            description=spec.get("description"),
            gym_name=spec.get("gym_name"),
        )
        outcome = await engine.execute_verifier(
            config,
            model_response={},
            database_id=database_id,
            gym_name=config.gym_name or task.gym_name,
        )
        rejected = scoring == "upstream" and query_rejected(outcome)
        if not rejected and (outcome.get("error") or not isinstance(outcome.get("passed"), bool)):
            raise RuntimeError("Verifier infrastructure failed or returned no boolean verdict")
        results.append({
            "name": spec.get("name"),
            "passed": False if rejected else bool(outcome.get("passed")),
            "detail": {k: v for k, v in outcome.items() if k != "passed"},
            **({"query_rejected": True} if rejected else {}),
        })
    return results


async def run_session(
    client: Any,
    task: Task,
    wall_clock_cap_s: float,
) -> tuple[str, list[Event], str, str | None]:
    """One message, streamed to a terminal state. Returns
    (session_id, events, terminal_status, error)."""
    session_id = await client.create_session()
    await client.send_message(session_id, build_prompt(task))

    started = time.monotonic()
    events: list[Event] = []
    cursor = 0
    while True:
        try:
            async for ev in client.stream_events(session_id, after=cursor):
                events.append(ev)
                cursor = max(cursor, ev.id)
        except httpx.TransportError:
            pass  # reconnect from cursor; status poll decides

        status = await client.get_session_status(session_id)
        if status in _TERMINAL_STATUSES:
            return session_id, events, status, None
        if time.monotonic() - started > wall_clock_cap_s:
            return session_id, events, "timeout", None
        await asyncio.sleep(0)


async def run_task(
    client: Any,
    registrar: Any,
    tunnel_url: str,
    proxy: Any,
    task: Task,
    wall_clock_cap_s: float = 1800.0,
    gym_proxies: dict[str, tuple[str, Any]] | None = None,
    scoring: str = "upstream",
) -> RolloutResult:
    """``gym_proxies`` (multi-gym tasks only): gym name -> (registered base
    URL, that gym's own proxy); ``tunnel_url`` / ``proxy`` serve single-gym
    tasks and are ignored for a multi-gym one."""
    if task.gyms:
        if not gym_proxies or any(g.name not in gym_proxies for g in task.gyms):
            raise ValueError(f"{task.task_id}: a multi-gym task needs one proxy per gym (gym_proxies)")
        if len({id(gym_proxies[g.name][1]) for g in task.gyms}) != len(task.gyms):
            # A proxy holds one task scope (database, tools, context, credentials): a second gym's
            # configure_task would overwrite the first's, and one gym's registration would serve the other.
            raise ValueError(f"{task.task_id}: each gym of a multi-gym task needs a proxy of its own")
        return await _run_multi_gym(client, registrar, gym_proxies, task, wall_clock_cap_s, scoring)
    if scoring not in SCORING_RULES:
        raise ValueError(f"unknown scoring rule {scoring!r}")
    started = time.monotonic()
    result = RolloutResult(task_id=task.task_id, session_id="", scoring=scoring)
    server_id: str | None = None

    try:
        try:
            result.database_id = seed_database(task)
            proxy.configure_task(result.database_id, task.selected_tools, task.restricted_tools,
                                 context=task.context, auth_config=task.auth_config, endpoint=task.mcp_endpoint)
            server_id = registrar.register(
                task.task_id.replace("/", "-"),
                f"{tunnel_url.rstrip('/')}{task.mcp_endpoint}",
            )

            (result.session_id, result.events,
             result.terminal_status, result.error) = await run_session(
                client, task, wall_clock_cap_s
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            result.error = f"{type(exc).__name__}: {exc}"
            result.terminal_status = result.terminal_status or "error"
        finally:
            proxy.set_database_id(None)
            if server_id is not None:
                try:
                    registrar.remove(task.task_id.replace("/", "-"))
                except Exception as exc:  # noqa: BLE001 - cleanup must not mask
                    result.error = result.error or f"cleanup: {exc}"

        # Verify against whatever state exists -- a failed session grades as
        # its verifiers find it, same as upstream's max-steps terminations.
        if result.database_id:
            try:
                result.verifier_results = await verify(task, result.database_id, scoring=scoring)
            except Exception as exc:  # noqa: BLE001
                result.verify_error = f"{type(exc).__name__}: {exc}"
    finally:
        if result.database_id:
            try:
                drop_database(task, result.database_id)
            except Exception as exc:  # noqa: BLE001 - retain cleanup failure
                result.error = result.error or f"database cleanup: {exc}"

    result.wall_clock_s = time.monotonic() - started
    return result


def server_key(task: Task, gym: Gym) -> str:
    """Registrar key of one gym's MCP row: distinct per gym of a task (the
    row is named eog-<sha1(key)>), stable per task (F13 rule unchanged)."""
    return f"{task.task_id.replace('/', '-')}@{gym.name}"


async def _run_multi_gym(
    client: Any,
    registrar: Any,
    gym_proxies: dict[str, tuple[str, Any]],
    task: Task,
    wall_clock_cap_s: float,
    scoring: str = "upstream",
) -> RolloutResult:
    if scoring not in SCORING_RULES:
        raise ValueError(f"unknown scoring rule {scoring!r}")
    started = time.monotonic()
    result = RolloutResult(task_id=task.task_id, session_id="", scoring=scoring)
    registered: list[str] = []

    try:
        try:
            for gym in task.gyms:  # upstream: every gym seeded before the run, config order
                result.database_ids[gym.name] = seed_gym(gym)
            result.database_id = result.database_ids[task.gyms[0].name]
            listed = {g.name: await gym_tool_names(g, result.database_ids[g.name], task.mcp_endpoint)
                      for g in task.gyms}
            owned = owned_tools(task, listed)
            for gym in task.gyms:
                if not owned[gym.name]:
                    continue  # nothing of this task's tool set lives here; still seeded and verified
                base_url, proxy = gym_proxies[gym.name]
                proxy.configure_task(result.database_ids[gym.name], sorted(owned[gym.name]), (),
                                     context=gym.context, auth_config=gym.auth_config, endpoint=task.mcp_endpoint)
                registered.append(server_key(task, gym))
                registrar.register(registered[-1], f"{base_url.rstrip('/')}{task.mcp_endpoint}")

            (result.session_id, result.events,
             result.terminal_status, result.error) = await run_session(
                client, task, wall_clock_cap_s
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            result.error = f"{type(exc).__name__}: {exc}"
            result.terminal_status = result.terminal_status or "error"
        finally:
            for _, proxy in gym_proxies.values():
                proxy.set_database_id(None)
            for key in registered:
                try:
                    registrar.remove(key)
                except Exception as exc:  # noqa: BLE001 - cleanup must not mask
                    result.error = result.error or f"cleanup: {exc}"

        if result.database_ids and len(result.database_ids) == len(task.gyms):
            try:
                result.verifier_results = await verify_gyms(task, result.database_ids, scoring=scoring)
            except Exception as exc:  # noqa: BLE001
                result.verify_error = f"{type(exc).__name__}: {exc}"
        elif result.database_ids:
            result.verify_error = "not every gym database was created"
    finally:
        by_name = {g.name: g for g in task.gyms}
        for name, database_id in result.database_ids.items():
            try:
                drop_gym(by_name[name], database_id)
            except Exception as exc:  # noqa: BLE001 - retain cleanup failure
                result.error = result.error or f"database cleanup: {exc}"

    result.wall_clock_s = time.monotonic() - started
    return result


def write_trace(out_dir: str, result: RolloutResult) -> str:
    task_dir = os.path.join(out_dir, "tasks", result.task_id.replace("/", "__"))
    os.makedirs(task_dir, exist_ok=True)
    with open(os.path.join(task_dir, "events.jsonl"), "w", encoding="utf-8") as fh:
        for ev in result.events:
            fh.write(json.dumps(
                {"id": ev.id, "type": ev.type, "data": ev.data}, default=str
            ) + "\n")
    meta = {
        "task_id": result.task_id,
        "session_id": result.session_id,
        "database_id": result.database_id,
        "wall_clock_s": result.wall_clock_s,
        "terminal_status": result.terminal_status,
        "error": result.error,
        "passed": result.passed,
        "verifier_results": result.verifier_results,
        "verify_error": result.verify_error,
        "scoring": result.scoring,
    }
    if result.database_ids:  # multi-gym only; single-gym meta.json unchanged
        meta["database_ids"] = result.database_ids
    with open(os.path.join(task_dir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)
    return task_dir
