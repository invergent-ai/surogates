"""On-demand tool groups: schemas the model sees only once it asks for them.

A prompt profile may declare ``tool_groups`` in its ``profile.yaml``: named
sets of tools that are rarely needed (a browser, schedules, image
generation, ...). Their schemas are held back from the request, and
``load_tools`` -- whose description lists the held-back groups, one line
each -- brings a group in from the model's next step on. Every request
then carries the schemas a task actually uses instead of all of them.

Held-back tools stay *callable*: a call to one (a skill's instructions may
name ``cron_create`` directly) is dispatched as usual and loads its group,
so the model never meets an "unknown tool" for a tool it was entitled to.
What a session may call is decided before this module runs (policy,
entitlements, channel); grouping only changes what is shown.

Loaded groups are read back from the conversation (successful
``load_tools`` results and calls to a group's tools), so a later turn of
the same session shows them again without another load.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from surogates.harness.prompt_profile import PROFILES_ROOT, profile_settings

LOAD_TOOLS = "load_tools"


@dataclass(frozen=True)
class ToolGroup:
    name: str
    summary: str
    tools: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    guide: str = ""

    def has(self, tool: str) -> bool:
        return tool in self.tools or any(tool.startswith(p) for p in self.prefixes)


def tool_groups(profile: str | None) -> tuple[ToolGroup, ...]:
    """The groups *profile* holds back (none without a profile)."""
    raw = profile_settings(profile).get("tool_groups") or []
    return tuple(_group(g) for g in raw if isinstance(g, dict) and g.get("name"))


def _group(g: dict) -> ToolGroup:
    return ToolGroup(
        name=str(g["name"]),
        summary=str(g.get("summary") or "").strip(),
        tools=tuple(g.get("tools") or ()),
        prefixes=tuple(g.get("prefixes") or ()),
        guide=str(g.get("guide") or "").strip(),
    )


def all_tool_groups() -> dict[str, ToolGroup]:
    """Every group any profile declares, by name (for the load_tools handler)."""
    out: dict[str, ToolGroup] = {}
    if PROFILES_ROOT.is_dir():
        for d in sorted(p for p in PROFILES_ROOT.iterdir() if p.is_dir()):
            for g in tool_groups(d.name):
                out.setdefault(g.name, g)
    return out


def group_of(tool: str, groups: Sequence[ToolGroup]) -> ToolGroup | None:
    return next((g for g in groups if g.has(tool)), None)


@dataclass
class DeferredTools:
    """The schemas held back from a session, by group, and the groups shown."""

    groups: tuple[ToolGroup, ...]
    held: dict[str, list[dict[str, Any]]]

    @property
    def names(self) -> frozenset[str]:
        """Tools held back: callable, not shown."""
        return frozenset(s["function"]["name"] for ss in self.held.values() for s in ss)

    def take(self, group_names: Iterable[str]) -> list[dict[str, Any]]:
        """Release the schemas of *group_names* (those still held back)."""
        out: list[dict[str, Any]] = []
        for name in group_names:
            out.extend(self.held.pop(name, []))
        return out

    def groups_of_calls(self, tool_names: Iterable[str]) -> set[str]:
        """Held-back groups a direct call to one of *tool_names* loads."""
        found: set[str] = set()
        for t in tool_names:
            g = group_of(t, self.groups)
            if g is not None and g.name in self.held:
                found.add(g.name)
        return found


def defer_tool_groups(
    schemas: list[dict[str, Any]],
    groups: Sequence[ToolGroup],
    loaded: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], DeferredTools]:
    """Split *schemas* into what is shown now and what waits for load_tools.

    Groups are held back only when ``load_tools`` itself is among *schemas*
    (an explicit tool allow-list without it keeps everything shown). The
    ``load_tools`` schema is rewritten to list the held-back groups, and
    dropped when nothing is held back.
    """
    names = [s["function"]["name"] for s in schemas]
    if not groups or LOAD_TOOLS not in names:
        return [s for s in schemas if s["function"]["name"] != LOAD_TOOLS], DeferredTools(tuple(groups), {})
    loaded = set(loaded)
    shown: list[dict[str, Any]] = []
    held: dict[str, list[dict[str, Any]]] = {}
    for s in schemas:
        name = s["function"]["name"]
        g = group_of(name, groups)
        if g is not None and g.name not in loaded:
            held.setdefault(g.name, []).append(s)
        else:
            shown.append(s)
    deferred = DeferredTools(tuple(groups), held)
    return refresh_load_tools(shown, deferred), deferred


def refresh_load_tools(schemas: list[dict[str, Any]], deferred: DeferredTools) -> list[dict[str, Any]]:
    """*schemas* with ``load_tools`` describing what is still held back (or gone)."""
    out = [s for s in schemas if s["function"]["name"] != LOAD_TOOLS]
    pending = [g for g in deferred.groups if g.name in deferred.held]
    if not pending:
        return out
    original = next((s for s in schemas if s["function"]["name"] == LOAD_TOOLS), None)
    if original is None:
        return out
    clone = copy.deepcopy(original)
    fn = clone["function"]
    lines = "\n".join(f"- {g.name}: {g.summary}" for g in pending)
    fn["description"] = (
        "Load more tools. These groups are available but not loaded yet:\n"
        f"{lines}\n"
        "Call this with the groups the task needs before using them; their tools "
        "work from your next step. Don't load groups you don't need."
    )
    items = fn["parameters"]["properties"]["groups"]["items"]
    items["enum"] = [g.name for g in pending]
    out.append(clone)
    return out


def loaded_by(tool_calls: Iterable[dict[str, Any]], results: Iterable[dict[str, Any]]) -> set[str]:
    """Groups that successful load_tools calls among *tool_calls* loaded."""
    ids = {
        tc.get("id"): tc for tc in tool_calls
        if (tc.get("function") or {}).get("name") == LOAD_TOOLS
    }
    found: set[str] = set()
    for r in results:
        if r.get("role") != "tool" or r.get("tool_call_id") not in ids:
            continue
        try:
            body = json.loads(r.get("content") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(body, dict) and body.get("success"):
            found.update(str(g) for g in body.get("loaded") or [])
    return found


def loaded_in_history(messages: Sequence[dict[str, Any]], groups: Sequence[ToolGroup]) -> set[str]:
    """Groups a conversation already loaded: by load_tools, or by calling a group's tool."""
    calls: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "assistant":
            calls.extend(m.get("tool_calls") or [])
    found = loaded_by(calls, messages)
    for tc in calls:
        g = group_of((tc.get("function") or {}).get("name", ""), groups)
        if g is not None:
            found.add(g.name)
    return found
