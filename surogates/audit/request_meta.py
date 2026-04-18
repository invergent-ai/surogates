"""Request metadata helpers for audit events.

Extracts forensic fields that are only reliable when the ASGI server has
been told about the reverse proxy in front of it.  In production the
platform runs behind a K8s Ingress, so ``request.client.host`` always
returns the ingress controller's IP — useless for auth audit.  The
helpers here prefer proxy headers when present and fall back to the
direct client only when they are not set.

Callers should run uvicorn with ``--proxy-headers --forwarded-allow-ips=...``
(or an equivalent ``ProxyHeadersMiddleware``) so that Starlette's own
``scope["client"]`` is already corrected; the explicit header reads
below are the second line of defence.
"""

from __future__ import annotations

from typing import Any


def client_ip(request: Any) -> str | None:
    """Return the best-effort client IP for *request*.

    Order:

    1. ``X-Forwarded-For`` leftmost entry (the originating client before
       any proxies appended themselves).
    2. ``X-Real-IP`` (single-IP variant used by nginx).
    3. ``request.client.host`` (the direct peer — correct only when the
       ASGI server is configured with proxy headers support, or when the
       platform is deployed without a proxy).

    Returns ``None`` when none of the sources yields a non-empty value.
    """
    headers = getattr(request, "headers", None)
    if headers is not None:
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded:
            first = forwarded.split(",", 1)[0].strip()
            if first:
                return first

        real_ip = headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip

    client = getattr(request, "client", None)
    if client is not None and getattr(client, "host", None):
        return client.host

    return None
