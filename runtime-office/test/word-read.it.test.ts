import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, USER, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

describe.skipIf(!itEnabled)("reading a document", () => {
  let fixture: Fixture;
  let pool: PagePool;
  let office: Office;
  let agent: string;
  let user: string;

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 4, idleMs: 600_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
    office = new Office(pool);
    const key = newKey();
    agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl });
    user = await office.open({ docKey: key, sessionId: "user", config: editorConfig(key, USER), documentServerUrl: IT.dsUrl });
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  const outline = async (id: string, request: Record<string, unknown> = {}): Promise<Any> =>
    office.read(id, { view: "outline", ...request } as Any);

  it("outlines the blocks with ids, hashes and kinds", async () => {
    const read = await outline(agent);
    const kinds = read.blocks.filter((b: Any) => b.kind !== "paragraph" || b.text).map((b: Any) => b.kind);
    expect(kinds).toEqual([
      "heading1", "paragraph", "heading2", "paragraph", "bullet", "bullet", "numbered", "numbered", "table", "heading2", "paragraph",
    ]);
    for (const block of read.blocks) {
      expect(block.id).toMatch(/\S/);
      expect(block.hash).toMatch(/^[0-9a-f]{8}$/);
    }
    expect(read.comments).toEqual([]);
    expect(read.tracked_changes).toEqual({});
  });

  it("gives every co-editor the same ids and hashes", async () => {
    expect((await outline(user)).blocks).toEqual((await outline(agent)).blocks);
  });

  it("pages a long outline", async () => {
    const whole = await outline(agent);
    const first = await outline(agent, { limit: 3 });
    expect(first.blocks).toHaveLength(3);
    expect(first.next).toBe(3);
    const rest = await outline(agent, { start: 3 });
    expect(rest.comments).toBeUndefined();
    expect(rest.blocks[0]).toEqual(whole.blocks[3]);
    expect(JSON.stringify(whole).length).toBeLessThan(50_000);
  });

  it("views a table's cells", async () => {
    const table = (await outline(agent)).blocks.find((b: Any) => b.kind === "table");
    const viewed: Any = await office.read(agent, { view: "block", target: table.id });
    expect(viewed.cells).toEqual([["Region", "Growth"], ["North", "12%"]]);
  });

  it("finds the block words are in", async () => {
    const found: Any = await office.read(agent, { view: "search", query: "revenue target" });
    expect(found.total).toBe(1);
    expect(found.matches[0]).toMatchObject({ kind: "paragraph", match: "revenue target" });
  });

  it("says a block is gone", async () => {
    expect(await office.read(agent, { view: "block", target: "no-such-id" })).toEqual({
      error: "No block no-such-id: read the outline again.",
    });
  });
});
