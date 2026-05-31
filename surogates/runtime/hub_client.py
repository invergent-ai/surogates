"""Async wrapper over the Surogate Hub SDK's ObjectsApi.

The Hub SDK is auto-generated from OpenAPI and
exposes a synchronous, lakeFS-style API:

    objects_api.get_object(user, repository, ref, path) -> bytearray
    objects_api.list_objects(user, repository, ref, prefix=None) -> ObjectStatsList

This wrapper:

* Pins ``user`` and ``repository`` once at construction so per-call
  signatures only need ``ref`` (the bundle version) and ``path``.
* Runs the synchronous SDK calls in a thread executor so they don't
  block the asyncio event loop.
* Maps the SDK's "not found" exception to :class:`LookupError` —
  consistent with the rest of ``surogates.runtime`` (RuntimeConfigCache,
  PlatformClient.get_runtime_config) where LookupError means
  "resource doesn't exist" and a separate exception type means
  "infra problem".
* Provides ``aclose()`` for symmetric teardown so caller code can
  treat HubBundleClient like the other runtime clients.
"""

from __future__ import annotations

import asyncio
from typing import Any

__all__ = ["HubBundleClient"]


class HubBundleClient:
    """Per-process async wrapper around one Hub ``(user, repository)``."""

    def __init__(
        self,
        *,
        objects_api: Any,
        user: str,
        repository: str,
    ) -> None:
        self._objects_api = objects_api
        self._user = user
        self._repository = repository

    async def read_bytes(self, ref: str, path: str) -> bytes:
        """Return the bytes of ``path`` at the given ``ref``.

        Raises :class:`LookupError` when the file does not exist at
        this version of the bundle.  Network / auth errors propagate
        verbatim — the cache layer interprets them.

        The SDK signals "object not found" via its own
        ``NotFoundException`` (an ``ApiException`` subclass with
        ``status == 404``); we also catch ``FileNotFoundError`` for
        any SDK build that surfaces a stdlib variant instead.  Both
        map to ``LookupError`` so callers like ``load_agent_md`` can
        treat optional files as absent without crashing the session.
        """
        from surogate_hub_sdk import ApiException

        try:
            result = await asyncio.to_thread(
                self._objects_api.get_object,
                self._user, self._repository, ref, path,
            )
        except FileNotFoundError as exc:
            raise LookupError(
                f"bundle {self._user}/{self._repository}@{ref}: "
                f"path {path!r} not found",
            ) from exc
        except ApiException as exc:
            if getattr(exc, "status", None) == 404:
                raise LookupError(
                    f"bundle {self._user}/{self._repository}@{ref}: "
                    f"path {path!r} not found",
                ) from exc
            raise
        # SDK returns bytearray; convert to bytes for stable hashing
        # and immutability.
        return bytes(result)

    async def list_paths(self, ref: str, *, prefix: str = "") -> list[str]:
        """List every object path under ``prefix`` at the given ``ref``.

        Returns a flat list of paths (no metadata).  The caller is
        responsible for distinguishing directories from files by path
        suffix (e.g., the skill loader filters for ``SKILL.md``).

        Paginates internally so even bundles with hundreds of files
        come back in one call.  No ``delimiter`` is passed, so the
        result is the recursive flat list (one entry per file).
        """
        paths: list[str] = []
        after: str | None = ""
        while True:
            page = await asyncio.to_thread(
                self._objects_api.list_objects,
                self._user, self._repository, ref,
                prefix=prefix or None,
                after=after,
                amount=1000,
            )
            for s in page.results or []:
                paths.append(s.path)
            pagination = getattr(page, "pagination", None)
            if not pagination or not getattr(pagination, "has_more", False):
                break
            after = getattr(pagination, "next_offset", None) or ""
        return paths

    async def aclose(self) -> None:
        """Symmetric teardown.  Today a no-op because the SDK is
        synchronous and uses a connection pool we don't own; kept on
        the surface so future SDK upgrades can plug a real close
        here without changing every caller."""
        return None
