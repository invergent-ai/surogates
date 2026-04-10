"""Builtin web search and extract tools -- multi-backend support.

Registers ``web_search`` and ``web_extract`` tools with the tool
registry.  Supports multiple backend providers:

- Tavily: https://tavily.com (search, extract)  -- TAVILY_API_KEY
- Exa: https://exa.ai (search, extract)  -- EXA_API_KEY

Backend is selected automatically based on available API keys, or
can be forced via ``WEB_BACKEND`` environment variable.

Backend compatibility:
- Tavily: search, extract  (simple API-key auth in JSON body)
- Exa: search, extract  (simple API-key auth via httpx)
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)


# ─── Constants ───────────────────────────────────────────────────────────────

_TAVILY_BASE_URL = "https://api.tavily.com"
_EXA_BASE_URL = "https://api.exa.ai"

# Search limits
_DEFAULT_SEARCH_LIMIT = 5
_MAX_SEARCH_LIMIT = 20

# Content limits
_MAX_EXTRACT_URLS = 5
_MAX_CONTENT_SIZE = 2_000_000        # 2M chars -- refuse entirely above this
_CHUNK_THRESHOLD = 500_000           # 500k chars -- use chunked processing above this
_CHUNK_SIZE = 100_000                # 100k chars per chunk
_MAX_OUTPUT_SIZE = 5000              # Hard cap on final output size
DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION = 5000


# ─── Backend Selection ───────────────────────────────────────────────────────

def _has_env(name: str) -> bool:
    """Return True when the named environment variable has a non-empty value."""
    val = os.getenv(name)
    return bool(val and val.strip())


def _get_backend() -> str:
    """Determine which web backend to use.

    Reads ``WEB_BACKEND`` environment variable first.  Falls back to
    whichever API key is present, picking the highest-priority available
    backend.
    """
    configured = (os.getenv("WEB_BACKEND") or "").lower().strip()
    if configured in ("tavily", "exa"):
        return configured

    # Fallback -- pick the highest-priority available backend.
    backend_candidates = (
        ("tavily", _has_env("TAVILY_API_KEY")),
        ("exa", _has_env("EXA_API_KEY")),
    )
    for backend, available in backend_candidates:
        if available:
            return backend

    return "tavily"  # default


def _is_backend_available(backend: str) -> bool:
    """Return True when the selected backend is currently usable."""
    if backend == "exa":
        return _has_env("EXA_API_KEY")
    if backend == "tavily":
        return _has_env("TAVILY_API_KEY")
    return False


# ─── Generic helpers ─────────────────────────────────────────────────────────

def _to_plain_object(value: Any) -> Any:
    """Convert SDK objects to plain python data structures when possible."""
    if value is None:
        return None

    if isinstance(value, (dict, list, str, int, float, bool)):
        return value

    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            pass

    if hasattr(value, "__dict__"):
        try:
            return {k: v for k, v in value.__dict__.items() if not k.startswith("_")}
        except Exception:
            pass

    return value


def _normalize_result_list(values: Any) -> List[Dict[str, Any]]:
    """Normalize mixed SDK/list payloads into a list of dicts."""
    if not isinstance(values, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for item in values:
        plain = _to_plain_object(item)
        if isinstance(plain, dict):
            normalized.append(plain)
    return normalized


def _extract_web_search_results(response: Any) -> List[Dict[str, Any]]:
    """Extract search results across various API response shapes.

    Handles multiple response structures:
    - ``{data: [{...}]}`` (list of results in data)
    - ``{data: {web: [...]}}`` (web results nested in data)
    - ``{data: {results: [...]}}`` (results nested in data)
    - ``{web: [...]}`` (top-level web results)
    - ``{results: [...]}`` (top-level results)
    - Object with ``.web`` attribute (SDK response)
    """
    response_plain = _to_plain_object(response)

    if isinstance(response_plain, dict):
        data = response_plain.get("data")
        if isinstance(data, list):
            return _normalize_result_list(data)

        if isinstance(data, dict):
            data_web = _normalize_result_list(data.get("web"))
            if data_web:
                return data_web
            data_results = _normalize_result_list(data.get("results"))
            if data_results:
                return data_results

        top_web = _normalize_result_list(response_plain.get("web"))
        if top_web:
            return top_web

        top_results = _normalize_result_list(response_plain.get("results"))
        if top_results:
            return top_results

    if hasattr(response, "web"):
        return _normalize_result_list(getattr(response, "web", []))

    return []


def _extract_scrape_payload(scrape_result: Any) -> Dict[str, Any]:
    """Normalize scrape payload shape across SDK and gateway variants.

    If the result contains a nested ``data`` dict, return that inner dict.
    Otherwise return the top-level dict.
    """
    result_plain = _to_plain_object(scrape_result)
    if not isinstance(result_plain, dict):
        return {}

    nested = result_plain.get("data")
    if isinstance(nested, dict):
        return nested

    return result_plain


def _truncate_content(content: str, max_size: int = _MAX_OUTPUT_SIZE) -> str:
    """Truncate content to *max_size* characters with a trailing notice.

    Returns content unchanged when it fits within the limit.
    """
    if len(content) <= max_size:
        return content
    return (
        content[:max_size]
        + f"\n\n[Content truncated -- showing first {max_size:,} of "
        f"{len(content):,} chars.]"
    )


def _format_search_results(web_results: List[Dict[str, Any]]) -> dict:
    """Wrap a list of web-result dicts in the standard response envelope.

    Returns ``{success: True, data: {web: [...]}}``
    """
    return {"success": True, "data": {"web": web_results}}


def clean_base64_images(text: str) -> str:
    """Remove base64 encoded images from text to reduce token count and clutter.

    Finds and removes base64 encoded images in various formats:
    - (data:image/png;base64,...)
    - (data:image/jpeg;base64,...)
    - (data:image/svg+xml;base64,...)
    - data:image/[type];base64,... (without parentheses)
    """
    # Pattern to match base64 encoded images wrapped in parentheses
    # Matches: (data:image/[type];base64,[base64-string])
    base64_with_parens_pattern = r'\(data:image/[^;]+;base64,[A-Za-z0-9+/=]+\)'

    # Pattern to match base64 encoded images without parentheses
    # Matches: data:image/[type];base64,[base64-string]
    base64_pattern = r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+'

    # Replace parentheses-wrapped images first
    cleaned_text = re.sub(base64_with_parens_pattern, '[BASE64_IMAGE_REMOVED]', text)

    # Then replace any remaining non-parentheses images
    cleaned_text = re.sub(base64_pattern, '[BASE64_IMAGE_REMOVED]', cleaned_text)

    return cleaned_text


# ─── Tavily Client ───────────────────────────────────────────────────────────

async def _tavily_request(endpoint: str, payload: dict) -> dict:
    """Send an async POST request to the Tavily API.

    Auth is provided via ``api_key`` in the JSON body (no header-based auth).
    Raises ``ValueError`` if ``TAVILY_API_KEY`` is not set.
    """
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise ValueError(
            "TAVILY_API_KEY environment variable not set. "
            "Get your API key at https://app.tavily.com/home"
        )
    payload["api_key"] = api_key
    url = f"{_TAVILY_BASE_URL}/{endpoint.lstrip('/')}"
    logger.info("Tavily %s request to %s", endpoint, url)
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()


def _normalize_tavily_search_results(response: dict) -> dict:
    """Normalize Tavily /search response to the standard web search format.

    Tavily returns ``{results: [{title, url, content, score, ...}]}``.
    We map to ``{success, data: {web: [{title, url, description, position}]}}``.
    """
    web_results = []
    for i, result in enumerate(response.get("results", [])):
        web_results.append({
            "title": result.get("title", ""),
            "url": result.get("url", ""),
            "description": result.get("content", ""),
            "position": i + 1,
        })
    return _format_search_results(web_results)


def _normalize_tavily_documents(response: dict, fallback_url: str = "") -> List[Dict[str, Any]]:
    """Normalize Tavily /extract or /crawl response to the standard document format.

    Maps results to ``{url, title, content, raw_content, metadata}`` and
    includes any ``failed_results`` / ``failed_urls`` as error entries.
    """
    documents: List[Dict[str, Any]] = []
    for result in response.get("results", []):
        url = result.get("url", fallback_url)
        raw = result.get("raw_content", "") or result.get("content", "")
        documents.append({
            "url": url,
            "title": result.get("title", ""),
            "content": raw,
            "raw_content": raw,
            "metadata": {"sourceURL": url, "title": result.get("title", "")},
        })
    # Handle failed results
    for fail in response.get("failed_results", []):
        documents.append({
            "url": fail.get("url", fallback_url),
            "title": "",
            "content": "",
            "raw_content": "",
            "error": fail.get("error", "extraction failed"),
            "metadata": {"sourceURL": fail.get("url", fallback_url)},
        })
    for fail_url in response.get("failed_urls", []):
        url_str = fail_url if isinstance(fail_url, str) else str(fail_url)
        documents.append({
            "url": url_str,
            "title": "",
            "content": "",
            "raw_content": "",
            "error": "extraction failed",
            "metadata": {"sourceURL": url_str},
        })
    return documents


# ─── Tavily Search & Extract ─────────────────────────────────────────────────

async def _tavily_search(query: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> dict:
    """Search using the Tavily API and return normalized results.

    Sends a POST to ``/search`` with ``max_results`` capped at
    ``_MAX_SEARCH_LIMIT``.
    """
    logger.info("Tavily search: '%s' (limit: %d)", query, limit)
    raw = await _tavily_request("search", {
        "query": query,
        "max_results": min(limit, _MAX_SEARCH_LIMIT),
        "include_raw_content": False,
        "include_images": False,
    })
    return _normalize_tavily_search_results(raw)


async def _tavily_extract(urls: List[str]) -> List[Dict[str, Any]]:
    """Extract content from URLs using the Tavily extract API.

    Returns a list of document dicts matching the standard shape
    ``{url, title, content, raw_content, metadata}``.
    """
    logger.info("Tavily extract: %d URL(s)", len(urls))
    raw = await _tavily_request("extract", {
        "urls": urls,
        "include_images": False,
    })
    return _normalize_tavily_documents(raw, fallback_url=urls[0] if urls else "")


# ─── Exa Client ──────────────────────────────────────────────────────────────

async def _exa_request(
    endpoint: str,
    payload: dict,
    method: str = "POST",
) -> dict:
    """Send an async request to the Exa API.

    Auth is provided via ``x-api-key`` header.
    Raises ``ValueError`` if ``EXA_API_KEY`` is not set.
    """
    api_key = os.getenv("EXA_API_KEY")
    if not api_key:
        raise ValueError(
            "EXA_API_KEY environment variable not set. "
            "Get your API key at https://exa.ai"
        )
    url = f"{_EXA_BASE_URL}/{endpoint.lstrip('/')}"
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "x-exa-integration": "surogates-agent",
    }
    logger.info("Exa %s request to %s", endpoint, url)
    async with httpx.AsyncClient() as client:
        if method.upper() == "POST":
            response = await client.post(url, json=payload, headers=headers, timeout=60)
        else:
            response = await client.get(url, params=payload, headers=headers, timeout=60)
        response.raise_for_status()
        return response.json()


def _normalize_exa_search_results(response: dict) -> dict:
    """Normalize Exa /search response to the standard web search format.

    Exa returns ``{results: [{title, url, text, highlights, ...}]}``.
    We map to ``{success, data: {web: [{title, url, description, position}]}}``.
    """
    web_results = []
    for i, result in enumerate(response.get("results", [])):
        highlights = result.get("highlights", [])
        description = " ".join(highlights) if highlights else result.get("text", "")
        web_results.append({
            "url": result.get("url", ""),
            "title": result.get("title", ""),
            "description": description,
            "position": i + 1,
        })
    return _format_search_results(web_results)


def _normalize_exa_documents(response: dict) -> List[Dict[str, Any]]:
    """Normalize Exa /contents response to the standard document format.

    Maps results to ``{url, title, content, raw_content, metadata}``.
    """
    documents: List[Dict[str, Any]] = []
    for result in response.get("results", []):
        content = result.get("text", "")
        url = result.get("url", "")
        title = result.get("title", "")
        documents.append({
            "url": url,
            "title": title,
            "content": content,
            "raw_content": content,
            "metadata": {"sourceURL": url, "title": title},
        })
    return documents


# ─── Exa Search & Extract ────────────────────────────────────────────────────

async def _exa_search(query: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> dict:
    """Search using the Exa API and return normalized results.

    Sends a POST to ``/search`` with ``num_results`` capped at
    ``_MAX_SEARCH_LIMIT``.  Requests highlights for richer descriptions.
    """
    logger.info("Exa search: '%s' (limit=%d)", query, limit)
    raw = await _exa_request("search", {
        "query": query,
        "num_results": min(limit, _MAX_SEARCH_LIMIT),
        "contents": {
            "highlights": True,
        },
    })
    return _normalize_exa_search_results(raw)


async def _exa_extract(urls: List[str]) -> List[Dict[str, Any]]:
    """Extract content from URLs using the Exa contents API.

    Returns a list of document dicts matching the standard shape
    ``{url, title, content, raw_content, metadata}``.
    """
    logger.info("Exa extract: %d URL(s)", len(urls))
    raw = await _exa_request("contents", {
        "ids": urls,
        "text": True,
    })
    return _normalize_exa_documents(raw)


# ─── Content processing helpers ──────────────────────────────────────────────

def _process_extracted_content(content: str, url: str = "", title: str = "") -> str:
    """Process extracted web content for the final response.

    Applies content size limits:
    - Content over ``_MAX_CONTENT_SIZE`` (2M chars) is refused entirely.
    - Content over ``_MAX_OUTPUT_SIZE`` (5000 chars) is truncated with a notice.
    - Content under ``_MAX_OUTPUT_SIZE`` is returned as-is.

    Also cleans base64 images from the content.
    """
    if not content:
        return content

    content_len = len(content)

    # Refuse if content is absurdly large
    if content_len > _MAX_CONTENT_SIZE:
        size_mb = content_len / 1_000_000
        logger.warning(
            "Content too large (%.1fMB > 2MB limit). Refusing to process.", size_mb
        )
        return (
            f"[Content too large to process: {size_mb:.1f}MB. "
            "Try using a more focused source or specific search query.]"
        )

    # Clean base64 images before truncation
    content = clean_base64_images(content)

    # Truncate if needed
    content = _truncate_content(content, _MAX_OUTPUT_SIZE)

    return content


def _build_extract_result(
    documents: List[Dict[str, Any]],
    process_content: bool = True,
) -> str:
    """Build the final JSON response string for web_extract.

    Trims each document to ``{url, title, content, error}`` and
    optionally applies content processing (truncation, base64 removal).
    """
    trimmed_results = []
    for doc in documents:
        content = doc.get("content", "") or doc.get("raw_content", "")
        if process_content:
            content = _process_extracted_content(
                content,
                url=doc.get("url", ""),
                title=doc.get("title", ""),
            )
        entry: Dict[str, Any] = {
            "url": doc.get("url", ""),
            "title": doc.get("title", ""),
            "content": content,
        }
        error = doc.get("error")
        if error:
            entry["error"] = error
        trimmed_results.append(entry)

    if not trimmed_results:
        return json.dumps({
            "error": "Content was inaccessible or not found",
        }, ensure_ascii=False)

    return json.dumps({"results": trimmed_results}, ensure_ascii=False)


def _format_search_json(response_data: dict) -> str:
    """Serialize a search response dict to a JSON string."""
    return json.dumps(response_data, indent=2, ensure_ascii=False)


# ─── Tool handlers ───────────────────────────────────────────────────────────

async def _web_search_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Search the web via the configured search backend.

    Dispatches to Tavily or Exa based on ``_get_backend()``.
    Returns a JSON string with ``{success, data: {web: [...]}}``.
    """
    query = arguments.get("query", "").strip()
    if not query:
        return json.dumps({"error": "No search query provided."})

    limit = arguments.get("limit", _DEFAULT_SEARCH_LIMIT)
    if not isinstance(limit, int) or limit < 1:
        limit = _DEFAULT_SEARCH_LIMIT
    limit = min(limit, _MAX_SEARCH_LIMIT)

    backend = _get_backend()

    # Check that the backend is available before dispatching
    if not _is_backend_available(backend):
        env_var = "TAVILY_API_KEY" if backend == "tavily" else "EXA_API_KEY"
        help_url = (
            "https://app.tavily.com/home" if backend == "tavily" else "https://exa.ai"
        )
        return json.dumps({
            "error": "not_configured",
            "message": (
                f"Web search requires the {env_var} environment variable. "
                f"Get your API key at {help_url}"
            ),
        })

    try:
        if backend == "exa":
            response_data = await _exa_search(query, limit)
        else:
            # Default: tavily
            response_data = await _tavily_search(query, limit)

        results_count = len(response_data.get("data", {}).get("web", []))
        logger.info("Found %d search results", results_count)
        return _format_search_json(response_data)

    except httpx.HTTPStatusError as exc:
        logger.error("%s search HTTP error: %s", backend, exc)
        return json.dumps({
            "error": f"{backend.title()} API error: {exc.response.status_code}",
            "message": str(exc),
        })
    except Exception as exc:
        logger.exception("web_search failed (backend=%s)", backend)
        return json.dumps({"error": f"Search failed: {exc}"})


