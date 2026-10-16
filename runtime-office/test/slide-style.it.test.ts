import { readFileSync } from "node:fs";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { formatsFor } from "../src/formats/index.js";
import { slideFormat } from "../src/formats/slide.js";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { Measurer, type ShapeSpec, type SlideSpec } from "../src/slides/measure.js";
import { eventually, itEnabled, pair, startFixture, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

// One slide carrying the format's styling and motion: a gradient background, a rounded card with a hard shadow that
// rises in, styled text that fades in after it, a turned arrow, a routed connector, and a card with a blurred shadow
// and text of its own that scales in as one.
const STYLED = `<section data-transition="fade" style="background:linear-gradient(160deg, #ffffff, #dde6f5);font-family:Arial;padding:128px">
  <div id="card" data-build-in="rise order 1" style="position:absolute;left:128px;top:128px;width:600px;height:300px;background:#14285A;border-radius:40px;box-shadow:12px 12px 0 rgba(0,0,0,0.3)"></div>
  <p data-build-in="fade order 2 auto" style="position:absolute;left:160px;top:180px;width:540px;font-size:48px;color:#ffffff;letter-spacing:4px;text-transform:uppercase">Styled card</p>
  <x-shape kind="arrow-right" style="position:absolute;left:800px;top:220px;width:300px;height:120px;background:#c81e1e;transform:rotate(-10deg)"></x-shape>
  <x-connector x1="1150" y1="280" x2="1500" y2="600" route="hv" head-end="filled" style="color:#14285A;border-width:4px"></x-connector>
  <div data-build-in="scale order 3" style="position:absolute;left:1300px;top:700px;width:300px;height:150px;padding:24px;background:#2f5597;box-shadow:0 8px 24px rgba(0,0,0,0.25)"><p style="font-size:32px;color:#ffffff">Grouped</p></div>
  <aside>Styled.</aside>
</section>`;

// A slide that fades in, and the HTML that replaces it: no transition; a card whose blurred shadow makes a group and
// which rises out; a table.
const FADING = `<section data-transition="fade" style="background:#ffffff"><h2 style="font-size:64px">Fading</h2></section>`;
const LATER = `<section style="background:#ffffff"><h2 style="font-size:64px">Later</h2></section>`;
const REPLACEMENT = `<section style="background:#ffffff;font-family:Arial;padding:128px">
  <table style="width:800px;font-size:32px"><tr><td>North</td><td>120</td></tr></table>
  <div data-build-out="rise" style="position:absolute;left:1200px;top:600px;width:400px;height:200px;background:#2f5597;box-shadow:0 8px 24px rgba(0,0,0,0.25)"></div>
</section>`;

// Two slides sharing a pinned card that moves: a painted div with a blurred shadow and two lines of text of its own.
const fixtureHtml = (name: string) => readFileSync(new URL(`fixtures/${name}`, import.meta.url), "utf8");
const TEAM = fixtureHtml("lisbon-team.html");
const BUDGET = fixtureHtml("lisbon-budget.html");

// Every drawing of a slide, a group's children after it, at its place on the slide in canvas px, worked out from the
// frames each editor holds and saves (editor internals: the builder reads no group child's frame).
const PLACES = `function () {
  var s = Api.GetPresentation().GetAllSlides().filter(function (x) { return String(x.Slide.Get_Id()) === scope.id; })[0];
  if (!s) return null;
  var mmPerPx = Api.GetPresentation().GetWidth() / 1920 / 36000, out = [];
  function walk(list, at) {
    list.forEach(function (sp) {
      var x = sp.spPr.xfrm, group = sp instanceof AscFormat.CGroupShape;
      var left = at.x + (x.offX - at.chX) * at.sx, top = at.y + (x.offY - at.chY) * at.sy, w = x.extX * at.sx, h = x.extY * at.sy;
      out.push({ kind: group ? "group" : sp instanceof AscFormat.CImageShape ? "picture" : "shape", x: Math.round(left / mmPerPx), y: Math.round(top / mmPerPx),
        w: Math.round(w / mmPerPx), h: Math.round(h / mmPerPx), text: sp.txBody ? sp.txBody.content.GetText ? String(sp.txBody.content.GetText()).trim() : "" : "" });
      if (group) walk(sp.spTree, { x: left, y: top, chX: x.chOffX, chY: x.chOffY, sx: at.sx * (x.chExtX ? x.extX / x.chExtX : 1), sy: at.sy * (x.chExtY ? x.extY / x.chExtY : 1) });
    });
  }
  walk(s.Slide.cSld.spTree, { x: 0, y: 0, chX: 0, chY: 0, sx: 1, sy: 1 });
  return JSON.stringify(out);
}`;
// Whether the editor is still loading a picture (editor internals: the builder says nothing of its image loader).
const LOADING = `function () {
  var all = AscCommon.g_image_loader.map_image_index;
  for (var src in all) if (all[src].Status === AscFonts.ImageLoadStatus.Loading) return true;
  return false;
}`;
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
// What a measured shape draws, a group's children after it, in canvas px.
const leaves = (shapes: ShapeSpec[], emuPerPx: number): { kind: string; x: number; y: number; w: number; h: number }[] =>
  shapes.flatMap((s) => [
    { kind: s.kind, x: Math.round(s.x / emuPerPx), y: Math.round(s.y / emuPerPx), w: Math.round(s.w / emuPerPx), h: Math.round(s.h / emuPerPx) },
    ...(s.kind === "group" ? leaves(s.children, emuPerPx) : []),
  ]);

describe.skipIf(!itEnabled)("Claude Slides' styling on a live deck", () => {
  let fixture: Fixture;
  let pool: PagePool;
  let office: Office;
  let opened: string[] = [];
  // The specs the slides measured to, in order.
  const measured: SlideSpec[] = [];

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 4, idleMs: 600_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
    const measurer = new Measurer(() => pool.measuringPage()), measure = measurer.measure.bind(measurer);
    measurer.measure = async (...args) => {
      const spec = await measure(...args);
      measured.push(spec);
      return spec;
    };
    office = new Office(pool, { ...formatsFor(pool), slide: slideFormat(measurer) });
  });
  afterEach(async () => {
    // Euro-Office's document server crashes when an editor that closes aborts a picture's download mid-stream: both
    // editors finish loading their pictures first, for up to 10 s.
    const until = Date.now() + 10_000;
    for (const id of opened) {
      const page = await pool.get(id).catch(() => null);
      while (page && Date.now() < until && (await page.command(LOADING, {}).catch(() => null))?.value !== false) await sleep(250);
    }
    for (const id of opened) await pool.close(id);
    opened = [];
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  it("builds every styled shape natively, with its builds and transition, and the user sees them", async () => {
    const { agent, user } = await pair(office, opened, "sample.pptx");
    const result: Any = await office.edit(agent, [{ op: "slides", slides: [{ html: STYLED }] }]);
    expect(result).toMatchObject({ applied: true, flushed: true });
    const id = result.results[0].slides[0].id;
    // What the person's editor holds (editor internals for the geometry and the background, which the builder reads
    // nothing of). The builder's GetTimeLine gives a slide with no timing an empty one, a change of its own: read before
    // the agent's slide has arrived, it would replace the agent's builds on both editors.
    await eventually(async () => {
      const seen = (await (await pool.get(user)).command(`function () {
        var s = Api.GetPresentation().GetAllSlides().filter(function (x) { return String(x.Slide.Get_Id()) === scope.id; })[0];
        if (!s) return null;
        var d = s.GetAllDrawings();
        return JSON.stringify({
          kinds: d.map(function (x) { return x.GetClassType(); }),
          geometry: d.map(function (x) { return x.Drawing && x.Drawing.spPr && x.Drawing.spPr.geometry ? x.Drawing.spPr.geometry.preset || "custom" : null; }),
          rotations: d.map(function (x) { return Math.round(x.GetRotation ? x.GetRotation() : 0); }),
          effects: s.Slide.timing ? s.GetTimeLine().GetAllEffects().map(function (e) { return [e.GetEffectType(), e.GetTriggerType()]; }) : [],
          transition: s.GetSlideShowTransition ? s.GetSlideShowTransition().GetEntryEffect() : null,
          background: !!(s.Slide.cSld.Bg && s.Slide.cSld.Bg.bgPr && s.Slide.cSld.Bg.bgPr.Fill && s.Slide.cSld.Bg.bgPr.Fill.fill instanceof AscFormat.CGradFill)
        });
      }`, { id })).value;
      const state = JSON.parse((seen as string | null) ?? "null");
      expect(state).not.toBeNull();
      expect(state.background).toBe(true);
      expect(state.geometry).toContain("roundRect");
      expect(state.geometry).toContain("rightArrow");
      expect(state.geometry).toContain("custom");   // the routed connector
      expect(state.rotations).toContain(350);
      expect(state.kinds).toContain("group");   // the blurred shadow behind its card, with the card's text
      expect(state.effects.map((e: Any) => e[0])).toEqual(["entranceFlyIn", "entranceFade", "entranceZoom"]);
      expect(state.effects.map((e: Any) => e[1])).toEqual(["onclick", "afterprevious", "onclick"]);
      expect(state.transition).toBe("effectFadeSmoothly");
    });
  });

  it("places a moving card's shadow, box and text where they measured, on both editors", async () => {
    const { agent, user } = await pair(office, opened, "sample.pptx");
    measured.length = 0;
    const result: Any = await office.edit(agent, [{ op: "slides", slides: [{ html: TEAM }, { html: BUDGET }] }]);
    expect(result).toMatchObject({ applied: true, flushed: true });
    // Neither section says data-transition="magic", the second least of all: the agent is told nothing will move.
    expect(result.results[0].note).toBe('ids match across slides but slide 4 has no data-transition="magic": nothing will move.');
    const E = 12192000 / 1920;
    for (const [n, slide] of result.results[0].slides.entries()) {
      // The card moves, so it is one group: its blurred shadow grouped behind its box, then its two lines of text.
      const card = measured[n]!.shapes.find((s) => s.magic === "handoff-card")!;
      expect(card.kind).toBe("group");
      expect(leaves([card], E).map((s) => s.kind)).toEqual(["group", "group", "picture", "shape", "shape", "shape"]);
      // The editor frames a group round its children, so what is compared is what each child draws.
      const want = leaves([card], E).filter((s) => s.kind !== "group");
      for (const page of [agent, user]) {
        await eventually(async () => {
          const places: Any[] = JSON.parse(((await (await pool.get(page)).command(PLACES, { id: slide.id })).value as string | null) ?? "null");
          expect(places).not.toBeNull();
          const drawn = places.filter((p) => p.kind !== "group");
          const near = (a: number, b: number) => Math.abs(a - b) <= 2;
          const at = drawn.findIndex((_, i) => want.every((w, j) => drawn[i + j]?.kind === w.kind && near(drawn[i + j].w, w.w) && near(drawn[i + j].h, w.h)));
          expect(at, JSON.stringify({ want, drawn })).toBeGreaterThanOrEqual(0);
          const got = drawn.slice(at, at + want.length);
          expect(got.map((g) => [g.x, g.y]), JSON.stringify({ want, got })).toEqual(want.map((w, j) => [near(got[j].x, w.x) ? got[j].x : w.x, near(got[j].y, w.y) ? got[j].y : w.y]));
        });
      }
    }
  });

  it("replaces a slide that is not the current one: its group lands on it, its exit rises out the top, its old transition goes", async () => {
    const { agent, user } = await pair(office, opened, "sample.pptx");
    // Two slides added: the last one is current.
    await office.edit(agent, [{ op: "slides", slides: [{ html: FADING }, { html: LATER }] }]);
    const fading = (await office.read(agent, { view: "outline" }) as Any).slides[2];
    const result: Any = await office.edit(agent, [{ op: "slides", slides: [{ id: fading.id, hash: fading.hash, html: REPLACEMENT }] }]);
    expect(result).toMatchObject({ applied: true, flushed: true });
    // Editor internals: the current slide, a group's children and a fly-out's direction (its preset subtype), which the
    // builder reads nothing of; the timeline only where the slide has one, as above.
    const read = `function () {
      var all = Api.GetPresentation().GetAllSlides(), s = all.filter(function (x) { return String(x.Slide.Get_Id()) === scope.id; })[0];
      if (!s) return null;
      var groups = s.GetAllDrawings().filter(function (x) { return x.GetClassType() === "group"; });
      return JSON.stringify({
        current: Api.GetPresentation().Presentation.CurPage, index: s.GetSlideIndex(),
        kinds: s.GetAllDrawings().map(function (x) { return x.GetClassType(); }),
        group: groups.map(function (g) { return g.Drawing.spTree.length; }),
        effects: s.Slide.timing ? s.GetTimeLine().GetAllEffects().map(function (e) { return [e.GetEffectType(), e.Effect.cTn.presetSubtype]; }) : [],
        transition: s.GetSlideShowTransition().GetEntryEffect(),
        others: all.filter(function (x) { return x !== s; }).map(function (x) { return x.GetAllDrawings().filter(function (d) { return d.GetClassType() === "group"; }).length; })
      });
    }`;
    for (const page of [agent, user]) {
      await eventually(async () => {
        const state = JSON.parse(((await (await pool.get(page)).command(read, { id: fading.id })).value as string | null) ?? "null");
        expect(state).not.toBeNull();
        expect(state.kinds.sort()).toEqual(["group", "table"]);
        expect(state.group).toEqual([2]);
        expect(state.others).toEqual([0, 0, 0]);
        expect(state.current).not.toBe(state.index);
        expect(state.effects).toEqual([["exitFlyOut", 1]]);
        expect(state.transition).toBe("effectNone");
      });
    }
  });
});
