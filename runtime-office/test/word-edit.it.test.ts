import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { compileOps } from "../src/ops.js";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, USER, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// Holds an edit's reply before the office acts on it, long enough for a sent change to reach the user: about 0.15 s
// usually, and a second for the first change on a fresh pair of pages.
class SlowOffice extends Office {
  protected override async run(page: EditorPage, scope: unknown): Promise<{ sentAt: number; value: unknown }> {
    const reply = await super.run(page, scope);
    if ((scope as { action?: string }).action === "edit") await sleep(2_000);
    return reply;
  }
}

describe.skipIf(!itEnabled)("editing a document", () => {
  let fixture: Fixture;
  let pool: PagePool;
  let office: Office;
  let opened: string[] = [];

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 4, idleMs: 600_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
    office = new Office(pool);
  });
  afterEach(async () => {
    for (const id of opened) await pool.close(id);
    opened = [];
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  async function pair(): Promise<{ agent: string; user: string }> {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl });
    const user = await office.open({ docKey: key, sessionId: "user", config: editorConfig(key, USER), documentServerUrl: IT.dsUrl });
    opened.push(agent, user);
    return { agent, user };
  }
  const blocks = async (id: string): Promise<Any[]> =>
    ((await office.read(id, { view: "outline" })) as Any).blocks.filter((b: Any) => b.kind !== "paragraph" || b.text);
  const byText = (list: Any[], text: string): Any => list.find((b) => (b.text ?? "").startsWith(text));
  const edit = async (id: string, ops: unknown[]): Promise<Any> => office.edit(id, compileOps(ops));
  async function eventually(check: () => Promise<void>, ms = 10_000): Promise<void> {
    const until = Date.now() + ms;
    for (;;) {
      try {
        return await check();
      } catch (error) {
        if (Date.now() > until) throw error;
        await sleep(250);
      }
    }
  }

  it("inserts after a block, and the user sees it", async () => {
    const { agent, user } = await pair();
    const regions = byText(await blocks(agent), "Regions");
    const result = await edit(agent, [{ op: "insert", target: { block: regions.id, hash: regions.hash }, content: { markdown: "Added by the agent." } }]);
    expect(result).toMatchObject({ applied: true, flushed: true });
    expect(result.results[0].ids).toHaveLength(1);
    await eventually(async () => {
      const seen = await blocks(user);
      expect(seen[seen.findIndex((b) => b.id === regions.id) + 1]).toMatchObject({ kind: "paragraph", text: "Added by the agent." });
    });
  });

  it("re-words quoted words in place", async () => {
    const { agent, user } = await pair();
    expect(await edit(agent, [{ op: "replace", target: { find: "revenue target" }, content: { text: "sales goal" } }])).toMatchObject({ applied: true });
    await eventually(async () => {
      expect(((await office.read(user, { view: "search", query: "sales goal" })) as Any).total).toBe(1);
      expect(((await office.read(user, { view: "search", query: "revenue target" })) as Any).total).toBe(0);
    });
  });

  it("replaces a block with a list, deletes one and moves another", async () => {
    const { agent } = await pair();
    const before = await blocks(agent);
    const risk = byText(before, "Hiring could slip");
    const north = byText(before, "North grew");
    const risks = byText(before, "Risks");
    const regions = byText(before, "Regions");
    const result = await edit(agent, [
      { op: "replace", target: { block: risk.id, hash: risk.hash }, content: { markdown: "- Hiring\n- Supply" } },
      { op: "delete", target: { block: north.id, hash: north.hash } },
      { op: "move", target: { block: risks.id, hash: risks.hash }, to: { block: regions.id, hash: regions.hash }, side: "before" },
    ]);
    expect(result).toMatchObject({ applied: true });
    const after = await blocks(agent);
    const texts = after.map((b) => b.text);
    expect(texts).not.toContain("Hiring could slip a quarter.");
    expect(texts).not.toContain("North grew fastest, then the east.");
    expect(texts.indexOf("Risks")).toBeLessThan(texts.indexOf("Regions"));
    expect(after.filter((b) => b.kind === "bullet").map((b) => b.text)).toEqual(expect.arrayContaining(["Hiring", "Supply"]));
  });

  it("refuses a block the user changed, keeps nothing of the batch, and shows the block as it is now", async () => {
    const { agent, user } = await pair();
    const north = byText(await blocks(agent), "North grew");
    await (await pool.get(user)).command("function () { Api.GetByInternalId(scope.id).AddText(' Edited.'); return true; }", { id: north.id });
    await eventually(async () => expect(byText(await blocks(agent), "North grew").text).toContain("Edited."));
    const result = await edit(agent, [
      { op: "insert", target: { end: true }, content: { markdown: "Should not stay." } },
      { op: "replace", target: { block: north.id, hash: north.hash }, content: { markdown: "Replaced." } },
    ]);
    expect(result.applied).toBe(false);
    expect(result.refused.op).toBe(1);
    expect(result.refused.block.text).toContain("Edited.");
    await eventually(async () => {
      expect(byText(await blocks(agent), "Should not stay")).toBeUndefined();
      expect(byText(await blocks(user), "Should not stay")).toBeUndefined();
      expect(byText(await blocks(user), "North grew").text).toContain("Edited.");
    });
  });

  it("never shows a co-editor part of a batch it refuses", async () => {
    const { agent, user } = await pair();
    const north = byText(await blocks(agent), "North grew");
    let settled = false;
    const editing = new SlowOffice(pool)
      .edit(agent, compileOps([
        { op: "insert", target: { end: true }, content: { markdown: "Should not stay." } },
        { op: "replace", target: { block: north.id, hash: "00000000" }, content: { markdown: "Replaced." } },
      ]))
      .finally(() => { settled = true; });
    const seen: string[] = [];
    while (!settled) {
      if (byText(await blocks(user), "Should not stay")) seen.push("user");
      await sleep(100);
    }
    expect(((await editing) as Any).refused.op).toBe(1);
    expect(seen).toEqual([]);
    await sleep(1_000);
    expect(byText(await blocks(agent), "Should not stay")).toBeUndefined();
    expect(byText(await blocks(user), "Should not stay")).toBeUndefined();
  });

  it("refuses a block the user deleted, and keeps nothing of the batch", async () => {
    const { agent, user } = await pair();
    const north = byText(await blocks(agent), "North grew");
    await (await pool.get(user)).command("function () { Api.GetByInternalId(scope.id).Delete(); return true; }", { id: north.id });
    await eventually(async () => expect(byText(await blocks(agent), "North grew")).toBeUndefined());
    const result = await edit(agent, [
      { op: "insert", target: { end: true }, content: { markdown: "Should not stay." } },
      { op: "insert", target: { block: north.id, hash: north.hash }, side: "after", content: { markdown: "Nowhere." } },
    ]);
    expect(result.refused.op).toBe(1);
    expect(result.refused.reason).toMatch(/is gone/);
    await eventually(async () => {
      expect(byText(await blocks(user), "Should not stay")).toBeUndefined();
      expect(byText(await blocks(user), "Nowhere")).toBeUndefined();
    });
    expect(await office.read(agent, { view: "block", target: north.id })).toEqual({ error: `No block ${north.id}: read the outline again.` });
  });

  it("fills a pending block, then says it is gone", async () => {
    const { agent } = await pair();
    await edit(agent, [{ op: "insert", target: { end: true }, content: { markdown: "<?block s?>", blocks: { s: { type: "pending", intent: "Summary goes here" } } } }]);
    expect((await blocks(agent)).find((b) => b.kind === "pending")).toMatchObject({ key: "s", intent: "Summary goes here" });
    expect(await edit(agent, [{ op: "replace", target: { pending: "s" }, content: { markdown: "## Summary\n\nDone." } }])).toMatchObject({ applied: true });
    const after = await blocks(agent);
    expect(after.find((b) => b.kind === "pending")).toBeUndefined();
    expect(after.slice(-2).map((b) => [b.kind, b.text])).toEqual([["heading2", "Summary"], ["paragraph", "Done."]]);
    const again = await edit(agent, [{ op: "delete", target: { pending: "s" } }]);
    expect(again.refused.reason).toMatch(/is gone/);
  });

  it("says quoted words are not in the document", async () => {
    const { agent } = await pair();
    expect((await edit(agent, [{ op: "delete", target: { find: "no such words" } }])).refused.reason).toMatch(/not in the document/);
  });

  it("flushes the agent's editor", async () => {
    const { agent } = await pair();
    await edit(agent, [{ op: "insert", target: { end: true }, content: { markdown: "One more." } }]);
    expect(await office.flush(agent)).toBe(true);
  });
});
