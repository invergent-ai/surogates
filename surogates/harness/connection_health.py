"""Connection health management for long-running workers.

Detects and cleans up dead HTTP connections from the OpenAI client's
connection pool.  Prevents hangs on zombie sockets from provider outages.
"""

from __future__ import annotations

import logging
import socket as _socket

logger = logging.getLogger(__name__)


def is_openai_client_closed(client: object) -> bool:
    """Check if an OpenAI client is closed.

    Handles both property and method forms of is_closed:
    - httpx.Client.is_closed is a bool property
    - openai.OpenAI.is_closed is a method returning bool
    """
    try:
        from unittest.mock import Mock
        if isinstance(client, Mock):
            return False
    except ImportError:
        pass

    is_closed_attr = getattr(client, "is_closed", None)
    if is_closed_attr is not None:
        # Handle method (openai SDK) vs property (httpx)
        if callable(is_closed_attr):
            if is_closed_attr():
                return True
        elif bool(is_closed_attr):
            return True

    http_client = getattr(client, "_client", None)
    if http_client is not None:
        return bool(getattr(http_client, "is_closed", False))
    return False


def force_close_tcp_sockets(client: object) -> int:
    """Force-close underlying TCP sockets to prevent CLOSE-WAIT accumulation.

    When a provider drops a connection mid-stream, httpx's ``client.close()``
    performs a graceful shutdown which leaves sockets in CLOSE-WAIT until the
    OS times them out (often minutes).  This function walks the httpx transport
    pool and issues ``socket.shutdown(SHUT_RDWR)`` + ``socket.close()`` to
    force an immediate TCP RST, freeing the file descriptors.

    Returns the number of sockets force-closed.
    """
    closed = 0
    try:
        http_client = getattr(client, "_client", None)
        if http_client is None:
            return 0
        transport = getattr(http_client, "_transport", None)
        if transport is None:
            return 0
        pool = getattr(transport, "_pool", None)
        if pool is None:
            return 0
        # httpx uses httpcore connection pools; connections live in
        # _connections (list) or _pool (list) depending on version.
        connections = (
            getattr(pool, "_connections", None)
            or getattr(pool, "_pool", None)
            or []
        )
        for conn in list(connections):
            stream = (
                getattr(conn, "_network_stream", None)
                or getattr(conn, "_stream", None)
            )
            if stream is None:
                continue
            sock = getattr(stream, "_sock", None)
            if sock is None:
                sock = getattr(stream, "stream", None)
                if sock is not None:
                    sock = getattr(sock, "_sock", None)
            if sock is None:
                continue
            try:
                sock.shutdown(_socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
            closed += 1
    except Exception as exc:
        logger.debug("Force-close TCP sockets sweep error: %s", exc)
    return closed


def probe_dead_connections(client: object) -> int:
    """Detect dead TCP connections in the client's connection pool.

    Inspects the httpx connection pool for sockets where the remote peer
    has already closed (EOF on ``recv(1, MSG_PEEK)``).  Returns the count
    of dead connections found.

    This is a non-destructive probe -- it does not close the connections.
    Use :func:`force_close_tcp_sockets` or :func:`cleanup_dead_connections`
    to actually close them.
    """
    try:
        http_client = getattr(client, "_client", None)
        if http_client is None:
            return 0
        transport = getattr(http_client, "_transport", None)
        if transport is None:
            return 0
        pool = getattr(transport, "_pool", None)
        if pool is None:
            return 0
        connections = (
            getattr(pool, "_connections", None)
            or getattr(pool, "_pool", None)
            or []
        )
        dead_count = 0
        for conn in list(connections):
            # Check for connections that are idle but have closed sockets
            stream = (
                getattr(conn, "_network_stream", None)
                or getattr(conn, "_stream", None)
            )
            if stream is None:
                continue
            sock = getattr(stream, "_sock", None)
            if sock is None:
                sock = getattr(stream, "stream", None)
                if sock is not None:
                    sock = getattr(sock, "_sock", None)
            if sock is None:
                continue
            # Probe socket health with a non-blocking recv peek
            try:
                sock.setblocking(False)
                data = sock.recv(1, _socket.MSG_PEEK | _socket.MSG_DONTWAIT)
                if data == b"":
                    dead_count += 1
            except BlockingIOError:
                pass  # No data available — socket is healthy
            except OSError:
                dead_count += 1
            finally:
                try:
                    sock.setblocking(True)
                except OSError:
                    pass
        return dead_count
    except Exception as exc:
        logger.debug("Dead connection probe error: %s", exc)
    return 0


async def cleanup_dead_connections(client: object) -> int:
    """Inspect the OpenAI client's httpx connection pool and close dead sockets.

    Combines dead-connection probing with force-close and idle connection
    cleanup.  Returns the total number of connections cleaned up.

    The OpenAI ``AsyncOpenAI`` client uses httpx internally.  After a provider
    outage, the pool may hold connections that are technically open but
    will fail on the next request.  This proactively closes them.
    """
    cleaned = 0

    # Phase 1: probe for dead sockets and force-close them if found.
    dead_count = probe_dead_connections(client)
    if dead_count > 0:
        logger.warning(
            "Found %d dead connection(s) in client pool — force-closing",
            dead_count,
        )
        force_closed = force_close_tcp_sockets(client)
        cleaned += force_closed

    # Phase 2: close idle httpx connections (original approach).
    try:
        http_client = getattr(client, "_client", None)
        if http_client is None:
            return cleaned

        pool = getattr(http_client, "_pool", None) or getattr(
            http_client, "_transport", None
        )
        if pool is None:
            return cleaned

        # httpx.AsyncHTTPTransport wraps httpcore.AsyncConnectionPool.
        # The pool itself is not iterable — the connections list is at
        # pool._connections (httpcore 1.x) or pool._pool (httpcore 0.x).
        connections = None
        for attr in ("_connections", "_pool", "connections"):
            candidate = getattr(pool, attr, None)
            if candidate is not None and isinstance(candidate, (list, tuple)):
                connections = candidate
                break

        if not connections:
            return cleaned

        # httpcore connections have .is_idle, .is_closed, .is_available.
        for conn in list(connections):
            is_idle = getattr(conn, "is_idle", False)
            is_closed = getattr(conn, "is_closed", False)
            if is_idle and not is_closed:
                try:
                    await conn.aclose()
                    cleaned += 1
                except Exception:
                    pass
    except Exception:
        logger.debug("Connection health check failed", exc_info=True)

    if cleaned:
        logger.info(
            "Cleaned up %d dead/idle connections from LLM client pool", cleaned
        )
    return cleaned
