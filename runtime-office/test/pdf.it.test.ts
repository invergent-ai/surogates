import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { EditorError } from "../src/formats/format.js";
import { formatsFor } from "../src/formats/index.js";
import { pdf } from "../src/formats/pdf.js";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, IT, USER, editorConfig, eventually, itEnabled, newKey, pair, startFixture, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

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
  const overlaps = (a: number[], b: number[]): boolean => a[0]! < b[2]! && b[0]! < a[2]! && a[1]! < b[3]! && b[1]! < a[3]!;
  const near = (a: number[], b: number[], by = 1): boolean => a.every((n, i) => Math.abs(n - b[i]!) < by);
  const fieldValues = (read: Any): Record<string, unknown> => Object.fromEntries(read.pages[0].fields.map((f: Any) => [f.name, f.value]));

  // A person's sticky note on a page, or their reply to the first one with words, made in their editor.
  async function personNotes(user: string, page: number, text: string): Promise<void> {
    await (await pool.get(user)).command(
      "function () { var a = Api.CreateTextAnnot([300, 100, 320, 120]); a.SetContents(scope.text); a.SetAuthorName(scope.name);" +
        " a.Annot.SetUserId(scope.id); Api.GetDocument().GetPage(scope.page).AddObject(a); return true; }",
      { page: page - 1, text, name: USER.name, id: USER.id },
    );
  }
  async function personReplies(user: string, text: string): Promise<void> {
    await (await pool.get(user)).command(
      "function () { var a = Api.GetDocument().GetPage(0).GetAllAnnots().filter(function (x) { return x.GetContents(); })[0];" +
        " var r = Api.CreateTextAnnot(a.GetRect()); r.SetContents(scope.text); r.SetAuthorName(scope.name); r.Annot.SetUserId(scope.id);" +
        " return a.AddReply(r); }",
      { text, name: USER.name, id: USER.id },
    );
  }
  // A person's comment on page 1 and their reply to it, made in their editor's comment box: Collaboration → Add Comment,
  // type, Add; then Add Reply, type, Reply. The builder selects the page's words first, as a double-click selects one.
  async function personCommentsInTheBox(user: string, text: string, reply: string): Promise<void> {
    const page = await pool.get(user);
    await page.command("function () { Api.GetDocument().GetPage(0).SetSelection({ x: 0.01, y: 0.01 }, { x: 100000, y: 100000 }); return true; }", {});
    const browser = (page as Any).page, frame = browser.frames().find((f: Any) => f.name() === "frameEditor");
    await frame.click('a[data-tab="review"]');
    await frame.click("#tlbtn-addcomment-0");
    await browser.keyboard.type(text);
    await frame.click("#id-comments-change-popover:not(.disabled)");
    await frame.click("label.user-reply");
    await browser.keyboard.type(reply);
    await frame.click("#id-comments-change-popover:not(.disabled)");
  }
  const notes = async (id: string, page: number): Promise<Any[]> =>
    (await outline(id)).pages[page - 1].annotations.filter((a: Any) => a.type === "text");
  const lookup = (id: string, query: string): Promise<Any> => office.read(id, { view: "thread", query, author: USER.id });

  it("finds the thread a person's comment or reply mentioned the agent in, and answers it there", async () => {
    const { agent, user } = await pair(office, opened, "sample.pdf");
    await personNotes(user, 1, "@Agent is March the right start?");
    let note: Any;
    await eventually(async () => {
      [note] = await notes(agent, 1);
      expect(note).toMatchObject({ author: "Flavius", text: "@Agent is March the right start?", replies: [], resolved: false });
    });
    expect((await lookup(agent, "@Agent is March the right start?")).thread).toMatchObject({ id: note.id, page: 1 });
    await personReplies(user, "@Agent and the fee terms?");
    await eventually(async () => expect((await lookup(agent, "@Agent and the fee terms?")).thread?.id).toBe(note.id));
    expect((await lookup(agent, "words nobody wrote")).thread).toBeNull();
    expect(await office.edit(agent, [{ op: "reply", thread: note.id, body: "March: the contract says 1 March 2026." }, { op: "resolve", thread: note.id }]))
      .toMatchObject({ applied: true });
    await eventually(async () => {
      expect((await notes(user, 1))[0]).toMatchObject({ id: note.id, resolved: true, replies: [
        { author: "Flavius", text: "@Agent and the fee terms?" }, { author: "Agent", text: "March: the contract says 1 March 2026." }] });
    });
  });

  it("finds the thread of a comment and a reply the person made in their editor's comment box", async () => {
    const { agent, user } = await pair(office, opened, "sample.pdf");
    await personCommentsInTheBox(user, "@Agent is March the right start?", "@Agent and the fee terms?");
    let thread: Any;
    await eventually(async () => {
      thread = (await lookup(agent, "@Agent and the fee terms?")).thread;
      expect(thread).toMatchObject({ page: 1, author: "Flavius", text: "@Agent is March the right start?",
        replies: [{ author: "Flavius", text: "@Agent and the fee terms?" }] });
    });
    expect((await lookup(agent, "@Agent is March the right start?")).thread?.id).toBe(thread.id);
  });

  it("picks the newest of two identical comments by the person, though it sits on an earlier page", async () => {
    const { agent, user } = await pair(office, opened, "sample.pdf");
    await personNotes(user, 2, "@Agent check this");
    await sleep(1_100);   // the second note is made well after the first
    await personNotes(user, 1, "@Agent check this");
    let newer: Any;
    await eventually(async () => {
      [newer] = await notes(agent, 1);
      expect(newer).toBeTruthy();
      expect(await notes(agent, 2)).toHaveLength(1);
    });
    expect((await lookup(agent, "@Agent check this")).thread).toMatchObject({ id: newer.id, page: 1 });
  });

  it("finds a thread made before its page opened", async () => {
    const key = newKey();
    const user = await office.open({ docKey: key, sessionId: "user", config: editorConfig(key, USER, "sample.pdf"), documentServerUrl: IT.dsUrl });
    opened.push(user);
    await personNotes(user, 1, "@Agent late join");
    await sleep(1_000);
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT, "sample.pdf"), documentServerUrl: IT.dsUrl });
    opened.push(agent);
    await eventually(async () => expect((await lookup(agent, "@Agent late join")).thread?.page).toBe(1));
  });

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
    // A note or stamp placed at a rect says where it landed: a stamp keeps the corner it was given, at its own size.
    expect(result.results[5]).toEqual({ op: 5, page: 1, rect: [60, 560, 280, 600] });
    expect(result.results[4]).toMatchObject({ op: 4, page: 1 });
    expect(near(result.results[4].rect.slice(0, 2), [300, 360], 2)).toBe(true);
    await eventually(async () => {
      const read = await outline(user);
      expect(read.total).toBe(3);
      expect(read.pages[1].rotation).toBe(90);
      expect(fieldValues(read)).toMatchObject({ client: "ACME Lisbon Lda", po_number: "PO-4471" });
      expect(read.pages[0].annotations.map((a: Any) => a.type).sort()).toEqual(["freeText", "stamp"]);
    });
  });

  // A page's text lines, each one box [x1, y1, x2, y2] in points from the top-left: the page selected whole, the last
  // selection's boxes cleared first.
  const textLines = async (id: string, page: number): Promise<number[][]> => (await (await pool.get(id)).command(
    "function () { var doc = Api.GetDocument(), page = doc.GetPage(scope.page); doc.Document.GetFile().removeSelection();" +
      " page.SetSelection({ x: 0.01, y: 0.01 }, { x: 100000, y: 100000 });" +
      " return page.GetSelectionQuads().map(function (q) { var xs = [q[0], q[2], q[4], q[6]], ys = [q[1], q[3], q[5], q[7]];" +
      " return [Math.min.apply(null, xs), Math.min.apply(null, ys), Math.max.apply(null, xs), Math.max.apply(null, ys)]; }); }",
    { page: page - 1 },
  )).value as number[][];

  it("places a note beside its words", async () => {
    const { agent, user } = await pair(office, opened, "sample.pdf");
    const lines = await textLines(agent, 1);
    // The fixture's line "Fees are due within thirty days of each invoice.", 12 pt, its baseline 130 pt from the top.
    expect(lines.some((line) => line[1]! < 130 && line[3]! > 125)).toBe(true);
    const result: Any = await office.edit(agent, [
      { op: "annotate", kind: "free_text", anchor: { find: "thirty days" }, text: "Net 30 from the invoice date." },
    ]);
    expect(result).toMatchObject({ applied: true, results: [{ op: 0, page: 1, beside: "thirty days" }] });
    const rect: number[] = result.results[0].rect;
    expect(rect[1]! < 134 && rect[3]! > 118).toBe(true);
    for (const line of lines) expect(overlaps(rect, line), JSON.stringify([rect, line])).toBe(false);
    await eventually(async () => {
      const notes = (await outline(user)).pages[0].annotations.filter((a: Any) => a.type === "freeText");
      expect(notes).toHaveLength(1);
      expect(near(notes[0].rect, rect), JSON.stringify([notes[0].rect, rect])).toBe(true);
    });
  });

  it("places a stamp by its words", async () => {
    const { agent, user } = await pair(office, opened, "sample.pdf");
    const lines = await textLines(agent, 1);
    const result: Any = await office.edit(agent, [
      { op: "annotate", kind: "stamp", anchor: { find: "thirty days" }, stamp: "approved" },
    ]);
    expect(result).toMatchObject({ applied: true, results: [{ op: 0, page: 1, beside: "thirty days" }] });
    const rect: number[] = result.results[0].rect;
    for (const line of lines) expect(overlaps(rect, line), JSON.stringify([rect, line])).toBe(false);
    const stamps = (await outline(agent)).pages[0].annotations.filter((a: Any) => a.type === "stamp");
    expect(stamps).toHaveLength(1);
    expect(near(stamps[0].rect, rect), JSON.stringify([stamps[0].rect, rect])).toBe(true);
    // As the person's editor draws it, which can grow it a few points a side.
    await eventually(async () => {
      const [drawn] = (await outline(user)).pages[0].annotations.filter((a: Any) => a.type === "stamp");
      expect(drawn).toBeTruthy();
      expect(drawn.rect[2]).toBeLessThanOrEqual(595);
      for (const line of lines) expect(overlaps(drawn.rect, line), JSON.stringify([drawn.rect, line])).toBe(false);
    });
  });

  it("finds words on a later page, and counts nth through the pages in order", async () => {
    const { agent } = await pair(office, opened, "sample.pdf");
    // "schedule" is only on page 2; "service" is twice on page 1 ("Service agreement", "the service") and once on page 2.
    const later: Any = await office.edit(agent, [{ op: "annotate", kind: "free_text", anchor: { find: "schedule" }, text: "Attach the schedule." }]);
    expect(later).toMatchObject({ applied: true, results: [{ op: 0, page: 2, beside: "schedule" }] });
    const [line] = await textLines(agent, 2);   // "Page two: the schedule of services."
    const rect: number[] = later.results[0].rect;
    expect(overlaps(rect, line!)).toBe(false);
    expect(rect[0]! > line![2]! && rect[1]! < line![3]! && rect[3]! > line![1]!, JSON.stringify([rect, line])).toBe(true);   // right of it
    const counted: Any = await office.edit(agent, [
      { op: "annotate", kind: "free_text", anchor: { find: "service", nth: 1 }, text: "The agreement's title." },
      { op: "annotate", kind: "free_text", anchor: { find: "service", nth: 3 }, text: "As the schedule lists them." },
    ]);
    expect(counted).toMatchObject({ applied: true, results: [{ op: 0, page: 1, beside: "service" }, { op: 1, page: 2, beside: "service" }] });
    // The second is mid-page on page 1, a line above and below it and the page's edge to its right.
    const second: Any = await office.edit(agent, [{ op: "annotate", kind: "free_text", anchor: { find: "service", nth: 2 }, text: "x" }]);
    expect(second.refused.reason).toBe('There is no free place beside "service" on page 1: give "page" and "rect".');
    const past: Any = await office.edit(agent, [{ op: "annotate", kind: "free_text", anchor: { find: "service", nth: 4 }, text: "x" }]);
    expect(past.refused.reason).toBe('"service" is in the document 3 times: nth counts from 1 up to 3.');
  });

  it("finds the page's own words, not a note's", async () => {
    const { agent } = await pair(office, opened, "sample.pdf");
    const first: Any = await office.edit(agent, [
      { op: "annotate", kind: "free_text", anchor: { find: "thirty days" }, text: "Net 30 from the invoice date." },
    ]);
    const note: number[] = first.results[0].rect;
    const [line] = (await textLines(agent, 1)).filter((l) => l[1]! < 130 && l[3]! > 125);   // "Fees are due … of each invoice."
    const second: Any = await office.edit(agent, [
      { op: "annotate", kind: "free_text", anchor: { find: "invoice" }, text: "Each invoice, not the month." },
    ]);
    expect(second).toMatchObject({ applied: true, results: [{ op: 0, page: 1, beside: "invoice" }] });
    const rect: number[] = second.results[0].rect;
    expect(overlaps(rect, note), JSON.stringify([rect, note])).toBe(false);
    // Placed from the page's "invoice", it starts within its line; from the note's, it would start right of the note.
    expect(rect[0]!, JSON.stringify([rect, line])).toBeLessThan(line![2]!);
    const past: Any = await office.edit(agent, [{ op: "annotate", kind: "free_text", anchor: { find: "invoice", nth: 2 }, text: "x" }]);
    expect(past.refused.reason).toBe('"invoice" is in the document 1 times: nth counts from 1 up to 1.');
  });

  it("refuses when no place beside the words is free, and changes nothing", async () => {
    const { agent } = await pair(office, opened, "sample.pdf");
    const result: Any = await office.edit(agent, [
      { op: "annotate", kind: "free_text", anchor: { find: "thirty days" }, text: "Net 30 from the invoice date." },
      { op: "annotate", kind: "stamp", anchor: { find: "thirty days" }, stamp: "approved" },
    ]);
    expect(result).toMatchObject({ applied: false, refused: { op: 1 } });
    expect(result.refused.reason).toBe('There is no free place beside "thirty days" on page 1: give "page" and "rect".');
    expect((await outline(agent)).pages[0].annotations).toEqual([]);
  });

  it("places two notes on one line apart, on the page as the call's earlier ops leave it", async () => {
    const { agent } = await pair(office, opened, "sample.pdf");
    const result: Any = await office.edit(agent, [
      { op: "pages", action: "add", page: 0 },
      { op: "annotate", kind: "free_text", anchor: { find: "thirty days" }, text: "Net 30 from the invoice date." },
      { op: "annotate", kind: "free_text", anchor: { find: "invoice" }, text: "Each invoice, not the month." },
    ]);
    expect(result).toMatchObject({ applied: true, results: [{ op: 0 }, { op: 1, page: 2 }, { op: 2, page: 2, beside: "invoice" }] });
    const [first, second] = [result.results[1].rect, result.results[2].rect];
    expect(overlaps(first, second), JSON.stringify([first, second])).toBe(false);
    for (const line of await textLines(agent, 2)) {
      for (const rect of [first, second]) expect(overlaps(rect, line), JSON.stringify([rect, line])).toBe(false);
    }
  });

  it("refuses words that are not there", async () => {
    const { agent } = await pair(office, opened, "sample.pdf");
    const result: Any = await office.edit(agent, [
      { op: "annotate", kind: "free_text", anchor: { find: "sixty days" }, text: "Net 60." },
    ]);
    expect(result).toMatchObject({ applied: false, refused: { op: 0, reason: '"sixty days" is not in the document.' } });
    expect((await outline(agent)).pages[0].annotations).toEqual([]);
  });

  it("refuses an nth past the matches", async () => {
    const { agent } = await pair(office, opened, "sample.pdf");
    const result: Any = await office.edit(agent, [
      { op: "annotate", kind: "free_text", anchor: { find: "thirty days", nth: 2 }, text: "Net 30." },
    ]);
    expect(result).toMatchObject({ applied: false, refused: { op: 0 } });
    expect(result.refused.reason).toBe('"thirty days" is in the document 1 times: nth counts from 1 up to 1.');
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
