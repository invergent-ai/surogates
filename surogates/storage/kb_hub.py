"""Minimal async Hub (lakeFS-flavoured) client for KB wiki content reads.

The runtime KB tools call this module to fetch the markdown content
of a wiki page after the ops DB has told them which page to read.
We do not import surogate_hub_sdk here on purpose:

  - The full SDK is generated bindings for every Hub endpoint; we
    only need ``GET /repositories/{repo}/refs/{ref}/objects?path=...``.
  - Pulling a few hundred KiB of generated code into the worker
    image just to read bytes off a key/value store is overkill.
  - httpx is already in the worker dependency closure.

Auth is HTTP Basic against the lakeFS-style access key + secret -- the
same credentials the ops process already uses for the compile pipeline.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


# Default budget for a single content fetch. The wiki pages are tiny
# (kilobytes of markdown), so anything that takes more than a few
# seconds is symptomatic of a broken Hub instance, not slow content.
DEFAULT_TIMEOUT_SECONDS: float = 10.0


class KBHubError(Exception):
    """Wraps Hub access failures with the context the LLM needs."""


async def fetch_wiki_object(
    *,
    endpoint_url: str,
    access_key_id: str,
    secret_access_key: str,
    repo_id: str,
    branch: str,
    path: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """Fetch a single object from a Hub repo by path.

    The ops compile pipeline uploads wiki pages under the ``wiki/``
    prefix in the KB repo. Callers pass the path *with* that prefix
    (``wiki/sources/d1.md``) -- this function does no path rewriting,
    so the prefix policy lives in exactly one place: the tool layer.

    Parameters
    ----------
    endpoint_url:
        Hub base URL, e.g. ``http://surogate-hub.surogate-hub.svc``.
        Trailing slash is stripped.
    access_key_id, secret_access_key:
        lakeFS-style credentials. The KB compile pipeline writes
        under the same key, so platform-level admin creds are the
        natural choice here.
    repo_id:
        Logical Hub repo id, e.g. ``p-8188edfc/kb-smoke-final-40726ff6``.
        ``OpsKnowledgeBase.hub_ref`` holds this verbatim.
    branch:
        Branch / ref to read from. Always ``main`` for wiki content
        in the v1 design.
    path:
        Object path within the repo, including the ``wiki/`` prefix.

    Returns
    -------
    bytes
        Raw object content. Caller decodes (we don't assume utf-8 even
        though wiki pages are markdown -- protects us from a future
        binary attachment on the same path).

    Raises
    ------
    KBHubError
        Wraps any HTTP / network failure with a message that tells
        the LLM something concrete (path + status) without leaking
        Hub internals.
    """
    base = endpoint_url.rstrip("/")
    encoded_path = quote(path, safe="")
    url = (
        f"{base}/api/v1/repositories/{repo_id}/refs/{branch}"
        f"/objects?path={encoded_path}"
    )
    auth = httpx.BasicAuth(access_key_id, secret_access_key)
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(url, auth=auth)
    except httpx.RequestError as exc:
        raise KBHubError(
            f"Hub network error fetching {path!r} from {repo_id}: {exc}"
        ) from exc

    if resp.status_code == 404:
        raise KBHubError(
            f"Wiki object {path!r} not found in {repo_id}@{branch}"
        )
    if resp.status_code == 401 or resp.status_code == 403:
        raise KBHubError(
            f"Hub auth failed for {repo_id} (status {resp.status_code})"
        )
    if resp.status_code >= 400:
        raise KBHubError(
            f"Hub returned {resp.status_code} for {path!r} in {repo_id}: "
            f"{resp.text[:200]}"
        )
    return resp.content