async def _web_extract_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Extract content from web page URLs via the configured backend.

    Dispatches to Tavily or Exa based on ``_get_backend()``.
    Returns a JSON string with ``{results: [{url, title, content, error?}]}``.
    """
    urls = arguments.get("urls", [])
    if not isinstance(urls, list):
        urls = []
    urls = urls[:_MAX_EXTRACT_URLS]  # Cap at 5 URLs per call

    if not urls:
        return json.dumps({"error": "No URLs provided."})

    backend = _get_backend()

    # Check that the backend is available before dispatching
    if not _is_backend_available(backend):
        env_var = "TAVILY_API_KEY" if backend == "tavily" else "EXA_API_KEY"
        help_url = (
            "https://app.tavily.com/home" if backend == "tavily" else "https://exa.ai"
        )
        return json.dumps({
            "error": "not_configured",
            "message": (
                f"Web extract requires the {env_var} environment variable. "
                f"Get your API key at {help_url}"
            ),
        })

    try:
        if backend == "exa":
            documents = await _exa_extract(urls)
        else:
            # Default: tavily
            documents = await _tavily_extract(urls)

        logger.info("Extracted content from %d page(s)", len(documents))
        return _build_extract_result(documents, process_content=True)

    except httpx.HTTPStatusError as exc:
        logger.error("%s extract HTTP error: %s", backend, exc)
        return json.dumps({
            "error": f"{backend.title()} API error: {exc.response.status_code}",
            "message": str(exc),
        })
    except Exception as exc:
        logger.exception("web_extract failed (backend=%s)", backend)
        return json.dumps({"error": f"Extract failed: {exc}"})


# ─── Web crawl handler ──────────────────────────────────────────────────────

async def _web_crawl_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Crawl a website via the Tavily crawl API.

    Sends a POST to ``/crawl`` with configurable depth and optional
    instructions.  Exa does not support crawling, so this always uses
    Tavily.  Returns a JSON string with ``{results: [{url, title,
    content, error?}]}``.
    """
    url = arguments.get("url", "").strip()
    if not url:
        return json.dumps({"error": "No URL provided."})

    instructions = arguments.get("instructions")
    depth = arguments.get("depth", "basic")
    if depth not in ("basic", "advanced"):
        depth = "basic"

    # Crawl only supported on Tavily
    if not _has_env("TAVILY_API_KEY"):
        return json.dumps({
            "error": "not_configured",
            "message": (
                "Web crawl requires the TAVILY_API_KEY environment variable. "
                "Get your API key at https://app.tavily.com/home  "
                "Alternatively, use web_search + web_extract instead."
            ),
        })

    # Ensure URL has protocol
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
        logger.info("Added https:// prefix to URL: %s", url)

    try:
        logger.info("Tavily crawl: %s (depth=%s)", url, depth)
        payload: Dict[str, Any] = {
            "url": url,
            "limit": 20,
            "extract_depth": depth,
        }
        if instructions:
            payload["instructions"] = instructions

        raw = await _tavily_request("crawl", payload)
        documents = _normalize_tavily_documents(raw, fallback_url=url)

        logger.info("Crawled %d page(s)", len(documents))
        return _build_extract_result(documents, process_content=True)

    except httpx.HTTPStatusError as exc:
        logger.error("Tavily crawl HTTP error: %s", exc)
        return json.dumps({
            "error": f"Tavily API error: {exc.response.status_code}",
            "message": str(exc),
        })
    except Exception as exc:
        logger.exception("web_crawl failed")
        return json.dumps({"error": f"Crawl failed: {exc}"})


