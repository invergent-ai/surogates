import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import type { EditorPage } from "../src/connector.js";
import type { Scope } from "../src/formats/format.js";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, editorConfig, eventually, IT, itEnabled, newKey, pair, startFixture, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// Holds an edit's reply before the office acts on it, long enough for a sent change to reach the user: about 0.15 s
// usually, and a second for the first change on a fresh pair of pages.
class SlowOffice extends Office {
  protected override async run(page: EditorPage, scope: Scope): Promise<{ sentAt: number; value: unknown }> {
    const reply = await super.run(page, scope);
    if (scope.action === "edit") await sleep(2_000);
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

  const blocks = async (id: string): Promise<Any[]> =>
    ((await office.read(id, { view: "outline" })) as Any).blocks.filter((b: Any) => b.kind !== "paragraph" || b.text);
  const byText = (list: Any[], text: string): Any => list.find((b) => (b.text ?? "").startsWith(text));
  const edit = async (id: string, ops: unknown[]): Promise<Any> => office.edit(id, ops);

  it("inserts after a block, and the user sees it", async () => {
    const { agent, user } = await pair(office, opened);
    const regions = byText(await blocks(agent), "Regions");
    const result = await edit(agent, [{ op: "insert", target: { block: regions.id, hash: regions.hash }, content: { markdown: "Added by the agent." } }]);
    expect(result).toMatchObject({ applied: true, flushed: true });
    expect(result.results[0].ids).toHaveLength(1);
    await eventually(async () => {
      const seen = await blocks(user);
      expect(seen[seen.findIndex((b) => b.id === regions.id) + 1]).toMatchObject({ kind: "paragraph", text: "Added by the agent." });
    });
  });

  it("shows what an insert made and how long the document is", async () => {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl });
    opened.push(agent);
    const second = (await blocks(agent)).filter((b) => b.kind === "numbered")[1];
    const result = await edit(agent, [
      { op: "insert", target: { block: second.id, hash: second.hash }, side: "after", content: { markdown: "3. Renew the support contract" } },
    ]);
    // The reader sees the number the item got: the list's next.
    expect(result.results[0].reads).toEqual([{ id: result.results[0].ids[0], kind: "numbered", label: "3.", text: "Renew the support contract" }]);
    expect(result.length.words).toBeGreaterThan(0);
    expect(result.length.pages).toBeGreaterThanOrEqual(1);
    // The length is the document's after the edit, its pages too.
    const long = Array.from({ length: 40 }, (_, i) => `Paragraph ${i + 1}: ${"words of body text ".repeat(20)}`).join("\n\n");
    const longer = await edit(agent, [{ op: "insert", target: { end: true }, content: { markdown: long } }]);
    expect(longer.length.words).toBeGreaterThan(result.length.words + 3_000);
    expect(longer.length.pages).toBeGreaterThan(1);
    // A refusal shows the list item as the outline does.
    const refused = await edit(agent, [{ op: "delete", target: { block: second.id, hash: "00000000" } }]);
    expect(refused.refused.block).toMatchObject({ kind: "numbered", label: "2.", text: "Open the Lisbon office" });
  });

  const alone = async (): Promise<string> => {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl });
    opened.push(agent);
    return agent;
  };
  const numbered = async (id: string): Promise<Any[]> => (await blocks(id)).filter((b) => b.kind === "numbered");
  const insertBeside = (id: string, item: Any, side: string, markdown: string, more: object = {}): Promise<Any> =>
    edit(id, [{ op: "insert", target: { block: item.id, hash: item.hash }, side, content: { markdown }, ...more }]);
  const labelled = async (id: string): Promise<string[][]> => (await numbered(id)).map((b) => [b.label, b.text]);

  it("continues a numbered list", async () => {
    const agent = await alone();
    const [first, second] = await numbered(agent);
    const result = await insertBeside(agent, second, "after", "3. Renew the support contract");
    expect(result.results[0].reads[0]).toMatchObject({ kind: "numbered", label: "3.", text: "Renew the support contract" });
    expect(await labelled(agent)).toEqual([["1.", "Hire two engineers"], ["2.", "Open the Lisbon office"], ["3.", "Renew the support contract"]]);
    expect((await numbered(agent)).slice(0, 2).map((b) => b.hash)).toEqual([first.hash, second.hash]);
  });

  it("starts a list on request", async () => {
    const agent = await alone();
    const second = (await numbered(agent))[1];
    const result = await insertBeside(agent, second, "after", "3. Renew the support contract", { list: "new" });
    expect(result.results[0].reads[0]).toMatchObject({ kind: "numbered", label: "1.", text: "Renew the support contract" });
  });

  it("keeps bullets apart from numbers", async () => {
    const agent = await alone();
    const second = (await numbered(agent))[1];
    const result = await insertBeside(agent, second, "after", "- An extra point");
    expect(result.results[0].reads[0]).toMatchObject({ kind: "bullet", label: "•", text: "An extra point" });
    expect((await numbered(agent)).map((b) => b.label)).toEqual(["1.", "2."]);
  });

  it("joins only the list item it lands next to", async () => {
    const agent = await alone();
    const [first, second] = await numbered(agent);
    const before = await insertBeside(agent, first, "before", "1. Agree the budget");
    expect(before.results[0].reads[0]).toMatchObject({ kind: "numbered", label: "1.", text: "Agree the budget" });
    expect(await labelled(agent)).toEqual([["1.", "Agree the budget"], ["2.", "Hire two engineers"], ["3.", "Open the Lisbon office"]]);
    // A heading between them: a list of its own.
    const apart = await insertBeside(agent, second, "after", "## Next\n\n1. Book the venue");
    expect(apart.results[0].reads.map((b: Any) => [b.kind, b.label])).toEqual([["heading2", undefined], ["numbered", "1."]]);
  });

  it("joins one level down", async () => {
    const agent = await alone();
    const first = (await numbered(agent))[0];
    const result = await insertBeside(agent, first, "after", "  1. A sub-step");
    // The sample's lists define their first level only, so a sub-step shows no number there: its numbering tells.
    const levels = await (await pool.get(agent)).command(
      "function () { return scope.ids.map(function (id) { var level = Api.GetByInternalId(id).GetNumbering(); " +
        "return [level.GetLevelIndex(), level.GetNumbering().Num.GetId()]; }); }",
      { ids: [first.id, result.results[0].ids[0]] },
    );
    const [[firstLevel, firstList], [level, list]] = levels.value as [[number, string], [number, string]];
    expect([firstLevel, level]).toEqual([0, 1]);
    expect(list).toBe(firstList);
    expect(await labelled(agent)).toEqual(expect.arrayContaining([["1.", "Hire two engineers"], ["2.", "Open the Lisbon office"]]));
  });

  it("starts an indented list that joins nothing at its first level", async () => {
    const agent = await alone();
    const before = await blocks(agent);
    const north = byText(before, "North grew");
    const risk = byText(before, "Hiring could slip");
    const result = await edit(agent, [
      { op: "insert", target: { block: north.id, hash: north.hash }, side: "after", content: { markdown: "  - a\n  - b" } },
      { op: "replace", target: { block: risk.id, hash: risk.hash }, content: { markdown: "  1. Hiring\n  2. Supply" } },
    ]);
    expect(result.results[1].reads.map((b: Any) => [b.kind, b.label])).toEqual([["numbered", "1."], ["numbered", "2."]]);
    const ids = [...result.results[0].ids, ...result.results[1].ids];
    const levels = await (await pool.get(agent)).command(
      "function () { return scope.ids.map(function (id) { return Api.GetByInternalId(id).GetNumbering().GetLevelIndex(); }); }",
      { ids },
    );
    expect(levels.value).toEqual([0, 0, 0, 0]);
  });

  it("keeps a nested list of the other kind under its item", async () => {
    const agent = await alone();
    const before = await blocks(agent);
    const north = byText(before, "North grew");
    const risk = byText(before, "Hiring could slip");
    const second = before.filter((b) => b.kind === "numbered")[1];
    const result = await edit(agent, [
      { op: "replace", target: { block: risk.id, hash: risk.hash }, content: { markdown: "1. a\n   - b" } },
      { op: "insert", target: { block: second.id, hash: second.hash }, side: "after", content: { markdown: "3. x\n   - y" } },
      { op: "insert", target: { block: north.id, hash: north.hash }, side: "after", content: { markdown: "  1. p\n     - q" } },
    ]);
    expect(result.results.map((r: Any) => r.reads[0].label)).toEqual(["1.", "3.", "1."]);
    const levels = await (await pool.get(agent)).command(
      "function () { return scope.ids.map(function (id) { return Api.GetByInternalId(id).GetNumbering().GetLevelIndex(); }); }",
      { ids: result.results.flatMap((r: Any) => r.ids) },
    );
    expect(levels.value).toEqual([0, 1, 0, 1, 0, 1]);
  });

  it("re-words quoted words in place", async () => {
    const { agent, user } = await pair(office, opened);
    expect(await edit(agent, [{ op: "replace", target: { find: "revenue target" }, content: { text: "sales goal" } }])).toMatchObject({ applied: true });
    await eventually(async () => {
      expect(((await office.read(user, { view: "search", query: "sales goal" })) as Any).total).toBe(1);
      expect(((await office.read(user, { view: "search", query: "revenue target" })) as Any).total).toBe(0);
    });
  });

  it("replaces a block with a list, deletes one and moves another", async () => {
    const { agent } = await pair(office, opened);
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
    expect(result.results[0].reads.map((b: Any) => [b.kind, b.label, b.text])).toEqual([["bullet", "•", "Hiring"], ["bullet", "•", "Supply"]]);
    expect(result.results[2].reads).toEqual([{ id: result.results[2].ids[0], kind: "heading2", text: "Risks" }]);
    const after = await blocks(agent);
    const texts = after.map((b) => b.text);
    expect(texts).not.toContain("Hiring could slip a quarter.");
    expect(texts).not.toContain("North grew fastest, then the east.");
    expect(texts.indexOf("Risks")).toBeLessThan(texts.indexOf("Regions"));
    expect(after.filter((b) => b.kind === "bullet").map((b) => b.text)).toEqual(expect.arrayContaining(["Hiring", "Supply"]));
  });

  it("refuses a block the user changed, keeps nothing of the batch, and shows the block as it is now", async () => {
    const { agent, user } = await pair(office, opened);
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
    const { agent, user } = await pair(office, opened);
    const north = byText(await blocks(agent), "North grew");
    let settled = false;
    const editing = new SlowOffice(pool)
      .edit(agent, [
        { op: "insert", target: { end: true }, content: { markdown: "Should not stay." } },
        { op: "replace", target: { block: north.id, hash: "00000000" }, content: { markdown: "Replaced." } },
      ])
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
    const { agent, user } = await pair(office, opened);
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
    const { agent } = await pair(office, opened);
    const planted = await edit(agent, [{ op: "insert", target: { end: true }, content: { markdown: "<?pending s: Summary goes here?>" } }]);
    expect(planted.results[0].reads).toEqual([{ id: planted.results[0].ids[0], kind: "pending", key: "s", intent: "Summary goes here" }]);
    expect((await blocks(agent)).find((b) => b.kind === "pending")).toMatchObject({ key: "s", intent: "Summary goes here" });
    const filled = await edit(agent, [{ op: "replace", target: { pending: "s" }, content: { markdown: "## Summary\n\nDone." } }]);
    expect(filled).toMatchObject({ applied: true });
    expect(filled.results[0].reads.map((b: Any) => [b.kind, b.text])).toEqual([["heading2", "Summary"], ["paragraph", "Done."]]);
    const after = await blocks(agent);
    expect(after.find((b) => b.kind === "pending")).toBeUndefined();
    expect(after.slice(-2).map((b) => [b.kind, b.text])).toEqual([["heading2", "Summary"], ["paragraph", "Done."]]);
    const again = await edit(agent, [{ op: "delete", target: { pending: "s" } }]);
    expect(again.refused.reason).toMatch(/is gone/);
  });

  it("says quoted words are not in the document", async () => {
    const { agent } = await pair(office, opened);
    expect((await edit(agent, [{ op: "delete", target: { find: "no such words" } }])).refused.reason).toMatch(/not in the document/);
  });

  it("writes a new document's first blocks in place of its one empty line", async () => {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT, "blank.docx"), documentServerUrl: IT.dsUrl });
    opened.push(agent);
    const result = await edit(agent, [{
      op: "insert", target: { end: true },
      content: { markdown: "# Plan\n\n## Risks\n\n<?pending r: The risks?>" },
    }]);
    expect(result).toMatchObject({ applied: true });
    const outline: Any = await office.read(agent, { view: "outline" });
    // The editor keeps a paragraph after a content control that ends the document.
    expect(outline.blocks.map((b: Any) => b.kind)).toEqual(["heading1", "heading2", "pending", "paragraph"]);
    // A document with more in it keeps its first line.
    expect(await edit(agent, [{ op: "insert", target: { end: true }, content: { markdown: "More." } }])).toMatchObject({ applied: true });
    expect(((await office.read(agent, { view: "outline" })) as Any).blocks[0].kind).toBe("heading1");
  });

  it("refuses to edit a protected document, and says so", async () => {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT, "protected.docx"), documentServerUrl: IT.dsUrl });
    opened.push(agent);
    const result = await edit(agent, [{ op: "insert", target: { end: true }, content: { markdown: "More." } }]);
    expect(result).toMatchObject({ applied: false, refused: { op: 0 } });
    expect(result.refused.reason).toMatch(/protected: it is read-only/);
  });

  it("flushes the agent's editor", async () => {
    const { agent } = await pair(office, opened);
    await edit(agent, [{ op: "insert", target: { end: true }, content: { markdown: "One more." } }]);
    expect(await office.flush(agent)).toBe(true);
  });
});
