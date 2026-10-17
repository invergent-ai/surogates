"""Score one stored rollout: the task's checks on its document, and what the trace says the agent did.

Reads only ``runs/<run_id>/<task>/r<k>/`` (events.jsonl, meta.json, documents/), so a new check or metric is applied
to old runs by ``obench check <run_id>``, never by re-running a session.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any

from obench import docs
from obench.checks import CheckResult, Ctx
from obench.client import Event
from obench.runner import doc_path, load_events
from obench.tasks import Task

FILE_TOOLS = {"write_file", "patch", "read_file", "terminal", "execute_code", "list_files", "search_files"}
_DOC_EXT = re.compile(r"\.(docx|xlsx|pptx|pdf)\b", re.IGNORECASE)
_ADMITS = re.compile(
    r"\b(couldn'?t|could not|can'?t|cannot|unable|not able|wasn'?t able|failed|didn'?t|did not|isn'?t supported|"
    r"not supported|doesn'?t support|no way to|not possible|unfortunately|however)\b",
    re.IGNORECASE,
)


def _parse(content: Any) -> Any:
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content
    return content


def _args(data: dict[str, Any]) -> dict[str, Any]:
    args = data.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return {"_raw": args}
    return args if isinstance(args, dict) else {}


def final_message(events: list[Event]) -> str:
    for ev in reversed(events):
        if ev.type == "llm.response":
            content = (ev.data.get("message") or {}).get("content") or ""
            if content.strip():
                return content
    return ""


def metrics(events: list[Event]) -> dict[str, Any]:
    tools: Counter[str] = Counter()
    refusals: list[str] = []
    office_errors: list[str] = []
    other_errors: list[str] = []
    reads_of_missing = 0
    office_busy = 0
    skill_views: list[str] = []
    file_tools_on_docs: list[str] = []
    calls: dict[str, tuple[str, dict]] = {}
    for ev in events:
        d = ev.data or {}
        if ev.type == "tool.call":
            name = d.get("name") or "?"
            args = _args(d)
            tools[name] += 1
            calls[d.get("tool_call_id") or str(ev.id)] = (name, args)
            if name == "skill_view":
                skill_views.append(f"{args.get('name')}{'/' + args['file_path'] if args.get('file_path') else ''}")
            if name in FILE_TOOLS and _DOC_EXT.search(json.dumps(args)):
                file_tools_on_docs.append(f"{name} {json.dumps(args)[:160]}")
        elif ev.type == "tool.result":
            name = d.get("name") or "?"
            content = _parse(d.get("content"))
            if isinstance(content, dict) and name == "office_read" and str(content.get("error", "")).startswith("No document at"):
                # The documented answer for a document not made yet, not a tool failure.
                reads_of_missing += 1
            elif isinstance(content, dict) and "as many documents open as it can hold" in str(content.get("error", "")):
                # runtime-office's page pool is full: a capacity condition of the deployment, not a model error.
                office_busy += 1
            elif isinstance(content, dict) and content.get("error"):
                text = f"{name}: {str(content['error'])[:300]}"
                (office_errors if name.startswith("office_") else other_errors).append(text)
            elif isinstance(content, dict) and name == "office_edit" and content.get("applied") is False:
                refused = content.get("refused") or {}
                refusals.append(f"op {refused.get('op')}: {str(refused.get('reason') or content)[:300]}")
            elif isinstance(content, str) and name.startswith("office_") and content.lower().startswith("error"):
                office_errors.append(f"{name}: {content[:300]}")
    served = Counter(
        str(ev.data.get("model")) for ev in events if ev.type == "llm.response" and ev.data.get("model")
    )
    return {
        "model_calls": sum(1 for ev in events if ev.type == "llm.request"),
        "served_models": dict(served),
        "tool_calls": dict(tools),
        "tool_calls_total": sum(tools.values()),
        "office_edit_calls": tools.get("office_edit", 0),
        "office_read_calls": tools.get("office_read", 0),
        "refusals": refusals,
        "office_errors": office_errors,
        "reads_of_missing_document": reads_of_missing,
        "office_busy": office_busy,
        "other_tool_errors": other_errors,
        "skill_views": skill_views,
        "read_office_skill": any(s.split("/")[0] == "office" for s in skill_views),
        "file_tools_on_documents": file_tools_on_docs,
        "file_or_terminal_calls": sum(n for name, n in tools.items() if name in FILE_TOOLS),
        "sandbox_unavailable": sum(1 for x in other_errors if "sandbox_unavailable" in x),
        "turns": sum(1 for ev in events if ev.type == "harness.wake"),
        "user_messages": sum(1 for ev in events if ev.type == "user.message"),
        "office_events": [f"{ev.type} {json.dumps(ev.data)[:160]}" for ev in events if ev.type.startswith("office.")],
        "failures": [f"{ev.type} {json.dumps(ev.data)[:300]}" for ev in events
                     if ev.type in ("session.fail", "session.failed", "llm.error", "harness.error")],
    }


def find_document(task: Task, meta: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The collected document the checks read, and a note when it is not at the asked-for path."""
    collected = [c for c in meta.get("collected") or [] if c.get("local")]
    for c in collected:
        if c["path"] == task.target:
            return c, ""
    for c in collected:
        if doc_path(c["path"]).lower() == task.target.lower():
            return c, f"saved as {c['path']}"
    same_kind = [c for c in collected if docs.kind_of(c["path"]) == task.kind and not c.get("input")]
    if task.mode == "create" and same_kind:
        return same_kind[0], f"saved as {same_kind[0]['path']}, not {task.target}"
    return None, f"no {task.target} in the workspace"


