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
  PATH_PAIRING,
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

export interface PairingResult {
  agentId: string;
  apiWebUrl: string;
}

/**
 * Resolve a publishable key to its owning ``(agentId, apiWebUrl)`` via the
 * control-plane pairing endpoint, so a key-only embed can discover which
 * agent it is and where to bootstrap.  ``doFetchImpl`` is injectable for
 * tests; defaults to the global ``fetch``.
 */
export async function resolvePairing(
  pairingBase: string,
  publishableKey: string,
  doFetchImpl: typeof fetch = fetch,
): Promise<PairingResult> {
  const url = pairingBase.replace(/\/+$/, '') + PATH_PAIRING(publishableKey);
  let response: Response;
  try {
    response = await doFetchImpl(url, { method: 'GET' });
  } catch (cause) {
    throw new SurogatesNetworkError('Widget pairing request failed', { cause });
  }
  if (!response.ok) {
    throw new SurogatesAuthError('Unknown or inactive widget key', {
      status: response.status,
    });
  }
  let body: { agent_id?: string; api_web_url?: string };
  try {
    body = (await response.json()) as { agent_id?: string; api_web_url?: string };
  } catch (cause) {
    throw new SurogatesProtocolError('Pairing returned a non-JSON body', {
      cause,
    });
  }
  if (typeof body?.agent_id !== 'string' || typeof body?.api_web_url !== 'string') {
    throw new SurogatesProtocolError(
      'Pairing response missing agent_id / api_web_url',
    );
  }
  return { agentId: body.agent_id, apiWebUrl: body.api_web_url };
}

/**
 * Append ``?agent_id=`` (or ``&agent_id=``) when an explicit id is set.
 *
 * Per-agent deployments resolve the agent from the request ``Host``
 * subdomain on every call, so the embed never needs this.  Shared/
 * multi-tenant runtimes (and the Studio preview) aren't reached through
 * the per-agent host, and the server resolves the agent on *every*
 * website request (the rate limiter depends on the agent context), not
 * just bootstrap -- so the id must ride on all four endpoints.
 *
 * BRIDGE (not the long-term design): the publishable key already scopes to
 * one agent, so the deeper fix is server-side resolution of the agent from
 * the key (a key->agent registry), after which shared mode would need no
 * query param at all. Until that lands, every endpoint must thread the id.
 */
function withAgentId(path: string, agentId?: string): string {
  if (!agentId) return path;
  const sep = path.includes('?') ? '&' : '?';
  return `${path}${sep}agent_id=${encodeURIComponent(agentId)}`;
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

/** ``POST /v1/website/sessions`` with a publishable key.
 *
 * ``agentId`` is optional.  In a per-agent deployment the server resolves
 * the agent from the request ``Host`` subdomain, so the embed never needs
 * it.  Shared/multi-tenant runtimes (and the Studio live preview) that
 * aren't reached through the per-agent host can pass it explicitly; it is
 * appended as the ``?agent_id=`` query param the server's resolver accepts.
 * Only the bootstrap needs it -- subsequent calls are cookie-bound.
 */
export async function bootstrap(
  apiUrl: string,
  publishableKey: string,
  agentId?: string,
): Promise<BootstrapResult> {
  const response = await doFetch(apiUrl + withAgentId(PATH_BOOTSTRAP, agentId), {
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
  agentId?: string,
): Promise<{ eventId: number }> {
  const response = await doFetch(apiUrl + withAgentId(PATH_MESSAGES(sessionId), agentId), {
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
  agentId?: string,
): Promise<void> {
  try {
    await doFetch(apiUrl + withAgentId(PATH_END(sessionId), agentId), {
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
  agentId?: string,
): EventSource {
  const url = apiUrl + withAgentId(PATH_EVENTS(sessionId, cursor), agentId);
  // ``withCredentials`` is the non-obvious flag that makes EventSource
  // send the session cookie cross-origin.  Without it the server sees
  // an unauthenticated request and returns 401, and the EventSource
  // quietly goes into reconnect loops without surfacing the reason.
  return new EventSource(url, { withCredentials: true });
}