# ─── Public helpers ──────────────────────────────────────────────────────────

def check_web_api_key() -> bool:
    """Check whether the configured web backend is available.

    Returns True when at least one backend has its API key set.
    """
    configured = (os.getenv("WEB_BACKEND") or "").lower().strip()
    if configured in ("exa", "tavily"):
        return _is_backend_available(configured)
    return any(
        _is_backend_available(backend)
        for backend in ("tavily", "exa")
    )


def get_active_backend() -> Optional[str]:
    """Return the name of the currently active backend, or None.

    Useful for diagnostic / status checks.
    """
    backend = _get_backend()
    if _is_backend_available(backend):
        return backend
    return None


def get_supported_backends() -> List[str]:
    """Return the list of supported backend names."""
    return ["tavily", "exa"]


def get_available_backends() -> List[str]:
    """Return the list of backends that are currently configured and usable."""
    return [b for b in get_supported_backends() if _is_backend_available(b)]


# ─── Registration ────────────────────────────────────────────────────────────

WEB_SEARCH_SCHEMA_PARAMS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "The search query to look up on the web",
        },
    },
    "required": ["query"],
}

WEB_EXTRACT_SCHEMA_PARAMS = {
    "type": "object",
    "properties": {
        "urls": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of URLs to extract content from (max 5 URLs per call)",
            "maxItems": 5,
        },
    },
    "required": ["urls"],
}