def original_model(task: Task, rollout_dir: str) -> Any:
    inp = task.original_input
    if inp is None:
        return None
    path = os.path.join(rollout_dir, "inputs", inp.name) if inp.derive else str(inp.source())
    return docs.read(path)


def claims(results: list[CheckResult], message: str, doc_found: bool, inputs: list[str] | None = None) -> dict[str, Any]:
    """Where the final message claims what the checks contradict.

    A failed check whose claim words the message uses is a contradicted claim, unless the message admits a problem
    (then the words are likely negated: "I could not write a fixed one"). Saying an uploaded input is not there is a
    contradicted claim too: the runner put it in the workspace before the message was sent.
    """
    low = message.lower()
    admits = bool(_ADMITS.search(message))
    contradicted = []
    if not admits:
        for r in results:
            if r.passed is False:
                said = [w for w in r.claim if re.search(rf"\b{re.escape(w.lower())}", low)]
                if said:
                    contradicted.append(f"says {said[0]!r}; {r.name}: {r.detail[:160]}")
    for name in inputs or []:
        base = re.escape(os.path.basename(name).lower())
        if re.search(rf"(\bno\b|not find|can'?t find|couldn'?t find|cannot find|not see|can'?t see)[^.\n]{{0,60}}{base}"
                     rf"|{base}[^.\n]{{0,40}}(not found|does ?n[o']t exist|is ?n[o']t (there|in)|is missing)", low):
            contradicted.append(f"says {os.path.basename(name)} is not there; it was uploaded before the message")
    if not doc_found and message and not admits:
        contradicted.insert(0, "reports the work done; no document was saved")
    failed = sum(1 for r in results if r.passed is False)
    return {
        "contradicted": contradicted,
        "admits_a_problem": admits,
        "claims_success_despite_failures": bool(failed and message and not admits),
    }


