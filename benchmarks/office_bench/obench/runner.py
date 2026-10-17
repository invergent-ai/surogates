"""Drive one task through one agent session, and keep everything it left behind.

A fresh session per rollout, always: its workspace is the task's filesystem. Flow: create the session as a Work chat
(channel studio, the operator stamp ops adds) -> upload the inputs at the workspace root, as the file pane's upload
does -> send the message (its HTTP status checked) -> stream events to a terminal state (the server closes every
stream after 300 s and a failed session never closes its own, so poll status and reconnect from the cursor) -> wait
for the turn's saves to land -> download every document in the workspace.

The documents are written by the editor's save at the turn's end, which lands in the workspace a few seconds after
the session completes and announces nothing when it works. So the runner downloads each document the agent edited
until two downloads agree (and, for an uploaded one, differ from the upload), up to a cap. The workspace tree's
sizes are not used to decide anything: a listing can report 0 bytes for a document that has been saved.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from obench.client import Event, HarnessError
from obench.docs import kind_of
from obench.tasks import Task

TERMINAL_STATUSES = {"completed", "archived", "failed"}
STUDIO_CHANNEL = "studio"
SAVE_SETTLE_S = 8.0  # the earliest a document is taken as saved, after the session completes
SAVE_POLL_S = 3.0
_MAX_COLLECT_BYTES = 50_000_000


class FatalRunError(Exception):
    """No task can run: the credential or the agent is wrong. Stops the whole run."""


@dataclass
class Rollout:
    task_id: str
    index: int
    session_id: str = ""
    session: dict[str, Any] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    wall_clock_s: float = 0.0
    terminal_status: str = ""
    error: str | None = None
    uploaded: dict[str, str] = field(default_factory=dict)  # workspace path -> sha256
    collected: list[dict[str, Any]] = field(default_factory=list)
    other_files: list[str] = field(default_factory=list)
    save: dict[str, Any] = field(default_factory=dict)
    released: str | None = None  # "yes", or why the session was kept


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def doc_path(raw: str) -> str:
    """A path as office_edit was given it, as the workspace names it."""
    p = (raw or "").strip().replace("\\", "/")
    for prefix in ("/workspace/", "workspace/", "__WORKSPACE__/"):
        if p.startswith(prefix):
            p = p[len(prefix):]
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def edited_paths(events: list[Event]) -> list[str]:
    out: list[str] = []
    for ev in events:
        if ev.type == "tool.call" and ev.data.get("name") == "office_edit":
            args = ev.data.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            p = doc_path(str((args or {}).get("path") or ""))
            if p and p not in out:
                out.append(p)
    return out


def session_config() -> dict[str, Any]:
    """What surogate-ops stamps on a Work chat it creates (create_live_session): the operator's ops identity."""
    user_id = os.environ.get("OFFICEBENCH_OPS_USER_ID")
    username = os.environ.get("OFFICEBENCH_OPS_USERNAME")
    ops = {k: v for k, v in (("user_id", user_id), ("username", username)) if v}
    return {"ops": ops} if ops else {}


def stage_inputs(task: Task, out_dir: str) -> list[tuple[str, str]]:
    """(local path, workspace name) for each input; derived inputs are written under out_dir/inputs/."""
    staged = []
    for inp in task.inputs:
        src = str(inp.source())
        if inp.derive is not None:
            local = os.path.join(out_dir, "inputs", inp.name)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            inp.derive(src, local)
            src = local
        staged.append((src, inp.name))
    return staged


async def _stream_until_terminal(client: Any, session_id: str, events: list[Event], started: float,
                                 cap_s: float) -> str:
    cursor = events[-1].id if events else 0
    while True:
        try:
            async for ev in client.stream_events(session_id, after=cursor):
                if ev.id > cursor:
                    events.append(ev)
                    cursor = ev.id
        except httpx.TransportError:
            pass  # a slow turn outlasted the read timeout: reconnect from the cursor
        status = await client.get_session_status(session_id)
        if status in TERMINAL_STATUSES:
            return status
        if time.monotonic() - started > cap_s:
            return "timeout"
        await asyncio.sleep(1.0)


async def _await_saves(client: Any, session_id: str, paths: list[str], uploaded: dict[str, str],
                       cap_s: float) -> dict[str, Any]:
    """Download each edited document until two downloads agree (and an uploaded one differs from its upload)."""
    if not paths:
        return {"edited": [], "landed": {}, "waited_s": 0.0}
    t0 = time.monotonic()
    last: dict[str, str | None] = {}
    landed: dict[str, bool] = {}
    while True:
        elapsed = time.monotonic() - t0
        for p in paths:
            if landed.get(p):
                continue
            try:
                digest = sha(await client.download_file(session_id, p))
            except HarnessError:
                digest = None
            if (digest is not None and digest == last.get(p) and elapsed >= SAVE_SETTLE_S
                    and digest != uploaded.get(p)):
                landed[p] = True
            last[p] = digest
        if all(landed.get(p) for p in paths) or elapsed > cap_s:
            break
        await asyncio.sleep(SAVE_POLL_S)
    return {
        "edited": paths,
        "landed": {p: bool(landed.get(p)) for p in paths},
        "waited_s": round(time.monotonic() - t0, 1),
    }