WEB_CRAWL_SCHEMA_PARAMS = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "The base URL to crawl (can include or exclude https://)",
        },
        "instructions": {
            "type": "string",
            "description": "Instructions for what to crawl/extract using LLM intelligence (optional)",
        },
        "depth": {
            "type": "string",
            "enum": ["basic", "advanced"],
            "description": 'Depth of extraction ("basic" or "advanced", default: "basic")',
        },
    },
    "required": ["url"],
}


def register(registry: ToolRegistry) -> None:
    """Register web_search, web_extract, and web_crawl tools."""
    registry.register(
        name="web_search",
        schema=ToolSchema(
            name="web_search",
            description=(
                "Search the web for information on any topic. Returns up to 5 "
                "relevant results with titles, URLs, and descriptions."
            ),
            parameters=WEB_SEARCH_SCHEMA_PARAMS,
        ),
        handler=_web_search_handler,
        toolset="web",
        max_result_size=100_000,
    )

    registry.register(
        name="web_extract",
        schema=ToolSchema(
            name="web_extract",
            description=(
                "Extract content from web page URLs. Returns page content in "
                "markdown format. Also works with PDF URLs (arxiv papers, "
                "documents, etc.) -- pass the PDF link directly and it converts "
                "to markdown text. Pages under 5000 chars return full markdown; "
                "larger pages are LLM-summarized and capped at ~5000 chars per "
                "page. Pages over 2M chars are refused. If a URL fails or times "
                "out, use the browser tool to access it instead."
            ),
            parameters=WEB_EXTRACT_SCHEMA_PARAMS,
        ),
        handler=_web_extract_handler,
        toolset="web",
        max_result_size=100_000,
    )

    registry.register(
        name="web_crawl",
        schema=ToolSchema(
            name="web_crawl",
            description=(
                "Crawl a website with specific instructions. Returns content "
                "from multiple pages in markdown format. Useful for exploring "
                "documentation sites, finding specific information across a "
                "site, or extracting structured data. Uses Tavily backend."
            ),
            parameters=WEB_CRAWL_SCHEMA_PARAMS,
        ),
        handler=_web_crawl_handler,
        toolset="web",
        max_result_size=100_000,
    )
