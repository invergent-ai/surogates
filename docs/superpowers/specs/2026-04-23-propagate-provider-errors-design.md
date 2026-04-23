# Propagate Model Provider Errors to the UI

**Status:** Approved for implementation
**Date:** 2026-04-23
**Author:** brainstorm with flavius

## Problem

When a model provider (OpenAI-compatible endpoint behind OpenRouter, local LM Studio, Anthropic, etc.) returns an error, the current behaviour is:

1. The harness retries internally (`llm_call.py`).
2. If retries are exhausted, the LLM call raises, the harness emits `harness.crash`, and the exception propagates to the orchestrator.
3. The orchestrator (`dispatcher.py`) retries the harness up to 3 times with exponential back-off.
4. After all retries fail, it emits `session.fail` with a raw `error` string and a `traceback`.

The frontend listens for `session.fail` and marks the last assistant message as `status: "error"`. But when the provider fails **before** any `llm.response` is emitted, there is no assistant message to mark. `findLastAssistantIndex` returns `-1` and nothing is rendered. The shimmer stops, and the user sees a frozen chat with no explanation.

Additionally, even when an assistant slot does exist, the frontend never reads `event.data.error`, so the actual reason is invisible.

Real-world example — session `350f79a6-5fe5-4097-b60e-2527d96cb194`:

```
46095 user.message "continue"
46096 harness.wake
46097 llm.request
46098 harness.crash  (retry 1)
46099 harness.crash
46100 harness.wake   (orchestrator retry 2)
46101 llm.request
46102 harness.crash  (retry 2)
...
46108 session.fail   reason=max_retries_exhausted
```

Underlying cause: `openai.APIError: Provider returned error` during streaming, wrapped as `ValueError: Invalid LLM response: response is None or has no choices`. User-facing text ends up being either nothing, or this gibberish.

## Goals

- Make provider failures visible in the chat UI with a human-readable title and optional raw detail.
- Show the user that the system is working through transient retries, not frozen.
- Let the user manually retry a failed session with one click.
- Classify **every** `harness.crash` — not just LLM errors — so database, storage, governance, network, and unknown failures all get meaningful UI treatment.

## Non-goals

- Server-side auto-retry beyond the existing 3 attempts.
- Partial replay (e.g. "skip the failed turn and continue").
- Classifying individual tool result errors inside a turn (already surfaced by tool result UI).
- Retry/resume buttons for any state other than `failed` (and existing `paused`).
- Changes to retry counts, backoff timing, or the resilience paths in `llm_call.py`.

## Architecture

Single concern: classify exceptions once on the backend, carry structured fields on the existing `harness.crash` and `session.fail` events, and render them on the frontend. Add one new REST endpoint and one new UI component.

No new event types. No schema migrations. The existing `error` and `traceback` fields stay for backward compatibility and debugging.

## Backend

### Module: `surogates/harness/error_classify.py` (new)

Pure, stateless classifier. One public function:

```python
from dataclasses import dataclass

ErrorCategory = Literal[
    "provider_error",
    "rate_limit",
    "auth_failed",
    "context_overflow",
    "network",
    "timeout",
    "invalid_response",
    "tool_error",
    "storage_error",
    "database_error",
    "governance_denied",
    "unknown",
]

@dataclass(frozen=True)
class ErrorInfo:
    category: ErrorCategory
    title: str       # user-facing, one line, no jargon
    detail: str      # trimmed raw str(exc) (≤500 chars)
    retryable: bool  # UI hint: does it make sense for the user to retry?

def classify_harness_error(exc: BaseException) -> ErrorInfo: ...
```

Classification order (first match wins):