async def _collect(client: Any, session_id: str, rollout: Rollout, rollout_dir: str) -> None:
    tree = await client.get_workspace_tree(session_id)
    for entry in tree:
        path = entry["path"]
        if kind_of(path) is None:
            if path not in rollout.uploaded:
                rollout.other_files.append(path)
            continue
        try:
            blob = await client.download_file(session_id, path)
        except Exception as exc:  # noqa: BLE001 - one bad file, not the rollout
            rollout.collected.append({"path": path, "error": str(exc)[:300]})
            continue
        if len(blob) > _MAX_COLLECT_BYTES:
            rollout.collected.append({"path": path, "error": f"{len(blob)} bytes, over the cap"})
            continue
        local = os.path.join(rollout_dir, "documents", *path.split("/"))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(blob)
        digest = sha(blob)
        rollout.collected.append({
            "path": path,
            "local": posixpath.join("documents", path),
            "size": len(blob),
            "sha256": digest,
            "input": path in rollout.uploaded,
            "changed": path not in rollout.uploaded or rollout.uploaded[path] != digest,
        })


async def run_task(client: Any, task: Task, rollout_dir: str, index: int = 1, wall_clock_cap_s: float = 1800.0,
                   save_cap_s: float = 60.0, release: bool = True) -> Rollout:
    started = time.monotonic()
    r = Rollout(task_id=task.id, index=index)
    try:
        staged = stage_inputs(task, rollout_dir)
        try:
            r.session_id = await client.create_session(STUDIO_CHANNEL, session_config())
        except HarnessError as exc:
            if exc.status in (401, 403, 404):
                raise FatalRunError(str(exc)) from exc
            raise
        for local, name in staged:
            key = await client.upload_file(r.session_id, local, name)
            with open(local, "rb") as fh:
                r.uploaded[key] = sha(fh.read())
        await client.send_message(r.session_id, task.prompt)
        r.terminal_status = await _stream_until_terminal(client, r.session_id, r.events, started, wall_clock_cap_s)
        r.save = await _await_saves(client, r.session_id, edited_paths(r.events), r.uploaded, save_cap_s)
        # Events that came after the turn (a save that failed or had to wait announces itself).
        late_from = len(r.events)
        try:
            await asyncio.wait_for(_drain(client, r.session_id, r.events), timeout=20)
        except (asyncio.TimeoutError, httpx.TransportError):
            pass
        r.save["late_events"] = [e.type for e in r.events[late_from:]]
        r.session = await client.get_session(r.session_id)
        await _collect(client, r.session_id, r, rollout_dir)
        r.released = await _release(client, r, release)
    except FatalRunError:
        raise
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        r.error = f"{type(exc).__name__}: {exc}"
        r.terminal_status = r.terminal_status or "error"
    r.wall_clock_s = round(time.monotonic() - started, 1)
    return r


async def _release(client: Any, r: Rollout, release: bool) -> str:
    """Delete the finished session once everything is downloaded, as a user deleting the chat would.

    runtime-office keeps a document's page open until it is idle, and holds a limited number of pages; sessions left
    open back to back fill them, and later sessions then get "The office service has as many documents open as it can
    hold". Deleting each session after collecting it frees its pages: the office reaper closes an archived chat's
    documents on its next pass.
    """
    if not release:
        return "kept (--keep-sessions)"
    if r.terminal_status not in TERMINAL_STATUSES or r.terminal_status == "archived":
        return f"kept ({r.terminal_status})"
    try:
        await client.delete_session(r.session_id)
    except Exception as exc:  # noqa: BLE001 - a session left behind is not a failed rollout
        return f"kept: {str(exc)[:200]}"
    return "yes"


async def _drain(client: Any, session_id: str, events: list[Event]) -> None:
    cursor = events[-1].id if events else 0
    async for ev in client.stream_events(session_id, after=cursor):
        if ev.id > cursor:
            events.append(ev)
            cursor = ev.id


def write_rollout(rollout_dir: str, task: Task, r: Rollout) -> None:
    os.makedirs(rollout_dir, exist_ok=True)
    with open(os.path.join(rollout_dir, "events.jsonl"), "w", encoding="utf-8") as fh:
        for ev in r.events:
            fh.write(json.dumps({"id": ev.id, "type": ev.type, "data": ev.data}, default=str) + "\n")
    session = {k: r.session.get(k) for k in ("status", "channel", "agent_id", "model", "message_count",
                                              "tool_call_count", "input_tokens", "output_tokens")}
    with open(os.path.join(rollout_dir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "task_id": task.id,
            "index": r.index,
            "kind": task.kind,
            "mode": task.mode,
            "target": task.target,
            "prompt": task.prompt,
            "session_id": r.session_id,
            "session": session,
            "wall_clock_s": r.wall_clock_s,
            "terminal_status": r.terminal_status,
            "error": r.error,
            "uploaded": r.uploaded,
            "collected": r.collected,
            "other_files": r.other_files,
            "save": r.save,
            "released": r.released,
        }, fh, indent=2)


def load_events(rollout_dir: str) -> list[Event]:
    path = os.path.join(rollout_dir, "events.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [Event(id=row["id"], type=row["type"], data=row["data"]) for row in map(json.loads, fh)]
