"""Credential rotation, fallback activation, invalid tool recovery, and budget warnings.

Provides standalone functions for production-hardening features that
the harness delegates to:
- Credential pool rotation on 401/402/429 errors
- Fallback provider chain activation
- Invalid tool call detection (unknown tools, malformed JSON)
- Budget pressure warning injection
- Fuzzy tool name repair
- API error summarization (Cloudflare HTML pages, JSON body errors)
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from surogates.harness.budget import IterationBudget
    from surogates.harness.credentials import CredentialPool
    from surogates.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Two-tier budget warning thresholds.
#   - Caution  (70% used): nudge the model to start consolidating work.
#   - Warning  (90% used): urgent, must respond now.
BUDGET_CAUTION_THRESHOLD: float = 0.7   # 70% of budget used
BUDGET_WARNING_THRESHOLD: float = 0.9   # 90% of budget used


def try_rotate_credential(
    credential_pool: CredentialPool | None,
    llm_client: AsyncOpenAI,
    status_code: int,
    exc: Exception,
    *,
    error_context: dict[str, Any] | None = None,
) -> tuple[AsyncOpenAI | None, bool]:
    """Try to rotate to the next credential in the pool.

    Returns ``(new_client, True)`` if rotation succeeded, or
    ``(None, False)`` if no rotation was possible.

    On 401, first attempts ``try_refresh_current()`` before rotating.
    This gives the current credential a second chance (e.g. after an
    OAuth token refresh) before marking it as exhausted.
    """
    if credential_pool is None:
        return None, False

    # On 401, try refreshing the current credential first.
    if status_code == 401:
        refreshed = credential_pool.try_refresh_current()
        if refreshed is not None:
            from openai import AsyncOpenAI as _AsyncOpenAI
            new_client = _AsyncOpenAI(
                api_key=refreshed.runtime_api_key,
                base_url=refreshed.runtime_base_url or str(llm_client.base_url),
            )
            logger.info(
                "Credential refreshed (status=401) for %s",
                refreshed.label or refreshed.id[:8],
            )
            return new_client, True

    next_cred = credential_pool.mark_exhausted_and_rotate(
        status_code, str(exc)[:500], error_context=error_context,
    )
    if next_cred is None:
        return None, False
    # Swap the OpenAI client
    from openai import AsyncOpenAI as _AsyncOpenAI
    new_client = _AsyncOpenAI(
        api_key=next_cred.runtime_api_key,
        base_url=next_cred.runtime_base_url or str(llm_client.base_url),
    )
    logger.info(
        "Credential rotated (status=%d) to %s",
        status_code, next_cred.label or next_cred.id[:8],
    )
    return new_client, True


def try_activate_fallback(
    fallback_chain: list[dict],
    fallback_index: int,
    llm_client: AsyncOpenAI,
    primary_config: dict | None,
    current_model: str | None,
    fallback_activated: bool,
) -> tuple[AsyncOpenAI | None, str | None, int, dict | None, bool]:
    """Switch to the next fallback in the chain.

    Returns ``(new_client, new_model, new_index, primary_config, fallback_activated)``
    where ``new_client`` is ``None`` if no fallback was available.
    """
    if fallback_index >= len(fallback_chain):
        return None, None, fallback_index, primary_config, fallback_activated

    fb = fallback_chain[fallback_index]
    new_index = fallback_index + 1

    provider = fb.get("provider", "").strip()
    model = fb.get("model", "").strip()
    if not provider or not model:
        # skip invalid, try next recursively
        return try_activate_fallback(
            fallback_chain, new_index, llm_client,
            primary_config, current_model, fallback_activated,
        )

    # Save primary config on first fallback
    new_primary_config = primary_config
    if not fallback_activated:
        new_primary_config = {
            "llm_client": llm_client,
            "model": current_model,
        }

    # Create new client for fallback
    from openai import AsyncOpenAI as _AsyncOpenAI
    api_key = fb.get("api_key") or llm_client.api_key
    base_url = fb.get("base_url") or str(llm_client.base_url)

    new_client = _AsyncOpenAI(api_key=api_key, base_url=base_url)

    logger.info("Fallback activated: %s via %s", model, provider)
    return new_client, model, new_index, new_primary_config, True


def repair_tool_name(name: str, registry: ToolRegistry) -> str | None:
    """Attempt to repair a misspelled tool name.

    1. Lowercase
    2. Normalize hyphens/spaces to underscores
    3. Fuzzy match with difflib (cutoff=0.7)

    Returns the repaired name, or ``None`` if no match.
    """
    known = registry.tool_names

    # 1. Lowercase
    lowered = name.lower()
    if lowered in known:
        return lowered

    # 2. Normalize hyphens/spaces to underscores
    normalized = lowered.replace("-", "_").replace(" ", "_")
    if normalized in known:
        return normalized

    # 3. Fuzzy match
    matches = difflib.get_close_matches(normalized, sorted(known), n=1, cutoff=0.7)
    return matches[0] if matches else None


def find_invalid_tool_calls(
    tool_calls: list[dict[str, Any]],
    registry: ToolRegistry,
) -> list[tuple[dict[str, Any], str]]:
    """Return list of (tool_call, error_message) for invalid calls.

    A tool call is invalid if:
    - The tool name is not registered (and cannot be repaired via fuzzy match)
    - The arguments JSON is malformed

    When a tool name is unknown but can be repaired via :func:`repair_tool_name`,
    the tool call is updated in-place and not reported as invalid.
    """
    invalid: list[tuple[dict[str, Any], str]] = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        tool_name = fn.get("name", "")
        args_raw = fn.get("arguments", "")

        # Unknown tool -- attempt repair first
        if tool_name and not registry.has(tool_name):
            repaired = repair_tool_name(tool_name, registry)
            if repaired is not None:
                logger.info(
                    "Repaired tool name %r -> %r", tool_name, repaired,
                )
                fn["name"] = repaired
            else:
                available = ", ".join(sorted(registry.tool_names))
                invalid.append((tc, json.dumps({
                    "error": f"Unknown tool: {tool_name!r}. Available tools: {available}",
                })))
                continue

        # Malformed JSON arguments
        if args_raw:
            try:
                json.loads(args_raw)
            except json.JSONDecodeError as e:
                invalid.append((tc, json.dumps({
                    "error": f"Malformed JSON in tool arguments: {e}. "
                             f"Please fix and retry with valid JSON.",
                })))
                continue

    return invalid


def get_budget_warning(budget: IterationBudget) -> str | None:
    """Return a budget pressure string, or ``None`` if not yet needed.

    Two-tier system:
      - Caution (70% used): nudge to consolidate work.
      - Warning (90% used): urgent, must respond now.
    """
    if budget.max_total <= 0:
        return None
    progress = budget.used / budget.max_total
    remaining = budget.remaining
    if progress >= BUDGET_WARNING_THRESHOLD:
        return (
            f"[BUDGET WARNING: Iteration {budget.used}/{budget.max_total}. "
            f"Only {remaining} iteration(s) left. "
            "Provide your final response NOW. No more tool calls unless absolutely critical.]"
        )
    if progress >= BUDGET_CAUTION_THRESHOLD:
        return (
            f"[BUDGET: Iteration {budget.used}/{budget.max_total}. "
            f"{remaining} iterations left. Start consolidating your work.]"
        )
    return None


def inject_budget_warning(
    tool_results: list[dict],
    budget: IterationBudget,
) -> list[dict]:
    """If budget pressure is detected, inject a warning into the last tool result.

    Uses the two-tier system from :func:`get_budget_warning`.

    Injection strategy:
    - If the tool result content is JSON dict, inject a ``_budget_warning`` key.
    - Otherwise, append as plain text.

    The ``_budget_warning`` key is preferred because it is cleanly stripped
    on replay by :func:`surogates.harness.sanitize.strip_budget_warnings`.
    """
    if not tool_results:
        return tool_results

    budget_warning = get_budget_warning(budget)
    if budget_warning is None:
        return tool_results

    # Inject into the last tool result's content.
    last = tool_results[-1]
    last_content = last.get("content", "")

    try:
        parsed = json.loads(last_content)
        if isinstance(parsed, dict):
            parsed["_budget_warning"] = budget_warning
            tool_results[-1] = {
                **last,
                "content": json.dumps(parsed, ensure_ascii=False),
            }
        else:
            tool_results[-1] = {
                **last,
                "content": last_content + f"\n\n{budget_warning}",
            }
    except (json.JSONDecodeError, TypeError):
        tool_results[-1] = {
            **last,
            "content": last_content + f"\n\n{budget_warning}",
        }

    return tool_results


# ---------------------------------------------------------------------------
# Error diagnostic messages
# ---------------------------------------------------------------------------

# Provider-specific guidance for common error codes.
_ERROR_GUIDANCE: dict[int, dict[str, str]] = {
    401: {
        "openai": "Check your OPENAI_API_KEY. Visit https://platform.openai.com/api-keys",
        "anthropic": "Check your ANTHROPIC_API_KEY. Visit https://console.anthropic.com/account/keys",
        "openrouter": "Check your OPENROUTER_API_KEY. Visit https://openrouter.ai/keys",
        "default": "Authentication failed. Verify your API key is correct and not expired.",
    },
    402: {
        "openai": "OpenAI billing limit reached. Add credits at https://platform.openai.com/account/billing",
        "anthropic": "Anthropic billing limit reached. Check your plan at https://console.anthropic.com/settings/billing",
        "openrouter": "OpenRouter credits exhausted. Add credits at https://openrouter.ai/credits",
        "default": "Billing limit or quota exhausted. Check your provider account.",
    },
    429: {
        "default": "Rate limited. The harness will retry with backoff automatically.",
    },
    500: {
        "default": "Provider internal server error. Usually transient — retrying.",
    },
    502: {
        "default": "Provider gateway error. The upstream model may be overloaded.",
    },
    503: {
        "default": "Provider temporarily unavailable. Model may be starting up.",
    },
}


def get_error_diagnostic(
    status_code: int | None,
    exc: Exception,
    *,
    provider: str = "",
) -> str:
    """Return a human-readable diagnostic message for an LLM API error.

    Provides provider-specific guidance when available, falls back to
    generic advice. Used for logging and status event emission.
    """
    if status_code is None:
        # Network-level error (no HTTP status)
        exc_name = type(exc).__name__
        exc_msg = str(exc)[:200]
        return f"Network error ({exc_name}): {exc_msg}. Check connectivity to the LLM provider."

    guidance_map = _ERROR_GUIDANCE.get(status_code, {})
    provider_lower = provider.lower() if provider else ""

    # Try provider-specific guidance first
    for key in (provider_lower, "default"):
        if key in guidance_map:
            return f"HTTP {status_code}: {guidance_map[key]}"

    # Generic fallback
    exc_msg = str(exc)[:300]
    return f"HTTP {status_code}: {exc_msg}"


def summarize_api_error(error: Exception) -> str:
    """Extract a human-readable one-liner from an API error.

    Handles Cloudflare HTML error pages (502, 503, etc.) by pulling the
    ``<title>`` tag instead of dumping raw HTML.  Falls back to the SDK's
    structured ``body.error.message`` when available, or a truncated
    ``str(error)`` for everything else.
    """
    raw = str(error)

    # Cloudflare / proxy HTML pages: grab the <title> for a clean summary.
    if "<!DOCTYPE" in raw or "<html" in raw:
        m = re.search(r"<title[^>]*>([^<]+)</title>", raw, re.IGNORECASE)
        title = m.group(1).strip() if m else "HTML error page (title not found)"
        ray = re.search(r"Cloudflare Ray ID:\s*<strong[^>]*>([^<]+)</strong>", raw)
        ray_id = ray.group(1).strip() if ray else None
        status_code = getattr(error, "status_code", None)
        parts: list[str] = []
        if status_code:
            parts.append(f"HTTP {status_code}")
        parts.append(title)
        if ray_id:
            parts.append(f"Ray {ray_id}")
        return " \u2014 ".join(parts)

    # JSON body errors from OpenAI/Anthropic SDKs.
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        error_field = body.get("error")
        msg = (
            error_field.get("message")
            if isinstance(error_field, dict)
            else body.get("message")
        )
        if msg:
            status_code = getattr(error, "status_code", None)
            prefix = f"HTTP {status_code}: " if status_code else ""
            return f"{prefix}{msg[:300]}"

    # Fallback: truncate the raw string.
    status_code = getattr(error, "status_code", None)
    prefix = f"HTTP {status_code}: " if status_code else ""
    return f"{prefix}{raw[:500]}"


def extract_api_error_context(error: Exception) -> dict[str, Any]:
    """Extract structured rate-limit / error details from a provider error.

    Returns a dict that may contain:

    - ``reason``: error code string (e.g. ``"rate_limited"``)
    - ``message``: human-readable description
    - ``reset_at``: epoch timestamp when the rate limit resets
    """
    context: dict[str, Any] = {}

    body = getattr(error, "body", None)
    payload = None
    if isinstance(body, dict):
        payload = body.get("error") if isinstance(body.get("error"), dict) else body
    if isinstance(payload, dict):
        reason = payload.get("code") or payload.get("error")
        if isinstance(reason, str) and reason.strip():
            context["reason"] = reason.strip()
        message = payload.get("message") or payload.get("error_description")
        if isinstance(message, str) and message.strip():
            context["message"] = message.strip()
        for key in ("resets_at", "reset_at"):
            value = payload.get(key)
            if value not in (None, ""):
                context["reset_at"] = value
                break
        retry_after = payload.get("retry_after")
        if retry_after not in (None, "") and "reset_at" not in context:
            try:
                context["reset_at"] = time.time() + float(retry_after)
            except (TypeError, ValueError):
                pass

    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after and "reset_at" not in context:
            try:
                context["reset_at"] = time.time() + float(retry_after)
            except (TypeError, ValueError):
                pass
        ratelimit_reset = headers.get("x-ratelimit-reset")
        if ratelimit_reset and "reset_at" not in context:
            context["reset_at"] = ratelimit_reset

    if "message" not in context:
        raw_message = str(error).strip()
        if raw_message:
            context["message"] = raw_message[:500]

    if "reset_at" not in context:
        message = context.get("message") or ""
        if isinstance(message, str):
            delay_match = re.search(
                r"quotaResetDelay[:\s\"]+(\d+(?:\.\d+)?)(ms|s)",
                message,
                re.IGNORECASE,
            )
            if delay_match:
                value = float(delay_match.group(1))
                seconds = value / 1000.0 if delay_match.group(2).lower() == "ms" else value
                context["reset_at"] = time.time() + seconds
            else:
                sec_match = re.search(
                    r"retry\s+(?:after\s+)?(\d+(?:\.\d+)?)\s*(?:sec|secs|seconds|s\b)",
                    message,
                    re.IGNORECASE,
                )
                if sec_match:
                    context["reset_at"] = time.time() + float(sec_match.group(1))

    return context