1. **LLM-shaped errors** — inspect `openai.APIError`, `openai.RateLimitError`, `openai.AuthenticationError`, `anthropic.APIError`, `anthropic.RateLimitError`, etc. Fall back to `getattr(exc, "status_code", None)` → 401/403 = auth, 429 = rate, 413 = payload, 5xx = provider.
2. **Context overflow** — `ValueError("Invalid LLM response...")` and any message containing the phrases already enumerated in `llm_call.py`'s `_CONTEXT_PHRASES` when the wrapper is a context-related one.
3. **Invalid response** — `ValueError` from `call_llm_non_streaming` for empty/malformed responses (current session's exact case).
4. **Network/timeout** — `asyncio.TimeoutError`, `httpx.ConnectError`, `httpx.ReadError`, `httpx.RemoteProtocolError`, `ConnectionError`.
5. **Database** — `sqlalchemy.exc.OperationalError`, `sqlalchemy.exc.DBAPIError`, `asyncpg.exceptions.*` (anything whose module starts with `asyncpg` or `sqlalchemy`).
6. **Storage** — `botocore.exceptions.ClientError`, `botocore.exceptions.EndpointConnectionError`, any `aioboto3`-raised error.
7. **Governance** — any exception whose type name contains `Policy` or `Governance` and has `denied`/`deny` semantics (grep the governance package for the concrete class).
8. **Fallback** — `unknown` with the first line of `str(exc)` (or the class name if empty) as detail.

Fixed title table:

| Category | Title |
|---|---|
| `provider_error` | "The model provider returned an error." |
| `rate_limit` | "Rate limit reached with the model provider." |
| `auth_failed` | "Authentication with the model provider failed." |
| `context_overflow` | "Conversation is too long for the selected model." |
| `network` | "Network error reaching the model provider." |
| `timeout` | "The model provider timed out." |
| `invalid_response` | "The model returned an empty or malformed response." |
| `tool_error` | "A tool failed during execution." |
| `storage_error` | "Workspace storage is unavailable." |
| `database_error` | "Session storage is unavailable." |
| `governance_denied` | "Action blocked by governance policy." |
| `unknown` | "The session failed due to an internal error." |

`retryable`:

- `False` for `auth_failed`, `governance_denied`, `context_overflow` (user must fix configuration/content).
- `True` for everything else.

`detail` is `str(exc)` truncated to 500 chars on the last whitespace boundary. The full traceback stays in the existing `traceback` field; `detail` is purely the message line.

### Emission sites to update

Three call sites add the new fields alongside existing ones. No event-type changes, no schema migration.

**`surogates/harness/loop.py:547-562`** (top-level `wake()` handler):

```python
except Exception as exc:
    logger.exception("Harness crash for session %s", session_id)
    info = classify_harness_error(exc)
    try:
        await self._store.emit_event(
            session_id,
            EventType.HARNESS_CRASH,
            {
                "worker_id": self._worker_id,
                "error": traceback.format_exc()[-2000:],   # existing
                "error_category": info.category,           # NEW
                "error_title": info.title,                 # NEW
                "error_detail": info.detail,               # NEW
                "retryable": info.retryable,               # NEW
            },
        )
    except Exception:
        logger.exception("Failed to emit HARNESS_CRASH event for session %s", session_id)
    ...
```

**`surogates/harness/loop.py:879-894`** (LLM call failure in `_run_loop`):

```python
except Exception as exc:
    logger.exception("LLM call failed for session %s (iteration %d)", session.id, iteration)
    info = classify_harness_error(exc)
    await self._store.emit_event(
        session.id,
        EventType.HARNESS_CRASH,
        {
            "worker_id": self._worker_id,
            "error": f"LLM call failed: {exc}",   # existing
            "iteration": iteration,               # existing
            "error_category": info.category,     # NEW
            "error_title": info.title,           # NEW
            "error_detail": info.detail,         # NEW
            "retryable": info.retryable,         # NEW
        },
    )
    raise
```

**`surogates/orchestrator/dispatcher.py:250-260`** (final `SESSION_FAIL`):

```python
info = classify_harness_error(exc)
await self.session_store.emit_event(
    session_id,
    EventType.SESSION_FAIL,
    {
        "reason": "max_retries_exhausted",            # existing
        "error": str(exc),                            # existing
        "traceback": traceback.format_exc()[-2000:],  # existing
        "attempts": _MAX_RETRIES,                     # existing
        "error_category": info.category,              # NEW
        "error_title": info.title,                    # NEW
        "error_detail": info.detail,                  # NEW
        "retryable": info.retryable,                  # NEW
    },
)
```

### New REST endpoint: `POST /v1/sessions/{id}/retry`

File: `surogates/api/routes/sessions.py` (new handler, next to `resume_session`).

```python
@router.post("/sessions/{session_id}/retry", response_model=Session)
async def retry_session(
    session_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> Session:
    """Retry a failed (or paused) session. Re-enqueues it for wake()."""
    store = _get_session_store(request)
    session = await _get_session_for_tenant(request, session_id, tenant)

    if session.status not in ("failed", "paused"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot retry session in '{session.status}' state.",
        )

    await store.emit_event(
        session_id,
        EventType.SESSION_RESUME,
        {"source": "user_retry"},
    )
    await store.update_session_status(session_id, "active")
    await enqueue_session(request.app.state.redis, session.agent_id, session_id)

    return await store.get_session(session_id)
```

Reuses `SESSION_RESUME` with a `source` discriminator so audit queries can distinguish user-initiated retries from pause/resume flows. The cursor is unchanged, so the replay picks up the last user message and the harness calls the LLM again — same code path as a normal wake.

## Frontend

### `web/src/types/session.ts`

Extend the session event union so TypeScript knows about the new fields. Add an `ErrorInfo` type:

```ts
export interface ErrorInfo {
  category: string;
  title: string;
  detail: string;
  retryable: boolean;
}
```

Extend `ChatMessage` with an optional `errorInfo?: ErrorInfo`.

### `web/src/hooks/use-session-runtime.ts`

**Retry indicator state.** Add a new piece of state:

```ts
const [retryIndicator, setRetryIndicator] = useState<
  { title: string; detail: string; attempt: number } | null
>(null);
```

- On `harness.crash`: set `retryIndicator` from `event.data.error_title` / `error_detail`, increment attempt counter. No new thread message.
- On `llm.request` and `llm.response`: clear `retryIndicator` (retry succeeded or a new attempt began producing output).
- On `session.fail`: clear `retryIndicator` (final error takes over).

Expose `retryIndicator` from the hook return value.

**Handle `session.fail` when there is no assistant slot.** Current code silently no-ops when `findLastAssistantIndex` returns -1. Change:

```ts
case "session.fail": {
  terminalRef.current = true;
  const doneIdx = findLastAssistantIndex(next);
  const errorInfo: ErrorInfo | undefined = event.data.error_category ? {
    category: event.data.error_category,
    title: event.data.error_title,
    detail: event.data.error_detail,
    retryable: event.data.retryable ?? false,
  } : undefined;

  if (doneIdx >= 0) {
    next[doneIdx] = { ...next[doneIdx], status: "error", errorInfo };
  } else {
    // No assistant message yet — insert a standalone error bubble.
    next.push({
      id: `error-${event.id}`,
      role: "system",
      kind: "error",
      status: "error",
      errorInfo,
      content: "",
    });
  }
  setIsRunning(false);
  break;
}
```

**Retry action.** Expose `retrySession()` from the hook:

```ts
async function retrySession() {
  terminalRef.current = false;
  setRetryIndicator(null);
  await api.retrySession(sessionId);
  // The SSE stream will push session.resume / harness.wake / llm.request from here.
}
```

### `web/src/api/sessions.ts`

Add the client method:

```ts
export async function retrySession(sessionId: string): Promise<Session> {
  return fetch(`/v1/sessions/${sessionId}/retry`, {
    method: "POST",
    headers: authHeaders(),
  }).then(handleResponse);
}
```

### `web/src/components/chat/error-message.tsx` (new)

Reusable error bubble:

- Red left border (`border-l-4 border-red-500`), warning icon, `errorInfo.title` in bold.
- "Show details" collapsible disclosure rendering `errorInfo.detail` in a monospace block.
- Action row at the bottom:
  - **Retry** button (`variant="secondary"`) — only rendered when `errorInfo.retryable && onRetry != null`. Click → calls `onRetry()`, disables while pending.
  - **Dismiss** button (`variant="ghost"`) — hides the bubble locally (session state unchanged). The event remains in the log, so the bubble will reappear on page reload. This is acceptable — dismiss is a transient UI gesture, not a server-side acknowledgement.
- Props: `{ errorInfo: ErrorInfo; onRetry?: () => Promise<void>; onDismiss?: () => void }`

### `web/src/components/chat/chat-thread.tsx` (or equivalent renderer)

- When rendering an assistant message with `status === "error" && errorInfo`, render the `<ErrorMessage>` inline below the message body.
- When encountering a system-role message with `kind === "error"`, render the `<ErrorMessage>` standalone.
- Wire `onRetry={runtime.retrySession}` so the button calls the hook.

### Retry indicator banner

Above the composer (or at the bottom of the message list), render when `runtime.retryIndicator` is non-null:

```
⚠ Provider error — retrying (2/3)…    [▸ details]
```

The chevron expands to show `retryIndicator.detail`. Small, muted styling — it is a transient status line, not a message.

## Data flow

```
Provider fails
  ↓
llm_call.py raises after its own retries
  ↓
harness _run_loop catches → classify_harness_error(exc) → emit harness.crash
    data: { worker_id, error, iteration, error_category, error_title,
            error_detail, retryable }
  ↓
Orchestrator _process catches → backs off → retries
  ↓ (UI receives harness.crash via SSE)
  ↓ setRetryIndicator({ title, detail, attempt })
  ↓
Next llm.request emitted → clear retryIndicator
  ↓
... retry succeeds OR 3 attempts exhausted ...
  ↓
dispatcher classifies exc → emit SESSION_FAIL
    data: { reason, error, traceback, attempts, error_category,
            error_title, error_detail, retryable }
  ↓ (UI receives session.fail via SSE)
  ↓ find assistant slot or append standalone bubble
  ↓ attach errorInfo
  ↓ render <ErrorMessage> with Retry button (if retryable)
  ↓
User clicks Retry
  ↓
POST /v1/sessions/{id}/retry
  ↓ emit SESSION_RESUME{source: "user_retry"}, status→active, enqueue
  ↓
Worker wakes, replays from cursor, calls LLM again.
```

## Testing

### Backend unit tests

**`tests/harness/test_error_classify.py`** (new)

- `test_classifies_openai_api_error` — `openai.APIError` → `provider_error`, retryable.
- `test_classifies_rate_limit` — `openai.RateLimitError` → `rate_limit`, retryable.
- `test_classifies_auth_failure` — `openai.AuthenticationError` / HTTP 401 → `auth_failed`, non-retryable.
- `test_classifies_context_overflow` — raw `ValueError("Context size exceeded")` → `context_overflow`, non-retryable.
- `test_classifies_invalid_response` — the exact `ValueError("Invalid LLM response: response is None or has no choices")` from the reporting session → `invalid_response`, retryable.
- `test_classifies_network_error` — `httpx.ReadError` → `network`, retryable.
- `test_classifies_timeout` — `asyncio.TimeoutError` → `timeout`, retryable.
- `test_classifies_database_error` — `sqlalchemy.exc.OperationalError` → `database_error`, retryable.
- `test_classifies_storage_error` — `botocore.exceptions.ClientError` → `storage_error`, retryable.
- `test_classifies_unknown_fallback` — plain `RuntimeError("weird")` → `unknown`, retryable, detail contains "weird".
- `test_detail_truncated_to_500_chars` — very long exception messages get trimmed on a whitespace boundary.

### Backend emission tests

**`tests/harness/test_loop_crash_emission.py`** (extend if exists, else new)

- Patch `_run_loop` to raise a concrete `openai.APIError`. Assert the emitted `harness.crash` event has `error_category == "provider_error"`, `error_title`, `error_detail`, `retryable == True`.
- Same for the top-level wake handler with an `OperationalError`.

**`tests/orchestrator/test_dispatcher.py`** (extend)

- Force harness to raise 3 times. Assert the emitted `session.fail` has the classified fields.

### Backend API tests

**`tests/api/test_sessions_retry.py`** (new)

- `test_retry_failed_session` — session in `failed`, POST retry, asserts 200 + `SESSION_RESUME{source: "user_retry"}` event + status `active` + Redis `ZADD`.
- `test_retry_paused_session` — same contract for paused sessions (reuses `/resume` semantics via this endpoint too).
- `test_retry_active_session_409` — 409 Conflict.
- `test_retry_completed_session_409` — 409 Conflict.
- `test_retry_tenant_isolation` — retrying a session that belongs to a different org returns 404.
- `test_retry_nonexistent_session_404`.

### Frontend tests

**`web/src/hooks/__tests__/use-session-runtime.test.ts`** (extend)

- Feeding `harness.crash` events populates `retryIndicator`; feeding `llm.request` clears it.
- Feeding `session.fail` with no preceding assistant inserts a standalone error message with populated `errorInfo`.
- Feeding `session.fail` with an assistant message sets that message's `status === "error"` + `errorInfo`.
- `retrySession()` calls the API client and resets `terminalRef`.

**`web/src/components/chat/__tests__/error-message.test.tsx`** (new)

- Renders title + icon.
- "Show details" disclosure toggles visibility of detail.
- Retry button hidden when `retryable === false`.
- Retry button disabled while `onRetry` promise is pending.

## Rollout / backward compatibility

- The new event fields are purely additive. Existing consumers that ignore them continue to work.
- The new `/retry` endpoint has no conflicts — it is a new route.
- Old `session.fail` / `harness.crash` events in the database predating this change simply won't have `error_category`; the UI falls back to a generic title derived from the existing `error` string (or omits the error bubble detail cleanly).
- The frontend check `if (event.data.error_category)` gates the whole new UI path, so old sessions don't trigger broken rendering.

## File inventory

**New files:**

- `surogates/harness/error_classify.py` — classifier module
- `tests/harness/test_error_classify.py` — classifier unit tests
- `tests/api/test_sessions_retry.py` — retry endpoint tests
- `web/src/components/chat/error-message.tsx` — error bubble component
- `web/src/components/chat/__tests__/error-message.test.tsx` — component tests

**Modified files:**

- `surogates/harness/loop.py` — both `HARNESS_CRASH` emission sites
- `surogates/orchestrator/dispatcher.py` — `SESSION_FAIL` emission
- `surogates/api/routes/sessions.py` — new `/retry` handler
- `web/src/hooks/use-session-runtime.ts` — retry indicator state, fail handler, `retrySession()`
- `web/src/types/session.ts` — `ErrorInfo` type, event field additions
- `web/src/api/sessions.ts` — `retrySession()` client method
- `web/src/components/chat/<thread renderer>` — render `<ErrorMessage>` inline and standalone
- `web/src/components/chat/<composer area>` — render retry indicator banner
- `tests/harness/test_loop_crash_emission.py` — extend with classification assertions
- `tests/orchestrator/test_dispatcher.py` — extend with classification assertions
- `web/src/hooks/__tests__/use-session-runtime.test.ts` — extend

## Open questions (resolved)

1. Retries visible? → **C: single transient indicator** (not one bubble per retry).
2. Error detail verbosity? → **D2: classified title + raw detail in a collapsible**.
3. Retry button in the UI? → **yes**, gated on `retryable === true`.
4. Classify all `harness.crash` or only LLM failures? → **classify all**, using the broader category list.
