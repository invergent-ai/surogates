"""Constants used by the harness loop."""

from __future__ import annotations

# Default TTL (seconds) for lease acquisition and renewal.
_LEASE_TTL_SECONDS: int = 60

# Per-call ceiling for any pre-wake step that touches Hub (bundle
# list / read_text via ``resolve_agent_def``).  Without this an
# unhealthy Hub silently strands sessions: the worker hangs in the
# pre-wake setup, the lease never gets acquired, the orphan sweeper
# re-enqueues every 60s, and the next pickup hangs on the same call.
# Surfacing as a ``TimeoutError`` turns the next failure into a
# visible ``harness.crash`` (category=timeout) instead of an invisible
# zombie loop.
_PRE_WAKE_HUB_TIMEOUT_SECONDS: float = 30.0

# Renewal cadence.  Time-based (not iteration-based) so a long-running
# iteration (e.g. slow LLM call, streaming fallback) cannot let the lease
# expire and get stolen by another worker.  Must be well under
# ``_LEASE_TTL_SECONDS`` so a single missed tick still leaves the lease alive.
_LEASE_RENEWAL_INTERVAL_SECONDS: float = 20.0

# Upper bound on how long ``wake()`` will wait for fire-and-forget background
# tasks (e.g. title generation) before releasing the session lease.  The drain
# is best-effort: anything still pending after this is cancelled so the worker
# can release the lease promptly.  Slightly longer than the title generator's
# own 30 s timeout to give it room to finish cleanly.
_BACKGROUND_DRAIN_TIMEOUT_SECONDS: float = 35.0

# Retry / resilience constants
_MAX_LENGTH_CONTINUATIONS: int = 3
_MAX_CONSECUTIVE_INVALID_TOOL_CALLS: int = 3
_MAX_EMPTY_RESPONSE_RETRIES: int = 3
_LENGTH_CONTINUATION_PROMPT: str = (
    "[System: Your previous response was truncated by the output "
    "length limit. Continue exactly where you left off. Do not "
    "restart or repeat prior text. Finish the answer directly.]"
)
_EMPTY_RESPONSE_NUDGE: str = (
    "[System: Your previous response was empty. Re-read the user's "
    "request and act now. If the user asked for a visual or rendered "
    "output (SVG, HTML, chart, table, markdown document), invoke "
    "create_artifact — do NOT paste the content as a code fence.]"
)
_DYNAMIC_LOOP_EXCLUDED_TOOLS: frozenset[str] = frozenset({
    "cron_create",
    "cron_delete",
    "cron_list",
})
