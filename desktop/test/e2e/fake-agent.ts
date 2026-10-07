// An agent's server as the shell meets it, on one origin: /auth/config, the OAuth routes
// the app signs in with, /auth/me, device registration, the device link, and a page
// standing for the web client at every other path. Tests drive that page as the web client
// would, and stand in for the system browser on the authorize page. With projects set, the
// page serves them on every load, as the web client serves its own.

import { createHash } from "node:crypto";
import { once } from "node:events";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

import type { ElectronApplication, Page } from "playwright-core";
import { expect } from "vitest";

import type { ProjectFixtures, ProjectsSource } from "../../../web/src/lib/projects.js";
import { FakeLinkServer } from "../fake-server.js";

// As surogates/devices/store.py issues one: surg_dev_ and token_urlsafe(33).
export const TOKEN = `surg_dev_${"t".repeat(44)}`;
// The token a reauthorization issues instead.
export const ROTATED = `surg_dev_${"r".repeat(44)}`;

// The signed-in user, as /auth/me and the fake link's welcome name them.
export const ACCOUNT = { name: "Flavius Burca", email: "flavius@example.com", userId: "u", orgId: "o" };

// The routes a test can hold: who signed in, adding this computer, and reauthorizing it.
type Held = "me" | "register" | "reauthorize";

function json(response: ServerResponse, status: number, body: unknown): void {
  response.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify(body));
}

function body(request: IncomingMessage): Promise<string> {
  return new Promise((resolve) => {
    let text = "";
    request.on("data", (chunk: Buffer) => {
      text += chunk.toString();
    });
    request.on("end", () => resolve(text));
  });
}

export class FakeAgent {
  config: Record<string, unknown> = { agent_id: "a", desktop_sessions: true, multi_session: true };
  // The projects the page serves: a fake ProjectsSource built on these, or none.
  projects: ProjectFixtures | null = null;
  // How long each load of the page takes to register them, as the web client waits for its bundle and /auth/me.
  registerAfterMs = 0;
  // Who signs in, as /auth/me answers.
  account = ACCOUNT;
  // False: the agent adds or restores a computer only on a more recent sign-in.
  recent = true;
  // How long before the token exchange the user signed in, as the tokens' auth_time says.
  signedInAgoS = 0;
  // True: the agent has no device left to restore.
  gone = false;
  // Routes that answer only once the test releases them, as a slow agent would, and how often each was asked.
  private readonly held = new Map<Held, Promise<void>>();
  readonly asked: Record<Held, number> = { me: 0, register: 0, reauthorize: 0 };
  readonly link = new FakeLinkServer({ token: TOKEN });
  readonly registered: unknown[] = [];
  readonly deleted: string[] = [];
  // The bearer of each reauthorization of this computer: the sign-in the agent bound to it.
  readonly reauthorized: string[] = [];
  // What a reauthorization answers, when not the new token.
  reauthorizeStatus = 200;
  // While set, the web client's page loads only once it settles.
  pagesHeld: Promise<void> | null = null;
  // What the app's OAuth calls sent, form by form.
  readonly oauth: Array<Record<string, string>> = [];
  private readonly codes = new Map<string, { challenge: string; redirectUri: string }>();
  // Refresh tokens the agent still takes: a refresh spends one, as rotation does, and a revoke ends it.
  private readonly live = new Set<string>();
  private issued = 0;
  readonly server: Server = createServer((request, response) => void this.answer(request, response));
  private linked = false;

