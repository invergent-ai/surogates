import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { ContentError } from "../src/markdown.js";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { eventually, itEnabled, pair, startFixture, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
// A slide in Claude Slides' format: a pinned band below, a heading over it, a card, a list, notes.
const SLIDE = `<section style="background:#fbfbf8;color:#1a1a1a;font-family:Arial, sans-serif;padding:128px;display:flex;flex-direction:column;gap:48px">
  <div style="position:absolute;left:0;top:0;width:1920px;height:96px;background:#14285A"></div>
  <h2 style="font-size:72px;font-weight:600">Hiring plan</h2>
  <div style="display:flex;flex-direction:row;gap:32px">
    <p style="background:#ffffff;border:2px solid #e3e6e4;padding:32px;font-size:40px;width:640px">Two engineers in Lisbon</p>
    <ul style="font-size:40px;width:800px"><li>One designer</li><li>A start in March</li></ul>
  </div>
  <aside>Start with the budget.</aside>
</section>`;

describe.skipIf(!itEnabled)("a slide deck", () => {
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

  const deck = () => pair(office, opened, "sample.pptx");
  const outline = async (id: string): Promise<Any> => office.read(id, { view: "outline" });

  it("outlines its slides with ids, titles and hashes", async () => {
    const { agent, user } = await deck();
    const read = await outline(agent);
    expect(read.canvas).toEqual([1920, 1080]);
    expect(read.slides.map((s: Any) => [s.number, s.title, s.notes, s.hidden])).toEqual([[1, "Quarterly review", false, false], [2, "Next quarter", true, false]]);
    expect(read.slides[0].hash).toMatch(/^[0-9a-f]{8}$/);
    expect((await outline(user)).slides).toEqual(read.slides);
  });

  it("views a slide in full, and finds words", async () => {
    const { agent } = await deck();
    const second = (await outline(agent)).slides[1];
    const view: Any = await office.read(agent, { view: "slide", target: second.id });
    expect(view.notes).toBe("Mention the lease.");
    expect(view.shapes.flatMap((s: Any) => (s.paras ?? []).flatMap((p: Any) => p.runs.map((r: Any) => r.text)))).toContain("Open the Lisbon office");
    expect(await office.read(agent, { view: "search", query: "lisbon" })).toMatchObject({ total: 1, matches: [{ id: second.id, number: 2 }] });
  });

  it("adds a Claude Slides slide after another as native shapes, and the user sees it", async () => {
    const { agent, user } = await deck();
    const first = (await outline(agent)).slides[0];
    const result: Any = await office.edit(agent, [{ op: "slides", slides: [{ html: SLIDE, after: first.id }] }]);
    expect(result).toMatchObject({ applied: true, flushed: true, results: [{ op: 0, slides: [{ number: 2 }] }] });
    await eventually(async () => {
      const slides = (await outline(user)).slides;
      expect(slides.map((s: Any) => s.title)).toEqual(["Quarterly review", "Hiring plan", "Next quarter"]);
      // The band, the heading, the card (one shape: its fill, border and text), the list.
      expect(slides[1]).toMatchObject({ notes: true, shapes: 4 });
    });
    const view: Any = await office.read(user, { view: "slide", target: (await outline(user)).slides[1].id });
    expect(view.shapes.map((s: Any) => s.fill ?? null)).toEqual(["#14285a", "none", "#ffffff", "none"]);
  });

  it("builds a new deck of three slides as those three: its template's empty slide gives way", async () => {
    const { agent, user } = await pair(office, opened, "blank.pptx");
    expect((await outline(agent)).slides.map((s: Any) => [s.layout, s.title])).toEqual([["Title Slide", ""]]);
    const titled = (title: string) => `<section style="background:#ffffff"><h2 style="font-size:64px">${title}</h2></section>`;
    const result: Any = await office.edit(agent, [{ op: "slides", slides: [{ html: titled("One") }, { html: titled("Two") }, { html: titled("Three") }] }]);
    expect(result.results[0].slides.map((s: Any) => s.number)).toEqual([1, 2, 3]);
    await eventually(async () => {
      expect((await outline(user)).slides.map((s: Any) => s.title)).toEqual(["One", "Two", "Three"]);
    });
  });

  it("outlines a slide by its heading, not by a badge above it", async () => {
    const { agent, user } = await deck();
    const badged = `<section style="background:#ffffff;padding:128px"><p style="position:absolute;left:1480px;top:88px;width:320px;font-size:26px;transform:rotate(8deg)">NEW HUB</p><h2 style="font-size:64px">Lisbon: team and setup</h2></section>`;
    expect(((await office.edit(agent, [{ op: "slides", slides: [{ html: badged }] }])) as Any).applied).toBe(true);
    await eventually(async () => {
      expect((await outline(user)).slides[2].title).toBe("Lisbon: team and setup");
    });
  });

  it("replaces a slide in place, keeping its id", async () => {
    const { agent } = await deck();
    const second = (await outline(agent)).slides[1];
    const result: Any = await office.edit(agent, [{ op: "slides", slides: [{ id: second.id, hash: second.hash, html: SLIDE }] }]);
    expect(result.results[0].slides[0]).toEqual({ id: second.id, number: 2 });
    expect((await outline(agent)).slides[1]).toMatchObject({ id: second.id, title: "Hiring plan" });
  });

  it("a replaced slide whose HTML sets no background shows the deck's again", async () => {
    const { agent, user } = await deck();
    // The second slide's own background beside its layout's and master's, and the fill the page draws for it and for
    // the first slide, which never had one (editor internals: the builder reads no background).
    const backgrounds = async (id: string): Promise<Any> => (await (await pool.get(id)).command(`function () {
      function d(bg) { if (!bg) return null; var f = bg.bgPr && bg.bgPr.Fill && bg.bgPr.Fill.fill; return f ? "fill " + (f.color && f.color.RGBA ? [f.color.RGBA.R, f.color.RGBA.G, f.color.RGBA.B].join(",") : f.type) : "theme " + bg.bgRef.idx; }
      function drawn(s) { var f = s.backgroundFill && s.backgroundFill.fill; return f && f.color && f.color.RGBA ? [f.color.RGBA.R, f.color.RGBA.G, f.color.RGBA.B].join(",") : null; }
      var all = Api.GetPresentation().GetAllSlides(), s = all[1].Slide;
      return { own: d(s.cSld.Bg), layout: d(s.Layout.cSld.Bg), master: d(s.Layout.Master.cSld.Bg), drawn: drawn(s), first: drawn(all[0].Slide) };
    }`, {})).value;
    let second = (await outline(agent)).slides[1];
    await office.edit(agent, [{ op: "slides", slides: [{ id: second.id, hash: second.hash, html: SLIDE }] }]);
    await eventually(async () => {
      expect((await backgrounds(user)).own).toBe("fill 251,251,248");
    });
    second = (await outline(agent)).slides[1];
    const plain = `<section><h2 style="font-size:72px">Plain</h2></section>`;
    expect(((await office.edit(agent, [{ op: "slides", slides: [{ id: second.id, hash: second.hash, html: plain }] }])) as Any).applied).toBe(true);
    for (const id of [agent, user]) {
      await eventually(async () => {
        const read = await backgrounds(id);
        expect(read.own).not.toBe("fill 251,251,248");
        expect(read.own).toBe(read.layout ?? read.master);
      });
    }
    // The person's page draws the deck's background again. (The agent's page draws nothing, and its drawn fill lags.)
    await eventually(async () => {
      const read = await backgrounds(user);
      expect(read.drawn).toBe(read.first);
    });
  });

  it("a slide the user changed refuses its replace", async () => {
    const { agent, user } = await deck();
    const second = (await outline(agent)).slides[1];
    await (await pool.get(user)).command(
      "function () { var d = Api.GetPresentation().GetSlideByIndex(1).GetAllDrawings()[0]; d.GetDocContent().GetElement(0).AddText(' (edited)'); return 1; }", {},
    );
    // The replace goes once the person's change has reached the agent: sent earlier, it would replace the slide.
    await eventually(async () => {
      expect((await outline(agent)).slides[1].title).toBe("Next quarter (edited)");
    });
    const result: Any = await office.edit(agent, [{ op: "slides", slides: [{ id: second.id, hash: second.hash, html: SLIDE }] }]);
    expect(result.applied).toBe(false);
    expect(result.refused.reason).toMatch(/changed since you read it/);
    expect(result.refused.slide.title).toBe("Next quarter (edited)");
  });

  it("orders and removes slides", async () => {
    const { agent, user } = await deck();
    const [first, second] = (await outline(agent)).slides;
    expect(((await office.edit(agent, [{ op: "order", ids: [second.id, first.id] }])) as Any).results[0].order).toEqual([second.id, first.id]);
    const moved = (await outline(agent)).slides;
    await office.edit(agent, [{ op: "remove", slides: [{ id: moved[1].id, hash: moved[1].hash }] }]);
    await eventually(async () => {
      expect((await outline(user)).slides.map((s: Any) => s.title)).toEqual(["Next quarter"]);
    });
  });

  it("refuses a slide whose text overflows, before the editor sees it", async () => {
    const { agent } = await deck();
    const crowded = `<section style="background:#fff"><p style="position:absolute;left:0;top:0;width:400px;height:60px;font-size:40px">${"Far too many words for this box. ".repeat(6)}</p></section>`;
    await expect(office.edit(agent, [{ op: "slides", slides: [{ html: crowded }] }])).rejects.toBeInstanceOf(ContentError);
    expect((await outline(agent)).slides).toHaveLength(2);
  });

  it("keeps nothing of a refused batch", async () => {
    const { agent, user } = await deck();
    const second = (await outline(agent)).slides[1];
    const result: Any = await office.edit(agent, [
      { op: "slides", slides: [{ html: SLIDE }] },
      { op: "remove", slides: [{ id: second.id, hash: "00000000" }] },
    ]);
    expect(result.applied).toBe(false);
    await sleep(2_000);
    for (const id of [agent, user]) expect((await outline(id)).slides).toHaveLength(2);
  });

  it("turns the editor's object registry back on after a failed command, so the document keeps saving", async () => {
    const { agent, user } = await deck();
    const page = await pool.get(agent);
    const second = (await outline(agent)).slides[1];
    // As a builder call that throws inside the editor's no-history section leaves it.
    await page.command("function () { AscCommon.g_oTableId.m_bTurnOff = true; return 1; }", {});
    const refused: Any = await office.edit(agent, [{ op: "remove", slides: [{ id: second.id, hash: "00000000" }] }]);
    expect(refused.applied).toBe(false);
    expect(await page.poisoned()).toBe(false);
    // The agent's next edit reaches the person, so the document still saves.
    expect(((await office.edit(agent, [{ op: "slides", slides: [{ html: SLIDE }] }])) as Any).applied).toBe(true);
    await eventually(async () => {
      expect((await outline(user)).slides).toHaveLength(3);
    });
  });

  it("lists a person's comment with its slide, and answers it there", async () => {
    const { agent, user } = await pair(office, opened, "sample.pptx");
    const page = await pool.get(user);
    // The builder's AddComment comments the editor's current slide, whichever slide it is called on, and ApiSlide.Select
    // does not move it on this page: the person goes to the second slide first (internal).
    await page.command("function () { var p = Api.GetPresentation(); p.Presentation.Set_CurPage(1); p.GetSlideByIndex(1).AddComment(914400, 914400, scope.text, scope.name, scope.id); return true; }",
      { text: "@Agent which month for Lisbon?", name: "Flavius", id: "user:it" });
    let thread = "";
    await eventually(async () => {
      thread = (await page.command("function () { var c = Api.GetPresentation().GetAllComments()[0]; return c ? c.Comment.GetData().m_sGuid : ''; }", {}))
        .value as string;
      expect(thread).toMatch(/^\{[0-9A-F-]+\}$/i);
    });
    const comments = async (id: string): Promise<Any[]> => ((await office.read(id, { view: "outline" })) as Any).comments;
    await eventually(async () => {
      expect(await comments(agent)).toEqual([{ id: thread, slide: 2, author: "Flavius", text: "@Agent which month for Lisbon?", replies: [], resolved: false }]);
    });
    const refused = (await office.edit(agent, [{ op: "reply", thread, body: "x" }, { op: "resolve", thread: "{00000000-0000-0000-0000-000000000000}" }])) as Any;
    expect(refused).toMatchObject({ applied: false, refused: { op: 1 } });
    expect(await office.edit(agent, [{ op: "reply", thread, body: "March: the lease starts then." }, { op: "resolve", thread }])).toMatchObject({ applied: true });
    await eventually(async () => {
      expect((await comments(user))[0]).toMatchObject({ resolved: true, replies: [{ author: "Agent", text: "March: the lease starts then." }] });
    });
  });
});
