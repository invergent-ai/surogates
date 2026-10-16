import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { eventually, itEnabled, pair, startFixture, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe.skipIf(!itEnabled)("a spreadsheet", () => {
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

  const range = async (id: string, target: string): Promise<Any> => office.read(id, { view: "range", target });
  // Opens the person's cell editor on a cell, as clicking into it and typing does (internal names, test only).
  async function typeInto(user: string, sheet: string, row: number, col: number, text: string): Promise<void> {
    const frame = ((await pool.get(user)) as Any).page.frames().find((f: Any) => f.name() === "frameEditor");
    await frame.evaluate(({ sheet, row, col, text }: Any) => {
      const w = window as Any, api = w.Asc.editor;
      api.asc_showWorksheet(api.wbModel.getWorksheetByName(sheet).getIndex());
      const view = api.wb.getWorksheet();
      view.setSelection(new w.Asc.Range(col, row, col, row));
      const options = new w.AscCommonExcel.CEditorEnterOptions();
      options.newText = text;
      options.focus = true;
      api.wb._onEditCell(options);
    }, { sheet, row, col, text });
  }

  // A person's comment on a cell, made in their editor; its GUID is what the editor's mention of it carries. The comment
  // lands after the command, as the agent's do.
  async function personComments(user: string, sheet: string, cell: string, text: string): Promise<string> {
    const page = await pool.get(user);
    await page.command("function () { var s = Api.GetSheet(scope.sheet); s.SetActive(); s.GetRange(scope.cell).AddComment(scope.text, scope.name); return true; }",
      { sheet, cell, text, name: "Flavius" });
    let guid = "";
    await eventually(async () => {
      guid = (await page.command("function () { var c = Api.GetSheet(scope.sheet).GetComments()[0]; return c ? c.Comment.asc_getGuid() : ''; }",
        { sheet })).value as string;
      expect(guid).toMatch(/^\{[0-9A-F-]+\}$/i);
    });
    return guid;
  }
  const threads = async (id: string): Promise<Any[]> => ((await office.read(id, { view: "outline" })) as Any).comments;

  it("lists a person's comment with its cell, and answers it on another sheet than the agent's", async () => {
    const { agent, user } = await pair(office, opened, "sample.xlsx");
    const thread = await personComments(user, "Notes", "A1", "@Agent are these thousands?");
    await eventually(async () => {
      expect(await threads(agent)).toEqual([{ id: thread, cell: "Notes!A1", author: "Flavius", text: "@Agent are these thousands?",
        replies: [], resolved: false }]);
    });
    expect(await office.edit(agent, [{ op: "reply", thread, body: "Yes: thousands of euros." }, { op: "resolve", thread }]))
      .toMatchObject({ applied: true });
    await eventually(async () => {
      expect((await threads(user))[0]).toMatchObject({ id: thread, resolved: true, replies: [{ author: "Agent", text: "Yes: thousands of euros." }] });
    });
  });

  it("applies nothing when one thread of the call is gone", async () => {
    const { agent, user } = await pair(office, opened, "sample.xlsx");
    const thread = await personComments(user, "Sales", "B2", "@Agent source?");
    await eventually(async () => expect((await threads(agent)).map((t: Any) => t.id)).toEqual([thread]));
    const refused = (await office.edit(agent, [
      { op: "reply", thread, body: "x" },
      { op: "reply", thread: "{00000000-0000-0000-0000-000000000000}", body: "y" },
    ])) as Any;
    expect(refused).toMatchObject({ applied: false, refused: { op: 1 } });
    expect(refused.refused.reason).toMatch(/is gone/);
    await sleep(500);
    expect((await threads(user))[0].replies).toEqual([]);
  });

  it("refuses a thread named like an object's built-in name as gone", async () => {
    const { agent } = await pair(office, opened, "sample.xlsx");
    for (const thread of ["constructor", "__proto__"]) {
      const refused = (await office.edit(agent, [{ op: "resolve", thread }])) as Any;
      expect(refused, thread).toMatchObject({ applied: false, refused: { op: 0 } });
      expect(refused.refused.reason).toMatch(/is gone/);
    }
  });

  it("outlines its sheets", async () => {
    const { agent } = await pair(office, opened, "sample.xlsx");
    const read: Any = await office.read(agent, { view: "outline" });
    expect(read.workbook_protected).toBe(false);
    expect(read.sheets.map((s: Any) => [s.name, s.used, s.protected])).toEqual([["Sales", "A1:D4", false], ["Notes", "A1", false]]);
    expect(read.sheets[0].top[0]).toEqual(["Region", "Q1", "Q2", "Total"]);
  });

  it("views a range as shown, with its formulas and number formats", async () => {
    const { agent } = await pair(office, opened, "sample.xlsx");
    expect(await range(agent, "Sales!A1:D2")).toEqual({
      range: "Sales!A1:D2", values: [["Region", "Q1", "Q2", "Total"], ["North", "120", "140", "260"]],
      formulas: { D2: "=B2+C2" }, formats: { B2: "#,##0" },
    });
    expect(await range(agent, "Missing!A1")).toEqual({ error: "There is no sheet Missing: read the outline again." });
  });

  it("finds text in every sheet", async () => {
    const { agent } = await pair(office, opened, "sample.xlsx");
    expect(await office.read(agent, { view: "search", query: "euros" })).toEqual({
      matches: [{ cell: "Notes!A1", text: "Figures in thousands of euros." }], total: 1,
    });
  });

  it("writes values and live formulas, which the user sees", async () => {
    const { agent, user } = await pair(office, opened, "sample.xlsx");
    const result: Any = await office.edit(agent, [{ op: "set", range: "Sales!A5", values: [["West", 50, 60, "=B5+C5"]] }]);
    expect(result).toEqual({ applied: true, results: [{ op: 0, range: "Sales!A5:D5" }], flushed: true });
    await eventually(async () => {
      const shown = await range(user, "Sales!A5:D5");
      expect(shown.values).toEqual([["West", "50", "60", "110"]]);
      expect(shown.formulas).toEqual({ D5: "=B5+C5" });
    });
  });

  it("refuses a write to a range that changed since it was read, showing it as it is", async () => {
    const { agent } = await pair(office, opened, "sample.xlsx");
    const result: Any = await office.edit(agent, [{ op: "set", range: "Sales!B2", values: [[121]], expect: [["99"]] }]);
    expect(result.applied).toBe(false);
    expect(result.refused).toMatchObject({ op: 0, now: [["120"]] });
    expect((await range(agent, "Sales!B2")).values).toEqual([["120"]]);
  });

  it("formats, and adds a named chart the user sees", async () => {
    const { agent, user } = await pair(office, opened, "sample.xlsx");
    const result: Any = await office.edit(agent, [
      { op: "format", range: "Sales!A1:D1", fill: "#E6F0FF", width: 14 },
      { op: "chart", range: "Sales!A1:C4", type: "column", title: "By region", anchor: "F2" },
    ]);
    expect(result).toMatchObject({ applied: true, results: [{ op: 0 }, { op: 1, chart: "By region" }] });
    await eventually(async () => {
      const charts = ((await office.read(user, { view: "outline" })) as Any).sheets[0].charts;
      expect(charts).toEqual([expect.objectContaining({ name: "By region", type: "column", title: "By region", anchor: "F2" })]);
    });
  });

  it("adds a sheet and writes into it in one batch while the user is connected", async () => {
    const { agent, user } = await pair(office, opened, "sample.xlsx");
    const result: Any = await office.edit(agent, [
      { op: "sheet", action: "add", name: "Plan", after: "Sales" },
      { op: "set", range: "Plan!A1", values: [["Hire two engineers"]] },
    ]);
    expect(result).toMatchObject({ applied: true, results: [{ op: 0, sheet: "Plan" }, { op: 1, range: "Plan!A1" }] });
    await eventually(async () => {
      expect(((await office.read(user, { view: "outline" })) as Any).sheets.map((s: Any) => s.name)).toEqual(["Sales", "Plan", "Notes"]);
      expect((await range(user, "Plan!A1")).values).toEqual([["Hire two engineers"]]);
    });
  });

  it("keeps nothing of a refused batch, a sheet it added included", async () => {
    const { agent, user } = await pair(office, opened, "sample.xlsx");
    const result: Any = await office.edit(agent, [
      { op: "sheet", action: "add", name: "Scratch" },
      { op: "set", range: "Sales!B2", values: [[1]], expect: [["0"]] },
    ]);
    expect(result.applied).toBe(false);
    await sleep(2_000);
    for (const id of [agent, user]) {
      expect(((await office.read(id, { view: "outline" })) as Any).sheets.map((s: Any) => s.name)).toEqual(["Sales", "Notes"]);
    }
  });

  it("refuses a batch writing a cell the user is typing in, and names them", async () => {
    const { agent, user } = await pair(office, opened, "sample.xlsx");
    await typeInto(user, "Sales", 6, 1, "typing");
    // The person's lock reaches the agent's editor within a moment; an edit sent before it would apply.
    await sleep(2_000);
    const result: Any = await office.edit(agent, [{ op: "set", range: "Sales!B7", values: [["agent"]] }]);
    expect(result.applied).toBe(false);
    expect(result.refused.reason).toMatch(/^Flavius is editing Sales!B7 right now/);
    expect((await range(agent, "Sales!B7")).values).toEqual([[""]]);
  });

  it("refuses locked cells and formats on a protected sheet", async () => {
    const { agent } = await pair(office, opened, "protected.xlsx");
    const locked: Any = await office.edit(agent, [{ op: "set", range: "Locked!B2", values: [[2000]] }]);
    expect(locked.refused.reason).toMatch(/protected part of Locked/);
    const open: Any = await office.edit(agent, [{ op: "set", range: "Locked!B3", values: [[6]] }]);
    expect(open.applied).toBe(true);
    const width: Any = await office.edit(agent, [{ op: "format", range: "Locked!A1", width: 25 }]);
    expect(width.refused.reason).toMatch(/protected against formatting/);
  });

  it("refuses a sheet change in a workbook whose structure is protected", async () => {
    const { agent } = await pair(office, opened, "structure.xlsx");
    const result: Any = await office.edit(agent, [{ op: "sheet", action: "add", name: "New" }]);
    expect(result.refused.reason).toMatch(/structure is protected/);
    expect(((await office.read(agent, { view: "outline" })) as Any).sheets.map((s: Any) => s.name)).toEqual(["Only"]);
  });

  it("is flushed after an edit, for the turn's save", async () => {
    const { agent } = await pair(office, opened, "sample.xlsx");
    await office.edit(agent, [{ op: "set", range: "Notes!A2", values: [["saved"]] }]);
    expect(await office.flush(agent)).toBe(true);
  });
});
