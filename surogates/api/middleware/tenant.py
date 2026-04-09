"""Tenant context injection middleware.

After the auth middleware has validated the JWT and set the
``TenantContext`` context-var, this middleware is a no-op for most
requests.  It exists as a dedicated layer so that future enhancements
(org-level feature flags, quota checks, etc.) have a clear home.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.middleware.base import RequestResponseEndpoint

from surogates.tenant.context import get_tenant

if TYPE_CHECKING:
    from fastapi import FastAPI, Request, Response

    from surogates.config import Settings

logger = logging.getLogger(__name__)


def setup_tenant_middleware(app: FastAPI, settings: Settings) -> None:
    """Attach middleware that enriches the request scope with tenant info.

    The auth middleware (which runs first) is responsible for decoding the
    JWT and calling ``set_tenant()``.  This layer reads the context-var and
    stores a reference on ``request.state`` for convenience, so route
    handlers that do *not* use ``Depends(get_current_tenant)`` can still
    access the tenant context.
    """

    @app.middleware("http")
    async def _tenant_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        try:
            ctx = get_tenant()
            request.state.tenant = ctx
        except LookupError:
            # No tenant context set -- the request is either unauthenticated
            # (public endpoint) or the auth middleware already rejected it.
            request.state.tenant = None

        return await call_next(request)
