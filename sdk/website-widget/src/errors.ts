/**
 * Error taxonomy for the website-widget SDK.
 *
 * Consumers should not have to parse strings to tell "the key is wrong"
 * apart from "the network blinked" -- different remediations apply.
 * Every error thrown or emitted by the SDK derives from
 * :class:`SurogatesError` so a single ``instanceof`` catch is always
 * safe; the subclasses encode the specific failure mode.
 */

export class SurogatesError extends Error {
  /** HTTP status code when the error originated from a server response. */
  readonly status?: number;
  /** Raw server detail string, if any. */
  readonly detail?: string;

  constructor(message: string, opts?: { status?: number; detail?: string; cause?: unknown }) {
    super(message);
    this.name = 'SurogatesError';
    if (opts?.status !== undefined) this.status = opts.status;
    if (opts?.detail !== undefined) this.detail = opts.detail;
    if (opts?.cause !== undefined) (this as { cause?: unknown }).cause = opts.cause;
  }
}

/**
 * The publishable key is invalid, expired, or the request Origin is not
 * in the agent's allow-list.  Non-retryable: the embed is misconfigured
 * and no amount of backoff will help.
 */
export class SurogatesAuthError extends SurogatesError {
  constructor(message: string, opts?: { status?: number; detail?: string }) {
    super(message, opts);
    this.name = 'SurogatesAuthError';
  }
}

/**
 * Rate-limited (HTTP 429) or the per-session message/token cap has
 * been reached.  Retryable after a backoff; the SDK exposes this as a
 * distinct type so consumers can show a "slow down" indicator instead
 * of a generic failure.
 */
export class SurogatesRateLimitError extends SurogatesError {
  /** Seconds the server asked us to wait, if it included a Retry-After header. */
  readonly retryAfter?: number;

  constructor(message: string, opts?: { status?: number; retryAfter?: number; detail?: string }) {
    super(message, opts);
    this.name = 'SurogatesRateLimitError';
    if (opts?.retryAfter !== undefined) this.retryAfter = opts.retryAfter;
  }
}

/**
 * The protocol itself is the problem -- the SDK received a response it
 * can't interpret, or a cookie/CSRF invariant was violated.  Indicates
 * an SDK-server version mismatch or a corrupt response; raises the
 * priority of whatever error reporting channel the host app has.
 */
export class SurogatesProtocolError extends SurogatesError {
  constructor(message: string, opts?: { status?: number; detail?: string; cause?: unknown }) {
    super(message, opts);
    this.name = 'SurogatesProtocolError';
  }
}

/**
 * Transport-level failure: network drop, CORS preflight refusal, DNS,
 * browser offline, 5xx from an ingress/load balancer.  Retryable by
 * design; the SDK does not auto-retry today -- the caller should back
 * off and re-issue the operation (typically by re-invoking
 * ``runAgent``).  A future revision may add internal exponential
 * backoff; consumers should continue to surface these errors to the
 * user rather than assume they'll be swallowed.
 */
export class SurogatesNetworkError extends SurogatesError {
  constructor(message: string, opts?: { cause?: unknown }) {
    super(message, opts);
    this.name = 'SurogatesNetworkError';
  }
}
