import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

// editor/slide.js run against a stand-in for the builder: a deck of slides that hold text boxes.
const SCRIPT = readFileSync(new URL("../editor/slide.js", import.meta.url), "utf8");
const EMPTY_SPEC = { background: null, shapes: [], notes: null, hidden: false, transition: null, ignored: [] };

// A text body's paragraphs, each with its alignment and its runs, each run with its text and a style name.
interface ParaStub { jc?: string; runs: { text: string; style?: string }[] }
function runOf(r: { text: string; style?: string }): Any {
  const run: Any = { text: r.text, style: r.style ?? "plain", GetClassType: () => "run", GetText: () => run.text };
  run.ClearContent = () => { run.text = ""; return true; };
  run.AddText = (text: string) => { run.text += text; return true; };
  return run;
}
function paraOf(p: ParaStub): Any {
  const para: Any = { jc: p.jc ?? "left", items: p.runs.map(runOf) };
  para.GetElementsCount = () => para.items.length;
  para.GetElement = (j: number) => para.items[j] ?? null;
  para.RemoveElement = (j: number) => { para.items.splice(j, 1); return true; };
  para.AddElement = (el: Any) => { para.items.push(el); return true; };
  para.Copy = () => paraOf({ jc: para.jc, runs: para.items.map((r: Any) => ({ text: r.text, style: r.style })) });
  return para;
}
function contentOf(paras: ParaStub[]): Any {
  const content: Any = { paras: paras.map(paraOf) };
  content.GetElementsCount = () => content.paras.length;
  content.GetElement = (i: number) => content.paras[i] ?? null;
  content.Push = (p: Any) => { content.paras.push(p); return true; };
  content.RemoveElement = (i: number) => { content.paras.splice(i, 1); return true; };
  content.GetText = () => content.paras.map((p: Any) => p.items.map((r: Any) => r.text).join("")).join("\r\n");
  return content;
}
// A shape's text: its paragraphs, alignment and run styles.
const paragraphs = (shape: Any) => shape.GetDocContent().paras.map((p: Any) => [p.jc, p.items.map((r: Any) => `${r.style}:${r.text}`)]);

// A text box; or, with kind, a drawing of that kind; body false: a shape without a text box.
interface Box { placeholder?: string; text?: string; name?: string; paras?: ParaStub[]; kind?: string; body?: false }
// A slide's own background, transition and comments are what a person can give it without typing.
interface SlideStub { layout?: string; boxes?: Box[]; notes?: string; background?: true; transition?: string; comments?: number }
function deck(slides: SlideStub[]) {
  let next = 100;
  const all: Any[] = [];
  const makeSlide = ({ layout = "Blank", boxes = [], notes = "", background, transition = "effectNone", comments = 0 }: SlideStub) => {
    const id = String(next++);
    let notesText = notes;
    const drawings = boxes.map((b, i) => {
      const content = contentOf(b.paras ?? [{ runs: [{ text: b.text ?? "" }] }]);
      return {
        box: b, GetInternalId: () => `${id}-${i}`, GetClassType: () => b.kind ?? "shape", Shape: b.body === false ? {} : { txBody: {} },
        GetDocContent: () => content,
        GetPlaceholder: () => (b.placeholder ? { GetType: () => b.placeholder } : null), GetName: () => b.name ?? "",
        GetPosX: () => 0, GetPosY: () => 0, GetWidth: () => 0, GetHeight: () => 0,
      };
    });
    const body = { GetDocContent: () => ({ RemoveAllElements: () => { notesText = ""; } }) };
    const slide: Any = {
      Slide: { Get_Id: () => id, cSld: { spTree: [], Bg: background ? {} : null } }, comments, drawings,
      GetSlideShowTransition: () => ({ GetEntryEffect: () => transition }),
      GetAllDrawings: () => drawings.slice(),
      GetLayout: () => ({ GetName: () => layout }),
      GetNotesPage: () => ({ GetBodyShapeText: () => notesText, GetBodyShape: () => body }),
      AddNotesText: (text: string) => { notesText += text; },
      GetVisible: () => true, SetVisible: () => undefined,
      GetSlideIndex: () => all.indexOf(slide),
      Delete: () => all.splice(all.indexOf(slide), 1),
      ApplyLayout: () => undefined, RemoveAllObjects: () => undefined, FollowLayoutBackground: () => true, SetSlideShowTransition: () => undefined,
      AddObject: () => true,
    };
    return slide;
  };
  all.push(...slides.map(makeSlide));
  const pres = {
    GetWidth: () => 12192000, GetHeight: () => 6858000, GetAllSlides: () => all.slice(), GetSlidesCount: () => all.length,
    AddSlide: (slide: Any, index: number) => all.splice(index, 0, slide), GetMaster: () => null,
    GetAllComments: () => all.flatMap((slide) => Array.from({ length: slide.comments }, () => ({}))),
  };
  const Api = {
    GetPresentation: () => pres, CreateSlide: () => makeSlide({}), CreateRun: () => runOf({ text: "", style: "new" }),
    CreateSlideShowTransition: () => ({ SetEntryEffect: () => undefined, SetDuration: () => undefined }),
    CreateImage: () => ({ SetPosition: () => undefined, Drawing: { setName: () => undefined } }),
  };
  const run = (scope: Any): Any => {
    const out = runInNewContext(`(${SCRIPT})()`, { Api, scope, AscFormat: {}, AscCommon: {} });
    if (out?.__error) throw new Error(out.__error);
    return out;
  };
  return { all, run, ids: () => all.map((s) => s.Slide.Get_Id()) };
}
const add = (n: number, after: string | null = null) => ({ op: "slides", slides: Array.from({ length: n }, () => ({ after, spec: EMPTY_SPEC })) });
const TEMPLATE = { layout: "Title Slide", boxes: [{ placeholder: "ctrTitle" }, { placeholder: "subTitle" }] };

