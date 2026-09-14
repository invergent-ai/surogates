"""The seam layer over the vendored automation-bench library.

Upstream's ``AutomationBenchEnv`` (a verifiers ToolEnv) owns the agent
loop; here the surogates harness owns the loop, so this module drives
the same library pieces directly, mirroring ``AutomationBenchEnv.setup_state``
step for step:

- task rows from ``automationbench.domains`` (prompt + info with
  initial_state / assertions / zapier_tools),
- a per-task ``WorldState`` built from the None-stripped initial state,
  with ``allowed_services`` computed exactly as upstream does,
- the ``api`` toolset callables (search/fetch discovery interface),
  with the ``world`` parameter injected here and hidden from the agent,
- scoring via upstream's ``rubric.partial_credit`` /
  ``task_completed_correctly`` over the same state shape verifiers
  would have carried.

Everything imported lazily so the offline tests run without the
vendored package; the seam tests pin the interfaces when it is present.
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any

_HIDDEN_PARAMS = {"world"}


@dataclass(frozen=True)
class Task:
    task_id: str
    domain: str
    task_name: str
    system_prompt: str
    user_prompt: str
    info: dict[str, Any]


def load_domain_tasks(domain: str) -> list[Task]:
    from automationbench.domains import get_domain_dataset

    tasks = []
    for row in get_domain_dataset(domain):
        info = row.get("info") or {}
        if isinstance(info, str):
            info = json.loads(info)
        prompt = row.get("prompt") or []
        system = next((m.get("content", "") for m in prompt
                       if m.get("role") == "system"), "")
        user = "\n\n".join(m.get("content", "") for m in prompt
                           if m.get("role") == "user")
        tasks.append(Task(
            task_id=f"{domain}/{row.get('example_id')}",
            domain=domain,
            task_name=str(info.get("task_name") or row.get("example_id")),
            system_prompt=str(system),
            user_prompt=str(user),
            info=dict(info),
        ))
    if not tasks:
        raise SystemExit(f"domain {domain} yielded no tasks")
    return tasks


def make_world(task: Task):
    """WorldState + normalized info, exactly as upstream's setup_state."""
    from automationbench.runner import compute_allowed_services, strip_none_values
    from automationbench.schema.world import WorldState

    info = json.loads(json.dumps(task.info))  # deep copy, JSON-safe
    initial = strip_none_values(info.get("initial_state") or {})
    info["assertions"] = [strip_none_values(a)
                          for a in info.get("assertions") or []]
    world = WorldState(**initial)
    world.meta.allowed_services = compute_allowed_services(
        initial, info["assertions"], info.get("zapier_tools") or []
    )
    return world, info


def tool_registry() -> dict[str, Any]:
    from automationbench.tools import API_TOOLS

    return {func.__name__: func for func in API_TOOLS}


def tool_catalog() -> list[dict[str, Any]]:
    """Agent-facing catalog: signatures minus the injected params."""
    catalog = []
    for name, func in sorted(tool_registry().items()):
        params = []
        for pname, param in inspect.signature(func).parameters.items():
            if pname in _HIDDEN_PARAMS:
                continue
            entry = {"name": pname}
            if param.annotation is not inspect.Parameter.empty:
                entry["type"] = getattr(param.annotation, "__name__",
                                        str(param.annotation))
            if param.default is not inspect.Parameter.empty:
                entry["default"] = param.default
            else:
                entry["required"] = True
            params.append(entry)
        catalog.append({
            "name": name,
            "description": (func.__doc__ or "").strip(),
            "parameters": params,
        })
    return catalog


def dispatch(world: Any, tool_name: str, tool_args: dict[str, Any]) -> str:
    """Run one tool call against the task world; errors become results.

    Mirrors upstream's update_tool_args: an empty-object argument means
    "use the default", and the world is injected when the raw signature
    asks for it.
    """
    registry = tool_registry()
    func = registry.get(tool_name)
    if func is None:
        return json.dumps({
            "error": f"Unknown tool: {tool_name}. "
                     f"Available: {sorted(registry)}"
        })
    args = {k: v for k, v in (tool_args or {}).items()
            if not (isinstance(v, dict) and len(v) == 0)}
    if "world" in inspect.signature(func).parameters:
        args["world"] = world
    try:
        result = func(**args)
    except TypeError as exc:
        return json.dumps({"error": f"bad arguments: {exc}"})
    except Exception as exc:  # noqa: BLE001 - env errors are results
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    return result if isinstance(result, str) else json.dumps(result, default=str)


def score(world: Any, info: dict[str, Any]) -> tuple[float, float]:
    """(partial_credit, strict) via upstream's rubric over vf-shaped state."""
    from automationbench import rubric

    state = {"world": world, "info": info}
    partial = float(rubric.partial_credit(state))
    strict = float(rubric.task_completed_correctly(state))
    return partial, strict


def dump_world(world: Any) -> dict[str, Any]:
    return world.model_dump(mode="json")
