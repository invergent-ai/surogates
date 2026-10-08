// An agent's server as the shell meets it, on one origin: /auth/config, the OAuth routes
// the app signs in with, /auth/me, device registration, the device link, and a page
// standing for the web client at every other path. Tests drive that page as the web client
// would, and stand in for the system browser on the authorize page. With projects set, the
// page serves them on every load, as the web client serves its own, and keeps what it
// changes of them here (PUT /fake/projects), as the agent's server keeps its projects.

import { createHash } from "node:crypto";
import { once } from "node:events";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

import type { ElectronApplication, Page } from "playwright-core";
import { expect } from "vitest";

import type { Project, ProjectFixtures, ProjectsSource, ThreadRow } from "../../../web/src/lib/projects.js";
import type { SignedInAccount } from "../../src/shell/session.js";
import { FakeLinkServer } from "../fake-server.js";

// As surogates/devices/store.py issues one: surg_dev_ and token_urlsafe(33).
export const TOKEN = `surg_dev_${"t".repeat(44)}`;
// The token a reauthorization issues instead.
export const ROTATED = `surg_dev_${"r".repeat(44)}`;

// The signed-in user, as /auth/me and the fake link's welcome name them.
export const ACCOUNT = { name: "Flavius Burca", email: "flavius@example.com", userId: "u", orgId: "o", orgName: "Surogate" };