  private async answer(request: IncomingMessage, response: ServerResponse): Promise<void> {
    const path = request.url ?? "/";
    const bearer = /^Bearer at-\d+$/.test(request.headers.authorization ?? "");
    if (path === "/api/v1/auth/config") return json(response, 200, this.config);
    if (request.method === "POST" && path === "/api/v1/auth/oauth/token") {
      const form = Object.fromEntries(new URLSearchParams(await body(request)));
      this.oauth.push(form);
      const code = form.grant_type === "authorization_code" ? this.codes.get(form.code ?? "") : undefined;
      if (code) this.codes.delete(form.code ?? "");
      const verified = code !== undefined && code.redirectUri === form.redirect_uri
        && createHash("sha256").update(form.code_verifier ?? "").digest("base64url") === code.challenge;
      if (form.grant_type === "refresh_token" ? !this.live.delete(form.refresh_token ?? "") : !verified) {
        return json(response, 400, { error: "invalid_grant" });
      }
      this.issued += 1;
      this.live.add(`rt-${this.issued}`);
      return json(response, 200, {
        access_token: `at-${this.issued}`, token_type: "Bearer", expires_in: 1800, refresh_token: `rt-${this.issued}`,
        auth_time: Math.floor(Date.now() / 1000) - this.signedInAgoS,
      });
    }
    if (request.method === "POST" && path === "/api/v1/auth/oauth/revoke") {
      const form = Object.fromEntries(new URLSearchParams(await body(request)));
      this.oauth.push(form);
      this.live.delete(form.token ?? "");
      return json(response, 200, {});
    }
    if (path === "/api/v1/auth/me" && bearer) {
      await this.answered("me");
      const { name, email, userId, orgId } = this.account;
      return json(response, 200, { id: userId, org_id: orgId, email, display_name: name });
    }
    if (request.method === "POST" && path === "/api/v1/auth/oauth/web-code" && bearer) return json(response, 200, { code: "web-code" });
    if (request.method === "POST" && path === "/api/v1/devices" && bearer) {
      this.registered.push(JSON.parse((await body(request)) || "{}"));
      await this.answered("register");
      if (!this.recent) return json(response, 403, { detail: { code: "recent_sign_in_required", message: "Sign in again" } });
      return json(response, 201, { id: this.link.identity.device_id, name: "Laptop", token: this.link.token });
    }
    if (request.method === "GET" && path === "/api/v1/devices" && bearer) {
      return json(response, 200, [{ id: this.link.identity.device_id, name: "Laptop", revoked_at: null }]);
    }
    if (request.method === "POST" && /^\/api\/v1\/devices\/[^/]+\/reauthorize$/.test(path) && bearer) {
      await this.answered("reauthorize");
      this.reauthorized.push(request.headers.authorization ?? "");
      if (!this.recent) return json(response, 403, { detail: { code: "recent_sign_in_required", message: "Sign in again" } });
      if (this.gone || path.split("/")[4] !== this.link.identity.device_id) return json(response, 404, { detail: "No such device." });
      if (this.reauthorizeStatus !== 200) return json(response, this.reauthorizeStatus, {});
      // A new token on the same device: the old one no longer connects, and its link is closed as revoked, as the agent's is.
      this.link.close(4403);
      this.link.token = ROTATED;
      return json(response, 200, { id: this.link.identity.device_id, name: "Laptop", token: ROTATED });
    }
    if (request.method === "DELETE" && path.startsWith("/api/v1/devices/") && bearer) {
      this.deleted.push(path.slice("/api/v1/devices/".length));
      response.writeHead(204).end();
      return;
    }
    await this.pagesHeld;
    const served = this.projects === null ? "" : `<script>(${serveProjects.toString()})(${JSON.stringify(this.projects).replace(/</g, "\\u003c")}, ${this.registerAfterMs})</script>`;
    response.writeHead(200, { "content-type": "text/html" }).end(`<!doctype html><title>Fake agent</title><p>The web client</p>${served}`);
  }

  /** Hold *route*: it answers only once the returned release is called. */
  hold(route: Held): () => void {
    const { promise, resolve } = Promise.withResolvers<void>();
    this.held.set(route, promise);
    return () => {
      this.held.delete(route);
      resolve();
    };
  }

  private answered(route: Held): Promise<void> | undefined {
    this.asked[route] += 1;
    return this.held.get(route);
  }

  /** The system browser on the agent's authorize page, once its user allowed the desktop: what the desktop's tab then says. */
  async approve(authorize: string): Promise<string> {
    const asked = new URL(authorize);
    const redirectUri = asked.searchParams.get("redirect_uri") ?? "";
    const code = `code-${this.codes.size + this.issued + 1}`;
    this.codes.set(code, { challenge: asked.searchParams.get("code_challenge") ?? "", redirectUri });
    const back = new URL(redirectUri);
    back.search = new URLSearchParams({ code, state: asked.searchParams.get("state") ?? "" }).toString();
    return (await fetch(back)).text();
  }

