// An agent's server as the shell meets it, on one origin: /auth/config, device
// registration, the device link, and a page standing for the web client at every
// other path. Tests drive that page as the web client would. With projects set, the
// page serves them on every load, as the web client serves its own.

import { once } from "node:events";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";

import type { ElectronApplication, Page } from "playwright-core";
import { expect } from "vitest";

import type { ProjectFixtures, ProjectsSource } from "../../../web/src/lib/projects.js";
import { FakeLinkServer } from "../fake-server.js";

// As surogates/devices/store.py issues one: surg_dev_ and token_urlsafe(33).
export const TOKEN = `surg_dev_${"t".repeat(44)}`;

export class FakeAgent {
  config: Record<string, unknown> = { agent_id: "a", desktop_sessions: true, multi_session: true };
  // The projects the page serves: a fake ProjectsSource built on these, or none.
  projects: ProjectFixtures | null = null;
  // How long each load of the page takes to register them, as the web client waits for its bundle and /auth/me.
  registerAfterMs = 0;
  readonly link = new FakeLinkServer({ token: TOKEN });
  readonly registered: unknown[] = [];
  readonly server: Server = createServer((request, response) => {
    if (request.url === "/api/v1/auth/config") {
      response.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify(this.config));
      return;
    }
    if (request.method === "POST" && request.url === "/api/v1/devices") {
      let body = "";
      request.on("data", (chunk: Buffer) => {
        body += chunk.toString();
      });
      request.on("end", () => {
        this.registered.push(JSON.parse(body || "{}"));
        response.writeHead(201, { "content-type": "application/json" })
          .end(JSON.stringify({ id: "d", name: "Laptop", token: TOKEN }));
      });
      return;
    }
    const served = this.projects === null ? "" : `<script>(${serveProjects.toString()})(${JSON.stringify(this.projects).replace(/</g, "\\u003c")}, ${this.registerAfterMs})</script>`;
    response.writeHead(200, { "content-type": "text/html" }).end(`<!doctype html><title>Fake agent</title><p>The web client</p>${served}`);
  });
  private linked = false;

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

// The signed-in user, as the fake link's welcome names them.
export const ACCOUNT = { name: "Flavius Burca", email: "flavius@example.com", userId: "u", orgId: "o" };

// What the web client does once its user signed in (web/src/lib/desktop-bridge.ts): it says who
// is signed in, then registers this computer if it is not yet.
export const register = (client: Page, account = ACCOUNT) => client.evaluate(async (signedIn) => {
  const desktop = window.surogateDesktop!;
  await desktop.setAccount(signedIn);
  const before = await desktop.getDevice();
  if (before.device) return { before, device: before.device };
  const issued = await fetch("/api/v1/devices", { method: "POST", body: JSON.stringify({ name: before.computerName }) })
    .then((response) => response.json() as Promise<{ token: string }>);
  return { before, device: await desktop.registerDevice(issued.token) };
}, account);

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
