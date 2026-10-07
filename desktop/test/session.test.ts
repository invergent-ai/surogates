import { existsSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { SecretStore } from "../src/shell/credentials.js";
import type { Fetch } from "../src/shell/oauth.js";
import { accountOf, DesktopSession, RECENT_MS, type SignedIn, SessionStore } from "../src/shell/session.js";

// Electron's safeStorage, as far as the store uses it: "sealing" reverses the text.
class Secrets implements SecretStore {
  constructor(public backend = "gnome_libsecret") {}
  isEncryptionAvailable(): boolean {
    return true;
  }
  getSelectedStorageBackend(): string {
    return this.backend;
  }
  encryptString(plain: string): Buffer {
    return Buffer.from([...plain].reverse().join(""));
  }
  decryptString(sealed: Buffer): string {
    return [...sealed.toString()].reverse().join("");
  }
}

const SIGNED_IN: SignedIn = {
  origin: "https://agent.example.com", agentId: "a", authTime: 1_700_000_000, refreshToken: "surg_rt_secret",
  account: { name: "Flavius Burca", email: "flavius@example.com", userId: "u", orgId: "o" },
};

let dir: string;
let path: string;
let errors: unknown[];

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "session-"));
  path = join(dir, "session.json");
  errors = [];
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

const store = (secrets: SecretStore = new Secrets()) => new SessionStore(path, secrets, (error) => errors.push(error));

describe("the sign-in, as this computer keeps it", () => {
  it("seals the refresh token, in a file only the user can read", () => {
    store().save(SIGNED_IN);
    expect(readFileSync(path, "utf8")).not.toContain("surg_rt_secret");
    expect(statSync(path).mode & 0o777).toBe(0o600);
    expect(store().get()).toEqual(SIGNED_IN);
    expect(store().unencrypted()).toBe(false);
  });

  it("keeps it as it is where the secret store protects nothing, says so, and seals it once there is one", () => {
    store(new Secrets("basic_text")).save(SIGNED_IN);
    expect(store(new Secrets("basic_text")).unencrypted()).toBe(true);
    expect(store().get()).toEqual(SIGNED_IN);
    expect(readFileSync(path, "utf8")).not.toContain("surg_rt_secret");
    expect(store().unencrypted()).toBe(false);
  });

  it("is signed out when the file holds something else, which is said", () => {
    store().save(SIGNED_IN);
    const broken = JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
    delete broken.account;
    writeFileSync(path, JSON.stringify(broken));
    expect(store().get()).toBeNull();
    expect(errors.map(String)).toEqual([expect.stringContaining("does not hold a sign-in Surogate can use")]);
  });
});

// The agent's token route as the session meets it: each refresh spends its token for the next.
function agent(refused = false) {
  const forms: Array<Record<string, string>> = [];
  const calls: Array<{ url: string; authorization: string | null }> = [];
  let release = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const fetch: Fetch = async (url, init) => {
    if (url.endsWith("/api/v1/auth/oauth/token") || url.endsWith("/api/v1/auth/oauth/revoke")) {
      forms.push(Object.fromEntries(new URLSearchParams(String(init.body))));
      await held;
      if (refused) return new Response(JSON.stringify({ error: "invalid_grant" }), { status: 400 });
      const n = forms.length;
      return new Response(JSON.stringify({ access_token: `at-${n}`, refresh_token: `rt-${n}`, expires_in: 1800, auth_time: 1_700_000_000 }));
    }
    calls.push({ url, authorization: new Headers(init.headers).get("authorization") });
    return new Response("{}");
  };
  return { forms, calls, fetch, release };
}

describe("the session", () => {
  it("uses the sign-in's access token while it lasts, then refreshes once for every caller, keeping the next refresh token first", async () => {
    const server = agent();
    const kept = store();
    let now = 0;
    const session = new DesktopSession(SIGNED_IN, { store: kept, fetch: server.fetch, onEnded: () => {}, now: () => now }, {
      accessToken: "at-0", refreshToken: SIGNED_IN.refreshToken, expiresAt: 1_800_000, authTime: SIGNED_IN.authTime,
    });
    await session.api("/api/v1/devices", { method: "POST" });
    expect(server.calls).toEqual([{ url: "https://agent.example.com/api/v1/devices", authorization: "Bearer at-0" }]);
    now = 1_800_000 - 30_000;
    const both = Promise.all([session.accessToken(), session.accessToken()]);
    server.release();
    expect(await both).toEqual(["at-1", "at-1"]);
    expect(server.forms).toEqual([{ grant_type: "refresh_token", client_id: "surogate-desktop", refresh_token: "surg_rt_secret" }]);
    expect(kept.get()?.refreshToken).toBe("rt-1");
  });

  it.each([
    ["a day", 1, "The agent ended this sign-in: sign in again in your browser"],
    ["thirty days", 30, "Your sign-in is 30 days old: sign in again in your browser"],
  ])("is signed out, and says why, when the agent refuses its refresh token %s after the sign-in", async (_name, days, why) => {
    const server = agent(true);
    server.release();
    const kept = store();
    kept.save(SIGNED_IN);
    const ended: string[] = [];
    const now = () => SIGNED_IN.authTime * 1000 + days * 86_400_000;
    const session = new DesktopSession(SIGNED_IN, { store: kept, fetch: server.fetch, onEnded: (reason) => ended.push(reason), now });
    await expect(session.accessToken()).rejects.toMatchObject({ code: "invalid_grant" });
    await expect(session.accessToken()).rejects.toMatchObject({ code: "invalid_grant" });
    expect([ended, existsSync(path), server.forms.length]).toEqual([[why], false, 1]);
  });

  it("signs out here first, then at the agent", async () => {
    const server = agent();
    server.release();
    const kept = store();
    kept.save(SIGNED_IN);
    await new DesktopSession(SIGNED_IN, { store: kept, fetch: server.fetch, onEnded: () => {} }).end();
    expect(existsSync(path)).toBe(false);
    expect(server.forms).toEqual([{ token: "surg_rt_secret", token_type_hint: "refresh_token", client_id: "surogate-desktop" }]);
  });

  it("is recent for nine minutes after the sign-in", () => {
    const at = (ms: number) => new DesktopSession(SIGNED_IN, { store: store(), fetch: agent().fetch, onEnded: () => {}, now: () => ms });
    expect(at(SIGNED_IN.authTime * 1000 + RECENT_MS - 1).recent()).toBe(true);
    expect(at(SIGNED_IN.authTime * 1000 + RECENT_MS).recent()).toBe(false);
  });
});

describe("who signed in", () => {
  it("is read from the agent's /auth/me, named by their email when they have no name", async () => {
    const asked: Array<string | null> = [];
    const fetch: Fetch = async (_url, init) => {
      asked.push(new Headers(init.headers).get("authorization"));
      return new Response(JSON.stringify({ id: "u", org_id: "o", email: "ada@example.com", display_name: null }));
    };
    expect(await accountOf("https://agent.example.com", "at-1", fetch)).toEqual({ name: "ada@example.com", email: "ada@example.com", userId: "u", orgId: "o" });
    expect(asked).toEqual(["Bearer at-1"]);
  });
});
