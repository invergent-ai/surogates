import { connect, type AddressInfo } from "node:net";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ContentError } from "../src/markdown.js";
import { Busy, PageGone } from "../src/pages.js";
import { createServer, type OfficeApi } from "../src/server.js";

const TOKEN = "t".repeat(32);

function fakeOffice(overrides: Partial<OfficeApi> = {}): OfficeApi {
  return {
    open: vi.fn(async () => "key.session"),
    read: vi.fn(async () => ({ blocks: [] })),
    edit: vi.fn(async () => ({ applied: true, results: [], flushed: true })),
    flush: vi.fn(async () => true),
    close: vi.fn(async () => undefined),
    health: () => ({ pages: 0, cap: 6 }),
    ...overrides,
  };
}

let close: (() => Promise<void>) | undefined;
afterEach(async () => {
  await close?.();
  close = undefined;
});

async function serve(office: OfficeApi, maxBody?: number): Promise<string> {
  const server = createServer(office, { token: TOKEN, maxBody });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  close = () => new Promise((resolve) => {
    server.close(() => resolve());
    server.closeAllConnections();
  });
  return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
}

const call = (url: string, init: RequestInit = {}, token: string | null = TOKEN) =>
  fetch(url, { ...init, headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) } });

describe("the HTTP API", () => {
  it("answers health and the host page without a token, and nothing else", async () => {
    const base = await serve(fakeOffice());
    expect(await (await call(`${base}/health`, {}, null)).json()).toEqual({ pages: 0, cap: 6 });
    expect(await (await call(`${base}/host.html`, {}, null)).text()).toContain("DocsAPI.DocEditor");
    expect((await call(`${base}/pages`, { method: "POST", body: "{}" }, null)).status).toBe(401);
    expect((await call(`${base}/pages`, { method: "POST", body: "{}" }, "x".repeat(32))).status).toBe(401);
  });

  it("opens a page", async () => {
    const office = fakeOffice();
    const base = await serve(office);
    const body = { doc_key: "key", session_id: "session", config: { a: 1 }, document_server_url: "http://docs" };
    const response = await call(`${base}/pages`, { method: "POST", body: JSON.stringify(body) });
    expect(await response.json()).toEqual({ page_id: "key.session" });
    expect(office.open).toHaveBeenCalledWith({ docKey: "key", sessionId: "session", config: { a: 1 }, documentServerUrl: "http://docs" });
    expect((await call(`${base}/pages`, { method: "POST", body: JSON.stringify({ doc_key: "key" }) })).status).toBe(400);
  });

  it("says busy, gone and failed apart", async () => {
    const base = await serve(fakeOffice({
      open: vi.fn(async () => { throw new Busy("full"); }),
      read: vi.fn(async () => { throw new PageGone("closed"); }),
    }));
    const opened = await call(`${base}/pages`, { method: "POST", body: JSON.stringify({ doc_key: "k", session_id: "s", config: {}, document_server_url: "u" }) });
    expect(opened.status).toBe(503);
    expect(await opened.json()).toEqual({ error: "full" });
    expect((await call(`${base}/pages/k.s/read`, { method: "POST", body: JSON.stringify({ view: "outline" }) })).status).toBe(410);
  });

  it("hands reads and ops to the office, and answers 400 to what it refuses", async () => {
    const office = fakeOffice({
      read: vi.fn(async () => { throw new ContentError('In a Word document "view" is "outline", "block" or "search".'); }),
    });
    const base = await serve(office);
    const refused = await call(`${base}/pages/k.s/read`, { method: "POST", body: JSON.stringify({ view: "everything" }) });
    expect(refused.status).toBe(400);
    expect(await refused.json()).toEqual({ error: 'In a Word document "view" is "outline", "block" or "search".' });
    expect(office.read).toHaveBeenCalledWith("k.s", { view: "everything" });
    const ops = [{ op: "insert", target: { end: true }, content: { markdown: "Hi." } }];
    const good = await call(`${base}/pages/k.s/edit`, { method: "POST", body: JSON.stringify({ ops }) });
    expect(await good.json()).toEqual({ applied: true, results: [], flushed: true });
    expect(office.edit).toHaveBeenCalledWith("k.s", ops);
  });

  it("flushes, closes, and refuses an oversized body", async () => {
    const office = fakeOffice();
    const base = await serve(office, 1000);
    expect(await (await call(`${base}/pages/k.s/flush`, { method: "POST", body: "{}" })).json()).toEqual({ flushed: true });
    expect((await call(`${base}/pages/k.s`, { method: "DELETE" })).status).toBe(204);
    expect(office.close).toHaveBeenCalledWith("k.s");
    expect((await call(`${base}/pages/k.s/edit`, { method: "POST", body: JSON.stringify({ ops: "x".repeat(2000) }) })).status).toBe(413);
  });

  it("answers 413 to a client that sends a large body whole before it reads the answer", async () => {
    const base = await serve(fakeOffice(), 1000);
    const body = "x".repeat(5_000_000);
    const socket = connect(Number(new URL(base).port), "127.0.0.1");
    let answer = "";
    socket.on("data", (chunk) => { answer += chunk; });
    const closed = new Promise((resolve) => socket.on("close", resolve));
    const head = `POST /pages/k.s/edit HTTP/1.1\r\nhost: x\r\nauthorization: Bearer ${TOKEN}\r\ncontent-length: ${body.length}\r\n\r\n`;
    // Like httpx: the request is written to its end first, which stalls when the server stops reading it.
    await new Promise<void>((resolve) => socket.end(head + body, resolve));
    await closed;
    expect(answer).toMatch(/^HTTP\/1\.1 413 /);
  }, 10_000);
});