describe("a new deck's empty slide", () => {
  it("gives way to the slides added to it, in the same command", () => {
    const { run, ids } = deck([TEMPLATE]);
    const out = run({ action: "edit", ops: [add(3, "100")] });
    expect(out.results[0].slides).toEqual([{ id: "101", number: 1 }, { id: "102", number: 2 }, { id: "103", number: 3 }]);
    expect(ids()).toEqual(["101", "102", "103"]);
  });

  it("stays when anyone typed in it or changed it, when it is not the deck's only slide, or when the call replaces it", () => {
    const typed = deck([{ ...TEMPLATE, boxes: [{ placeholder: "ctrTitle", text: "Lisbon" }, { placeholder: "subTitle" }] }]);
    typed.run({ action: "edit", ops: [add(1)] });
    expect(typed.ids()).toEqual(["100", "101"]);
    const noted = deck([{ ...TEMPLATE, notes: "Open with the decision." }]);
    noted.run({ action: "edit", ops: [add(1)] });
    expect(noted.ids()).toEqual(["100", "101"]);
    const drawn = deck([{ ...TEMPLATE, boxes: [...TEMPLATE.boxes, {}] }]);
    drawn.run({ action: "edit", ops: [add(1)] });
    expect(drawn.ids()).toEqual(["100", "101"]);
    for (const changed of [{ background: true as const }, { transition: "effectFadeSmoothly" }, { comments: 1 }]) {
      const touched = deck([{ ...TEMPLATE, ...changed }]);
      touched.run({ action: "edit", ops: [add(1)] });
      expect(touched.ids(), JSON.stringify(changed)).toEqual(["100", "101"]);
    }
    const two = deck([TEMPLATE, TEMPLATE]);
    two.run({ action: "edit", ops: [add(1)] });
    expect(two.ids()).toEqual(["100", "101", "102"]);
    const other = deck([{ ...TEMPLATE, layout: "Title and Content" }]);
    other.run({ action: "edit", ops: [add(1)] });
    expect(other.ids()).toEqual(["100", "101"]);
    const replaced = deck([TEMPLATE]), hash = replaced.run({ action: "outline" }).slides[0].hash;
    replaced.run({ action: "edit", ops: [{ op: "slides", slides: [{ id: "100", hash, spec: EMPTY_SPEC }, { after: null, spec: EMPTY_SPEC }] }] });
    expect(replaced.ids()).toEqual(["100", "101"]);
  });
});

