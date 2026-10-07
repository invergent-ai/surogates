// The app's own sign-in to its agent: who is signed in, and the refresh token that keeps
// them signed in, sealed by the OS secret store as the device token is. Access tokens live
// only in memory, for the app's own calls to the agent: adding this computer, restoring it,
// and giving the window's web client a session of its own. The page never sees them.

import { rmSync } from "node:fs";

import type { DesktopAccount } from "../../../web/src/lib/desktop-bridge-contract.js";
import { report } from "../report.js";
import { type SecretStore, seal, seals, unseal } from "./credentials.js";
import { type Fetch, OAuthError, refreshTokens, revokeTokens, type Tokens } from "./oauth.js";
import { readState, writeState } from "./state-file.js";

export interface SignedIn {
  origin: string;
  agentId: string;
  account: DesktopAccount;
  authTime: number; // when the user signed in, in seconds since the epoch
  refreshToken: string;
}

// The agent asks for a sign-in from its last 10 minutes to add or restore a computer; a minute less here, for the time a call takes.
export const RECENT_MS = 9 * 60_000;
// How long a sign-in lasts at the agent, from the browser sign-in (FAMILY_LIFETIME there).
export const SIGN_IN_LIFETIME_MS = 30 * 86_400_000;
// An access token this close to its end is refreshed first.
const MARGIN_MS = 60_000;
const REQUEST_TIMEOUT_MS = 15_000;

interface Stored extends Omit<SignedIn, "refreshToken"> {
  sealed?: string;
  plain?: string;
}

function usable(value: unknown): value is Stored {
  const { origin, agentId, account, authTime, sealed, plain } = (typeof value === "object" && value !== null ? value : {}) as Record<string, unknown>;
  const { name, email, userId, orgId } = (typeof account === "object" && account !== null ? account : {}) as Record<string, unknown>;
  return typeof origin === "string" && typeof agentId === "string" && typeof authTime === "number"
    && [name, email, userId, orgId].every((field) => typeof field === "string")
    && (typeof sealed === "string" || typeof plain === "string");
}

export class SessionStore {
  constructor(
    private readonly path: string,
    private readonly secrets: SecretStore,
    // A file that cannot be read, or a token the secret store cannot open: the app is signed out.
    private readonly onError: (error: unknown) => void,
  ) {}

  get(): SignedIn | null {
    const stored = readState<unknown>(this.path, null, (error) => report(this.onError, error));
    if (stored === null) return null;
    if (!usable(stored)) {
      report(this.onError, new Error(`${this.path} does not hold a sign-in Surogate can use, so it is signed out`));
      return null;
    }
    const { sealed, plain, ...signedIn } = stored;
    let refreshToken: string;
    try {
      refreshToken = unseal(this.secrets, { sealed, plain });
    } catch (error) {
      report(this.onError, error);
      return null;
    }
    // A token kept as it is while this computer had no secret store is sealed once it has one. Best
    // effort: one that cannot be sealed is no less safe than it was, so it is said, kept as it is,
    // and sealed at a later read.
    if (plain !== undefined && seals(this.secrets)) {
      try {
        this.save({ ...signedIn, refreshToken });
      } catch (error) {
        report(this.onError, error);
      }
    }
    return { ...signedIn, refreshToken };
  }

  save(signedIn: SignedIn): void {
    const { refreshToken, ...rest } = signedIn;
    writeState(this.path, { ...rest, ...seal(this.secrets, refreshToken) }, 0o600);
  }

  clear(): void {
    rmSync(this.path, { force: true });
  }

  // Whether the refresh token is kept as it is: the shell then says credentials here are not encrypted.
  unencrypted(): boolean {
    const stored = readState<unknown>(this.path, null, () => {});
    return usable(stored) && stored.plain !== undefined;
  }
}

export interface SessionOptions {
  store: SessionStore;
  fetch: Fetch;
  // The agent ended this sign-in (a refresh it refused): the app is signed out, and says *why*.
  onEnded(why: string): void;
  now?: () => number;
}

// When the agent issued *accessToken*, by its own clock, in seconds; null when it does not say.
function issuedAt(accessToken: string): number | null {
  try {
    const { iat } = JSON.parse(Buffer.from(accessToken.split(".")[1] ?? "", "base64url").toString()) as { iat?: unknown };
    return typeof iat === "number" ? iat : null;
  } catch {
    return null;
  }
}

