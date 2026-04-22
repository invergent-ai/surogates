"""Fire-and-forget prompt submission for service-account clients.

Synthetic data pipelines and similar non-interactive workloads submit
prompts through these endpoints instead of the interactive
``/v1/sessions/{id}/messages`` flow.  Each accepted prompt creates a
new session (``channel="api"``), emits the ``user.message`` event, and
enqueues the session for the worker — no streaming, no response body
beyond the created identifiers.  The pipeline reads results straight
from the ``events`` table using the returned ``session_id``.

Authentication is by service-account token (``surg_sk_…``) only; the
middleware rejects interactive JWTs on this path to keep the
programmatic channel cleanly separated from the web/Slack channels and
out of training-data exports.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from surogates.config import enqueue_session
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.storage.tenant import session_bucket
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()


API_CHANNEL = "api"
# Upper bound on a single batch request.  Larger batches should page.
MAX_BATCH_SIZE = 100
# Upper bound on a single prompt body.  ~200k characters is ~50k tokens —
# beyond what any current model accepts in a single message — and keeps
# ``events.data`` JSONB rows from ballooning under malicious input.
MAX_PROMPT_LENGTH = 200_000


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class PromptRequest(BaseModel):
    """A single prompt submission.

    *idempotency_key* is scoped per org: two requests carrying the
    same key for the same org resolve to the same session.  *metadata*
    is free-form passthrough stored on
    ``sessions.config['pipeline_metadata']`` so the caller can join
    results back to its source dataset without a side-table.
    """

    prompt: str = Field(..., min_length=1, max_length=MAX_PROMPT_LENGTH)
    idempotency_key: str | None = Field(default=None, max_length=200)
    metadata: dict | None = None


class PromptAccepted(BaseModel):
    """Result of a single prompt submission.

    ``session_id`` and ``event_id`` are populated when the prompt was
    accepted; they are ``None`` when this slot represents a failed
    batch entry (with ``error`` set instead).
    """

    session_id: UUID | None = None
    event_id: int | None = None
    deduplicated: bool = False
    error: str | None = None


class BatchPromptRequest(BaseModel):
    prompts: list[PromptRequest] = Field(..., min_length=1, max_length=MAX_BATCH_SIZE)


class BatchPromptResponse(BaseModel):
    results: list[PromptAccepted]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_session_store(request: Request) -> SessionStore:
    store: SessionStore | None = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store not available.",
        )
    return store


def _require_service_account(tenant: TenantContext) -> UUID:
    """Return the service-account id, or raise 403 for JWT callers.

    A bare ``surg_sk_`` token produces a TenantContext with
    ``service_account_id`` set and ``session_scope_id`` unset.  A
    worker-minted ``service_account_session`` JWT also sets
    ``service_account_id`` but additionally carries a
    ``session_scope_id`` — refused here so a leaked session JWT cannot
    be reused to open new sessions.
    """
    if tenant.service_account_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This endpoint requires a service-account token "
                "(prefix 'surg_sk_')."
            ),
        )
    if tenant.session_scope_id is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Session-scoped service-account tokens cannot submit "
                "new prompts."
            ),
        )
    return tenant.service_account_id


async def _submit_one(
    body: PromptRequest,
    *,
    request: Request,
    tenant: TenantContext,
    service_account_id: UUID,
    store: SessionStore,
) -> PromptAccepted:
    """Create a session + user.message event for one prompt.

    Handles idempotent retries at two levels: a pre-insert lookup
    covers the common case, and an ``IntegrityError`` catch covers the
    race where two concurrent requests carry the same key.
    """
    settings = request.app.state.settings

    if body.idempotency_key is not None:
        existing = await store.get_session_by_idempotency_key(
            tenant.org_id, body.idempotency_key
        )
        if existing is not None:
            return PromptAccepted(
                session_id=existing.id,
                event_id=None,
                deduplicated=True,
            )

    session_id = uuid4()
    bucket = session_bucket(session_id)

    storage = request.app.state.storage
    await storage.create_bucket(bucket)

    config: dict = {
        "workspace_bucket": bucket,
        "workspace_path": storage.resolve_bucket_path(bucket),
        "service_account_id": str(service_account_id),
    }
    if body.metadata:
        config["pipeline_metadata"] = body.metadata

    model = settings.llm.model or "gpt-5.4"

    try:
        session = await store.create_session(
            session_id=session_id,
            user_id=None,
            org_id=tenant.org_id,
            agent_id=settings.agent_id,
            channel=API_CHANNEL,
            model=model,
            config=config,
            service_account_id=service_account_id,
            idempotency_key=body.idempotency_key,
        )
    except IntegrityError:
        # Concurrent insert with the same idempotency key.  The other
        # request won — return its session.
        if body.idempotency_key is None:
            raise
        existing = await store.get_session_by_idempotency_key(
            tenant.org_id, body.idempotency_key
        )
        if existing is None:
            # Truly exceptional: constraint fired but row not visible.
            raise
        try:
            await storage.delete_bucket(bucket)
        except Exception:
            logger.warning(
                "Failed to clean up bucket %s after idempotency race",
                bucket,
                exc_info=True,
            )
        return PromptAccepted(
            session_id=existing.id,
            event_id=None,
            deduplicated=True,
        )

    event_id = await store.emit_event(
        session.id,
        EventType.USER_MESSAGE,
        {"content": body.prompt},
    )

    await enqueue_session(request.app.state.redis, session.agent_id, session.id)

    return PromptAccepted(session_id=session.id, event_id=event_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/api/prompts",
    response_model=PromptAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_prompt(
    body: PromptRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> PromptAccepted:
    """Submit a single prompt for asynchronous processing.

    Returns ``202 Accepted`` with the ``session_id`` the pipeline can
    use to read back events once the worker has processed the prompt.
    When *idempotency_key* matches a prior submission, the original
    session is returned with ``deduplicated=true`` and no new work is
    enqueued.
    """
    service_account_id = _require_service_account(tenant)
    store = _get_session_store(request)
    return await _submit_one(
        body,
        request=request,
        tenant=tenant,
        service_account_id=service_account_id,
        store=store,
    )


@router.post(
    "/api/prompts:batch",
    response_model=BatchPromptResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_prompts_batch(
    body: BatchPromptRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> BatchPromptResponse:
    """Submit up to :data:`MAX_BATCH_SIZE` prompts in one round-trip.

    Each prompt is processed independently — one bad entry does not
    fail the rest.  The response preserves input order so callers can
    zip results back to their input rows.  Partial failures surface as
    a 500 only when every item fails; otherwise callers inspect the
    response to see which submissions were accepted vs deduplicated.
    """
    service_account_id = _require_service_account(tenant)
    store = _get_session_store(request)

    results: list[PromptAccepted] = []
    errors = 0
    for prompt in body.prompts:
        try:
            accepted = await _submit_one(
                prompt,
                request=request,
                tenant=tenant,
                service_account_id=service_account_id,
                store=store,
            )
            results.append(accepted)
        except HTTPException:
            raise
        except Exception:
            # Log the full traceback server-side, but surface only a
            # stable code to the caller — raw exception strings can leak
            # DB constraint names, storage paths, and other internals.
            logger.exception("Failed to accept prompt in batch")
            errors += 1
            results.append(PromptAccepted(error="internal_error"))

    if errors == len(body.prompts):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="All prompts in the batch failed to enqueue.",
        )

    return BatchPromptResponse(results=results)
