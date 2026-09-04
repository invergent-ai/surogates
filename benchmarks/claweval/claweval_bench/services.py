"""Mock-service lifecycle and audit collection, reusing upstream code.

The vendored claw-eval ships a ``ServiceManager`` that spawns each task's
mock services, health-checks them, and resets them between trials. Fidelity
matters more than independence here, so this module wraps it rather than
reimplementing it: the services the agent hits are byte-for-byte the ones
the upstream harness runs, started from the vendored checkout so relative
fixture paths in ``task.yaml`` resolve.

Audit collection mirrors ``claw_eval.runner.loop``: for every service with
a reset endpoint, GET ``<reset minus /reset>/audit`` best-effort. Graders
receive ``{service_name: audit_json}``.
"""
from __future__ import annotations

import pathlib
from typing import Any

import httpx


def start_services(task: Any, vendor_root: pathlib.Path):
    """Context manager: the task's mock services, running from vendor root."""
    from claw_eval.runner.services import ServiceManager

    mock_today = getattr(task.environment, "mock_today", None)
    return ServiceManager(task.services, cwd=vendor_root, mock_today=mock_today)


def reset_services(manager: Any) -> None:
    manager.reset_all()


def collect_audits(task: Any) -> dict[str, dict]:
    audits: dict[str, dict] = {}
    for svc in task.services:
        if not svc.reset_endpoint:
            continue
        audit_url = svc.reset_endpoint.rsplit("/reset", 1)[0] + "/audit"
        try:
            resp = httpx.get(audit_url, timeout=5.0)
            audits[svc.name] = resp.json()
        except Exception:  # noqa: BLE001 - best-effort, mirrors upstream
            pass
    return audits
