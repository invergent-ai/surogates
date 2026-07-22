"""Operator-only tool exposing the ops Users-page reports to the agent.

Surogate-ops generates and automatically maintains two kinds of
end-user intelligence for every agent: a per-user activity report
(cached in ``users.memory_summary["reports"][<agent_id>]`` in THIS
database) and a cross-user overview report (cached on the ops-side
``agents.cohort_report`` column, read here via the read-only ops
engine).  This tool lets the agent's OPERATOR ask for them in chat —
"how's the cohort doing?", "status of Maria?" — without opening the
Studio page.

Privacy is the whole design: reports describe OTHER end-users, so the
tool is owner-scoped exactly like session_search's cross-principal
mode (see tools/owner_scope.py — ops-chat service-account principal +
server-stamped config, never conversation-inferred).  The harness
strips the tool's schema from non-operator sessions (loop.py's tool
filter) and the worker strips its prompt guidance, but the handler
re-checks with the full database-backed predicate: a tool that
trusted only schema visibility would be one prompt injection away
from leaking every user's report.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from surogates.db.ops_engine import get_ops_session_factory, init_ops_engine
from surogates.db.ops_models import OpsAgent
from surogates.tools.owner_scope import is_owner_scoped
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

_REFUSAL = (
    "user_reports is only available to the agent's operator in the "
    "Studio console; this session is not operator-scoped."
)

_PARAMS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["overview", "get", "list"],
            "description": (
                "'overview' returns the cross-user overview report; "
                "'get' returns one end-user's report (requires 'user'); "
                "'list' shows which end-users have a maintained report."
            ),
        },
        "user": {
            "type": "string",
            "description": (
                "For action='get': the end-user's display name, email "
                "or id. A unique partial display-name match is accepted."
            ),
        },
    },
    "required": ["action"],
}


def _ensure_ops_session_factory():
    """Ops session factory with the same lazy fallback as kb_tools."""
    factory = get_ops_session_factory()
    if factory is not None:
        return factory

    from surogates.config import Settings
    settings = Settings()
    if not settings.ops_db.url:
        return None

    return init_ops_engine(
        settings.ops_db.url,
        pool_size=settings.ops_db.pool_size,
        pool_overflow=settings.ops_db.pool_overflow,
    )


async def _fetch_org_users(session_store: Any, org_id: UUID) -> list[Any]:
    """All end-user rows of the org: (id, display_name, email,
    memory_summary).  Raw SQL via the store's session factory — the
    same access idiom session_search uses."""
    async with session_store._sf() as db:
        rows = await db.execute(
            sa.text(
                "SELECT id, display_name, email, memory_summary "
                "FROM users WHERE org_id = :org_id",
            ),
            {"org_id": str(org_id)},
        )
        return list(rows)


def _report_payload(memory_summary: Any, agent_id: str) -> dict | None:
    if not isinstance(memory_summary, dict):
        return None
    payload = (memory_summary.get("reports") or {}).get(agent_id)
    return payload if isinstance(payload, dict) else None


def _match_user(rows: list[Any], needle: str) -> Any | None:
    """Resolve by id, exact email, exact name, then unique partial name.

    Ambiguity returns None rather than guessing — the LLM gets the
    roster from action='list' to disambiguate.
    """
    lowered = needle.strip().lower()
    for row in rows:
        if str(row.id).lower() == lowered or row.email.lower() == lowered:
            return row
    exact = [r for r in rows if r.display_name.lower() == lowered]
    if len(exact) == 1:
        return exact[0]
    partial = [r for r in rows if lowered in r.display_name.lower()]
    return partial[0] if len(partial) == 1 else None


async def _fetch_overview(agent_id: str) -> dict | None:
    factory = _ensure_ops_session_factory()
    if factory is None:
        return None
    async with factory() as db:
        row = await db.execute(
            sa.select(OpsAgent.cohort_report).where(OpsAgent.id == agent_id),
        )
        payload = row.scalar_one_or_none()
    return payload if isinstance(payload, dict) else None


async def _user_reports_handler(arguments: dict, **kwargs: Any) -> str:
    session_store = kwargs.get("session_store")
    tenant = kwargs.get("tenant")
    agent_id = str(kwargs.get("agent_id") or "")
    session_config = kwargs.get("session_config")

    if session_store is None or tenant is None or not agent_id:
        return json.dumps({"error": "user_reports is unavailable here"})

    # Full owner-scope proof (service-account name lookup included) —
    # defense in depth on top of the worker's schema-level hiding.
    if not await is_owner_scoped(
        session_store,
        getattr(tenant, "service_account_id", None),
        session_config,
    ):
        return json.dumps({"error": _REFUSAL})

    org_id = getattr(tenant, "org_id", None)
    if org_id is None:
        return json.dumps({"error": "user_reports is unavailable here"})

    action = str(arguments.get("action") or "").strip()
    if action not in ("overview", "get", "list"):
        return json.dumps(
            {"error": "action must be one of: overview, get, list"},
        )
    if action == "overview":
        overview = await _fetch_overview(agent_id)
        if overview is None:
            return json.dumps(
                {
                    "overview": None,
                    "hint": (
                        "No overview report exists yet — generate it once "
                        "from the Users page in Studio; it then stays "
                        "fresh automatically."
                    ),
                },
            )
        return json.dumps(
            {
                "overview": {
                    "report_md": overview.get("report_md"),
                    "updated_at": overview.get("generated_at"),
                    "user_count": overview.get("user_count"),
                },
            },
        )

    rows = await _fetch_org_users(session_store, org_id)

    if action == "list":
        entries = []
        for row in rows:
            payload = _report_payload(row.memory_summary, agent_id)
            if payload is not None:
                entries.append(
                    {
                        "display_name": row.display_name,
                        "email": row.email,
                        "updated_at": payload.get("generated_at"),
                    },
                )
        return json.dumps(
            {
                "users_with_reports": entries,
                "hint": (
                    "Use action='get' with user=<name or email> for one "
                    "user's full report."
                ),
            },
        )

    needle = str(arguments.get("user") or "").strip()
    if not needle:
        return json.dumps(
            {"error": "action='get' requires the 'user' argument"},
        )
    matched = _match_user(rows, needle)
    if matched is None:
        return json.dumps(
            {
                "error": (
                    f"no end-user matching {needle!r} — use "
                    "action='list' to see who has a report"
                ),
            },
        )
    payload = _report_payload(matched.memory_summary, agent_id)
    if payload is None:
        return json.dumps(
            {
                "display_name": matched.display_name,
                "report": None,
                "hint": (
                    "No report exists for this user yet — generate it "
                    "once from the Users page in Studio; it then "
                    "stays fresh automatically."
                ),
            },
        )
    return json.dumps(
        {
            "display_name": matched.display_name,
            "email": matched.email,
            "report_md": payload.get("report_md"),
            "updated_at": payload.get("generated_at"),
        },
    )


def register(registry: ToolRegistry) -> None:
    """Register the user_reports tool.

    Always registered; the harness strips it from the LLM-visible
    schema for non-operator sessions, and the handler independently
    refuses without full owner scope.
    """
    registry.register(
        name="user_reports",
        schema=ToolSchema(
            name="user_reports",
            description=(
                "Operator-only: read the maintained end-user reports of "
                "this agent. action='overview' returns the cross-user "
                "overview report; action='list' shows which end-users "
                "have an individual report; action='get' with "
                "user=<name, email or id> returns that user's full "
                "activity report. Reports are markdown, generated from "
                "the Studio Users page and kept fresh automatically."
            ),
            parameters=_PARAMS,
        ),
        handler=_user_reports_handler,
        toolset="user_reports",
    )
