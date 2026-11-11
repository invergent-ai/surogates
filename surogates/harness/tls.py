"""One TLS context per process for the HTTP clients a wake builds.

httpx builds a new ``ssl.SSLContext`` for every client it makes, loading the
whole CA bundle each time: about 11 ms of CPU, on the worker's event loop.
Every wake built at least two such clients (the API client, and one
``AsyncOpenAI`` per model slot), so under load a worker spent about a tenth
of its loop loading the same certificates again.

All of them use httpx's default context: verify against ``SSL_CERT_FILE``,
else ``SSL_CERT_DIR``, else certifi's bundle (``httpx.create_ssl_context``).
No client sets TLS options of its own -- no tenant or endpoint carries any
-- so one context serves them all.  It is built again when the bundle it
read changes on disk, so a rotated CA bundle is trusted as soon as it is
there, as before.  Keys and tokens stay per client and per wake; the
context holds only what a client trusts.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import threading

import httpx
from openai import DefaultAsyncHttpxClient

_lock = threading.Lock()
_cached: tuple[tuple, ssl.SSLContext] | None = None


def _file_identity(path: str) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns, st.st_size


def _bundle() -> tuple:
    """What httpx's default context is built from, and that bundle's state on disk."""
    cert_file = os.environ.get("SSL_CERT_FILE") or ""
    cert_dir = os.environ.get("SSL_CERT_DIR") or ""
    if cert_file:
        source = cert_file
    elif cert_dir:
        source = cert_dir
    else:
        import certifi

        source = certifi.where()
    return cert_file, cert_dir, source, _file_identity(source)


def shared_ssl_context() -> ssl.SSLContext:
    """httpx's default verifying context, built once per CA bundle."""
    global _cached
    bundle = _bundle()
    cached = _cached
    if cached is not None and cached[0] == bundle:
        return cached[1]
    with _lock:
        if _cached is None or _cached[0] != bundle:
            _cached = (bundle, httpx.create_ssl_context())
        return _cached[1]


class _LLMHttpClient(DefaultAsyncHttpxClient):
    """openai's default HTTP client for a model slot, on the shared context.

    Closed when collected, as the client ``AsyncOpenAI`` makes for itself
    is (``openai._base_client.AsyncHttpxClientWrapper``).
    """

    def __del__(self) -> None:
        if self.is_closed:
            return
        try:
            asyncio.get_running_loop().create_task(self.aclose())
        except Exception:  # noqa: BLE001 -- no loop at collection: nothing to close on
            pass


def llm_http_client() -> httpx.AsyncClient:
    """The HTTP client for one ``AsyncOpenAI``: openai's defaults (timeout,
    connection limits, redirects), with the shared TLS context."""
    return _LLMHttpClient(verify=shared_ssl_context())