describe("a magic move that cannot play", () => {
  // A slide pinning a card by the id "next-step", and arriving by a magic move or not.
  const pinning = (magic: boolean) => ({
    after: null, spec: { ...EMPTY_SPEC, transition: magic ? { effect: "magic", ms: 1000 } : null,
      shapes: [{ kind: "picture", src: "icon:0", x: 0, y: 0, w: 100, h: 100, rot: 0, magic: "next-step" }] },
  });
  const typed = { boxes: [{ text: "Agenda" }] };
  const NOTE = 'ids match across slides but slide 3 has no data-transition="magic": nothing will move.';

  it("is told: two neighbouring slides share an id, and the second does not arrive by a magic move", () => {
    for (const first of [false, true]) {
      const { run } = deck([typed]);
      expect(run({ action: "edit", ops: [{ op: "slides", slides: [pinning(first), pinning(false)] }] }).results[0].note).toBe(NOTE);
    }
  });

  it("says nothing when the second arrives by one", () => {
    const { run } = deck([typed]);
    expect(run({ action: "edit", ops: [{ op: "slides", slides: [pinning(false), pinning(true)] }] }).results[0].note).toBeUndefined();
  });
});

describe("CSS a slide was built with though it changes nothing", () => {
  const COLLAPSE = "border-collapse: collapse on a table: native tables already collapse";
  const DISC = "list-style-type: disc on a ul: a native bulleted list already shows •";
  const typed = { boxes: [{ text: "Agenda" }] };

  it("is said in the op's note, slide by slide", () => {
    const { run } = deck([typed]);
    const out = run({ action: "edit", ops: [{ op: "slides", slides: [
      { after: null, spec: { ...EMPTY_SPEC, ignored: [COLLAPSE, DISC] } }, { after: null, spec: EMPTY_SPEC },
    ] }] });
    expect(out.results[0].note).toBe(`Slide 2 is built, but this CSS changes nothing there and can be left out: ${COLLAPSE}; ${DISC}.`);
  });

  it("comes after a magic move's note, which it keeps", () => {
    const { run } = deck([typed]);
    const pinned = { kind: "picture", src: "icon:0", x: 0, y: 0, w: 100, h: 100, rot: 0, magic: "next-step" };
    const out = run({ action: "edit", ops: [{ op: "slides", slides: [
      { after: null, spec: { ...EMPTY_SPEC, shapes: [pinned] } }, { after: null, spec: { ...EMPTY_SPEC, shapes: [pinned], ignored: [COLLAPSE] } },
    ] }] });
    expect(out.results[0].note).toBe('ids match across slides but slide 3 has no data-transition="magic": nothing will move. '
      + `Slide 3 is built, but this CSS changes nothing there and can be left out: ${COLLAPSE}.`);
  });
});

describe("a slide's title in the outline", () => {
  it("is its heading's text, before any text above it; else its title placeholder's or its first text", () => {
    const { run } = deck([
      { boxes: [{ text: "NEW HUB" }, { text: "Lisbon: team and setup", name: "Title" }] },
      { boxes: [{ text: "Draft" }, { text: "Quarterly review", placeholder: "title" }] },
      { boxes: [{ text: "Just words" }] },
    ]);
    expect(run({ action: "outline" }).slides.map((s: Any) => s.title)).toEqual(["Lisbon: team and setup", "Quarterly review", "Just words"]);
  });
});

