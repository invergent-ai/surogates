import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

// editor/slide.js run against a stand-in for the builder: a deck of slides that hold text boxes.
const SCRIPT = readFileSync(new URL("../editor/slide.js", import.meta.url), "utf8");
const EMPTY_SPEC = { background: null, shapes: [], notes: "", hidden: false, transition: null };

interface Box { placeholder?: string; text?: string }
function deck(slides: { layout?: string; boxes?: Box[]; notes?: string }[]) {
  let next = 100;
  const all: Any[] = [];
  const makeSlide = ({ layout = "Blank", boxes = [], notes = "" }: { layout?: string; boxes?: Box[]; notes?: string }) => {
    const id = String(next++);
    const slide: Any = {
      Slide: { Get_Id: () => id, cSld: { spTree: [] } },
      GetAllDrawings: () => boxes.map((b) => ({
        GetClassType: () => "shape", Shape: { txBody: {} }, GetDocContent: () => ({ GetText: () => b.text ?? "" }),
        GetPlaceholder: () => (b.placeholder ? { GetType: () => b.placeholder } : null),
        GetPosX: () => 0, GetPosY: () => 0, GetWidth: () => 0, GetHeight: () => 0,
      })),
      GetLayout: () => ({ GetName: () => layout }),
      GetNotesPage: () => ({ GetBodyShapeText: () => notes, GetBodyShape: () => null }),
      GetVisible: () => true, SetVisible: () => undefined,
      GetSlideIndex: () => all.indexOf(slide),
      Delete: () => all.splice(all.indexOf(slide), 1),
      ApplyLayout: () => undefined, RemoveAllObjects: () => undefined, FollowLayoutBackground: () => true, SetSlideShowTransition: () => undefined,
    };
    return slide;
  };
  all.push(...slides.map(makeSlide));
  const pres = {
    GetWidth: () => 12192000, GetHeight: () => 6858000, GetAllSlides: () => all.slice(), GetSlidesCount: () => all.length,
    AddSlide: (slide: Any, index: number) => all.splice(index, 0, slide), GetMaster: () => null,
  };
  const Api = { GetPresentation: () => pres, CreateSlide: () => makeSlide({}), CreateSlideShowTransition: () => ({ SetEntryEffect: () => undefined }) };
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

  it("stays when anyone typed in it, when it is not the deck's only slide, or when the call replaces it", () => {
    const typed = deck([{ ...TEMPLATE, boxes: [{ placeholder: "ctrTitle", text: "Lisbon" }, { placeholder: "subTitle" }] }]);
    typed.run({ action: "edit", ops: [add(1)] });
    expect(typed.ids()).toEqual(["100", "101"]);
    const noted = deck([{ ...TEMPLATE, notes: "Open with the decision." }]);
    noted.run({ action: "edit", ops: [add(1)] });
    expect(noted.ids()).toEqual(["100", "101"]);
    const drawn = deck([{ ...TEMPLATE, boxes: [...TEMPLATE.boxes, {}] }]);
    drawn.run({ action: "edit", ops: [add(1)] });
    expect(drawn.ids()).toEqual(["100", "101"]);
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
