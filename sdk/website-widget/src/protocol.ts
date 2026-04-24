/**
 * HTTP + SSE wire layer.
 *
 * Pure functions that talk to the website channel's three endpoints,
 * isolated here so :class:`WebsiteAgentClient` can stay focused on
 * state-machine orchestration.  All fetches use ``credentials:
 * 'include'`` so the browser sends the HttpOnly session cookie that
 * ``bootstrap`` set -- without it the message/SSE/end routes return
 * 401 and the whole channel falls apart.
 *
 * SSE is handled by the native ``EventSource`` rather than a custom
 * fetch-based parser because the native implementation is zero bytes,
 * auto-reconnects on network drops, and respects browser-internal
 * backoff heuristics.  The one thing it can't do is resume with a
 * server-side cursor, so we manage the ``?after=N`` reopen logic
 * explicitly in :class:`WebsiteAgentClient`.
 */

import {
  CSRF_HEADER,
  PATH_BOOTSTRAP,
  PATH_END,
  PATH_EVENTS,
  PATH_MESSAGES,
  VERSION_HEADER,
  SDK_VERSION,
} from './constants.js';
import {
  SurogatesAuthError,
  SurogatesNetworkError,
  SurogatesProtocolError,
  SurogatesRateLimitError,
} from './errors.js';

export interface BootstrapResult {
  sessionId: string;
  csrfToken: string;
  expiresAt: number;
  agentName: string;
}

/**
 * Shape the website channel returns from ``POST /v1/website/sessions``.
 * Mirrors the pydantic ``BootstrapResponse`` server-side; kept separate
 * from :class:`BootstrapResult` (camelCase) so future protocol drift
 * only needs to move here, not through every consumer.
 */
interface RawBootstrapBody {
  session_id: string;
  csrf_token: string;
  expires_at: number;
  agent_name: string;
}

/** Fetch wrapper that normalises every HTTP failure into a typed error. */
async function doFetch(
  url: string,
  init: RequestInit & { headers?: Record<string, string> },
): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      credentials: 'include',
      headers: {
        ...init.headers,
        [VERSION_HEADER]: SDK_VERSION,
      },
    });
  } catch (cause) {
    // ``fetch`` only rejects on true network failure / CORS preflight
    // refusal.  HTTP error responses resolve.
    throw new SurogatesNetworkError('Network request failed', { cause });
  }
  return response;
}

/** Pull the ``detail`` string out of the server's error body, best-effort. */
async function extractDetail(response: Response): Promise<string | undefined> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    return typeof body.detail === 'string' ? body.detail : undefined;
  } catch {
    return undefined;
  }
}

async function raiseForStatus(response: Response, action: string): Promise<never> {
  const detail = await extractDetail(response);
  if (response.status === 401 || response.status === 403) {
    throw new SurogatesAuthError(`${action} rejected (${response.status})`, {
      status: response.status,
      ...(detail !== undefined && { detail }),
    });
  }
  if (response.status === 429) {
    const retryAfterHeader = response.headers.get('Retry-After');
    const retryAfter = retryAfterHeader ? Number(retryAfterHeader) : undefined;
    throw new SurogatesRateLimitError(`${action} rate-limited`, {
      status: response.status,
      ...(retryAfter !== undefined && Number.isFinite(retryAfter) && { retryAfter }),
      ...(detail !== undefined && { detail }),
    });
  }
  // 5xx responses are transport-layer hiccups -- an ingress, a load
  // balancer, or a restarting worker -- not SDK/server protocol
  // drift.  Surface them as :class:`SurogatesNetworkError` so
  // consumers treat them as retryable (matching the documented
  // semantics of that class) and don't escalate them as priority-1
  // diagnostics the way a genuine ``SurogatesProtocolError`` warrants.
  if (response.status >= 500) {
    throw new SurogatesNetworkError(
      `${action} failed (${response.status}${detail ? `: ${detail}` : ''})`,
    );
  }
  throw new SurogatesProtocolError(`${action} failed (${response.status})`, {
    status: response.status,
    ...(detail !== undefined && { detail }),
  });
}

/** ``POST /v1/website/sessions`` with a publishable key. */
export async function bootstrap(
  apiUrl: string,
  publishableKey: string,
): Promise<BootstrapResult> {
  const response = await doFetch(apiUrl + PATH_BOOTSTRAP, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${publishableKey}`,
      'Content-Type': 'application/json',
    },
  });
  // Accept any 2xx -- the route currently declares 201, but a future
  // server change to 200 (or a proxy that strips the status to 200)
  // must not silently break the SDK.  Non-2xx still funnels through
  // ``raiseForStatus`` to get the right typed error back.
  if (!response.ok) {
    await raiseForStatus(response, 'Bootstrap');
  }
  let body: RawBootstrapBody;
  try {
    body = (await response.json()) as RawBootstrapBody;
  } catch (cause) {
    throw new SurogatesProtocolError('Bootstrap returned non-JSON body', { cause });
  }
  if (!body || typeof body.session_id !== 'string' || typeof body.csrf_token !== 'string') {
    throw new SurogatesProtocolError('Bootstrap response missing required fields');
  }
  return {
    sessionId: body.session_id,
    csrfToken: body.csrf_token,
    expiresAt: Number(body.expires_at) || 0,
    agentName: body.agent_name ?? '',
  };
}

/** ``POST /v1/website/sessions/{id}/messages`` with cookie + CSRF. */
export async function sendMessage(
  apiUrl: string,
  sessionId: string,
  csrfToken: string,
  content: string,
): Promise<{ eventId: number }> {
  const response = await doFetch(apiUrl + PATH_MESSAGES(sessionId), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      [CSRF_HEADER]: csrfToken,
    },
    body: JSON.stringify({ content }),
  });
  // Any 2xx is a successful enqueue; the route declares 202 today
  // but we don't want to break if it's ever relaxed to 200.
  if (!response.ok) {
    await raiseForStatus(response, 'Send message');
  }
  const body = (await response.json().catch(() => ({}))) as { event_id?: number };
  return { eventId: Number(body.event_id ?? 0) };
}

/** ``POST /v1/website/sessions/{id}/end``.  Best-effort; swallows errors. */
export async function endSession(
  apiUrl: string,
  sessionId: string,
  csrfToken: string,
): Promise<void> {
  try {
    await doFetch(apiUrl + PATH_END(sessionId), {
      method: 'POST',
      headers: { [CSRF_HEADER]: csrfToken },
    });
  } catch {
    // The local cookie/state is always cleared by the caller regardless.
    // A failed end() is not worth surfacing -- the session will idle out.
  }
}

/** Open an SSE connection at ``?after=<cursor>``.  Caller owns the close. */
export function openEventStream(
  apiUrl: string,
  sessionId: string,
  cursor: number,
): EventSource {
  const url = apiUrl + PATH_EVENTS(sessionId, cursor);
  // ``withCredentials`` is the non-obvious flag that makes EventSource
  // send the session cookie cross-origin.  Without it the server sees
  // an unauthenticated request and returns 401, and the EventSource
  // quietly goes into reconnect loops without surfacing the reason.
  return new EventSource(url, { withCredentials: true });
}