export class DesktopSession {
  private access: { token: string; expiresAt: number } | null = null;
  private refreshing: Promise<string> | null = null;
  private ended = false;
  // How far the agent's clock is ahead of this computer's, from its last access token: a sign-in's
  // age is the agent's to measure. Until a token says, this computer's clock stands in.
  private skewMs = 0;

  /** *first* holds the tokens of a sign-in that just happened; a session read back at launch has none. */
  constructor(private signedIn: SignedIn, private readonly options: SessionOptions, first?: Tokens) {
    if (first) this.took(first);
  }

  get account(): DesktopAccount {
    return this.signedIn.account;
  }

  get origin(): string {
    return this.signedIn.origin;
  }

  /** Whether the agent would still take this sign-in as recent enough to add or restore a computer. */
  recent(): boolean {
    return this.agentNow() - this.signedIn.authTime * 1000 < RECENT_MS;
  }

  /** An access token for the agent: the one in hand, or one refreshed once for every caller waiting. */
  accessToken(): Promise<string> {
    const now = (this.options.now ?? Date.now)();
    if (this.ended) return Promise.reject(new OAuthError("invalid_grant", "Signed out of the agent"));
    if (this.access && this.access.expiresAt - MARGIN_MS > now) return Promise.resolve(this.access.token);
    // One refresh at a time: a refresh token spent twice ends the whole sign-in.
    this.refreshing ??= this.refresh().finally(() => {
      this.refreshing = null;
    });
    return this.refreshing;
  }

  /** *path* on the agent, as the signed-in user. */
  async api(path: string, init: RequestInit = {}): Promise<Response> {
    const url = new URL(path, this.signedIn.origin);
    if (url.origin !== this.signedIn.origin) throw new Error(`${path} is not on the agent`);
    const headers = new Headers(init.headers);
    headers.set("authorization", `Bearer ${await this.accessToken()}`);
    return this.options.fetch(url.href, {
      ...init, headers, signal: init.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  }

  /** Sign out: forget the sign-in here, then end it at the agent. */
  async end(): Promise<void> {
    this.ended = true;
    this.access = null;
    this.options.store.clear();
    await revokeTokens(this.signedIn.origin, this.signedIn.refreshToken, this.options.fetch);
  }

  private async refresh(): Promise<string> {
    let tokens: Tokens;
    try {
      tokens = await refreshTokens(this.signedIn.origin, this.signedIn.refreshToken, this.options.fetch, this.options.now);
    } catch (error) {
      if (error instanceof OAuthError && error.code === "invalid_grant" && !this.ended) {
        this.ended = true;
        this.options.store.clear();
        const aged = this.agentNow() - this.signedIn.authTime * 1000 >= SIGN_IN_LIFETIME_MS;
        this.options.onEnded(aged
          ? "Your sign-in is 30 days old: sign in again in your browser"
          : "The agent ended this sign-in: sign in again in your browser");
      }
      throw error;
    }
    // Signed out meanwhile: the sign-in is over, for whoever was waiting too.
    if (this.ended) throw new OAuthError("invalid_grant", "Signed out of the agent");
    // Kept before it is used: the spent token never works again, so a crash must not lose its successor.
    this.signedIn = { ...this.signedIn, refreshToken: tokens.refreshToken };
    this.options.store.save(this.signedIn);
    this.took(tokens);
    return tokens.accessToken;
  }

  private took(tokens: Tokens): void {
    this.access = { token: tokens.accessToken, expiresAt: tokens.expiresAt };
    const iat = issuedAt(tokens.accessToken);
    if (iat !== null) this.skewMs = iat * 1000 - (this.options.now ?? Date.now)();
  }

  private agentNow(): number {
    return (this.options.now ?? Date.now)() + this.skewMs;
  }
}

/** Who *accessToken* signs in as, from the agent's GET /api/v1/auth/me. */
export async function accountOf(origin: string, accessToken: string, fetch: Fetch): Promise<DesktopAccount> {
  const response = await fetch(new URL("/api/v1/auth/me", origin).href, {
    headers: { authorization: `Bearer ${accessToken}` },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(`The agent did not say who signed in (HTTP ${response.status})`);
  const me = (await response.json().catch(() => null)) as Record<string, unknown> | null;
  const { id, org_id: orgId, email, display_name: name } = me ?? {};
  if (typeof id !== "string" || typeof orgId !== "string" || typeof email !== "string") {
    throw new Error("The agent's answer about who signed in is not one Surogate understands");
  }
  return { name: typeof name === "string" && name !== "" ? name : email, email, userId: id, orgId };
}
