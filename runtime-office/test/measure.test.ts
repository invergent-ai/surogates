import { EventEmitter } from "node:events";
import { errors, type Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { TimedOut, type EditorPage } from "../src/connector.js";
import { ContentError } from "../src/markdown.js";
import { PagePool } from "../src/pages.js";
import { Measurer, SLIDE_TIMEOUT_MS, type Canvas, type FontSource } from "../src/slides/measure.js";
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

  it("tells a slide without notes from one whose empty aside clears them", async () => {
    expect((await measure(`<section style="background:#ffffff"><p style="font-size:32px">Hi</p></section>`)).notes).toBeNull();
    expect((await measure(`<section style="background:#ffffff"><p style="font-size:32px">Hi</p><aside></aside></section>`)).notes).toBe("");
  });

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

  // Models write a figure as a div of text in a card, and a step number as a box that flex centres it in.
  it("builds a div of text as one text box, styled by the div itself", async () => {
    for (const div of [
      `<div style="font-size:48px;font-weight:700;color:#0369A1;padding-bottom:12px">30%</div>`,
      `<div data-text style="font-size:48px;font-weight:700;color:#0369A1;padding-bottom:12px">30%</div>`,
    ]) {
      const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;font-family:Arial">
        <div style="background:#F8FAFC;border:2px solid #E2E8F0;padding:32px;width:600px">${div}<p style="font-size:32px">Lower costs</p></div></section>`);
      const text = spec.shapes[1];
      expect(text, div).toMatchObject({ kind: "shape", x: 162 * E, y: 162 * E, fill: null, text: { insets: [0, 0, 0, 12 * E] } });
      expect(text.text.paras, div).toHaveLength(1);
      expect(text.text.paras[0].runs, div).toEqual([expect.objectContaining({ text: "30%", size: 24, b: true, color: { hex: "#0369a1", alpha: 1 } })]);
    }
  });

  it("lays a div's text and inline elements out as one paragraph", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;font-family:Arial">
      <div style="font-size:32px;width:1200px">Up <b>30%</b> on <span style="color:#c81e1e">cost</span></div></section>`);
    const text = spec.shapes[0];
    expect(text.text.paras[0].runs.map((r: Any) => r.text)).toEqual(["Up ", "30%", " on ", "cost"]);
    // One line, as a p sets it.
    expect(text.h / E).toBeCloseTo(32 * 1.4, 0);
  });

  it("centres a div's text where its flex or grid layout centres it", async () => {
    for (const centred of ["display:flex;align-items:center;justify-content:center", "display:grid;place-items:center"]) {
      const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;font-family:Arial">
        <div style="background:#38BDF8;color:#0F172A;width:96px;height:96px;${centred};font-size:48px;font-weight:700">1</div></section>`);
      expect(spec.shapes, centred).toHaveLength(1);
      expect(spec.shapes[0], centred).toMatchObject({
        w: 96 * E, h: 96 * E, fill: { type: "solid", color: { hex: "#38bdf8", alpha: 1 } },
        text: { anchor: "ctr", paras: [{ align: "center", runs: [{ text: "1" }] }] },
      });
    }
  });

  it("refuses text beside blocks in one div, saying to put it in a p, which then goes in", async () => {
    await expect(measure(`<section style="background:#fff"><div style="font-size:32px">Lead-in <p>Para</p></div></section>`))
      .rejects.toThrow('Text "Lead-in" sits beside blocks in <section > div> ("Lead-in Para"): put it in a <p>.');
    await expect(measure(`<section style="background:#fff"><div style="font-size:32px"><p>Lead-in</p><p>Para</p></div></section>`)).resolves.toBeTruthy();
  });

  it("moves a box by its margin, and keeps margins inside the overflow checks", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;font-family:Arial">
      <h1 style="font-size:96px;margin:0 0 24px 0">Lisbon</h1><p style="font-size:32px;margin:0">Next</p></section>`);
    const [h1, p] = spec.shapes;
    expect(h1.y).toBe(128 * E);
    expect((p.y - h1.y - h1.h) / E).toBeCloseTo(24, 1);
    await expect(measure(`<section style="background:#fff;font-family:Arial"><div style="position:absolute;left:100px;top:100px;width:400px;height:120px;background:#eeeeee">
      <p style="font-size:32px;margin-top:100px">Pushed down</p></div></section>`)).rejects.toThrow(/overflows its box by \d+ px/);
  });

  it("keeps the space margins put between a list's items and between a text box's paragraphs", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;font-family:Arial">
      <ul style="font-size:32px"><li style="margin-bottom:16px">North</li><li>East</li></ul>
      <div data-text style="font-size:32px"><p style="margin-bottom:20px">One</p><p>Two</p></div></section>`);
    // 1 px is 0.5 pt on 16:9.
    expect(spec.shapes[0].text.paras.map((p: Any) => p.before)).toEqual([0, 8]);
    expect(spec.shapes[1].text.paras.map((p: Any) => p.before)).toEqual([0, 10]);
  });

  it("refuses a div whose own flex or grid layout keeps its pieces apart, and takes them as separate p's", async () => {
    const APART = "lays out its pieces apart (gap, space-between or columns), which one text box cannot: put each in its own <p>.";
    for (const div of [
      `<div style="display:flex;justify-content:space-between;width:600px;font-size:32px"><b>Q1</b><span>$4.2M</span></div>`,
      `<div style="display:grid;grid-template-columns:1fr 1fr;width:600px;font-size:32px"><span>North</span><span>120</span><span>South</span><span>90</span></div>`,
      `<div style="display:flex;flex-direction:column;font-size:32px">Revenue<b>up</b></div>`,
      `<div style="display:flex;gap:16px;font-size:32px">Revenue <b>up</b></div>`,
    ]) {
      await expect(measure(`<section style="background:#fff;padding:128px">${div}</section>`), div).rejects.toThrow(`<section > div> ${"("}`);
      await expect(measure(`<section style="background:#fff;padding:128px">${div}</section>`), div).rejects.toThrow(APART);
    }
    const spec: Any = await measure(`<section style="background:#fff;padding:128px;font-family:Arial">
      <div style="display:flex;justify-content:space-between;width:600px"><p style="font-size:32px">Q1</p><p style="font-size:32px">$4.2M</p></div></section>`);
    expect(spec.shapes.map((s: Any) => s.text.paras[0].runs[0].text)).toEqual(["Q1", "$4.2M"]);
    expect(spec.shapes[1].x).toBeGreaterThan(spec.shapes[0].x + spec.shapes[0].w);
  });

  it("refuses a negative margin, which fits text the editor cannot", async () => {
    for (const html of [
      `<p style="width:400px;font-size:32px">Left words <span style="margin-left:-80px">pulled back over them</span></p>`,
      `<div data-text style="font-size:32px"><p>One</p><p style="margin-top:-30px">Two</p></div>`,
      `<ul style="font-size:32px"><li>One</li><li style="margin-top:-30px">Two</li></ul>`,
    ]) {
      await expect(measure(`<section style="background:#fff;padding:128px">${html}</section>`), html).rejects.toThrow("no negative margin: use gap or padding.");
    }
  });

  it("says to draw an empty inline element beside blocks as a div, which then goes in", async () => {
    await expect(measure(`<section style="background:#fff;padding:128px"><div style="display:flex;gap:12px"><span style="display:inline-block;width:16px;height:16px;background:#ff0000"></span><p>Revenue</p></div></section>`))
      .rejects.toThrow('<section > div > span> sits beside blocks in <section > div> ("Revenue"): draw it as a <div>.');
    const spec: Any = await measure(`<section style="background:#fff;padding:128px"><div style="display:flex;gap:12px"><div style="width:16px;height:16px;background:#ff0000"></div><p>Revenue</p></div></section>`);
    expect(spec.shapes.map((s: Any) => s.fill?.color.hex ?? s.text.paras[0].runs[0].text)).toEqual(["#ff0000", "Revenue"]);
  });

  it("refuses a painted span inside text, saying to draw it as a div beside the text, which then goes in", async () => {
    const DRAW = "a painted <span> inside text can't be drawn: draw it as a <div> beside the text.";
    for (const html of [
      `<p style="font-size:32px">Status: <span style="background:#15803D;color:#ffffff;padding:4px 12px">Live</span></p>`,
      `<div style="font-size:32px">Status: <span style="background:#15803D;color:#ffffff;padding:4px 12px">Live</span></div>`,
      `<div style="display:flex;gap:12px;align-items:center;font-size:32px"><span style="display:inline-block;width:16px;height:16px;background:#ff0000"></span>Revenue</div>`,
      `<p style="font-size:32px"><span style="border:2px solid #000000">Boxed</span></p>`,
    ]) {
      await expect(measure(`<section style="background:#fff;padding:128px">${html}</section>`), html).rejects.toThrow(DRAW);
    }
    const spec: Any = await measure(`<section style="background:#fff;padding:128px;font-family:Arial">
      <div style="display:flex;gap:12px;align-items:center"><p style="font-size:32px">Status:</p><div style="font-size:32px;background:#15803D;color:#ffffff;padding:4px 12px">Live</div></div></section>`);
    expect(spec.shapes.map((s: Any) => [s.fill?.color.hex ?? null, s.text.paras[0].runs[0].text])).toEqual([[null, "Status:"], ["#15803d", "Live"]]);
  });

  it("leaves out a hidden list item, paragraph or span", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px;font-family:Arial">
      <ul style="font-size:32px"><li>North</li><li style="display:none">Hidden</li><li>East</li></ul>
      <div data-text style="font-size:32px"><p>One</p><p style="display:none">Hidden</p><p>Two</p></div>
      <p style="font-size:32px">Shown <span style="display:none">hidden </span>text</p></section>`);
    const texts = (shape: Any) => shape.text.paras.map((p: Any) => [p.runs.map((r: Any) => r.text).join(""), p.before]);
    expect(texts(spec.shapes[0])).toEqual([["North", 0], ["East", 0]]);
    expect(texts(spec.shapes[1])).toEqual([["One", 0], ["Two", 0]]);
    expect(texts(spec.shapes[2])).toEqual([["Shown text", 0]]);
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

  it("names the slide's first heading's shape Title, and no other", async () => {
    const spec: Any = await measure(`<section style="background:#fff;padding:128px;font-family:Arial">
      <p style="position:absolute;left:1480px;top:88px;width:320px;font-size:26px">NEW HUB</p>
      <h2 style="font-size:64px">Lisbon: team and setup</h2><h3 style="font-size:44px">Who goes</h3></section>`);
    expect(spec.shapes.filter((s: Any) => s.name === "Title").map((s: Any) => s.text.paras[0].runs[0].text)).toEqual(["Lisbon: team and setup"]);
  });

  it("refuses a table row or cell background that is not solid", async () => {
    for (const [row, cell, said] of [
      ["background:rgba(255,255,255,0.08)", "", 'tr:nth-of-type(2)> ("North120"): a table row\'s background must be a solid colour (the editor draws it opaque).'],
      ["", "background:rgba(20,40,90,0.5)", 'td:nth-of-type(1)> ("North"): a table cell\'s background must be a solid colour (the editor draws it opaque).'],
      ["background:linear-gradient(90deg, #14213D, #1A5C9A)", "", 'tr:nth-of-type(2)> ("North120"): a table row\'s background must be a solid colour'],
      ["", "background:linear-gradient(90deg, #14213D, #1A5C9A)", 'td:nth-of-type(1)> ("North"): a table cell\'s background must be a solid colour'],
    ]) {
      const html = `<section style="background:#14213D;color:#ffffff;padding:128px"><table style="width:1200px;font-size:32px">
        <tr style="background:#14213D"><th>Region</th><th>Q1</th></tr><tr style="${row}"><td style="${cell}">North</td><td>120</td></tr></table></section>`;
      await expect(measure(html), html).rejects.toThrow(said!);
    }
  });

  it("takes gradient text in a table cell: its background is the text's", async () => {
    const spec: Any = await measure(`<section style="background:#fff;padding:128px;font-family:Arial"><table style="width:1200px;font-size:32px">
      <tr><td style="background:linear-gradient(90deg,#c81e1e,#2f5597);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent">North</td><td>120</td></tr></table></section>`);
    expect(spec.shapes[0].rows[0].cells[0].paras[0].runs[0].gradient).toMatchObject({ type: "linear", angle: 90 });
  });

  it("refuses gradient text set on a row or a table: only a cell's reaches its text", async () => {
    const clipped = "background:linear-gradient(90deg,#c81e1e,#2f5597);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent";
    for (const [table, row, said] of [
      ["", clipped, "tr> (\"North120\"): a table row's background must be a solid colour"],
      [clipped, "", "<section > table> (\"North120\"): the editor draws no background on a <table>"],
    ]) {
      const html = `<section style="background:#fff;padding:128px"><table style="width:1200px;font-size:32px;${table}"><tr style="${row}"><td>North</td><td>120</td></tr></table></section>`;
      await expect(measure(html), html).rejects.toThrow(said!);
    }
  });

  it("refuses a background on a table or its row groups, which the editor does not draw", async () => {
    for (const [table, body, said] of [
      ["background:#14213D", "", "<section > table> (\"North120\"): the editor draws no background on a <table>: put the background on the table's rows (<tr>)."],
      ["", "background:linear-gradient(90deg, #14213D, #1A5C9A)", "<section > table > tbody> (\"North120\"): the editor draws no background on a <tbody>: put the background on the table's rows (<tr>)."],
    ]) {
      const html = `<section style="background:#fff;padding:128px"><table style="width:1200px;font-size:32px;${table}"><tbody style="${body}"><tr><td>North</td><td>120</td></tr></tbody></table></section>`;
      await expect(measure(html), html).rejects.toThrow(said!);
    }
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

  const svgData = (svg: string) => "data:image/svg+xml;base64," + Buffer.from(svg).toString("base64");
  const handedOn = (src: string) => Buffer.from(src.split(",")[1]!, "base64").toString();

  it("measures matplotlib's SVG, and hands it on without its DOCTYPE", async () => {
    const svg = `<?xml version="1.0" encoding="utf-8" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN"
  "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg xmlns:xlink="http://www.w3.org/1999/xlink" width="400pt" height="200pt" viewBox="0 0 400 200" xmlns="http://www.w3.org/2000/svg" version="1.1">
 <defs>
  <style type="text/css">*{stroke-linejoin: round; stroke-linecap: butt}</style>
  <linearGradient id="g"><stop offset="0" stop-color="#14285a"/></linearGradient>
  <path id="a" d="M 0 0 L 10 0 L 10 10 z"/>
 </defs>
 <rect width="400" height="200" style="fill: url(#g)"/>
 <use xlink:href="#a" x="20" y="20" style="fill: #c81e1e"/>
 <text x="40" y="100">online = 34%</text>
</svg>`;
    const spec: Any = await measure(`<section style="background:#ffffff;padding:128px"><img src="${svgData(svg)}" alt="chart" style="width:400px;height:200px"></section>`);
    expect(spec.shapes[0]).toMatchObject({ kind: "picture", w: 400 * E, h: 200 * E });
    const sent = handedOn(spec.shapes[0].src);
    expect(sent).not.toContain("DOCTYPE");
    expect(sent).toContain('xlink:href="#a"');
    expect(sent).toContain("online = 34%");
  });

  const svg = (inside: string, head = "") => `${head}<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="8" height="8">${inside}</svg>`;
  const script = svg("<script>alert(1)</script>");
  const sized = 'alt="x" style="width:8px;height:8px"';
  const quoted = (body: string) => `<img src="${svgData(body)}" ${sized}>`;
  const b64 = Buffer.from(script).toString("base64");
  it.each([
    ["a script", quoted(script), "holds a script"],
    ["a prefixed script", quoted(svg("<s:script xmlns:s='http://www.w3.org/2000/svg'>alert(1)</s:script>")), "holds a script"],
    ["a foreignObject", quoted(svg('<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>')), "holds a foreignObject"],
    ["an onload", quoted(svg('<rect width="8" height="8" onload="alert(1)"/>')), "the attribute onload"],
    ["an external href", quoted(svg('<image href="http://169.254.169.254/latest" width="8" height="8"/>')), "links to http://169.254.169.254/latest"],
    ["an external xlink:href", quoted(svg("<use xlink:href='https://example.com/a.svg#b'/>")), "links to https://example.com/a.svg#b"],
    ["an escaped external url()", quoted(svg('<rect width="8" height="8" style="fill:u\\72l(http://example.com/x)"/>')), "links to http://example.com/x"],
    ["an ENTITY", quoted(svg("<text>&x;</text>", '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>')), "declares <!ENTITY>"],
    ["a DOCTYPE with declarations", quoted(svg("<rect width='8' height='8'/>", '<!DOCTYPE svg [<!ATTLIST svg width CDATA "8">]>')), "declares <!ATTLIST>"],
    // The same script however the img writes its src.
    ["an unquoted src", `<img src=data:image/svg+xml;base64,${b64} ${sized}>`, "holds a script"],
    ["a src after a decoy", `<img data-src="data:," src="data:image/svg+xml;base64,${b64}" ${sized}>`, "holds a script"],
    ["a character reference in the data", `<img src="data:image/svg+xml;base64,${b64.slice(0, 8)}&#${b64.charCodeAt(8)};${b64.slice(9)}" ${sized}>`, "holds a script"],
    ["UTF-16 data", `<img src="data:image/svg+xml;base64,${Buffer.from(`\ufeff${script}`, "utf16le").toString("base64")}" ${sized}>`, "holds a script"],
    ["percent-encoded data", `<img src="data:image/svg+xml,${encodeURIComponent(script)}" ${sized}>`, "holds a script"],
    ["an inline svg's onload", `<svg aria-label="x" onload="alert(1)" width="8" height="8"><rect width="8" height="8"/></svg>`, "the attribute onload"],
    // Data inside an SVG is a PNG or JPEG by its declared type and its whole signature.
    ["a nested SVG starting like a PNG", quoted(svg(`<image width="8" height="8" href="data:image/svg+xml;base64,${Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47]), Buffer.from(script)]).toString("base64")}"/>`)), "links to data:image/svg+xml"],
    ["a nested PNG with half a signature", quoted(svg(`<image width="8" height="8" href="data:image/png;base64,${Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47]), Buffer.from(script)]).toString("base64")}"/>`)), "links to data:image/png"],
  ])("refuses an SVG picture with %s, naming the picture", async (_what, img, said) => {
    const refusal = await measure(`<section style="background:#fff">${img}</section>`).then(() => "measured", (error: Error) => error.message);
    expect(refusal).toContain(said);
    expect(refusal).toMatch(/^<section > (img|svg)>: .*is plain shapes/);
  });

  it("refuses a picture whose data is not the PNG or JPEG it says", async () => {
    const svgAsPng = "data:image/png;base64," + Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"/>').toString("base64");
    await expect(measure(`<section style="background:#fff"><img src="${svgAsPng}" alt="x" style="width:8px;height:8px"></section>`)).rejects.toThrow("is not a PNG or JPEG");
    const halfSigned = "data:image/png;base64," + Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47]), Buffer.from(script)]).toString("base64");
    await expect(measure(`<section style="background:#fff"><img src="${halfSigned}" alt="x" style="width:8px;height:8px"></section>`)).rejects.toThrow("is not a PNG or JPEG");
  });

  it("checks an SVG holding thousands of DOCTYPE look-alikes in well under a second", async () => {
    const doc = `${svg("")}<!--${"<!DOCTYPE a".repeat(30_000)}-->`;
    const started = Date.now();
    await measure(`<section style="background:#fff">${quoted(doc)}</section>`);
    expect(Date.now() - started).toBeLessThan(1500);
  });

  it("checks thousands of svgs nested in one inline svg quickly", async () => {
    const started = Date.now();
    await expect(measure(`<section style="background:#fff"><svg aria-label="x" width="8" height="8">${"<svg>".repeat(10_000)}</svg></section>`)).rejects.toThrow("an svg is at most 52 KB");
    expect(Date.now() - started).toBeLessThan(3000);
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

  it("refuses white text on a white card", async () => {
    await expect(measure(`<section style="background:#14213D;padding:128px">
      <div style="background:#ffffff;padding:40px;width:900px"><h2 style="font-size:64px;color:#FBFBF8">Invisible heading</h2></div></section>`))
      .rejects.toThrow('<section > div > h2> ("Invisible heading"): its text (#fbfbf8) on #ffffff reads at 1.0:1; text needs at least 3:1 here: try #14213D.');
  });

  it("refuses text under 3:1, counting a gradient by its worst stop and faded text by its opacity, naming the toned colour that reads better", async () => {
    for (const [html, said] of [
      [`<section style="background:#ffffff;padding:128px"><div style="background:#F8FAFC;padding:32px;width:600px"><p style="font-size:48px;color:#38BDF8">30%</p></div></section>`, "its text (#38bdf8) on #f8fafc reads at 2.0:1; text needs at least 3:1 here: try #14213D."],
      [`<section style="background:#14213D;padding:128px"><p style="font-size:48px;color:#1A5C9A">Accent</p></section>`, "its text (#1a5c9a) on #14213d reads at 2.3:1; text needs at least 3:1 here: try #FBFBF8."],
      [`<section style="background:linear-gradient(90deg, #14213D, #F8FAFC);padding:128px"><h2 style="font-size:64px;color:#FBFBF8">Fading out</h2></section>`, "its text (#fbfbf8) on #f8fafc reads at 1.0:1"],
      [`<section style="background:#14213D;padding:128px"><p style="font-size:48px;color:#FBFBF8;opacity:0.3">Faint</p></section>`, "its text (#fbfbf8 at 30%) on #14213d reads at 2.6:1"],
    ]) {
      await expect(measure(html!), html).rejects.toThrow(said!);
    }
  });

  // Claude Slides paints a pinned element listed before the flow under it, where Chromium paints it over.
  it("takes white text on a pinned dark band", async () => {
    const spec: Any = await measure(`<section style="background:#FBFBF8;padding:128px">
      <div style="position:absolute;left:0;top:0;width:1920px;height:400px;background:#14285A"></div>
      <h1 style="font-size:96px;color:#FBFBF8">Over the band</h1></section>`);
    expect(spec.shapes.map((s: Any) => s.fill?.color.hex ?? s.text?.paras[0].runs[0].text)).toEqual(["#14285a", "Over the band"]);
  });

  it("composites a translucent card", async () => {
    const spec: Any = await measure(`<section style="background:#14213D;padding:128px">
      <div style="background:rgba(255,255,255,0.1);padding:40px;width:900px"><h2 style="font-size:64px;color:#FBFBF8">On glass</h2></div></section>`);
    expect(spec.shapes.map((s: Any) => s.text?.paras[0].runs[0].text ?? null)).toEqual([null, "On glass"]);
  });

  it("skips text over a picture", async () => {
    const spec: Any = await measure(`<section style="background:#ffffff">
      <img src="${PNG}" alt="Lisbon" style="position:absolute;left:0;top:0;width:1920px;height:1080px">
      <h1 style="position:absolute;left:128px;top:400px;font-size:96px;color:#FBFBF8">Over the photo</h1></section>`);
    expect(spec.shapes.map((s: Any) => s.kind)).toEqual(["picture", "shape"]);
  });

  it("refuses text outside a text element, which nothing would draw", async () => {
    for (const [html, said] of [
      [`<section style="background:#fff">Revenue up 12%</section>`, 'Text "Revenue up 12%" sits directly in <section> ("Revenue up 12%"): put it in a <p>.'],
      [`<section style="background:#fff"><div><b>Lead-in</b><p>Para</p></div></section>`, "<section > div > b> (\"Lead-in\") sits beside blocks in <section > div> (\"Lead-inPara\"): put it in a <p>."],
      [`<section style="background:#fff"><ul style="font-size:32px">Loose<li>Item</li></ul></section>`, 'Text "Loose" sits directly in <section > ul> ("LooseItem"): put it in an <li>.'],
      [`<section style="background:#fff"><div data-text style="font-size:32px"><p>One</p><div>Two</div></div></section>`, "a div[data-text] holds p, ul and ol, or only text"],
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

  it("takes CSS that changes nothing in the native shape, saying so, and refuses what would change it", async () => {
    const table: Any = await measure(`<section style="background:#fff;padding:128px;font-family:Arial">
      <table style="width:800px;font-size:32px;border-collapse:collapse"><tr><td>North</td><td>120</td></tr></table></section>`);
    expect(table.shapes[0].kind).toBe("table");
    expect(table.ignored).toEqual(["border-collapse: collapse on a table: native tables already collapse"]);
    const lists: Any = await measure(`<section style="background:#fff;padding:128px;font-family:Arial">
      <ul style="font-size:32px;list-style-type:disc"><li>One</li><li>Two</li></ul>
      <ol style="font-size:32px;list-style-type:decimal"><li>First</li><li>Second</li></ol></section>`);
    expect(lists.shapes.map((s: Any) => s.text.paras[0].bullet ?? (s.text.paras[0].numbered && "numbered"))).toEqual(["•", "numbered"]);
    expect(lists.ignored).toEqual([
      "list-style-type: disc on a ul: a native bulleted list already shows •",
      "list-style-type: decimal on an ol: a native numbered list already counts 1., 2., 3.",
    ]);
    expect((await measure(`<section style="background:#fff"><p style="font-size:32px">Plain</p></section>`)).ignored).toEqual([]);
    for (const html of [
      `<table style="font-size:32px;border-collapse:separate"><tr><td>North</td></tr></table>`,
      `<ol style="font-size:32px;list-style-type:lower-roman"><li>First</li></ol>`,
      `<ul style="font-size:32px;list-style-type:decimal"><li>One</li></ul>`,
      `<ol style="font-size:32px;list-style-type:disc"><li>First</li></ol>`,
      `<div style="border-collapse:collapse"><p style="font-size:32px">Not a table</p></div>`,
    ]) {
      const property = /(border-collapse|list-style-type)/.exec(html)![1];
      await expect(measure(`<section style="background:#fff;padding:128px">${html}</section>`), html).rejects.toThrow(`uses ${property}, which is not in the slide format`);
    }
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
      <x-embed style="position:absolute;left:500px;top:10px;width:400px;height:200px"><div style="font:48px Arial">Live</div></x-embed>
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

  it("draws an embed as static HTML and CSS: its script does not run", async () => {
    const embed = (script: string) => measure(`<section style="background:#fff">
      <x-embed style="position:absolute;left:500px;top:10px;width:400px;height:200px"><div id="n" style="width:400px;height:200px;background:#c81e1e"></div>${script}</x-embed>
    </section>`);
    const ran: Any = await embed(`<script>document.getElementById("n").style.background = "#00ff00";</script>`);
    const plain: Any = await embed("");
    expect(ran.shapes[0].src).toBe(plain.shapes[0].src);
  });

  it("refuses an embed that does not draw in time, naming it", async () => {
    // The embed's browser context answers its page's load with a timeout, as a page that never finishes loading does.
    const timeouts: (number | undefined)[] = [];
    const stalled = { route: async () => undefined, close: async () => undefined, newPage: async () => ({
      setContent: async (_html: string, options?: { timeout?: number }) => {
        timeouts.push(options?.timeout);
        throw new errors.TimeoutError("page.setContent: Timeout exceeded.");
      },
    }) };
    // *target* with *swap*'s members in place of its own.
    const swapped = <T extends object>(target: T, swap: Record<string, unknown>): T => new Proxy(target, {
      get: (object, key) => {
        if (typeof key === "string" && key in swap) return swap[key];
        const value: unknown = Reflect.get(object, key);
        return typeof value === "function" ? value.bind(object) : value;
      },
    });
    const real = await measuringPage();
    const browser = swapped(real.page.context().browser()!, { newContext: async () => stalled });
    const context = swapped(real.page.context(), { browser: () => browser });
    const page = swapped(real.page, { context: () => context });
    try {
      const refused = new Measurer(async () => page, systemFonts).measure(`<section style="background:#fff">
        <x-embed style="position:absolute;left:500px;top:10px;width:400px;height:200px"><div>Live</div></x-embed></section>`, WIDE, editor);
      await expect(refused).rejects.toThrow(ContentError);
      await expect(refused).rejects.toThrow("The x-embed at 500,10 px did not draw within 5 s");
      expect(timeouts).toEqual([5_000]);
    } finally {
      await real.close();
    }
  });

  it("reads a hidden slide", async () => {
    expect((await measure(`<section hidden style="background:#fff"><p style="font-size:32px">x</p></section>`)).hidden).toBe(true);
  });
});

// A measuring page that passes every check; with *hang* "load" its slide never finishes loading.
function fakePage(hang: "load" | "none") {
  const context = { close: vi.fn(async () => undefined) };
  const page = {
    setViewportSize: async () => undefined,
    setContent: () => (hang === "load" ? new Promise(() => undefined) : Promise.resolve()),
    evaluate: async () => ({ stacks: [], faces: [] }),
    context: () => context,
  };
  return { page: page as unknown as Page, context };
}

describe("a slide that does not measure in time", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("is stopped at its deadline, saying the step it was at, and the slide after it still measures", async () => {
    vi.useFakeTimers();
    const pages = [fakePage("load"), fakePage("none")];
    const fonts: FontSource = { list: () => new Promise(() => undefined), css: async () => "" };
    const page = vi.fn(async () => pages.shift()!.page);
    const measurer = new Measurer(page, fonts);
    const first = measurer.measure("<section></section>", WIDE, editor);
    const stopped = expect(first).rejects.toThrow(new TimedOut(`measuring the slide took over ${SLIDE_TIMEOUT_MS / 1000} s; it was loading the slide`));
    const second = measurer.measure("<section></section>", WIDE, editor);
    const named = expect(second).rejects.toThrow("it was reading the document server's font list");
    await vi.advanceTimersByTimeAsync(SLIDE_TIMEOUT_MS);
    await stopped;
    await vi.advanceTimersByTimeAsync(SLIDE_TIMEOUT_MS);
    await named;
    expect(page).toHaveBeenCalledTimes(2);
  });

  it("closes the page it stopped on", async () => {
    vi.useFakeTimers();
    const { page, context } = fakePage("load");
    const stopped = expect(new Measurer(async () => page, systemFonts).measure("<section></section>", WIDE, editor)).rejects.toThrow(TimedOut);
    await vi.advanceTimersByTimeAsync(SLIDE_TIMEOUT_MS);
    await stopped;
    expect(context.close).toHaveBeenCalled();
  });
});

describe("the measuring page", () => {
  it("is made again after its renderer crashes", async () => {
    const made: { page: EventEmitter & { isClosed(): boolean }; close: ReturnType<typeof vi.fn> }[] = [];
    const browser = {
      on: () => undefined,
      newContext: async () => {
        const page = Object.assign(new EventEmitter(), { isClosed: () => false });
        const close = vi.fn(async () => undefined);
        made.push({ page, close });
        return { route: async () => undefined, newPage: async () => page, close };
      },
    };
    const pool = new PagePool({ hostUrl: "", maxPages: 1, idleMs: 1, openTimeoutMs: 1, commandTimeoutMs: 1, ignoreTls: false });
    (pool as unknown as { browser: Promise<unknown> }).browser = Promise.resolve(browser);
    const first = await pool.measuringPage();
    expect(await pool.measuringPage()).toBe(first);
    made[0]!.page.emit("crash");
    const second = await pool.measuringPage();
    expect(second).not.toBe(first);
    expect(made[0]!.close).toHaveBeenCalled();
  });
});
