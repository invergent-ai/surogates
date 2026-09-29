"""Executable model of the device-operation protocol (design Section 2).

Checks the state machine, not the transport: an effect happens at most once,
a crash before the effect lets recovery perform it exactly once, a crash
between *started* and *result* reports ``interrupted`` and never reruns, a
cancelled operation never runs, and a worker with a stale lease can neither
dispatch nor commit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


class Crash(Exception):
    """The laptop process died at a named point."""


def digest(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()


@dataclass
class Journal:
    """Stand-in for the Postgres ``device_operations`` table."""

    rows: dict = field(default_factory=dict)

    def insert(self, op_id: str, request: dict, lease_token: str) -> dict:
        d = digest(request)
        row = self.rows.get(op_id)
        if row is not None:
            if row["digest"] != d:
                raise ValueError("digest mismatch for an existing operation id")
            return row
        row = {"state": "pending", "digest": d, "request": request, "lease_token": lease_token, "result": None}
        self.rows[op_id] = row
        return row

    def cancel(self, op_id: str) -> None:
        row = self.rows[op_id]
        if row["state"] == "pending":
            row["state"] = "cancelled"

    def commit_result(self, op_id: str, result: dict) -> None:
        row = self.rows[op_id]
        if row["state"] == "pending":
            row["state"], row["result"] = "completed", result

    def pending(self) -> list[str]:
        return [k for k, r in self.rows.items() if r["state"] == "pending"]

    def cancelled(self) -> list[str]:
        return [k for k, r in self.rows.items() if r["state"] == "cancelled"]


@dataclass
class Laptop:
    """The tool host's durable local operation record plus a real-effect counter."""

    records: dict = field(default_factory=dict)
    effects: dict = field(default_factory=dict)
    crash_at: str | None = None

    def _maybe_crash(self, point: str) -> None:
        if self.crash_at == point:
            self.crash_at = None
            raise Crash(point)

    def cancel(self, op_id: str, d: str) -> None:
        rec = self.records.get(op_id)
        if rec is None or rec["state"] == "received":
            self.records[op_id] = {"digest": d, "state": "cancelled", "result": {"cancelled": True}}

    def compact(self, op_id: str) -> None:
        rec = self.records[op_id]
        if rec["state"] == "result":
            rec["result"] = {"completed": True, "payload": "reclaimed"}

    def handle(self, op_id: str, request: dict) -> dict:
        d = digest(request)
        rec = self.records.get(op_id)
        if rec is not None:
            if rec["digest"] != d:
                return {"error": "protocol: digest changed"}
            if rec["state"] in ("result", "cancelled"):
                return rec["result"]
            if rec["state"] == "started":
                rec.update(state="result", result={"interrupted": True, "outcome": "unknown"})
                return rec["result"]
        self.records[op_id] = {"digest": d, "state": "received", "result": None}
        self._maybe_crash("after_received")
        self.records[op_id]["state"] = "started"
        self._maybe_crash("after_started")
        self.effects[op_id] = self.effects.get(op_id, 0) + 1
        self._maybe_crash("after_effect")
        result = {"ok": True, "op": op_id}
        self.records[op_id].update(state="result", result=result)
        self._maybe_crash("after_result")
        return result


@dataclass
class Api:
    """Reconciles the journal with the laptop on connect and at each heartbeat."""

    journal: Journal
    laptop: Laptop
    online: bool = True

    def reconcile(self) -> None:
        if not self.online:
            return
        for op_id in self.journal.cancelled():
            self.laptop.cancel(op_id, self.journal.rows[op_id]["digest"])
        for op_id in self.journal.pending():
            row = self.journal.rows[op_id]
            try:
                result = self.laptop.handle(op_id, row["request"])
            except Crash:
                self.online = False
                return
            self.journal.commit_result(op_id, result)


@dataclass
class Lease:
    token: str = "t1"


@dataclass
class Worker:
    journal: Journal
    api: Api
    lease: Lease
    my_token: str
    committed: dict = field(default_factory=dict)

    def _check_lease(self) -> None:
        if self.my_token != self.lease.token:
            raise PermissionError("stale lease")

    def dispatch(self, op_id: str, request: dict) -> None:
        self._check_lease()
        self.journal.insert(op_id, request, self.my_token)
        self.api.reconcile()  # stands in for the Redis nudge, which may be lost

    def commit(self, op_id: str, tool_call_id: str) -> dict | None:
        row = self.journal.rows[op_id]
        if row["state"] != "completed":
            return None
        self._check_lease()
        self.committed[tool_call_id] = row["result"]
        row["state"] = "consumed"
        return row["result"]