// The routes a test can hold: who the agent is, who signed in, adding this computer, reauthorizing it,
// an inbox item's read and a chat's title.
type Held = "config" | "me" | "register" | "reauthorize" | "item" | "title";

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
  // How long each load of the page takes to register them, as the web client waits for its bundle and
  // /auth/me; below zero, the page registers them only when a test calls window.fakeProjects.register().
  registerAfterMs = 0;
  // Who signs in, as /auth/me answers.
  account: SignedInAccount = ACCOUNT;
  // False: the agent adds or restores a computer only on a more recent sign-in.
  recent = true;
  // How long before the token exchange the user signed in, as the tokens' auth_time says.
  signedInAgoS = 0;
  // True: the agent has no device left to restore.
  gone = false;
  // Where /auth/config redirects, and where the web client's pages redirect, as a moved agent or a sign-on gateway does:
  // every page but the one it redirects to.
  configRedirect: string | null = null;
  pagesRedirect: string | null = null;
  // When the agent revoked the computer, as its device list says; null while it is active.
  revokedAt: string | null = null;
  // Routes that answer only once the test releases them, as a slow agent would, and how often each was asked.
  private readonly held = new Map<Held, Promise<void>>();
  readonly asked: Record<Held, number> = { config: 0, me: 0, register: 0, reauthorize: 0, item: 0, title: 0 };
  readonly link = new FakeLinkServer({ token: TOKEN });
  readonly registered: unknown[] = [];
  readonly deleted: string[] = [];
  // The bearer of each reauthorization of this computer: the sign-in the agent bound to it.
  readonly reauthorized: string[] = [];
  // What a reauthorization answers, when not the new token.
  reauthorizeStatus = 200;
  // What /auth/me answers, when not who signed in.
  meStatus = 200;
  // While set, the web client's page loads only once it settles.
  pagesHeld: Promise<void> | null = null;
  // The web client's own HTML, served in place of the page standing for it.
  page: string | null = null;
  // What the app's OAuth calls sent, form by form.
  readonly oauth: Array<Record<string, string>> = [];
  private readonly codes = new Map<string, { challenge: string; redirectUri: string }>();
  // Refresh tokens the agent still takes: a refresh spends one, as rotation does, and a revoke ends it.
  private readonly live = new Set<string>();
  private issued = 0;
  // The user's inbox, by item id, and the inbox streams open on it.
  readonly inbox = new Map<number, Record<string, unknown>>();
  readonly inboxStreams = new Set<ServerResponse>();
  // Each chat's title, the event streams open on each chat, what each asked for, and the last event id.
  readonly titles = new Map<string, string>();
  readonly chatStreams = new Map<string, Set<ServerResponse>>();
  readonly chatsAsked: string[] = [];
  // What a chat's event stream answers, when not the stream: an agent out of reach answers 503.
  chatStatus = 200;
  private lastEvent = 100;
  readonly server: Server = createServer((request, response) => void this.answer(request, response));
  private linked = false;

  private async answer(request: IncomingMessage, response: ServerResponse): Promise<void> {
    const path = request.url ?? "/";
    const bearer = /^Bearer at-\d+$/.test(request.headers.authorization ?? "");
    if (path === "/api/v1/auth/config" && this.configRedirect) {
      response.writeHead(302, { location: this.configRedirect }).end();
      return;
    }
    if (path === "/api/v1/auth/config") {
      await this.answered("config");
      return json(response, 200, this.config);
    }
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
      if (this.meStatus !== 200) return json(response, this.meStatus, {});
      const { name, email, userId, orgId, orgName } = this.account;
      return json(response, 200, { id: userId, org_id: orgId, org_name: orgName, email, display_name: name });
    }
    if (request.method === "POST" && path === "/api/v1/auth/oauth/web-code" && bearer) return json(response, 200, { code: "web-code" });
    if (request.method === "POST" && path === "/api/v1/devices" && bearer) {
      this.registered.push(JSON.parse((await body(request)) || "{}"));
      await this.answered("register");
      if (!this.recent) return json(response, 403, { detail: { code: "recent_sign_in_required", message: "Sign in again" } });
      return json(response, 201, { id: this.link.identity.device_id, name: "Laptop", token: this.link.token });
    }
    if (request.method === "GET" && path === "/api/v1/devices" && bearer) {
      return json(response, 200, [{ id: this.link.identity.device_id, name: "Laptop", revoked_at: this.revokedAt }]);
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
    const events = /^\/api\/v1\/sessions\/([^/?]+)\/events\?(.*)$/.exec(path);
    if (events && bearer) {
      const [, chat, query] = events as unknown as [string, string, string];
      this.chatsAsked.push(`${chat}?${query}`);
      if (this.chatStatus !== 200) return json(response, this.chatStatus, {});
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.write(": connected\r\n\r\n");
      // A chat between its turns is completed, as the agent leaves one: a stream that does not watch ends there.
      if (new URLSearchParams(query).get("watch") !== "1") {
        response.end(`event: session.done\r\ndata: ${JSON.stringify({ reason: "completed", status: "completed" })}\r\n\r\n`);
        return;
      }
      response.write(`id: ${this.lastEvent}\r\nevent: stream.start\r\ndata: {}\r\n\r\n`);
      const open = this.chatStreams.get(chat) ?? new Set();
      open.add(response);
      this.chatStreams.set(chat, open);
      response.on("close", () => open.delete(response));
      return;
    }
    const chat = /^\/api\/v1\/sessions\/([^/?]+)\?agent_id=(.*)$/.exec(path);
    if (chat && bearer) {
      if (chat[2] !== this.config.agent_id) return json(response, 400, { detail: "no agent_id in request" });
      await this.answered("title");
      return json(response, 200, { id: chat[1], title: this.titles.get(chat[1]!) ?? null });
    }
    if (path.startsWith("/api/v1/inbox") && bearer) {
      const asked = new URL(path, "http://agent");
      // As the agent answers on an address with no subdomain of its own: the agent named, or none.
      if (asked.searchParams.get("agent_id") !== this.config.agent_id) return json(response, 400, { detail: "no agent_id in request" });
      if (asked.pathname === "/api/v1/inbox/stream") {
        // As the agent opens it: what waits unread, then each item as it comes.
        response.writeHead(200, { "content-type": "text/event-stream" });
        const unread = [...this.inbox.values()].filter((item) => item.status === "pending").map((item) => item.id);
        response.write(`event: snapshot\r\ndata: ${JSON.stringify({ unread_ids: unread })}\r\n\r\n`);
        this.inboxStreams.add(response);
        response.on("close", () => this.inboxStreams.delete(response));
        return;
      }
      await this.answered("item");
      const found = this.inbox.get(Number(/^\/api\/v1\/inbox\/(\d+)$/.exec(asked.pathname)?.[1]));
      return found ? json(response, 200, found) : json(response, 404, { detail: "Not found." });
    }
    if (request.method === "DELETE" && path.startsWith("/api/v1/devices/") && bearer) {
      this.deleted.push(path.slice("/api/v1/devices/".length));
      response.writeHead(204).end();
      return;
    }
    if (request.method === "PUT" && path === "/fake/projects") {
      this.projects = JSON.parse(await body(request)) as ProjectFixtures;
      response.writeHead(204).end();
      return;
    }
    await this.pagesHeld;
    const redirected = this.pagesRedirect === null ? null : new URL(this.pagesRedirect, "http://agent");
    if (redirected && !path.startsWith("/api/") && redirected.pathname + redirected.search !== path) {
      response.writeHead(302, { location: redirected.href }).end();
      return;
    }
    const served =this.projects === null ? "" : `<script>(${serveProjects.toString()})(${JSON.stringify(this.projects).replace(/</g, "\\u003c")}, ${this.registerAfterMs})</script>`;
    response.writeHead(200, { "content-type": "text/html; charset=utf-8" })
      .end(this.page ?? `<!doctype html><title>Fake agent</title><p>The web client</p>${served}`);
  }

  /** A new item in the user's inbox, as the agent makes one, told on every inbox stream open: its id. */
  tell(item: { kind: string; title: string; session_id: string }): number {
    const id = this.inbox.size + 1;
    this.inbox.set(id, { id, status: "pending", ...item });
    for (const stream of this.inboxStreams) stream.write(`event: item\r\ndata: ${JSON.stringify({ item_id: id, kind: item.kind })}\r\n\r\n`);
    return id;
  }

  /** A turn of *chat* ends, as the agent tells it on the chat's watching streams. */
  turnEnds(chat: string): void {
    this.lastEvent += 1;
    for (const stream of this.chatStreams.get(chat) ?? []) stream.write(`id: ${this.lastEvent}\r\nevent: session.complete\r\ndata: {}\r\n\r\n`);
  }

  /** End every chat's stream, as a restart of the agent does. */
  dropChats(): void {
    for (const open of this.chatStreams.values()) {
      for (const stream of open) stream.end();
      open.clear();
    }
  }

  /** End every inbox stream, as a restart of the agent does. */
  dropInbox(): void {
    for (const stream of this.inboxStreams) stream.end();
    this.inboxStreams.clear();
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
  // The sign-in shows until the web client has loaded again, with the session it gave it: on a busy
  // machine the window's storage cleared first and that load can take more than ten seconds.
  await expect.poll(() => page.isVisible("#sign-in"), { timeout: 30_000 }).toBe(false);
  return tab;
}