describe("a reply in a slide's comment thread", () => {
  // A deck with one comment, whose data the editor replaces on EditComment unless someone holds the comment.
  function commented(held: boolean) {
    class CommentData {
      m_sGuid = "{G}";
      m_aReplies: Any[] = [];
      m_bSolved = false;
      Add_Reply(reply: Any) { this.m_aReplies.push(reply); }
      SetSolved(solved: boolean) { this.m_bSolved = solved; }
      SetText(text: string) { (this as Any).m_sText = text; }
      SetUserName(name: string) { (this as Any).m_sUserName = name; }
    }
    let data = new CommentData();
    const comment = {
      Comment: { GetData: () => data, Get_Id: () => "1_1", Parent: { slide: { num: 0 } } },
      GetAuthorName: () => "Flavius", GetText: () => "@Agent which month?", IsSolved: () => data.m_bSolved,
      GetRepliesCount: () => data.m_aReplies.length,
      GetReply: (i: number) => ({ GetAuthorName: () => data.m_aReplies[i].m_sUserName, GetText: () => data.m_aReplies[i].m_sText }),
    };
    const pres = {
      GetWidth: () => 12192000, GetHeight: () => 6858000, GetAllComments: () => [comment], GetAllSlides: () => [],
      Presentation: { EditComment: (_id: string, next: Any) => { if (!held) data = next; } },
    };
    const run = (scope: Any): Any =>
      runInNewContext(`(${SCRIPT})()`, { Api: { GetPresentation: () => pres }, scope, AscFormat: {}, AscCommon: { CCommentData: CommentData } });
    return { run, data: () => data };
  }
  const agent = { id: "agent:a:s", name: "Agent" };

  it("replies and resolves on a copy of the comment's data", () => {
    const deck = commented(false);
    const before = deck.data();
    expect(deck.run({ action: "edit", ops: [{ op: "reply", thread: "{G}", body: "March." }, { op: "resolve", thread: "{G}" }], agent }))
      .toEqual({ results: [{ op: 0 }, { op: 1 }] });
    expect(deck.data()).not.toBe(before);
    expect(before.m_aReplies).toEqual([]);
    expect(deck.data().m_aReplies.map((r: Any) => [r.m_sUserName, r.m_sText, r.m_sUserId])).toEqual([["Agent", "March.", "agent:a:s"]]);
    expect(deck.data().m_bSolved).toBe(true);
    expect(deck.run({ action: "outline" }).comments).toEqual([
      { id: "{G}", slide: 1, author: "Flavius", text: "@Agent which month?", replies: [{ author: "Agent", text: "March." }], resolved: true },
    ]);
  });

  it("is refused when someone holds the comment, and when the thread is gone", () => {
    expect(commented(true).run({ action: "edit", ops: [{ op: "reply", thread: "{G}", body: "March." }], agent }).refused)
      .toMatchObject({ op: 0, reason: expect.stringMatching(/Someone is changing comment thread \{G\}/) });
    expect(commented(false).run({ action: "edit", ops: [{ op: "resolve", thread: "{H}" }], agent }).refused.reason)
      .toBe("Comment thread {H} is gone: its comment was deleted, or the slide it was on was removed. Read the outline again.");
  });
});

describe("a slide rewritten from HTML", () => {
  const notes = (run: (scope: Any) => Any) => JSON.parse(run({ action: "slide", target: "100" }).__json).notes;
  const rewrite = (run: (scope: Any) => Any, spec: Any) =>
    run({ action: "edit", ops: [{ op: "slides", slides: [{ id: "100", hash: run({ action: "outline" }).slides[0].hash, spec }] }] });

  it("keeps its speaker notes when its HTML has none", () => {
    const { run } = deck([{ notes: "Mention the lease." }]);
    expect(rewrite(run, EMPTY_SPEC).results).toEqual([{ op: 0, slides: [{ id: "100", number: 1 }] }]);
    expect(notes(run)).toBe("Mention the lease.");
  });

  it("takes its HTML's notes in their place, and an empty aside clears them", () => {
    const { run } = deck([{ notes: "Mention the lease." }]);
    rewrite(run, { ...EMPTY_SPEC, notes: "Open with the budget." });
    expect(notes(run)).toBe("Open with the budget.");
    rewrite(run, { ...EMPTY_SPEC, notes: "" });
    expect(notes(run)).toBe("");
  });
});

