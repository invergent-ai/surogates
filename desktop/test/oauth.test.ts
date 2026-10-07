import { createHash } from "node:crypto";
import { once } from "node:events";
import { createServer, request, type Server } from "node:http";
import type { AddressInfo } from "node:net";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CLIENT_ID, OAuthError, refreshTokens, revokeTokens, signInWithBrowser, type Tokens } from "../src/shell/oauth.js";

// The agent's token and revoke routes, as surogates/api/routes/oauth.py answers them.
class Agent {
  readonly forms: Array<Record<string, string>> = [];
  answer: (form: Record<string, string>) => { status: number; body: unknown } = () => ({
    status: 200,
    body: { access_token: `at-${this.forms.length}`, token_type: "Bearer", expires_in: 1800, refresh_token: `rt-${this.forms.length}`, auth_time: 1_700_000_000 },
  });
  // Held until released: what the browser does while the exchange is under way.
  hold: Promise<void> | null = null;
  readonly server: Server = createServer((incoming, response) => {
    let body = "";
    incoming.on("data", (chunk: Buffer) => {
      body += chunk.toString();
    });
    incoming.on("end", async () => {
      const form = Object.fromEntries(new URLSearchParams(body));
      this.forms.push(form);
      await this.hold;
      const { status, body: answered } = this.answer(form);
      response.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify(answered));
    });
  });

  async start(): Promise<string> {
    this.server.listen(0, "127.0.0.1");
    await once(this.server, "listening");
    return `http://127.0.0.1:${(this.server.address() as AddressInfo).port}`;
  }
}

// What the system browser does with an address: a GET, with its Host as given unless told another.
function browse(url: string, host?: string): Promise<{ status: number; text: string }> {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const sent = request({ host: target.hostname, port: target.port, path: `${target.pathname}${target.search}`, headers: host ? { host } : {} }, (response) => {
      let text = "";
      response.on("data", (chunk: Buffer) => {
        text += chunk.toString();
      });
      response.on("end", () => resolve({ status: response.statusCode ?? 0, text }));
    });
    sent.on("error", reject);
    sent.end();
  });
}

const s256 = (verifier: string) => createHash("sha256").update(verifier).digest("base64url");

let agent: Agent;
let origin: string;

beforeEach(async () => {
  agent = new Agent();
  origin = await agent.start();
});

afterEach(async () => {
  agent.server.closeAllConnections();
  await new Promise<void>((resolve) => agent.server.close(() => resolve()));
});

// A sign-in whose browser does *act* with the address it is opened at.
function signIn(act: (url: URL) => Promise<unknown>, more: { signal?: AbortSignal; timeoutMs?: number } = {}) {
  let tab: Promise<unknown> = Promise.resolve();
  const signedIn = signInWithBrowser({
    origin, computer: "Flavius's ThinkPad", fetch: (url, init) => fetch(url, init), now: () => 1_000,
    open: async (url) => {
      tab = act(new URL(url));
    },
    ...more,
  });
  return { signedIn, tab: () => tab };
}

const callback = (url: URL, query: Record<string, string>) => {
  const back = new URL(url.searchParams.get("redirect_uri")!);
  back.search = new URLSearchParams(query).toString();
  return back.href;
};
const approved = (url: URL) => callback(url, { code: "the-code", state: url.searchParams.get("state")! });

