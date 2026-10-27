"""Keel's analysis routes: through ``keel-link``, Keel has the runtime's model group an agent's raised hands by
reason and draft a passage of policy from agreeing answers.

Both are stateless: no table, no session, no key.  They run as the agent
the call names, a stopped one too: analysing what an agent already did is not
the agent working.  The same link key and errors as the step routes
(:mod:`surogates.api.routes.keel`), whose helpers these routes share.

Mounted only on the keel-link app, never on the public API.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from surogates.api.routes.keel import (
    agent_id_of,
    read_body,
    refused,
    require_link,
    resolve_agent,
)
from surogates.keel.analysis import AnalysisError
from surogates.keel.hand_reasons import HandReasonsRequest, label_hands
from surogates.keel.policy_text import PolicyTextRequest, draft_policy_text

router = APIRouter()


@router.post("/keel/hand-reasons")
async def hand_reasons(request: Request) -> dict[str, Any]:
    """Group up to 25 raised hands of the agent by reason: a label for each hand the model labelled usably, and
    what the call used.  A hand with no entry was not labelled this time."""
    require_link(request)
    body = await read_body(request, HandReasonsRequest)
    agent = await resolve_agent(request, agent_id_of(request), body.org_id, running=False)
    try:
        return await label_hands(body, agent, vault=getattr(request.app.state, "credential_vault", None))
    except AnalysisError as exc:
        raise refused(exc.status, exc.code, exc.message) from None


@router.post("/keel/policy-text")
async def policy_text(request: Request) -> dict[str, Any]:
    """Draft one passage of policy, in Markdown, from the answers people gave a reason's raised hands: the
    passage and what the call used.  Every identifier of a hand in it is replaced."""
    require_link(request)
    body = await read_body(request, PolicyTextRequest)
    agent = await resolve_agent(request, agent_id_of(request), body.org_id, running=False)
    try:
        return await draft_policy_text(body, agent, vault=getattr(request.app.state, "credential_vault", None))
    except AnalysisError as exc:
        raise refused(exc.status, exc.code, exc.message) from None