describe("one shape's text", () => {
  const TITLE: Box = { placeholder: "title", paras: [{ jc: "center", runs: [{ text: "", style: "empty" }, { text: "Next ", style: "title" }, { text: "quarter", style: "bold" }] }] };
  const BODY: Box = { paras: [{ runs: [{ text: "Open the Lisbon office", style: "body" }] }, { jc: "right", runs: [{ text: "In March", style: "small" }] }] };
  const setting = (run: (scope: Any) => Any, shape: string, text: string) =>
    run({ action: "edit", ops: [{ op: "text", slide: "100", hash: run({ action: "outline" }).slides[0].hash, shape, text }] });

  it("sets the text in the first run's style, keeping the paragraph and the shape", () => {
    const { run, all } = deck([{ layout: "Title Only", boxes: [TITLE, BODY], notes: "Mention the lease." }]);
    expect(setting(run, "100-0", "Q3 plan")).toEqual({ results: [{ op: 0 }] });
    expect(paragraphs(all[0].drawings[0])).toEqual([["center", ["title:Q3 plan"]]]);
    expect(all[0].drawings[0].GetPlaceholder().GetType()).toBe("title");
    expect(paragraphs(all[0].drawings[1])).toEqual([["left", ["body:Open the Lisbon office"]], ["right", ["small:In March"]]]);
    expect(all[0].GetNotesPage().GetBodyShapeText()).toBe("Mention the lease.");
  });

  it("sets each line in its own paragraph: a line past the shape's paragraphs as its last one, and a paragraph past the lines goes", () => {
    const { run, all } = deck([{ boxes: [BODY] }]);
    setting(run, "100-0", "One\nTwo\nThree");
    expect(paragraphs(all[0].drawings[0])).toEqual([["left", ["body:One"]], ["right", ["small:Two"]], ["right", ["small:Three"]]]);
    setting(run, "100-0", "Only");
    expect(paragraphs(all[0].drawings[0])).toEqual([["left", ["body:Only"]]]);
  });

  it("fills an empty text box, and a paragraph without runs gets one", () => {
    const { run, all } = deck([{ boxes: [{ paras: [{ jc: "center", runs: [] }] }] }]);
    setting(run, "100-0", "Lisbon");
    expect(paragraphs(all[0].drawings[0])).toEqual([["center", ["new:Lisbon"]]]);
  });

  it("is refused on a slide changed since it was read, a shape the slide does not have, and a shape with no text", () => {
    const { run, all } = deck([{ boxes: [BODY, { kind: "image" }, { body: false }] }]);
    const refused = (op: Any) => run({ action: "edit", ops: [{ op: "text", slide: "100", hash: run({ action: "outline" }).slides[0].hash, text: "x", ...op }] }).refused;
    expect(refused({ shape: "100-0", hash: "00000000" })).toMatchObject({ op: 0, reason: "Slide 100 changed since you read it. This is it now; work from it." });
    expect(refused({ shape: "100-9" }).reason).toMatch(/^Slide 100 has no shape 100-9 now/);
    for (const shape of ["100-1", "100-2"]) expect(refused({ shape }).reason).toMatch(new RegExp(`^Shape ${shape} has no text to change`));
    expect(paragraphs(all[0].drawings[0])).toEqual([["left", ["body:Open the Lisbon office"]], ["right", ["small:In March"]]]);
  });

  it("is refused, before anything changes, on a slide an earlier op of the call removes", () => {
    const { run, all } = deck([{ boxes: [BODY] }, {}]);
    const hash = run({ action: "outline" }).slides[0].hash;
    const out = run({ action: "edit", ops: [{ op: "remove", slides: [{ id: "100", hash }] }, { op: "text", slide: "100", hash, shape: "100-0", text: "x" }] });
    expect(out.refused).toMatchObject({ op: 1, reason: "Slide 100 is removed by this call." });
    expect(all).toHaveLength(2);
  });

  it("keeps a new deck's empty slide when the call sets its text", () => {
    const { run, ids } = deck([TEMPLATE]);
    const hash = run({ action: "outline" }).slides[0].hash;
    run({ action: "edit", ops: [{ op: "text", slide: "100", hash, shape: "100-0", text: "Lisbon" }, add(1)] });
    expect(ids()).toEqual(["100", "101"]);
  });
});
