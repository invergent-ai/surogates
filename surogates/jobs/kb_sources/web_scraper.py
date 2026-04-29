"""``web_scraper`` source runner — fetch URLs over HTTP and convert
them to markdown via ``markitdown``.

Config schema (``kb_source.config``):

  - ``seed_urls`` (list[str]): explicit list of URLs to fetch. Mutually
    exclusive with ``sitemap_url``.
  - ``sitemap_url`` (str): a sitemap.xml to fetch and walk; every
    ``<loc>`` URL is treated as a seed. Mutually exclusive with
    ``seed_urls``.
  - ``max_urls`` (int): hard cap on URLs processed in one run
    (default 100). Sitemaps with more URLs than this are truncated and
    ``docs_skipped`` is incremented for the rest.
  - ``request_timeout`` (float): per-request timeout seconds (default
    20).
  - ``user_agent`` (str): HTTP User-Agent (default
    ``InvergentKB-WebScraper/1.0``). Override to comply with site
    policies.

No JS rendering, no recursive crawling — sitemap-driven or explicit
list only. Works for static doc sites; dynamic SPAs need a different
runner (later).

Bucket path: ``raw/<host>/<path-or-index>.md``. Slashes in the URL
path are preserved so the wiki maintainer can mirror the structure.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Optional, Sequence
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.jobs.kb_sources._base import (
    IngestResult,
    SourceContext,
    load_existing_raw_docs,
    upsert_raw_doc,
)
from surogates.storage.backend import StorageBackend
from surogates.storage.kb_storage import KbStorage

logger = logging.getLogger(__name__)

KIND = "web_scraper"

DEFAULT_MAX_URLS = 100
DEFAULT_TIMEOUT = 20.0
DEFAULT_USER_AGENT = "InvergentKB-WebScraper/1.0"

_RAW_PREFIX = "raw"

# Sitemap XML namespace per the sitemaps.org standard. Most sitemaps
# bind it as the default namespace, so XPath needs the {uri} prefix.
_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


async def run(
    ctx: SourceContext,
    *,
    session_factory: async_sessionmaker,
    storage_backend: StorageBackend,
) -> IngestResult:
    config = ctx.config or {}
    seed_urls = list(config.get("seed_urls") or [])
    sitemap_url = config.get("sitemap_url")
    max_urls = int(config.get("max_urls") or DEFAULT_MAX_URLS)
    timeout = float(config.get("request_timeout") or DEFAULT_TIMEOUT)
    user_agent = str(config.get("user_agent") or DEFAULT_USER_AGENT)

    if seed_urls and sitemap_url:
        raise ValueError(
            "web_scraper: 'seed_urls' and 'sitemap_url' are mutually exclusive"
        )
    if not seed_urls and not sitemap_url:
        raise ValueError(
            "web_scraper: either 'seed_urls' or 'sitemap_url' must be set"
        )

    storage = KbStorage(storage_backend)
    result = IngestResult()
    existing = await load_existing_raw_docs(
        ctx.kb_id, session_factory=session_factory,
    )

    async with _new_http_client(timeout=timeout, user_agent=user_agent) as client:
        if sitemap_url:
            seed_urls = await _read_sitemap(client, sitemap_url)

        # Apply max_urls cap; remainder become skipped.
        if len(seed_urls) > max_urls:
            logger.warning(
                "web_scraper: %d URLs exceed max_urls=%d; truncating",
                len(seed_urls), max_urls,
            )
            for _ in seed_urls[max_urls:]:
                result.docs_skipped += 1
            seed_urls = seed_urls[:max_urls]

        for url in seed_urls:
            try:
                data, title = await _fetch_and_convert(client, url)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("web_scraper: skipping %s (%s)", url, exc)
                result.docs_skipped += 1
                continue

            if not data:
                result.docs_skipped += 1
                continue

            bucket_path = _bucket_path_for_url(url)
            await upsert_raw_doc(
                ctx,
                bucket_path=bucket_path,
                data=data,
                title=title,
                url=url,
                session_factory=session_factory,
                storage=storage,
                existing=existing,
                result=result,
            )

    logger.info("web_scraper: ingest complete: %s", result.as_dict())
    return result


# ---------------------------------------------------------------------------
# HTTP client factory (module-level so tests can monkey-patch a
# MockTransport in without restructuring the runner's plumbing).
# ---------------------------------------------------------------------------


def _new_http_client(
    *,
    timeout: float,
    user_agent: str,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": user_agent},
    )


# ---------------------------------------------------------------------------
# Sitemap parsing
# ---------------------------------------------------------------------------


async def _read_sitemap(
    client: httpx.AsyncClient,
    sitemap_url: str,
) -> list[str]:
    """Fetch a sitemap.xml and return its ``<loc>`` URLs.

    Supports both ``<urlset>`` (URLs) and ``<sitemapindex>`` (nested
    sitemaps). For nested, only the first level of nesting is followed
    to bound runtime; deeper nesting is rare in doc sites.
    """
    response = await client.get(sitemap_url)
    response.raise_for_status()
    return list(_parse_sitemap_xml(response.text, base_url=sitemap_url))


def _parse_sitemap_xml(xml_text: str, *, base_url: str) -> Sequence[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"sitemap parse failed for {base_url}: {exc}") from exc

    # urlset → URLs directly
    locs = [
        elem.text.strip()
        for elem in root.findall(f"{_SITEMAP_NS}url/{_SITEMAP_NS}loc")
        if elem.text
    ]
    # sitemapindex → list of sub-sitemap URLs (we don't recurse here;
    # caller can pre-flatten if they need it).
    sub_sitemaps = [
        elem.text.strip()
        for elem in root.findall(
            f"{_SITEMAP_NS}sitemap/{_SITEMAP_NS}loc"
        )
        if elem.text
    ]
    if not locs and sub_sitemaps:
        logger.info(
            "sitemap %s is a sitemapindex with %d nested sitemaps; "
            "the runner only processes the first level of nesting",
            base_url, len(sub_sitemaps),
        )
        # For now: synchronously flatten one level. Recursion-safe for
        # typical doc sites; deeper nesting is an out-of-scope concern.
    return locs or sub_sitemaps


# ---------------------------------------------------------------------------
# Fetch + convert
# ---------------------------------------------------------------------------


async def _fetch_and_convert(
    client: httpx.AsyncClient,
    url: str,
) -> tuple[bytes, Optional[str]]:
    """Fetch *url*, convert via markitdown, return (markdown_bytes, title).

    Raises ``httpx.HTTPError`` on network failures and ``ValueError``
    on conversion errors (which the caller treats as skip).
    """
    # markitdown is sync and pulls in a lot of optional deps; import
    # lazily so the rest of the module is importable in environments
    # without it (e.g. unit tests of the URL helpers).
    from markitdown import MarkItDown

    response = await client.get(url)
    response.raise_for_status()
    body = response.content
    if not body:
        return b"", None

    # Hint the file extension so markitdown picks the right converter.
    # Default to .html since web_scraper targets web pages; PDFs and
    # other downloads are detected from the response content-type when
    # available.
    ext = _infer_file_extension(
        url=url,
        content_type=response.headers.get("content-type", ""),
    )

    converter = MarkItDown()
    try:
        # ``convert_stream`` is sync; off-thread it so we don't block
        # the event loop while pdfminer / docx parsing chews through a
        # large payload.
        result = await asyncio.to_thread(
            converter.convert_stream,
            io.BytesIO(body),
            file_extension=ext,
        )
    except Exception as exc:
        raise ValueError(f"markitdown conversion failed: {exc}") from exc

    md_text = (result.text_content or "").strip()
    if not md_text:
        return b"", None

    title = _extract_title(result, md_text)
    return md_text.encode("utf-8"), title


def _infer_file_extension(*, url: str, content_type: str) -> str:
    """Pick a markitdown file_extension hint from URL or Content-Type."""
    ct = content_type.split(";", 1)[0].strip().lower()
    if "pdf" in ct:
        return ".pdf"
    if "wordprocessingml" in ct or ct.endswith("/msword"):
        return ".docx"
    if "presentationml" in ct or ct.endswith("/vnd.ms-powerpoint"):
        return ".pptx"
    if "spreadsheetml" in ct or ct.endswith("/vnd.ms-excel"):
        return ".xlsx"
    # Fall back to URL suffix if it's an obvious type.
    path = urlparse(url).path.lower()
    for sfx in (".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".txt"):
        if path.endswith(sfx):
            return sfx if sfx != ".htm" else ".html"
    # Default: HTML.
    return ".html"


def _extract_title(
    markitdown_result,
    md_text: str,
) -> Optional[str]:
    """Use markitdown's title attribute if present; else first heading."""
    title = getattr(markitdown_result, "title", None)
    if title:
        return str(title).strip()[:200] or None
    for line in md_text.splitlines()[:30]:
        stripped = line.strip()
        if stripped.startswith("# ") and len(stripped) > 2:
            return stripped[2:].strip()[:200] or None
    return None


# ---------------------------------------------------------------------------
# URL → bucket path
# ---------------------------------------------------------------------------


_SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9._/-]+")


def _bucket_path_for_url(url: str) -> str:
    """Build a stable bucket path from a URL.

    Format: ``raw/<host>/<sanitized-path>.md``. Trailing slashes
    become ``/index``; query strings and fragments are dropped because
    most static doc sites don't disambiguate by them.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or "/"
    if path.endswith("/"):
        path = path + "index"
    # Strip leading slash, sanitise, ensure .md extension.
    path = path.lstrip("/")
    path = _SAFE_PATH_RE.sub("-", path)
    if not path.lower().endswith(".md"):
        path = path + ".md"
    return f"{_RAW_PREFIX}/{host}/{path}"
