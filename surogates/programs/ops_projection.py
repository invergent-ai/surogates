"""Fetch ops' projection of active check-in Programs.

The runtime does not read ops tables, so the only way it learns which
Programs are active — and, just as importantly, which have stopped being
active — is this runtime-scoped endpoint.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PATH = "/api/programs/active"


async def fetch_active_programs(
    http_client: Any,
    *,
    base_url: str,
    runtime_key: str,
    timeout: float = 15.0,
) -> list[dict] | None:
    """Ops' active Programs, or ``None`` when the answer is not trustworthy.

    The ``None``-versus-``[]`` distinction is the whole point of this
    function's shape.  An empty list is ops saying "no Programs are active",
    and reconcile answers that by deactivating every mirrored schedule.  A
    failed, rejected, or malformed fetch must never be able to say that — one
    unreachable ops server would otherwise silently stop every check-in in the
    fleet.  Callers skip reconciliation entirely on ``None``.
    """
    if not base_url or not runtime_key:
        return None

    url = f"{base_url.rstrip('/')}{_PATH}"
    try:
        response = await http_client.get(
            url,
            headers={"Authorization": f"Bearer {runtime_key}"},
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 — a tick must not die on a bad fetch
        logger.warning(
            "[programs] could not reach ops for the projection at %s",
            url, exc_info=True,
        )
        return None

    if not getattr(response, "is_success", False):
        logger.warning(
            "[programs] ops refused the projection at %s: %s",
            url, getattr(response, "status_code", "?"),
        )
        return None

    body = response.json()
    if not isinstance(body, list):
        # A proxy error page can answer 200 with an object; reading that as
        # "no Programs are active" would pause the whole fleet.
        logger.warning(
            "[programs] ops returned a %s for the projection, expected a list",
            type(body).__name__,
        )
        return None
    return body