/**
 * Quit, and hold the quit once it has gone on, past the window's hide: a sign-in under way, held at who
 * signed in, keeps the app stopping until the returned release. *page* is the window's own.
 */
export async function quitHeld(shell: ElectronApplication, page: Page, agent: FakeAgent): Promise<() => void> {
  const release = agent.hold("me");
  const asking = agent.asked.me;
  const before = (await opened(shell)).length;
  await page.evaluate(() => (window as unknown as { surogateShell: { signIn(): Promise<void> } }).surogateShell.signIn());
  await expect.poll(async () => (await opened(shell)).length).toBe(before + 1);
  void agent.approve((await opened(shell))[before]!).catch(() => {});
  await expect.poll(() => agent.asked.me).toBe(asking + 1);
  void shell.evaluate(({ app: electron }) => electron.quit()).catch(() => {});
  // The quit went on: the inbox is followed no more.
  await expect.poll(() => agent.inboxStreams.size).toBe(0);
  return release;
}

// Signed in as ACCOUNT, and this computer added as Laptop: what most tests start from.
export async function signedInAndAdded(shell: ElectronApplication, page: Page, agent: FakeAgent): Promise<void> {
  await signIn(shell, page, agent);
  await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
}

// Run in the fake agent's page: a ProjectsSource on *data*, registered with the desktop after
// *delay* ms. The page keeps it as window.fakeProjects, whose changed() tells the source's subscribers,
// whose lists counts the times the projects were listed, whose reads names each read of threads (a
// thread's id, or null for them all), whose refusal, when set, is what a change of a project or a
// thread answers, whose unreachable, when set, makes the list and every call on a project fail as
// the web client's fetch does with the agent out of reach, whose lag is how many ms a change of a
// project takes to answer once it is made, as a slow agent's, and whose register() registers the
// source. What it changes of a project it keeps at the fake agent, so the next load serves it.
// The source's methods read it through this, as an object's own methods may.
function serveProjects(data: ProjectFixtures, delay: number): void {
  const listeners = new Map<string, Set<(threadId: string | null) => void>>();
  const fake = {
    data,
    lists: 0,
    reads: [] as Array<string | null>,
    refusal: null as string | null,
    unreachable: false,
    lag: 0,
    register: () => {},
    changed: (id: string, threadId: string | null) => {
      for (const listener of listeners.get(id) ?? []) listener(threadId);
    },
  };
  const keep = async (served: ProjectFixtures) => {
    await fetch("/fake/projects", { method: "PUT", body: JSON.stringify(served) });
    await new Promise((resolve) => setTimeout(resolve, fake.lag));
  };
  const source = {
    served: data,
    one(id: string) {
      if (fake.unreachable) throw new Error("API server is not reachable.");
      const found = this.served.projects.find((project) => project.id === id);
      if (!found) throw new Error("No such project");
      return found;
    },
    async list() {
      fake.lists++;
      if (fake.unreachable) throw new Error("API server is not reachable.");
      return this.served.projects.map(({ id, name, icon, createdAt, updatedAt, waiting, working }) =>
        ({ id, name, icon, createdAt, updatedAt, waiting, working }));
    },
    async get(id: string) {
      return this.one(id);
    },
    async threads(id: string, threadId?: string) {
      fake.reads.push(threadId ?? null);
      return (this.served.threads[this.one(id).id] ?? []).filter((row) => threadId === undefined || row.id === threadId);
    },
    async library(id: string) {
      return this.served.library[this.one(id).id] ?? [];
    },
    async routines(id: string) {
      return this.served.routines[this.one(id).id] ?? [];
    },
    async create(input: { name: string; goal?: string; instructions?: string }) {
      if (fake.refusal) throw new Error(fake.refusal);
      const now = new Date().toISOString();
      const made: Project = {
        id: crypto.randomUUID(), name: input.name, icon: null, createdAt: now, updatedAt: now, waiting: 0, working: 0,
        goal: input.goal || null, instructions: input.instructions ?? "", masterSessionId: crypto.randomUUID(),
        coordinatorTier: null, threadTier: null,
      };
      this.served.projects.push(made);
      await keep(this.served);
      return made;
    },
    async update(id: string, change: Parameters<ProjectsSource["update"]>[1]) {
      if (fake.refusal) throw new Error(fake.refusal);
      const changed = Object.assign(this.one(id), change);
      await keep(this.served);
      return changed;
    },
    async archive(id: string) {
      this.one(id);
      this.served.projects = this.served.projects.filter((project) => project.id !== id);
      await keep(this.served);
    },
    // Resolved or reopened, the row is the same row, moved, as the route answers it.
    async resolve(id: string, threadId: string) {
      return this.moved(id, threadId, "resolved");
    },
    async reopen(id: string, threadId: string) {
      return this.moved(id, threadId, "idle");
    },
    moved(id: string, threadId: string, group: "resolved" | "idle") {
      if (fake.refusal) throw new Error(fake.refusal);
      const row = (this.served.threads[this.one(id).id] ?? []).find((found) => found.id === threadId);
      if (!row) throw new Error("No such thread.");
      return Object.assign(row, { group, reason: null, resolvedAt: group === "resolved" ? new Date().toISOString() : null });
    },
    subscribe(id: string, onChange: (threadId: string | null) => void) {
      const heard = listeners.get(id) ?? new Set();
      heard.add(onChange);
      listeners.set(id, heard);
      return () => void heard.delete(onChange);
    },
  } satisfies ProjectsSource & { served: ProjectFixtures; one(id: string): Project; moved(id: string, threadId: string, group: string): ThreadRow };
  fake.register = () => void window.surogateDesktop?.registerProjects(source);
  Object.assign(window, { fakeProjects: fake });
  if (delay >= 0) setTimeout(fake.register, delay);
}
