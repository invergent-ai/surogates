import type { Page } from "playwright-core";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { Measurer, type Canvas } from "../src/slides/measure.js";
import { chromiumThere, measuringPage, systemFonts } from "./support/chromium.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

const WIDE: Canvas = { width: 1920, height: 1080, emuPerPx: 6350 };
const E = 6350;
const editor = {} as EditorPage;

describe.skipIf(!chromiumThere)("measuring Claude Slides' styling and motion", () => {
  let close: () => Promise<void>;
  let measurer: Measurer;

  let page: Page;

  beforeAll(async () => {
    const opened = await measuringPage();
    close = opened.close;
    page = opened.page;
    measurer = new Measurer(async () => opened.page, systemFonts);
  });
  afterAll(async () => {
    await close();
  });

  const one = async (child: string, section = "background:#ffffff"): Promise<Any> =>
    (await measurer.measure(`<section style="${section}">${child}</section>`, WIDE, editor)).shapes;
  const box = "position:absolute;left:100px;top:100px;width:400px;height:200px";

  it("reads a linear gradient's angle and stops, and a corner keyword as scaled", async () => {
    const [shape] = await one(`<div style="${box};background:linear-gradient(135deg, #ff0000, rgba(255,255,0,0.5) 40%, #0000ff)"></div>`);
    expect(shape.fill).toEqual({ type: "linear", angle: 135, scaled: false, stops: [
      { color: { hex: "#ff0000", alpha: 1 }, at: 0 }, { color: { hex: "#ffff00", alpha: 0.5 }, at: 40 }, { color: { hex: "#0000ff", alpha: 1 }, at: 100 }] });
    const [corner] = await one(`<div style="${box};background:linear-gradient(to bottom right, #000000, #ffffff)"></div>`);
    expect(corner.fill).toMatchObject({ angle: 135, scaled: true });
  });

  it("expands a repeating gradient into hard stops", async () => {
    const [shape] = await one(`<div style="${box};background:repeating-linear-gradient(90deg, #14285A 0px 20px, #FFFFFF 20px 40px)"></div>`);
    expect(shape.fill.stops).toHaveLength(40);
    expect(shape.fill.stops.slice(0, 4).map((s: Any) => [s.color.hex, s.at])).toEqual([["#14285a", 0], ["#14285a", 5], ["#ffffff", 5], ["#ffffff", 10]]);
  });

  it("makes rounded corners a roundRect, a half-radius an ellipse, four radii a path", async () => {
    const [round] = await one(`<div style="${box};background:#000;border-radius:40px"></div>`);
    expect(round.geometry).toEqual({ preset: "roundRect", adj: 20000 });
    const [ellipse] = await one(`<div style="${box};background:#000;border-radius:50%"></div>`);
    expect(ellipse.geometry).toEqual({ preset: "ellipse" });
    const [corners] = await one(`<div style="${box};background:#000;border-radius:10px 40px 80px 20px"></div>`);
    expect(corners.geometry.custom[0].d[0]).toEqual(["M", 10 * E, 0]);
  });

  it("draws x-shapes, hr and connectors natively", async () => {
    const shapes = await one(`<x-shape kind="arrow-right" style="${box};background:#2f5597"></x-shape>
      <hr style="position:absolute;left:100px;top:400px;width:800px;border-top:4px solid #c81e1e">
      <x-connector x1="100" y1="600" x2="500" y2="800" route="hv" head-end="filled" style="color:#14285A;border-width:4px"></x-connector>`);
    expect(shapes[0]).toMatchObject({ kind: "shape", geometry: { preset: "rightArrow", adj1: 50000, adj2: 80000 }, fill: { type: "solid" } });
    expect(shapes[1]).toMatchObject({ kind: "shape", geometry: { preset: "line" }, h: 0, w: 800 * E, stroke: { w: 4 * E } });
    expect(shapes[2]).toMatchObject({ kind: "path", points: [[100 * E, 600 * E], [500 * E, 600 * E], [500 * E, 800 * E]], head: "none", tail: "filled", stroke: { w: 4 * E } });
  });

  it("places a connector given points by them, its border only its line", async () => {
    const shapes = await one(`<x-connector x1="100" y1="600" x2="500" y2="600" style="color:#14285A;border:6px dashed #c81e1e"></x-connector><p style="font-size:32px">After</p>`);
    expect(shapes[0]).toMatchObject({ kind: "path", points: [[100 * E, 600 * E], [500 * E, 600 * E]], stroke: { w: 6 * E, color: { hex: "#14285a" }, dash: "dash" } });
    expect(shapes[1]).toMatchObject({ y: 0, text: { paras: [{ runs: [{ text: "After" }] }] } });
  });

  it("draws a double border and borders on some sides as shapes after the box, grouped with it", async () => {
    const [doubled] = await one(`<div style="${box};background:#eeeeee;border:12px double #c81e1e"></div>`);
    const [plain, double] = doubled.children;
    expect(plain).toMatchObject({ fill: { type: "solid" }, stroke: null });
    expect(double.geometry.custom.map((p: Any) => [p.fill, p.stroke])).toEqual([[false, true], [false, true]]);
    expect(double.stroke).toMatchObject({ w: 4 * E });
    const [sided] = await one(`<div style="${box};background:#eeeeee;border-top:8px solid #c81e1e;border-left:4px dashed #14285a"></div>`);
    const [card, top, left] = sided.children;
    expect(card.stroke).toBeNull();
    expect(top).toMatchObject({ geometry: { preset: "rect" }, x: 100 * E, y: 100 * E, w: 400 * E, h: 8 * E, fill: { color: { hex: "#c81e1e" } } });
    expect(left).toMatchObject({ geometry: { preset: "line" }, x: 102 * E, w: 0, stroke: { w: 4 * E, dash: "dash" } });
  });

  it("keeps a hard shadow native and puts a blurred one behind as a picture", async () => {
    const [hard] = await one(`<div style="${box};background:#ddd;box-shadow:12px 12px 0 #14285a"></div>`);
    expect(hard.shadow).toEqual({ x: 12 * E, y: 12 * E, spread: 0, color: { hex: "#14285a", alpha: 1 } });
    const [soft] = await one(`<div style="${box};background:#ddd;box-shadow:0 8px 24px rgba(0,0,0,0.25)"></div>`);
    expect(soft.kind).toBe("group");
    expect(soft.children.map((c: Any) => c.kind)).toEqual(["picture", "shape"]);
    expect(soft.children[0].src).toMatch(/^data:image\/png;base64,/);
  });

  it("reads a rotation about the untransformed box, and a skewed box as a parallelogram", async () => {
    const [rotated] = await one(`<div style="${box};background:#000;transform:rotate(30deg)"></div>`);
    expect(rotated).toMatchObject({ x: 100 * E, y: 100 * E, w: 400 * E, h: 200 * E, rot: 30 });
    const [skewed] = await one(`<div style="${box};background:#000;transform:skewX(-20deg)"></div>`);
    expect(skewed.geometry.custom).toBeDefined();
  });

  it("reads letter spacing, case, strikethrough, outline, gradient text and opacity", async () => {
    const [text] = await one(`<p style="position:absolute;left:100px;top:100px;width:1200px;font-size:64px;letter-spacing:10px;text-transform:uppercase;text-decoration:line-through;-webkit-text-stroke:2px #c81e1e;color:rgba(20,40,90,0.5)">Styled</p>`);
    expect(text.text.paras[0].runs[0]).toMatchObject({ text: "Styled", spacing: 5, caps: true, s: true, outline: { w: 2 * E, color: { hex: "#c81e1e", alpha: 1 } }, color: { hex: "#14285a", alpha: 0.5 } });
    const [gradient] = await one(`<p style="position:absolute;left:100px;top:100px;font-size:64px;background:linear-gradient(90deg,#c81e1e,#2f5597);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent">Gradient</p>`);
    expect(gradient.fill).toBeNull();
    expect(gradient.text.paras[0].runs[0].gradient).toMatchObject({ type: "linear", angle: 90 });
  });

  it("puts a box's opacity on its shape and into its text", async () => {
    const [label] = await one(`<p style="position:absolute;left:100px;top:100px;font-size:48px;background:#14285a;color:#ffffff;opacity:0.5">Faded</p>`);
    expect(label).toMatchObject({ opacity: 0.5, fill: { color: { alpha: 1 } } });
    expect(label.text.paras[0].runs[0].color).toEqual({ hex: "#ffffff", alpha: 0.5 });
  });

  it("draws a filtered element and a radial background into pictures", async () => {
    const [filtered] = await one(`<div style="${box};background:#c81e1e;filter:blur(4px)"></div>`);
    expect(filtered).toMatchObject({ kind: "picture", src: expect.stringMatching(/^data:image\/png/) });
    const [radial] = await one(`<p style="${box};font-size:48px;background:radial-gradient(#ffffff, #000080);color:#ffffff">Glow</p>`);
    expect(radial.kind).toBe("group");
    expect(radial.children.map((c: Any) => c.kind)).toEqual(["picture", "shape"]);
    expect(radial.children[1].text.paras[0].runs[0].text).toBe("Glow");
  });

  it("draws a translucent container whole, and none of its children again", async () => {
    const shapes = await one(`<div style="${box};background:#14285a;opacity:0.5;padding:24px"><p style="font-size:32px;color:#ffffff">Inside</p></div>`);
    expect(shapes).toHaveLength(1);
    expect(shapes[0]).toMatchObject({ kind: "picture", x: 100 * E, w: 400 * E, rot: 0 });
  });

  it("reads builds, a transition and magic-move names", async () => {
    const [built] = await one(`<div id="card" data-build-in="rise order 2 auto" style="${box};background:#000"></div>`, "background:#fff");
    expect(built).toMatchObject({ build: { in: { effect: "rise", order: 2, auto: true } }, magic: "card" });
    const spec = await measurer.measure(`<section data-transition="magic" style="background:#fff"><p style="font-size:32px">x</p></section>`, WIDE, editor);
    expect(spec.transition).toEqual({ effect: "magic", ms: 1000 });
  });

  it("puts a picture's radius and opacity on the picture", async () => {
    const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
    const [picture] = await one(`<img alt="" src="${png}" style="position:absolute;left:100px;top:100px;width:400px;height:300px;border-radius:40px;opacity:0.5">`);
    expect(picture).toMatchObject({ kind: "picture", radius: 13333, opacity: 0.5 });
  });

  it("refuses motion and connectors outside the format", async () => {
    for (const [html, said, section] of [
      [`<div data-build-in="spin" style="${box};background:#000"></div>`, "data-build-in is"],
      [`<p data-build-in="fade" style="font-size:32px">Flow</p>`, "builds go on pinned elements"],
      [`<p style="font-size:32px">x</p>`, "data-transition is one of", `data-transition="zoom" style="background:#fff"`],
      [`<div id="card" data-transition="magic" style="${box};background:#000"></div>`, '<section > div>: data-transition belongs on the <section> of the slide it leads into.'],
      [`<x-connector x1="0" y1="0" x2="100" y2="100" route="curvy"></x-connector>`, "route is one of"],
      [`<x-connector x1="0" y1="0" x2="100"></x-connector>`, "gives x1, y1, x2 and y2"],
    ]) {
      const page = `<section ${section ?? 'style="background:#fff"'}>${html}</section>`;
      await expect(measurer.measure(page, WIDE, editor), page).rejects.toThrow(said!);
    }
  });

  it("draws the icons of an element drawn whole into its picture", async () => {
    const [card] = await one(`<div style="${box};background:#14285a;opacity:0.9;padding:24px"><x-icon name="Star" style="width:96px;height:96px;color:#ffffff"></x-icon></div>`);
    expect(card).toMatchObject({ kind: "picture", w: 400 * E });
    // Light pixels where the icon is (24 to 120 px into the card, at device scale 2): the card alone is flat navy.
    const light = await page.evaluate(async (src: string) => {
      const img = new Image();
      img.src = src;
      await img.decode();
      const canvas = document.createElement("canvas");
      [canvas.width, canvas.height] = [img.width, img.height];
      const context = canvas.getContext("2d")!;
      context.drawImage(img, 0, 0);
      const data = context.getImageData(48, 48, 192, 192).data;
      let n = 0;
      for (let i = 0; i < data.length; i += 4) if (data[i]! > 128) n++;
      return n;
    }, card.src);
    expect(light).toBeGreaterThan(100);
  });

  it("refuses what an element drawn whole, or one moving as a group, would lose", async () => {
    for (const [html, said, section] of [
      [`<div style="${box};background:#eeeeee;transform:rotate(10deg)"><x-connector></x-connector></div>`, "pin it beside the box"],
      [`<div id="results" style="position:absolute;left:100px;top:100px;width:1200px"><h2 style="font-size:64px">Results</h2><table style="font-size:32px"><tr><td>A</td><td>1</td></tr></table></div>`, "give the table its own motion"],
      [`<div id="hero" style="${box};background:#000000"><div data-build-in="pop" style="position:absolute;left:10px;top:10px;width:50px;height:50px;background:#ffffff"></div></div>`, "builds go on the section's pinned children"],
      [`<div data-build-in="fade" style="${box};background:#000000"><div id="dot" style="position:absolute;left:10px;top:10px;width:50px;height:50px;background:#ffffff"></div></div>`, "magic-move ids go on the section's pinned children", `data-transition="magic" style="background:#fff"`],
    ]) {
      const html2 = `<section ${section ?? 'style="background:#fff"'}>${html}</section>`;
      await expect(measurer.measure(html2, WIDE, editor), html2).rejects.toThrow(said!);
    }
  });

  it("refuses text inside a shape, an icon, a connector, which nothing draws", async () => {
    for (const html of [
      `<x-shape kind="rect" style="${box};background:#000">Label</x-shape>`,
      `<x-icon name="Check" style="${box}"><b>x</b></x-icon>`,
      `<x-connector x1="0" y1="0" x2="100" y2="100">to</x-connector>`,
    ]) {
      await expect(one(html), html).rejects.toThrow("text goes in a text element beside it");
    }
  });
});
