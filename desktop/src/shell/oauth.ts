// Surogate Desktop's sign-in, as an OAuth 2.0 native app (RFC 8252), the way Claude Code
// signs in. The system browser opens the agent's /oauth/authorize page with a PKCE
// challenge (RFC 7636, S256) and a loopback redirect, on a port this computer chose for
// that one attempt. The user signs in there and allows the desktop; the browser comes back
// to http://127.0.0.1:<port>/callback with a one-time code, which is exchanged, with its
// verifier, for an access token and a refresh token that rotates on every use.

import { createHash, randomBytes } from "node:crypto";
import { once } from "node:events";
import { createServer, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

export const CLIENT_ID = "surogate-desktop";
// How long the browser has to come back: signing in may mean finding a password.
export const SIGN_IN_TIMEOUT_MS = 10 * 60_000;
const REQUEST_TIMEOUT_MS = 15_000;

export interface Tokens {
  accessToken: string;
  refreshToken: string;
  expiresAt: number; // when the access token ends, in ms since the epoch
  authTime: number; // when the user signed in, in seconds since the epoch, as the agent recorded it
}

/** A sign-in that did not finish: *code* is the OAuth error, or what went wrong here. */
export class OAuthError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
  }
}

export type Fetch = (url: string, init: RequestInit) => Promise<Response>;

export interface BrowserSignIn {
  origin: string; // the agent's
  computer: string; // the consent page names this computer
  open(url: string): Promise<void>; // in the system browser
  fetch: Fetch;
  signal?: AbortSignal; // a sign-in started again, or the app quitting
  timeoutMs?: number;
  now?: () => number;
}

// What the browser tab shows: nothing it loads, and no address it passes on.
const HEADERS = {
  "cache-control": "no-store",
  "referrer-policy": "no-referrer",
  "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'",
  connection: "close",
};

