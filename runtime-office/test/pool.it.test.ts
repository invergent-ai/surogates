import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Busy, PagePool, type OpenRequest } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, type Fixture } from "./support/office.js";

const request = (key: string, sessionId = "s"): OpenRequest => ({
  docKey: key, sessionId, config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl,
});

describe.skipIf(!itEnabled)("PagePool against the document server", () => {
  let fixture: Fixture;
  let pool: PagePool;

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({
      hostUrl: fixture.hostUrl, maxPages: 2, idleMs: 60_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true,
    });
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  it("opens a document, runs a command and a method, and reuses the page", async () => {
    const key = newKey();
    const id = await pool.open(request(key));
    expect(await pool.open(request(key))).toBe(id);
    expect(pool.size()).toBe(1);
    const page = await pool.get(id);
    expect((await page.command("function () { return Api.GetDocument().GetElementsCount(); }", {})).value).toBeGreaterThan(5);
    expect((await page.method("GetVersion")).value).toMatch(/^9\.3/);
    // Text that says "import" passes the editor's check on command code.
    const text = "We import goods (Import duties apply).";
    expect((await page.command("function () { return scope.text; }", { text })).value).toBe(text);
    expect(page.user).toEqual(AGENT);
    await pool.close(id);
    expect(pool.size()).toBe(0);
  });

  it("reads the editor's modified flag from its frame", async () => {
    const key = newKey();
    const id = await pool.open(request(key));
    const page = await pool.get(id);
    expect(page.kind).toBe("word");
    expect(await page.modified()).toBe(false);
    const sent = Date.now();
    await page.command("function () { var p = Api.CreateParagraph(); p.AddText('x'); Api.GetDocument().Push(p); return 1; }", {});
    expect(await page.waitFlushed(sent, 10_000)).toBe(true);
    expect(await page.modified()).toBe(false);
    expect(page.editorUrl()).toMatch(/\/web-apps\//);
    await pool.close(id);
  });

  it("refuses a document past its limit", async () => {
    await pool.open(request(newKey()));
    await pool.open(request(newKey()));
    await expect(pool.open(request(newKey()))).rejects.toBeInstanceOf(Busy);
  });

  it("closes pages left idle", async () => {
    expect(await pool.sweep(Date.now() + 120_000)).toBe(2);
    expect(pool.size()).toBe(0);
  });
});