describe("signing in through the system browser", () => {
  it("opens the agent's authorize page with S256 PKCE and a loopback callback, and exchanges the code with its verifier", async () => {
    let asked: URL | undefined;
    const { signedIn, tab } = signIn((url) => {
      asked = url;
      return browse(approved(url));
    });
    const tokens: Tokens = await signedIn;
    expect(tokens).toEqual({ accessToken: "at-1", refreshToken: "rt-1", expiresAt: 1_000 + 1_800_000, authTime: 1_700_000_000 });
    const redirectUri = asked!.searchParams.get("redirect_uri");
    expect(redirectUri).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/callback$/);
    expect(agent.forms).toEqual([{
      grant_type: "authorization_code", client_id: CLIENT_ID, code: "the-code", redirect_uri: redirectUri,
      code_verifier: expect.stringMatching(/^[A-Za-z0-9_-]{43}$/),
    }]);
    // The browser was sent to the agent's own page, with the challenge of the verifier exchanged.
    expect(`${asked!.origin}${asked!.pathname}`).toBe(`${origin}/oauth/authorize`);
    expect(Object.fromEntries(asked!.searchParams)).toMatchObject({
      response_type: "code", client_id: CLIENT_ID, code_challenge_method: "S256", computer: "Flavius's ThinkPad",
      code_challenge: s256(agent.forms[0]!.code_verifier!),
    });
    expect(asked!.searchParams.get("state")).toMatch(/^[A-Za-z0-9_-]{32}$/);
    expect(await tab()).toEqual({ status: 200, text: expect.stringContaining("Signed in. You can close this tab and return to Surogate.") });
    await expect(browse(redirectUri!)).rejects.toThrow(/ECONNREFUSED/);
  });

  it("ignores a callback with another sign-in's state, and still takes its own", async () => {
    const stray: Array<{ status: number; text: string }> = [];
    const { signedIn, tab } = signIn(async (url) => {
      // Any page or program on this computer can knock with a state of its own: it ends nothing.
      stray.push(await browse(callback(url, { code: "stray-code", state: "another-state-entirely" })));
      return browse(approved(url));
    });
    expect(await signedIn).toMatchObject({ accessToken: "at-1" });
    expect(stray).toEqual([{ status: 400, text: expect.stringContaining("not one Surogate started") }]);
    expect(agent.forms.map((form) => form.code)).toEqual(["the-code"]);
    expect(await tab()).toMatchObject({ status: 200 });
  });

  it("says a sign-in declined in the browser was declined", async () => {
    const { signedIn } = signIn((url) => browse(callback(url, { error: "access_denied", state: url.searchParams.get("state")! })));
    await expect(signedIn).rejects.toThrow("Sign-in was declined in the browser");
  });

  it("answers only its own callback: other paths are not found, another Host is not answered, and a second callback is told it is over", async () => {
    let release = () => {};
    agent.hold = new Promise((resolve) => {
      release = resolve;
    });
    const seen: Array<{ status: number }> = [];
    const { signedIn, tab } = signIn(async (url) => {
      const back = new URL(approved(url));
      seen.push(await browse(new URL("/favicon.ico", back).href));
      seen.push(await browse(back.href, "attacker.example:80"));
      const first = browse(back.href);
      await vi.waitFor(() => expect(agent.forms).toHaveLength(1));
      // The exchange is under way: the loopback still listens, for nothing.
      seen.push(await browse(back.href));
      release();
      return first;
    });
    await signedIn;
    expect(await tab()).toMatchObject({ status: 200 });
    expect(seen.map((answer) => answer.status)).toEqual([404, 404, 410]);
    expect(agent.forms).toHaveLength(1);
  });

  it("stops waiting when started again, and leaves no port open", async () => {
    const controller = new AbortController();
    let port = "";
    const { signedIn } = signIn(async (url) => {
      port = new URL(url.searchParams.get("redirect_uri")!).port;
      controller.abort();
    }, { signal: controller.signal });
    await expect(signedIn).rejects.toMatchObject({ code: "cancelled" });
    await expect(browse(`http://127.0.0.1:${port}/callback`)).rejects.toThrow(/ECONNREFUSED/);
  });

  it("gives up when the browser never comes back, and leaves no port open", async () => {
    let port = "";
    const { signedIn } = signIn(async (url) => {
      port = new URL(url.searchParams.get("redirect_uri")!).port;
    }, { timeoutMs: 50 });
    await expect(signedIn).rejects.toMatchObject({ code: "timeout" });
    await expect(browse(`http://127.0.0.1:${port}/callback`)).rejects.toThrow(/ECONNREFUSED/);
  });

  it("gives up in time even when opening the browser never finishes", async () => {
    const signedIn = signInWithBrowser({
      origin, computer: "c", fetch: (url, init) => fetch(url, init), open: () => new Promise(() => {}), timeoutMs: 50,
    });
    await expect(signedIn).rejects.toMatchObject({ code: "timeout" });
  });

  it("opens no browser for a sign-in already cancelled", async () => {
    const opened: string[] = [];
    const signedIn = signInWithBrowser({
      origin, computer: "c", fetch: (url, init) => fetch(url, init), signal: AbortSignal.abort(),
      open: async (url) => {
        opened.push(url);
      },
    });
    await expect(signedIn).rejects.toMatchObject({ code: "cancelled" });
    expect(opened).toEqual([]);
  });

  it("is cancelled, with no tokens, when started again during the exchange", async () => {
    let release = () => {};
    agent.hold = new Promise((resolve) => {
      release = resolve;
    });
    const controller = new AbortController();
    const { signedIn, tab } = signIn(async (url) => {
      const back = browse(approved(url));
      await vi.waitFor(() => expect(agent.forms).toHaveLength(1));
      controller.abort();
      release();
      return back;
    }, { signal: controller.signal });
    await expect(signedIn).rejects.toMatchObject({ code: "cancelled" });
    expect(await tab()).toMatchObject({ status: 400 });
  });

  it("ends a callback with this sign-in's state but no code", async () => {
    const { signedIn, tab } = signIn((url) => browse(callback(url, { state: url.searchParams.get("state")! })));
    await expect(signedIn).rejects.toMatchObject({ code: "invalid_request" });
    expect(await tab()).toMatchObject({ status: 400 });
  });

  it("ends at once when the browser cannot be opened", async () => {
    const signedIn = signInWithBrowser({
      origin, computer: "c", fetch: (url, init) => fetch(url, init), open: () => Promise.reject(new Error("no browser")),
    });
    await expect(signedIn).rejects.toThrow("no browser");
  });

  it("says why the agent refused the exchange", async () => {
    agent.answer = () => ({ status: 400, body: { error: "invalid_grant" } });
    const { signedIn, tab } = signIn((url) => browse(approved(url)));
    await expect(signedIn).rejects.toEqual(new OAuthError("invalid_grant", "The agent ended this sign-in: sign in again"));
    expect(await tab()).toMatchObject({ status: 400 });
  });
});

describe("refreshing and signing out", () => {
  it("spends the refresh token for new tokens", async () => {
    const tokens = await refreshTokens(origin, "rt-old", (url, init) => fetch(url, init), () => 0);
    expect(agent.forms).toEqual([{ grant_type: "refresh_token", client_id: CLIENT_ID, refresh_token: "rt-old" }]);
    expect(tokens.refreshToken).toBe("rt-1");
  });

  it("ends the sign-in at the agent", async () => {
    await revokeTokens(origin, "rt-old", (url, init) => fetch(url, init));
    expect(agent.forms).toEqual([{ token: "rt-old", token_type_hint: "refresh_token", client_id: CLIENT_ID }]);
  });
});
