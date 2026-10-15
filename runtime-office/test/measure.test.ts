import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { Measurer, type Canvas } from "../src/slides/measure.js";
import { chromiumThere, measuringPage, systemFonts } from "./support/chromium.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

const WIDE: Canvas = { width: 1920, height: 1080, emuPerPx: 6350 };
const E = 6350;
const PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
const editor = {} as EditorPage;

describe.skipIf(!chromiumThere)("measuring a Claude Slides slide", () => {
  let close: () => Promise<void>;
  let measurer: Measurer;

  beforeAll(async () => {
    const opened = await measuringPage();
    close = opened.close;
    measurer = new Measurer(async () => opened.page, systemFonts);
  });
  afterAll(async () => {
    await close();
  });

  const measure = (html: string, canvas: Canvas = WIDE) => measurer.measure(html, canvas, editor);

  // The format's own example of a content slide, its picture a workspace path made a data URL by the worker.
  const PLAN = `<section id="plan" style="background:#fbfbf8;color:#1a1a1a;font-family:Georgia, serif;padding:128px 128px 160px;display:flex;flex-direction:column;justify-content:space-between;gap:48px">
<h2 style="font-size:72px;font-weight:400;line-height:1.1">Changes</h2>
<div style="display:flex;gap:32px">
<div style="flex:1;display:flex;flex-direction:column;gap:12px;background:#ffffff;padding:40px;border:1px solid #e3e6e4">
<h3 style="font-size:32px;font-weight:600">One path</h3>
</div>
<img src="${PNG}" alt="HQ" style="width:480px;height:360px;object-fit:cover">
</div>
<p style="position:absolute;left:128px;bottom:64px;font-size:24px;color:#6a7179">Source: poll</p>
<aside>Say hi.</aside>
</section>`;

  it("measures the format's example slide into native shapes", async () => {
    const spec: Any = await measure(PLAN);
    expect(spec.background).toEqual({ type: "solid", color: { hex: "#fbfbf8", alpha: 1 } });
    expect(spec.notes).toBe("Say hi.");
    // Flow content first (the heading, the card and its text, the picture), then the pinned footer above it.
    expect(spec.shapes.map((s: Any) => [s.kind, s.text?.paras[0]?.runs[0]?.text ?? null])).toEqual([
      ["shape", "Changes"], ["shape", null], ["shape", "One path"], ["picture", null], ["shape", "Source: poll"],
    ]);
    const heading = spec.shapes[0];
    expect(heading).toMatchObject({ x: 128 * E, y: 128 * E, rot: 0, geometry: { preset: "rect" }, fill: null, stroke: null });
    expect(heading.text.paras[0].runs[0]).toMatchObject({ size: 36, font: "Georgia", color: { hex: "#1a1a1a", alpha: 1 } });
    expect(spec.shapes[1]).toMatchObject({ fill: { type: "solid", color: { hex: "#ffffff", alpha: 1 } }, stroke: { w: E, color: { hex: "#e3e6e4", alpha: 1 }, dash: "solid" } });
    expect(spec.shapes[4]).toMatchObject({ x: 128 * E, text: { insets: [0, 0, 0, 0] } });
  });

  it("keeps a text box's padding and background on the one shape, as a pill", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px">
      <p style="position:absolute;left:200px;top:200px;font-size:32px;background:#14285A;color:#ffffff;padding:12px 24px">Pill</p></section>`);
    expect(spec.shapes[0]).toMatchObject({
      x: 200 * E, y: 200 * E, fill: { type: "solid", color: { hex: "#14285a", alpha: 1 } },
      text: { insets: [24 * E, 12 * E, 24 * E, 12 * E], wrap: true },
    });
  });

  it("puts pinned children listed before the first flow child below the flow layer", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;display:flex">
      <div style="position:absolute;left:0;top:0;width:1920px;height:400px;background:#14285A"></div>
      <h1 style="font-size:96px;color:#ffffff">Over the band</h1>
      <div style="position:absolute;left:0;top:900px;width:1920px;height:20px;background:#c81e1e"></div></section>`);
    expect(spec.shapes.map((s: Any) => s.fill?.color.hex ?? s.text?.paras[0].runs[0].text)).toEqual(["#14285a", "Over the band", "#c81e1e"]);
  });

  // Claude Slides' format: a section or div with no display is a column; display:flex takes CSS's own default, a row.
  it("lays a display:flex section or div out as a row, and a div with no display as a column", async () => {
    const two = `<div style="width:200px;height:100px;background:#c81e1e"></div><div style="width:200px;height:100px;background:#14285A"></div>`;
    const at = (spec: Any) => spec.shapes.map((s: Any) => [s.x / E, s.y / E]);
    expect(at(await measure(`<section style="background:#ffffff;padding:128px"><div style="display:flex;gap:20px">${two}</div></section>`))).toEqual([[128, 128], [348, 128]]);
    expect(at(await measure(`<section style="background:#ffffff;padding:128px;display:flex;gap:20px">${two}</section>`))).toEqual([[128, 128], [348, 128]]);
    expect(at(await measure(`<section style="background:#ffffff;padding:128px"><div style="gap:20px">${two}</div></section>`))).toEqual([[128, 128], [128, 248]]);
  });

  it("reads a list and a div of paragraphs as one text box each", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;font-family:Arial">
      <ul style="font-size:32px"><li>North grew fastest</li><li>East held</li></ul>
      <div data-text style="font-size:32px"><p>First paragraph.</p><p>Second <b>bold</b> one.</p></div></section>`);
    expect(spec.shapes[0].text.paras.map((p: Any) => [p.bullet, p.runs[0].text])).toEqual([["•", "North grew fastest"], ["•", "East held"]]);
    expect(spec.shapes[1].text.paras.map((p: Any) => p.runs.map((r: Any) => r.text).join(""))).toEqual(["First paragraph.", "Second bold one."]);
  });

  it("reads a coloured span in a paragraph and in a list item as its own run", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;font-family:Arial">
      <p>Revenue <span style="color:#c81e1e">up</span> again</p><ul><li>East <span style="color:#c81e1e">held</span></li></ul></section>`);
    expect(spec.shapes[0].text.paras[0].runs.map((r: Any) => [r.text, r.color.hex])).toEqual([["Revenue ", "#000000"], ["up", "#c81e1e"], [" again", "#000000"]]);
    expect(spec.shapes[1].text.paras[0].runs[1]).toMatchObject({ text: "held", color: { hex: "#c81e1e" } });
  });

  it("lays out a table with its row backgrounds", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;font-family:Arial">
      <table style="width:1200px;font-size:32px"><tr style="background:#14285A"><th style="width:50%;color:#ffffff">Region</th><th style="width:25%;color:#ffffff">Q1</th><th style="width:25%;color:#ffffff">Q2</th></tr>
      <tr style="background:#EEF2FA"><td>North</td><td>120</td><td>140</td></tr></table></section>`);
    const table = spec.shapes[0];
    expect(table.kind).toBe("table");
    expect(table.cols).toEqual([600 * E, 300 * E, 300 * E]);
    expect(table.rows.map((r: Any) => r.fill?.hex ?? null)).toEqual(["#14285a", "#eef2fa"]);
    expect(table.rows[0].cells[0].paras[0].runs[0]).toMatchObject({ text: "Region", b: true, color: { hex: "#ffffff", alpha: 1 } });
  });

  it("fits a picture by object-fit: contain as a smaller box, cover as a crop", async () => {
    const wide = "data:image/svg+xml;base64," + Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"/>').toString("base64");
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;display:flex;flex-direction:row;gap:40px">
      <img src="${wide}" alt="a" style="width:300px;height:300px;object-fit:contain">
      <img src="${wide}" alt="b" style="width:300px;height:300px;object-fit:cover"></section>`);
    expect(spec.shapes[0]).toMatchObject({ kind: "picture", y: (128 + 75) * E, w: 300 * E, h: 150 * E });
    expect(spec.shapes[1]).toMatchObject({ kind: "picture", w: 300 * E, h: 300 * E, crop: [25, 0, 25, 0] });
  });

  it("counts a picture's data against no size limit", async () => {
    const big = "data:image/svg+xml;base64," + Buffer.from(`<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"><!-- ${"x".repeat(160_000)} --></svg>`).toString("base64");
    expect(big.length).toBeGreaterThan(200_000);
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px"><img src="${big}" alt="a" style="width:400px;height:200px"></section>`);
    expect(spec.shapes[0]).toMatchObject({ kind: "picture", w: 400 * E, h: 200 * E });
  });

  it("refuses text that overflows its box, and a word wider than its box", async () => {
    await expect(measure(`<section style="background:#fff"><p style="position:absolute;left:0;top:0;width:300px;height:40px;font-size:32px">This sentence is far too long for a box this small.</p></section>`))
      .rejects.toThrow(/overflows its box by \d+ px/);
    await expect(measure(`<section style="background:#fff"><p style="position:absolute;left:0;top:0;width:120px;font-size:48px">Leadership</p></section>`))
      .rejects.toThrow(/overflows its box/);
  });

  it("refuses a fixed-height card whose text needs more lines", async () => {
    await expect(measure(`<section style="background:#fff;font-family:Arial"><div style="position:absolute;left:100px;top:100px;width:400px;height:200px;padding:40px;background:#eeeeee">
      <p style="font-size:32px">Revenue grew in every region this quarter, led by the north and the new accounts.</p></div></section>`))
      .rejects.toThrow(/overflows its box by \d+ px/);
  });

  it("measures a pinned bar across a padded card's top edge", async () => {
    const spec: Any = await measure(`<section style="background:#fff;padding:128px"><div style="position:relative;padding:40px;background:#eeeeee;width:600px">
      <div style="position:absolute;left:0;top:0;width:100%;height:8px;background:#c81e1e"></div><p>Card</p></div></section>`);
    expect(spec.shapes.map((s: Any) => s.fill?.color.hex ?? s.text?.paras[0].runs[0].text)).toEqual(["#eeeeee", "Card", "#c81e1e"]);
  });

  // The editor lays lines by their pitch; the glyphs reaching past a tight line height are no overflow there.
  it("measures a 120 px Georgia h1 at line-height 1.1", async () => {
    const spec: Any = await measure(`<section style="background:#fff;padding:128px;font-family:Georgia"><h1 style="font-size:120px;line-height:1.1">Quarterly gains</h1></section>`);
    expect(spec.shapes[0].text.paras[0].runs[0]).toMatchObject({ text: "Quarterly gains", font: "Georgia" });
  });

  it("measures a DejaVu Sans h1 at line-height 1", async () => {
    const spec: Any = await measure(`<section style="background:#fff;padding:128px;font-family:'DejaVu Sans'"><h1 style="font-size:96px;line-height:1">Quarterly gains</h1></section>`);
    expect(spec.shapes[0].text.paras[0].runs[0]).toMatchObject({ text: "Quarterly gains", font: "DejaVu Sans" });
  });

  it("measures a text box whose paragraphs have a tighter line height than it", async () => {
    const spec: Any = await measure(`<section style="background:#fff;padding:128px;font-family:'DejaVu Sans'">
      <div data-text><p style="font-size:96px;line-height:1">Quarterly gains</p></div>
      <ul style="font-size:64px"><li style="line-height:1">Quarterly gains</li></ul></section>`);
    expect(spec.shapes.map((s: Any) => s.text.paras[0].runs[0].text)).toEqual(["Quarterly gains", "Quarterly gains"]);
  });

  it("refuses a text box too short for its paragraphs, tight line heights and all", async () => {
    await expect(measure(`<section style="background:#fff;font-family:'DejaVu Sans'"><div data-text style="position:absolute;left:100px;top:100px;width:1200px;height:125px">
      <p style="font-size:96px;line-height:1">Quarterly gains</p><p style="font-size:32px;line-height:1.2">and one more line</p></div></section>`))
      .rejects.toThrow(/overflows its box by \d+ px/);
  });

  it("measures a heading on the slide's bottom edge whose bold words reach past its lines", async () => {
    const spec: Any = await measure(`<section style="background:#fff;font-family:'DejaVu Sans'">
      <h1 style="position:absolute;left:128px;bottom:0;font-size:96px;line-height:1">Quarterly <b>gains</b></h1></section>`);
    expect(spec.shapes[0].text.paras[0].runs.map((r: Any) => r.text).join("")).toBe("Quarterly gains");
  });

  it("refuses text under 24 px", async () => {
    await expect(measure(`<section style="background:#fff"><p style="font-size:20px">Small print</p></section>`)).rejects.toThrow("the least on a slide is 24 px");
  });

  it("refuses text outside a text element, which nothing would draw", async () => {
    const OUTSIDE = "text goes in h1–h3, p, a list, or a div[data-text] of them";
    for (const [html, said] of [
      [`<section style="background:#fff"><div style="background:#eeeeee;padding:24px">Revenue up 12%</div></section>`, OUTSIDE],
      [`<section style="background:#fff">Revenue up 12%</section>`, OUTSIDE],
      [`<section style="background:#fff"><div>Lead-in <p>Para</p></div></section>`, OUTSIDE],
      [`<section style="background:#fff"><div data-text>Hello <b>world</b></div></section>`, OUTSIDE],
      [`<section style="background:#fff"><table><tr><td><p>One</p><p>Two</p></td></tr></table></section>`, "a table cell holds plain text"],
      [`<section style="background:#fff"><p>Up <svg width="40" height="40"></svg></p></section>`, "a heading, paragraph or list item holds text"],
    ]) {
      await expect(measure(html!), html).rejects.toThrow(said!);
    }
  });

  it("refuses what is not in the format, and ignores what the format ignores", async () => {
    for (const [html, said] of [
      [`<section style="background:#fff"><style>p{color:red}</style><p>x</p></section>`, "not <style>"],
      [`<section style="background:#fff"><p onclick="alert(1)">x</p></section>`, "the attribute onclick"],
      [`<section style="background:#fff"><p style="font-size:2em">x</p></section>`, "no em"],
      [`<section style="background:#fff"><p style="margin:12px;font-size:32px">x</p></section>`, "no margin"],
      [`<section style="background:#fff"><p style="z-index:3;font-size:32px">x</p></section>`, "uses z-index"],
      [`<section style="background:#fff"><p style="font-size:32px"><span style="font-size:40px">x</span></p></section>`, "no sizes or fonts on a span"],
      [`<section style="background:#fff"><img src="https://example.com/a.png" alt=""></section>`, "a picture from the workspace"],
      [`<section style="background:#fff"><x-shape kind="star" style="width:100px;height:100px"></x-shape></section>`, "kind is one of"],
      [`<section style="background:#fff"><p>a</p></section><p>b</p>`, "one <section> and nothing beside it"],
    ]) {
      await expect(measure(html!), html).rejects.toThrow(said!);
    }
    await expect(measure(`<section class="x" data-section="Intro" style="background:#fff"><p data-note="y" style="font-size:32px">ok</p></section>`)).resolves.toBeTruthy();
  });

  it("resolves a font stack to a face the document server has", async () => {
    const spec: Any = await measure(`<section style="background:#fff;font-family:'DM Sans', Georgia, serif"><p style="font-size:32px">x</p></section>`);
    expect(spec.shapes[0].text.paras[0].runs[0].font).toBe("Georgia");
  });

  it("a 4:3 deck measures on a 1920 by 1440 canvas", async () => {
    const tall: Canvas = { width: 1920, height: 1440, emuPerPx: 9144000 / 1920 };
    const spec: Any = await measure(`<section style="background:#fff"><div style="position:absolute;left:0;top:1300px;width:1920px;height:100px;background:#000000"></div></section>`, tall);
    expect(spec.shapes[0]).toMatchObject({ y: Math.round(1300 * tall.emuPerPx), h: Math.round(100 * tall.emuPerPx) });
    await expect(measure(`<section style="background:#fff"><div style="position:absolute;left:0;top:1000px;width:10px;height:100px;background:#000000"></div></section>`))
      .rejects.toThrow("past the slide's edge");
  });

  it("draws an svg, an icon and an embed into pictures", async () => {
    const spec: Any = await measure(`<section style="background:#fff">
      <svg aria-label="bar" style="position:absolute;left:10px;top:10px" width="200" height="100" viewBox="0 0 200 100"><rect width="200" height="100" fill="#c81e1e"/></svg>
      <x-icon name="Check" style="position:absolute;left:300px;top:10px;width:96px;height:96px;color:#14285A"></x-icon>
      <x-embed style="position:absolute;left:500px;top:10px;width:400px;height:200px"><div id="n" style="font:48px Arial">…</div><script>document.getElementById("n").textContent = "Live";</script></x-embed>
    </section>`);
    expect(spec.shapes.map((s: Any) => [s.kind, s.x / E, s.w / E])).toEqual([["picture", 10, 200], ["picture", 300, 96], ["picture", 500, 400]]);
    expect(spec.shapes[0].src).toMatch(/^data:image\/png;base64,/);
    expect(spec.shapes[1].src).toMatch(/^data:image\/svg\+xml;base64,/);
    expect(Buffer.from(spec.shapes[1].src.split(",")[1], "base64").toString()).toContain("#14285a");
    expect(spec.shapes[2].src).toMatch(/^data:image\/png;base64,/);
  });

  it("draws a slide svg as itself, past an svg inside an embed", async () => {
    const spec: Any = await measure(`<section style="background:#fff">
      <x-embed style="position:absolute;left:500px;top:10px;width:400px;height:200px"><svg data-draw="0" width="60" height="60"><rect width="60" height="60" fill="#00ff00"/></svg></x-embed>
      <svg aria-label="bar" style="position:absolute;left:10px;top:10px" width="200" height="100" viewBox="0 0 200 100"><rect width="200" height="100" fill="#c81e1e"/></svg>
    </section>`);
    expect(spec.shapes.map((s: Any) => [s.kind, s.x / E])).toEqual([["picture", 500], ["picture", 10]]);
    // The PNG's own size, from its header: the 200 by 100 svg at device scale 2.
    const png = Buffer.from(spec.shapes[1].src.split(",")[1], "base64");
    expect([png.readUInt32BE(16), png.readUInt32BE(20)]).toEqual([400, 200]);
  });

  it("reads a hidden slide", async () => {
    expect((await measure(`<section hidden style="background:#fff"><p style="font-size:32px">x</p></section>`)).hidden).toBe(true);
  });
});