  /** The system browser on the authorize page, once its user denied the desktop. */
  async deny(authorize: string): Promise<string> {
    const asked = new URL(authorize);
    const back = new URL(asked.searchParams.get("redirect_uri") ?? "");
    back.search = new URLSearchParams({ error: "access_denied", state: asked.searchParams.get("state") ?? "" }).toString();
    return (await fetch(back)).text();
  }

  // On *port* when given: an agent that went away comes back where it was.
  async start(port = 0): Promise<string> {
    this.server.listen(port, "127.0.0.1");
    await once(this.server, "listening");
    if (!this.linked) await this.link.start({ server: this.server, path: "/api/v1/devices/connect" });
    this.linked = true;
    return `http://127.0.0.1:${(this.server.address() as AddressInfo).port}`;
  }

  async stop(): Promise<void> {
    this.server.closeAllConnections();
    this.link.drop();
    await new Promise<void>((resolve) => this.server.close(() => resolve()));
  }
}

// The first run's form: the agent's address, then Enter.
export async function connect(page: Page, address: string): Promise<void> {
  await page.fill("#address", address);
  await page.press("#address", "Enter");
}

// The agent's web client: the page on the agent's origin, once loaded.
export async function webClient(shell: ElectronApplication, origin: string): Promise<Page> {
  let found: Page | undefined;
  await expect.poll(() => {
    found = shell.windows().find((page) => page.url().startsWith(origin));
    return found !== undefined;
  }).toBe(true);
  await found!.waitForLoadState();
  return found!;
}

// What the shell sent to the system browser, in order.
export const opened = (shell: ElectronApplication) => shell.evaluate(() => (globalThis as unknown as { opened: string[] }).opened);

/**
 * Sign in as the user would: Continue on the window's sign-in, then allow the desktop on the
 * agent's authorize page in the "browser". What the desktop's tab says once it is back.
 */
export async function signIn(shell: ElectronApplication, page: Page, agent: FakeAgent): Promise<string> {
  const before = (await opened(shell)).length;
  await page.click("#sign-in-button");
  let authorize = "";
  await expect.poll(async () => {
    authorize = (await opened(shell))[before] ?? "";
    return authorize;
  }).not.toBe("");
  const tab = await agent.approve(authorize);
  // The sign-in shows until the web client has loaded again, with the session it gave it.
  await expect.poll(() => page.isVisible("#sign-in")).toBe(false);
  return tab;
}

// Signed in as ACCOUNT, and this computer added as Laptop: what most tests start from.
export async function signedInAndAdded(shell: ElectronApplication, page: Page, agent: FakeAgent): Promise<void> {
  await signIn(shell, page, agent);
  await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
}

// Run in the fake agent's page: a ProjectsSource on *data*, registered with the desktop after
// *delay* ms. The page keeps it as window.fakeProjects, whose changed() tells the source's subscribers,
// and whose lists counts the times the projects were listed.
function serveProjects(data: ProjectFixtures, delay: number): void {
  const listeners = new Map<string, Set<(threadId: string | null) => void>>();
  const fake = {
    data,
    lists: 0,
    changed: (id: string, threadId: string | null) => {
      for (const listener of listeners.get(id) ?? []) listener(threadId);
    },
  };
  const one = (id: string) => {
    const found = data.projects.find((project) => project.id === id);
    if (!found) throw new Error("No such project");
    return found;
  };
  const refuse = () => Promise.reject(new Error("The fake source changes nothing"));
  const source: ProjectsSource = {
    list: async () => {
      fake.lists++;
      return data.projects.map(({ id, name, icon, createdAt, updatedAt, waiting, working }) =>
        ({ id, name, icon, createdAt, updatedAt, waiting, working }));
    },
    get: async (id) => one(id),
    threads: async (id) => data.threads[one(id).id] ?? [],
    library: async (id) => data.library[one(id).id] ?? [],
    routines: async (id) => data.routines[one(id).id] ?? [],
    create: refuse,
    update: refuse,
    archive: refuse,
    resolve: refuse,
    reopen: refuse,
    subscribe: (id, onChange) => {
      const heard = listeners.get(id) ?? new Set();
      heard.add(onChange);
      listeners.set(id, heard);
      return () => heard.delete(onChange);
    },
  };
  Object.assign(window, { fakeProjects: fake });
  setTimeout(() => void window.surogateDesktop?.registerProjects(source), delay);
}
