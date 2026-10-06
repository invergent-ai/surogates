// An agent's server as the shell meets it, on one origin: /auth/config, device
// registration, the device link, and a page standing for the web client at every
// other path. Tests drive that page as the web client would.

import { once } from "node:events";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";

import type { ElectronApplication, Page } from "playwright-core";
import { expect } from "vitest";

import { FakeLinkServer } from "../fake-server.js";

// As surogates/devices/store.py issues one: surg_dev_ and token_urlsafe(33).
export const TOKEN = `surg_dev_${"t".repeat(44)}`;

export class FakeAgent {
  config: Record<string, unknown> = { agent_id: "a", desktop_sessions: true, multi_session: true };
  page = "<!doctype html><title>Fake agent</title><p>The web client</p>";
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
    response.writeHead(200, { "content-type": "text/html" }).end(this.page);
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
