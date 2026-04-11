"""Memory REST API -- read and mutate persistent memory entries.

Provides endpoints for reading and modifying the agent's durable
memory (``MEMORY.md`` and ``USER.md``).  All endpoints are tenant-scoped
via ``TenantContext``.

Memory is stored in the tenant's S3 bucket (or local filesystem in dev)
via ``StorageBackend``.  The entry format (``§``-delimited) matches the
Hermes ``MemoryStore`` convention.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from surogates.memory.store import ENTRY_DELIMITER, scan_memory_content
from surogates.storage.tenant import TenantStorage
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter()

# Character limits matching MemoryStore.
_MEMORY_CHAR_LIMIT = 2200
_USER_CHAR_LIMIT = 1375


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class MemoryEntries(BaseModel):
    """Current state of both memory targets."""

    memory: list[str]
    user: list[str]
    memory_usage: str
    user_usage: str


class MemoryMutateRequest(BaseModel):
    action: Literal["add", "replace", "remove"]
    target: Literal["memory", "user"]
    content: str | None = None
    old_text: str | None = None


class MemoryMutateResponse(BaseModel):
    success: bool
    message: str | None = None
    error: str | None = None
    entries: list[str] | None = None
    usage: str | None = None
    entry_count: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_tenant_storage(request: Request, tenant: TenantContext) -> TenantStorage:
    """Create a TenantStorage for the current request."""
    return TenantStorage(
        backend=request.app.state.storage,
        org_id=tenant.org_id,
        user_id=tenant.user_id,
    )


def _parse_entries(raw: str | None) -> list[str]:
    """Split raw file content into entries."""
    if not raw or not raw.strip():
        return []
    return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]


def _serialize_entries(entries: list[str]) -> str:
    """Join entries back into file content."""
    return ENTRY_DELIMITER.join(entries) if entries else ""


def _format_usage(entries: list[str], limit: int) -> str:
    """Format a usage string like '45% — 990/2200 chars'."""
    used = sum(len(e) for e in entries)
    pct = int(used / limit * 100) if limit > 0 else 0
    return f"{pct}% — {used}/{limit} chars"


def _char_limit(target: str) -> int:
    return _MEMORY_CHAR_LIMIT if target == "memory" else _USER_CHAR_LIMIT


def _filename(target: str) -> str:
    return "MEMORY.md" if target == "memory" else "USER.md"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/memory", response_model=MemoryEntries)
async def get_memory(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> MemoryEntries:
    """Load current memory entries for both targets."""
    ts = _get_tenant_storage(request, tenant)
    await ts.ensure_bucket()

    memory_raw = await ts.read_memory_file("MEMORY.md")
    user_raw = await ts.read_memory_file("USER.md")

    memory_entries = _parse_entries(memory_raw)
    user_entries = _parse_entries(user_raw)

    return MemoryEntries(
        memory=memory_entries,
        user=user_entries,
        memory_usage=_format_usage(memory_entries, _MEMORY_CHAR_LIMIT),
        user_usage=_format_usage(user_entries, _USER_CHAR_LIMIT),
    )


@router.post("/memory", response_model=MemoryMutateResponse)
async def mutate_memory(
    body: MemoryMutateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> MemoryMutateResponse:
    """Add, replace, or remove a memory entry."""
    ts = _get_tenant_storage(request, tenant)
    await ts.ensure_bucket()

    filename = _filename(body.target)
    limit = _char_limit(body.target)

    # Load current entries.
    raw = await ts.read_memory_file(filename)
    entries = _parse_entries(raw)

    if body.action == "add":
        if not body.content:
            raise HTTPException(status_code=422, detail="content is required for 'add' action.")

        # Security scan.
        scan_err = scan_memory_content(body.content)
        if scan_err:
            return MemoryMutateResponse(success=False, error=scan_err)

        # Check char limit.
        used = sum(len(e) for e in entries) + len(body.content)
        if used > limit:
            return MemoryMutateResponse(
                success=False,
                error=f"Would exceed {body.target} char limit ({used}/{limit}). Remove or replace an entry first.",
            )

        entries.append(body.content)

    elif body.action == "replace":
        if not body.old_text:
            raise HTTPException(status_code=422, detail="old_text is required for 'replace' action.")
        if not body.content:
            raise HTTPException(status_code=422, detail="content is required for 'replace' action.")

        scan_err = scan_memory_content(body.content)
        if scan_err:
            return MemoryMutateResponse(success=False, error=scan_err)

        idx = next((i for i, e in enumerate(entries) if body.old_text in e), None)
        if idx is None:
            return MemoryMutateResponse(
                success=False,
                error=f"No entry containing '{body.old_text[:50]}' found in {body.target}.",
            )

        # Check char limit with replacement.
        used = sum(len(e) for e in entries) - len(entries[idx]) + len(body.content)
        if used > limit:
            return MemoryMutateResponse(
                success=False,
                error=f"Would exceed {body.target} char limit ({used}/{limit}).",
            )

        entries[idx] = body.content

    elif body.action == "remove":
        if not body.old_text:
            raise HTTPException(status_code=422, detail="old_text is required for 'remove' action.")

        idx = next((i for i, e in enumerate(entries) if body.old_text in e), None)
        if idx is None:
            return MemoryMutateResponse(
                success=False,
                error=f"No entry containing '{body.old_text[:50]}' found in {body.target}.",
            )

        entries.pop(idx)

    else:
        raise HTTPException(status_code=422, detail=f"Unknown action '{body.action}'.")

    # Write back.
    await ts.write_memory_file(filename, _serialize_entries(entries))

    return MemoryMutateResponse(
        success=True,
        message=f"Entry {'added' if body.action == 'add' else body.action + 'd'}.",
        entries=entries,
        usage=_format_usage(entries, limit),
        entry_count=len(entries),
    )
