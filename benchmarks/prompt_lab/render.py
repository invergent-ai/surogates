"""Render the system prompt + tool schemas a Surogates session would send.

Run with the lab venv and PYTHONPATH pointing at the checkout to render, e.g.
  PYTHONPATH=../surogates-base ../venv/bin/python render.py studio > out.json
Uses the checkout's own PromptBuilder, ToolRegistry and platform skills.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

logging.disable(logging.CRITICAL)

import surogates  # noqa: E402
from surogates.harness.prompt import PromptBuilder  # noqa: E402
from surogates.session.models import Session  # noqa: E402
from surogates.tenant.context import TenantContext  # noqa: E402
from surogates.tools.loader import ResourceLoader  # noqa: E402
from surogates.tools.registry import ToolRegistry  # noqa: E402
from surogates.tools.runtime import ToolRuntime  # noqa: E402

REPO = Path(surogates.__file__).resolve().parent.parent

BROWSER = [
    "browser_navigate", "browser_get_state", "browser_evaluate", "browser_close",
    "browser_click", "browser_type", "browser_press_key", "browser_scroll",
    "browser_drag", "browser_wait", "browser_screenshot", "browser_upload_file",
]
# A plain Studio (web) agent with the platform defaults: what a customer's
# general-purpose agent is offered when no KB, channel, case or team applies.
STUDIO = [
    "memory", "skills_list", "skill_view", "skill_manage", "vision_analyze",
    "generate_image", "generate_video", "web_search", "web_extract", *BROWSER,
    "read_file", "write_file", "patch", "search_files", "list_files",
    "delegate_task", "consult_expert", "terminal", "process", "session_search",
    "todo", "ask_user_question", "create_artifact", "github",
    "research_memory", "research_outline",
    "cron_create", "cron_list", "cron_get", "cron_update", "cron_pause",
    "cron_resume", "cron_delete", "cron_run_now",
]
KB = ["kb_search_pages", "kb_list_pages", "kb_read_page"]

PROFILES = {
    "studio": {"tools": STUDIO, "kbs": []},
    "studio_kb": {
        "tools": STUDIO + KB,
        "kbs": [{
            "id": "kb-handbook", "name": "handbook", "display_name": "Northwind Employee Handbook",
            "description": "HR policies, leave, expenses, travel and IT rules of Northwind Logistics.",
            "mode": "grounding",
            "pages_tree": "- policies/leave.md\n- policies/expenses.md\n- policies/travel.md\n- it/security.md\n- it/devices.md",
        }],
    },
    # A customer-support chat: the KB is the support handbook, the person on
    # the other side is a customer, not a signed-in employee.
    "support_kb": {
        "tools": STUDIO + KB,
        "speaker": None,
        "kbs": [{
            "id": "kb-support", "name": "support", "display_name": "Shopdesk Support Handbook",
            "description": "How support agents handle customers: identity checks, refunds, damaged items, address changes, shipping.",
            "mode": "grounding",
            "pages_tree": "- support/refunds.md\n- support/address-changes.md\n- support/shipping.md",
        }],
    },
    "minimal": {"tools": ["read_file", "write_file", "terminal", "web_search", "web_extract"], "kbs": []},
}


def load_skills():
    loader = ResourceLoader()
    skills = loader._load_skills_from_dir(str(REPO / "skills"), "platform")
    for s in skills:
        s.builtin = True
    return skills


def render(profile: str, model: str = "surogate") -> dict:
    prof = PROFILES[profile]
    reg = ToolRegistry()
    ToolRuntime(reg).register_builtins()
    names = [n for n in prof["tools"] if reg.has(n)]
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    session = Session(
        id=UUID(int=1), user_id=UUID(int=2), org_id=UUID(int=3), agent_id="agent-lab",
        channel="web", status="active", model=model,
        config={"workspace_path": "/workspace"}, created_at=now, updated_at=now,
    )
    tenant = TenantContext(
        org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/nonexistent-lab",
    )
    builder = PromptBuilder(
        tenant, skills=load_skills(), session=session, available_tools=set(names),
        available_kbs=prof["kbs"], speaker=prof.get("speaker", ("Dana Pop", "dana.pop@northwind.example")),
    )
    system = builder.build()
    tools = reg.get_schemas(set(names))
    try:
        from surogates.harness.prompt_profile import apply_tool_profile
        tools = apply_tool_profile(tools, getattr(builder, "prompt_profile", None))
    except ImportError:
        pass
    order = {n: i for i, n in enumerate(names)}
    tools.sort(key=lambda t: order[t["function"]["name"]])
    return {"profile": profile, "repo": str(REPO), "model": model, "prompt_profile": getattr(builder, "prompt_profile", None),
            "system": system, "tools": tools}


if __name__ == "__main__":
    out = render(sys.argv[1] if len(sys.argv) > 1 else "studio",
                 sys.argv[2] if len(sys.argv) > 2 else "surogate")
    json.dump(out, sys.stdout)