const escaped = (text: string): string => text.replace(/[&<>"]/g, (found) => `&#${found.charCodeAt(0)};`);
const page = (text: string): string =>
  `<!doctype html><meta charset="utf-8"><title>Surogate</title><body style="font:15px system-ui;margin:4em auto;max-width:32em"><p>${escaped(text)}</p>`;

interface Callback {
  query: URLSearchParams;
  answer(status: number, text: string): void;
}

// The one callback the browser brings back, to this loopback address only, with this sign-in's
// *state*. Any other request is not found, and a callback with another state is refused while
// the wait goes on: whatever can reach this port ends nothing. What comes after the callback
// is told the sign-in is over.
function waitForCallback(server: Server, host: string, state: string, timeoutMs: number, signal?: AbortSignal): Promise<Callback> {
  return new Promise((resolve, reject) => {
    let taken = false;
    const finish = (): void => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", cancelled);
    };
    const fail = (error: OAuthError): void => {
      finish();
      server.closeAllConnections();
      reject(error);
    };
    const cancelled = (): void => fail(new OAuthError("cancelled", "Sign-in was cancelled"));
    const timer = setTimeout(() => fail(new OAuthError("timeout", "Sign-in was not finished in the browser in time")), timeoutMs);
    signal?.addEventListener("abort", cancelled, { once: true });
    if (signal?.aborted) cancelled();
    server.on("request", (request, response: ServerResponse) => {
      const url = new URL(request.url ?? "/", `http://${host}`);
      // A page that renamed another host 127.0.0.1 sends its own Host: it is not answered.
      if (request.headers.host !== host || request.method !== "GET" || url.pathname !== "/callback") {
        response.writeHead(404, HEADERS).end();
        return;
      }
      if (taken) {
        response.writeHead(410, { ...HEADERS, "content-type": "text/html; charset=utf-8" }).end(page("This sign-in is over."));
        return;
      }
      if (url.searchParams.get("state") !== state) {
        response.writeHead(400, { ...HEADERS, "content-type": "text/html; charset=utf-8" })
          .end(page("This sign-in is not one Surogate started, so it was refused."));
        return;
      }
      taken = true;
      finish();
      resolve({
        query: url.searchParams,
        answer: (status, text) => response.writeHead(status, { ...HEADERS, "content-type": "text/html; charset=utf-8" }).end(page(text)),
      });
    });
  });
}

async function postToken(origin: string, form: Record<string, string>, fetch: Fetch, now: () => number): Promise<Tokens> {
  const response = await fetch(new URL("/api/v1/auth/oauth/token", origin).href, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(form).toString(),
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  const body = (await response.json().catch(() => null)) as Record<string, unknown> | null;
  if (!response.ok) {
    const code = typeof body?.error === "string" ? body.error : `http_${response.status}`;
    throw new OAuthError(code, code === "invalid_grant" ? "The agent ended this sign-in: sign in again" : `The agent refused the sign-in (${code})`);
  }
  const { access_token: accessToken, refresh_token: refreshToken, expires_in: expiresIn, auth_time: authTime } = body ?? {};
  if (typeof accessToken !== "string" || typeof refreshToken !== "string" || typeof expiresIn !== "number" || typeof authTime !== "number") {
    throw new OAuthError("invalid_response", "The agent's answer to the sign-in is not one Surogate understands");
  }
  return { accessToken, refreshToken, expiresAt: now() + expiresIn * 1000, authTime };
}

/** Sign in through the system browser: the tokens, once the browser came back and the code was exchanged. */
export async function signInWithBrowser(options: BrowserSignIn): Promise<Tokens> {
  const now = options.now ?? Date.now;
  const verifier = randomBytes(32).toString("base64url");
  const challenge = createHash("sha256").update(verifier).digest("base64url");
  const state = randomBytes(24).toString("base64url");
  const loopback = createServer();
  loopback.listen(0, "127.0.0.1");
  await once(loopback, "listening");
  const host = `127.0.0.1:${(loopback.address() as AddressInfo).port}`;
  const redirectUri = `http://${host}/callback`;
  // However this attempt ends, its wait ends with it: a browser that failed to open leaves no timer behind.
  const ended = new AbortController();
  const signal = options.signal ? AbortSignal.any([options.signal, ended.signal]) : ended.signal;
  const callback = waitForCallback(loopback, host, state, options.timeoutMs ?? SIGN_IN_TIMEOUT_MS, signal);
  callback.catch(() => {});
  try {
    const authorize = new URL("/oauth/authorize", options.origin);
    authorize.search = new URLSearchParams({
      response_type: "code", client_id: CLIENT_ID, redirect_uri: redirectUri, state,
      code_challenge: challenge, code_challenge_method: "S256", computer: options.computer,
    }).toString();
    await options.open(authorize.href);
    const { query, answer } = await callback;
    try {
      const error = query.get("error");
      if (error !== null) {
        throw new OAuthError(error, error === "access_denied" ? "Sign-in was declined in the browser" : `The agent refused the sign-in (${error})`);
      }
      const code = query.get("code");
      if (!code) throw new OAuthError("invalid_request", "The browser came back without a sign-in code");
      const tokens = await postToken(options.origin, {
        grant_type: "authorization_code", client_id: CLIENT_ID, code, redirect_uri: redirectUri, code_verifier: verifier,
      }, options.fetch, now);
      answer(200, "Signed in. You can close this tab and return to Surogate.");
      return tokens;
    } catch (error) {
      answer(400, `Surogate is not signed in: ${error instanceof Error ? error.message : String(error)}`);
      throw error;
    }
  } finally {
    ended.abort();
    loopback.close();
  }
}

/** Spend *refreshToken* for new tokens: the refresh token is replaced, and the one given never works again. */
export function refreshTokens(origin: string, refreshToken: string, fetch: Fetch, now: () => number = Date.now): Promise<Tokens> {
  return postToken(origin, { grant_type: "refresh_token", client_id: CLIENT_ID, refresh_token: refreshToken }, fetch, now);
}

/** End the sign-in *refreshToken* belongs to, at the agent (RFC 7009). */
export async function revokeTokens(origin: string, refreshToken: string, fetch: Fetch): Promise<void> {
  const response = await fetch(new URL("/api/v1/auth/oauth/revoke", origin).href, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ token: refreshToken, token_type_hint: "refresh_token", client_id: CLIENT_ID }).toString(),
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (!response.ok) throw new OAuthError(`http_${response.status}`, `The agent did not end the sign-in (HTTP ${response.status})`);
}