def evaluate(task: Task, rollout_dir: str) -> dict[str, Any]:
    with open(os.path.join(rollout_dir, "meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    events = load_events(rollout_dir)
    message = final_message(events)
    m = metrics(events)
    found, note = find_document(task, meta)
    results: list[CheckResult] = []
    doc = None
    if found is None:
        results.append(CheckResult("document saved", False, note))
    else:
        try:
            doc = docs.read(os.path.join(rollout_dir, found["local"]))
            results.append(CheckResult("document saved and opens", True, note or f"{found['size']} bytes"))
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult("document saved and opens", False, f"cannot open: {type(exc).__name__}: {exc}"))
    if task.mode == "edit" and found is not None and not found.get("changed", True):
        results.append(CheckResult("the document changed", False, "byte-identical to the upload: no edit was saved"))
    if doc is not None:
        ctx = Ctx(
            original=original_model(task, rollout_dir),
            final_message=message,
            inputs_changed=[c["path"] for c in meta.get("collected") or [] if c.get("input") and c.get("changed")],
        )
        results.extend(chk.run(doc, ctx) for chk in task.checks)
    else:
        results.extend(CheckResult(chk.name, False, "no document", chk.claim) for chk in task.checks)
    session = meta.get("session") or {}
    out = {
        "task_id": task.id,
        "index": meta.get("index"),
        "kind": task.kind,
        "mode": task.mode,
        "session_id": meta.get("session_id"),
        "channel": session.get("channel"),
        "agent_id": session.get("agent_id"),
        "terminal_status": meta.get("terminal_status"),
        "error": meta.get("error"),
        "wall_clock_s": meta.get("wall_clock_s"),
        "save": meta.get("save"),
        "checks": [r.__dict__ for r in results],
        "passed": sum(1 for r in results if r.passed is True),
        "failed": sum(1 for r in results if r.passed is False),
        "unknown": sum(1 for r in results if r.passed is None),
        "metrics": m,
        "final_message": message,
        "claims": claims(results, message, doc is not None, inputs=[i.name for i in task.inputs]),
    }
    with open(os.path.join(rollout_dir, "checks.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    return out


def trajectory(task: Task, rollout_dir: str, result: dict[str, Any]) -> str:
    """A readable trace next to the raw events: what a failing task did, without re-running it."""
    events = load_events(rollout_dir)
    m = result["metrics"]
    lines = [
        f"# {task.id} r{result.get('index')} ({task.kind}, {task.mode})",
        "",
        f"**Prompt:** {task.prompt}",
        "",
        f"**Session:** `{result.get('session_id')}` channel={result.get('channel')} agent={result.get('agent_id')} "
        f"status={result.get('terminal_status')} wall={result.get('wall_clock_s')}s",
        f"**Served model:** {m['served_models']}  **Model calls:** {m['model_calls']}  **Tools:** {m['tool_calls']}",
        f"**Save:** {result.get('save')}",
        f"**Checks:** {result['passed']} passed, {result['failed']} failed, {result['unknown']} unknown",
        "",
    ]
    for c in result["checks"]:
        mark = {True: "PASS", False: "FAIL", None: "????"}[c["passed"]]
        lines.append(f"- {mark} {c['name']}: {c['detail']}")
    if result.get("error"):
        lines += ["", f"**Error:** {result['error']}"]
    lines += ["", "## Trace", "", "```"]
    for ev in events:
        d = ev.data or {}
        if ev.type in ("llm.delta", "llm.request", "iteration.summary", "session.title_updated", "turn.summary"):
            continue
        if ev.type == "user.message":
            lines.append(f"[{ev.id}] USER: {str(d.get('content'))[:2000]}")
        elif ev.type == "llm.thinking":
            lines.append(f"[{ev.id}] thinking: {str(d.get('reasoning'))[:400]}")
        elif ev.type == "llm.response":
            content = (d.get("message") or {}).get("content")
            if content:
                lines.append(f"[{ev.id}] ASSISTANT ({d.get('model')}): {content[:2500]}")
        elif ev.type == "tool.call":
            lines.append(f"[{ev.id}] CALL {d.get('name')} {json.dumps(_args(d), ensure_ascii=False)[:2500]}")
        elif ev.type == "tool.result":
            lines.append(f"[{ev.id}] RESULT {d.get('name')} ({d.get('elapsed_ms')} ms) {str(d.get('content'))[:1500]}")
        else:
            lines.append(f"[{ev.id}] {ev.type} {json.dumps(d, ensure_ascii=False)[:400]}")
    lines.append("```")
    return "\n".join(lines) + "\n"
