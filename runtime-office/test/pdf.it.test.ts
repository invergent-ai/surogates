import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { EditorError } from "../src/formats/format.js";
import { formatsFor } from "../src/formats/index.js";
import { pdf } from "../src/formats/pdf.js";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { eventually, itEnabled, pair, startFixture, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

describe.skipIf(!itEnabled)("a PDF", () => {
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

  const outline = async (id: string): Promise<Any> => office.read(id, { view: "outline" });
  const fieldValues = (read: Any): Record<string, unknown> => Object.fromEntries(read.pages[0].fields.map((f: Any) => [f.name, f.value]));

  it("outlines its pages, fields and annotations", async () => {
    const { agent } = await pair(office, opened, "sample.pdf");
    const read = await outline(agent);
    expect(read.total).toBe(2);
    expect(read.pages[0]).toMatchObject({ page: 1, rotation: 0, first_line: "Service agreement" });
    expect(read.pages[0].fields.map((f: Any) => [f.name, f.type])).toEqual([["client", "text"], ["signed", "checkbox"]]);
  });

  it("reads a page's text, and finds words", async () => {
    const { agent } = await pair(office, opened, "sample.pdf");
    expect(((await office.read(agent, { view: "page", target: "2" })) as Any).text).toMatch(/schedule of services/);
    expect(await office.read(agent, { view: "search", query: "invoice" })).toMatchObject({ total: 1, matches: [{ page: 1 }] });
  });

  it("fills and adds fields, adds and rotates pages and stamps, and the user sees it", async () => {
    const { agent, user } = await pair(office, opened, "sample.pdf");
    const result: Any = await office.edit(agent, [
      { op: "fill", fields: { client: "ACME Lisbon Lda", signed: true } },
      { op: "add_field", type: "text", page: 1, rect: [170, 250, 420, 270], name: "po_number", value: "PO-4471" },
      { op: "pages", action: "add", page: 2 },
      { op: "pages", action: "rotate", page: 2, degrees: 90 },
      { op: "annotate", kind: "stamp", page: 1, rect: [300, 360, 450, 410], stamp: "approved" },
      { op: "annotate", kind: "free_text", page: 1, rect: [60, 560, 280, 600], text: "Checked against the order." },
    ]);
    expect(result).toMatchObject({ applied: true, flushed: true });
    await eventually(async () => {
      const read = await outline(user);
      expect(read.total).toBe(3);
      expect(read.pages[1].rotation).toBe(90);
      expect(fieldValues(read)).toMatchObject({ client: "ACME Lisbon Lda", po_number: "PO-4471" });
      expect(read.pages[0].annotations.map((a: Any) => a.type).sort()).toEqual(["freeText", "stamp"]);
    });
  });

  it("redacts words from the document", async () => {
    const { agent } = await pair(office, opened, "sample.pdf");
    const result: Any = await office.edit(agent, [{ op: "redact", text: "thirty" }]);
    expect(result.results[0]).toEqual({ op: 0, redacted: 1 });
    expect((await office.read(agent, { view: "search", query: "thirty" }) as Any).total).toBe(0);
  });

  it("keeps the document as it was after a refused batch", async () => {
    const { agent } = await pair(office, opened, "sample.pdf");
    const result: Any = await office.edit(agent, [
      { op: "fill", fields: { client: "Should not land" } },
      { op: "fill", fields: { missing: "x" } },
    ]);
    expect(result).toMatchObject({ applied: false, refused: { op: 1 } });
    expect((await outline(agent)).pages[0].fields.find((f: Any) => f.name === "client").value).toBeNull();
  });

  it("refuses a fill whose field the user changed since the agent read it, and keeps the user's value", async () => {
    const { agent, user } = await pair(office, opened, "sample.pdf");
    expect(await office.edit(user, [{ op: "fill", fields: { client: "The person wrote this" } }])).toMatchObject({ applied: true });
    await eventually(async () => expect(fieldValues(await outline(agent)).client).toBe("The person wrote this"));
    const result: Any = await office.edit(agent, [{ op: "fill", fields: { client: "ACME Lisbon Lda" }, expect: { client: null } }]);
    expect(result).toMatchObject({ applied: false, refused: { op: 0 } });
    expect(result.refused.reason).toContain('"The person wrote this"');
    for (const id of [agent, user]) expect(fieldValues(await outline(id)).client).toBe("The person wrote this");
  });

  // A batch whose edit runs and then fails, as one that throws partway does.
  const failingOffice = () => new Office(pool, {
    ...formatsFor(pool),
    pdf: { ...pdf, edit: async (context, ops) => { await pdf.edit(context, ops); throw new EditorError("the command failed partway"); } },
  });

  it("undoes a failed batch that filled a field, in both editors", async () => {
    const { agent, user } = await pair(office, opened, "sample.pdf");
    expect(await office.edit(user, [{ op: "fill", fields: { client: "The person wrote this" } }])).toMatchObject({ applied: true });
    await eventually(async () => expect(fieldValues(await outline(agent)).client).toBe("The person wrote this"));
    await expect(failingOffice().edit(agent, [{ op: "fill", fields: { client: "Should not stay" } }])).rejects.toEqual(
      new EditorError("the command failed partway; what it changed was undone"));
    for (const id of [agent, user]) {
      await eventually(async () => expect(fieldValues(await outline(id)).client).toBe("The person wrote this"));
    }
  });

  it("undoes a batch that failed after changing the document, in both editors, and keeps the person's own edit", async () => {
    const failing = failingOffice();
    const { agent, user } = await pair(office, opened, "sample.pdf");
    expect(await office.edit(user, [{ op: "fill", fields: { client: "The person wrote this" } }])).toMatchObject({ applied: true });
    await eventually(async () => expect(fieldValues(await outline(agent)).client).toBe("The person wrote this"));
    await expect(failing.edit(agent, [{ op: "pages", action: "add", page: 1 }])).rejects.toEqual(
      new EditorError("the command failed partway; what it changed was undone"));
    for (const id of [agent, user]) {
      await eventually(async () => {
        const read = await outline(id);
        expect(read.total).toBe(2);
        expect(fieldValues(read).client).toBe("The person wrote this");
      });
    }
  });
});
