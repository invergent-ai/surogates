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
