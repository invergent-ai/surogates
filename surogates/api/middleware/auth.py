"""JWT authentication middleware.

Delegates to the tenant auth subsystem. This module exists so the
``surogates.api.middleware`` package has a stable import path used by
``create_app()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from surogates.tenant.auth.middleware import setup_auth_middleware

if TYPE_CHECKING:
    pass

__all__ = ["setup_auth_middleware"]
