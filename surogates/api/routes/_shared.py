"""Helpers shared across resource-CRUD route modules (skills, agents, ...).

Keeps the routes thin: raising a 422 from a validator's return string and
collapsing the loader's DB-vs-FS source taxonomy down to the tenancy
layer the frontend cares about are both one-liners needed by every
resource module, so they live here instead of being copied verbatim.
"""

from __future__ import annotations

from fastapi import HTTPException, status


def raise_validation(err: str | None) -> None:
    """Raise HTTP 422 when *err* is truthy; no-op otherwise."""
    if err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=err,
        )


def normalize_source(source: str) -> str:
    """Collapse DB-backed source variants to the tenancy-layer name.

    The loader distinguishes ``org``/``org_db`` and ``user``/``user_db``
    so merge precedence is deterministic, but API consumers only care
    about the tenancy layer, not the storage backend.
    """
    if source == "org_db":
        return "org"
    if source == "user_db":
        return "user"
    return source
