import { errors, type Page } from "playwright-core";
import { TimedOut, withTimeout, type EditorPage } from "../connector.js";
import { ContentError } from "../markdown.js";
import { Fonts, resolveFamily, type Face, type FontList } from "./fonts.js";
import { ICON_NAMES, iconSvg } from "./icons.js";
import { inspect, type InspectArgs, type Inspected, type Shot } from "./inspect.js";

/** The slide's canvas in px: 1920 wide, as tall as the deck's shape makes it, and the deck's EMU per px. */
export interface Canvas { width: number; height: number; emuPerPx: number }
export interface Color { hex: string; alpha: number }
export type Fill = { type: "solid"; color: Color } | { type: "linear"; angle: number; scaled: boolean; stops: { color: Color; at: number }[] };
export interface Stroke { w: number; color: Color; dash: "solid" | "dash" | "sysDot" }
export type Geometry =
  | { preset: "rect" | "ellipse" | "diamond" | "line" }
  | { preset: "roundRect"; adj: number }
  | { preset: "rightArrow" | "leftArrow" | "upArrow" | "downArrow"; adj1: number; adj2: number }
  | { custom: { w: number; h: number; fill: boolean; stroke: boolean; d: (string | number)[][] }[] };
export interface Run {
  text: string; b?: true; i?: true; u?: true; s?: true; caps?: true; size: number; font: string;
  color: Color; gradient?: Fill; spacing?: number; outline?: Stroke; href?: string;
}
export interface Para {
  align: string; line: number; before: number; after: number;
  bullet?: string; numbered?: true; start?: number; level?: number; indent?: number; hanging?: number; runs: Run[];
}
export interface Build { in?: { effect: string; order: number; auto: boolean }; out?: { effect: string; order: number; auto: boolean } }
interface Placed { x: number; y: number; w: number; h: number; rot: number; flipH?: true; flipV?: true; name?: string; magic?: string; build?: Build }
export interface Shadow { x: number; y: number; spread: number; color: Color }
export type ShapeSpec =
  | (Placed & { kind: "shape"; geometry: Geometry; fill: Fill | null; stroke: Stroke | null; shadow?: Shadow; opacity?: number;
      text?: { insets: number[]; anchor: "t" | "ctr" | "b"; wrap: boolean; paras: Para[] } })
  | (Placed & { kind: "picture"; src: string; crop?: number[]; radius?: number; opacity?: number; shadow?: Shadow })
  | (Placed & { kind: "table"; cols: number[]; pad: number[]; rows: { h: number; fill: Color | null; cells: { paras: Para[]; fill: Color | null; border: Stroke | null }[] }[] })
  | (Placed & { kind: "path"; points: number[][]; stroke: Stroke; head: "none" | "open" | "filled"; tail: "none" | "open" | "filled" })
  | (Placed & { kind: "group"; children: ShapeSpec[] });
export interface Transition { effect: string; ms: number }
// notes: the <aside>'s words, or null when the slide has none (a replaced slide then keeps the notes it had).
export interface SlideSpec { background: Fill | null; shapes: ShapeSpec[]; notes: string | null; hidden: boolean; transition: Transition | null }

/** Where a slide's fonts come from: the document server's, in runtime-office. */
export interface FontSource {
  list(editor: EditorPage): Promise<FontList>;
  css(editor: EditorPage, list: FontList, wanted: Face[]): Promise<string>;
}

// Counted without picture data: the worker inlines every workspace picture as a data URL.
const MAX_HTML = 200_000;
export const MIN_TEXT_PX = 24;
const EMBED_SETTLE_MS = 800;
// An embed draws in well under a second: one that has not in this long never will.
const EMBED_TIMEOUT_MS = 5_000;
// A slide measures in about a second, eight embeds and its fonts' first fetch in several more.
export const SLIDE_TIMEOUT_MS = 20_000;

/** What a measurement is doing, for the error that says where it stopped. */
type Step = { now: string };

export class Measurer {
  private queue: Promise<unknown> = Promise.resolve();

  constructor(private readonly page: () => Promise<Page>, private readonly fonts: FontSource = new Fonts()) {}

  /** *html*, one slide, checked and measured on *canvas* with the fonts of the document server *editor* loaded from. */
  measure(html: string, canvas: Canvas, editor: EditorPage): Promise<SlideSpec> {
    const run = this.queue.then(() => this.timed(html, canvas, editor), () => this.timed(html, canvas, editor));
    this.queue = run.catch(() => undefined);
    return run;
  }

  // Slides are measured one at a time, every document's on one page: one not measured in time is stopped, and the page,
  // in a state not known (a renderer stuck in a layout, a font that never came), is closed so the next gets a fresh one.
  private async timed(html: string, canvas: Canvas, editor: EditorPage): Promise<SlideSpec> {
    if (html.replace(/data:[^"')\s]+/g, "data:").length > MAX_HTML) throw new ContentError(`A slide's HTML is at most ${MAX_HTML} characters.`);
    const page = await this.page();
    const step: Step = { now: "loading the slide" };
    try {
      return await withTimeout(this.now(page, html, canvas, editor, step), SLIDE_TIMEOUT_MS, "");
    } catch (error) {
      if (!(error instanceof TimedOut)) throw error;
      await page.context().close().catch(() => undefined);
      throw new TimedOut(`measuring the slide took over ${SLIDE_TIMEOUT_MS / 1000} s; it was ${step.now}`);
    }
  }

  private async now(page: Page, html: string, canvas: Canvas, editor: EditorPage, step: Step): Promise<SlideSpec> {
    await page.setViewportSize({ width: canvas.width, height: canvas.height });
    await page.setContent(documentOf(html, canvas));
    const args = (phase: InspectArgs["phase"]): InspectArgs => ({ phase, canvas, minPx: MIN_TEXT_PX, icons: ICON_NAMES });
    step.now = "checking the slide";
    const checked = await this.inspect(page, args("check"));
    // The check gives each SVG picture the data it checked: the pictures load again before anything is measured.
    await page.evaluate(() => Promise.all(Array.from(document.images, (img) => img.decode().catch(() => undefined))));
    // Each font stack lays text out in its first family the document server has: Chromium measures with that one.
    step.now = "reading the document server's font list";
    const list = await this.fonts.list(editor);
    const has = new Map([...list.faces.values()].map((faces) => [faces.name.toLowerCase(), faces.name]));
    const resolved = Object.fromEntries((checked as { stacks: string[] }).stacks.map((stack) => [stack, resolveFamily(stack, has)]));
    await page.evaluate((families: Record<string, string>) => {
      for (const node of Array.from(document.querySelectorAll<HTMLElement>("section, section *"))) {
        if (node.closest("svg, x-embed")) continue;
        const family = families[getComputedStyle(node).fontFamily];
        if (family) node.style.fontFamily = `"${family}"`;
      }
    }, resolved);
    // Its faces read again, now the resolved families.
    const faces = ((await this.inspect(page, args("faces"))) as { faces: Face[] }).faces;
    step.now = "loading the slide's fonts from the document server";
    const css = await this.fonts.css(editor, list, faces);
    if (css) {
      await page.addStyleTag({ content: css });
      await page.evaluate(() => {
        const loads: Promise<unknown>[] = [];
        document.fonts.forEach((font) => loads.push(font.load()));
        return Promise.all(loads).then(() => true);
      });
    }
    step.now = "measuring the shapes";
    const { spec, icons, embeds, shots } = (await this.inspect(page, args("shapes"))) as Extract<Inspected, { spec: SlideSpec }>;
    step.now = "painting the icons";
    if (shots.length) await paintIcons(page);
    await this.drawPictures(page, canvas, spec, { icons, embeds, shots }, step);
    return spec;
  }

  private async inspect(page: Page, args: InspectArgs): Promise<Inspected> {
    const answer = (await page.evaluate(inspect, args)) as Inspected;
    if ("error" in answer) throw new ContentError(answer.error);
    return answer;
  }

  // Each placeholder picture made what it shows: an svg drawn by Chromium, an icon from the icon set, an embed drawn on
  // a page of its own, and a shot taken in turn.
  private async drawPictures(page: Page, canvas: Canvas, spec: SlideSpec, drawn: Omit<Extract<Inspected, { spec: SlideSpec }>, "spec">, step: Step): Promise<void> {
    const { icons, embeds, shots } = drawn;
    const visit = async (shapes: ShapeSpec[]): Promise<void> => {
      for (const shape of shapes) {
        if (shape.kind === "group") await visit(shape.children);
        if (shape.kind !== "picture" || shape.src.startsWith("data:")) continue;
        const [kind, n] = shape.src.split(":");
        const i = Number(n);
        const where = `at ${Math.round(shape.x / canvas.emuPerPx)},${Math.round(shape.y / canvas.emuPerPx)} px`;
        step.now = `drawing the ${kind === "shot" ? "picture" : kind === "embed" ? "x-embed" : kind} ${where}`;
        if (kind === "svg") shape.src = png(await page.locator(`section svg[data-draw="${i}"]`).screenshot({ type: "png", omitBackground: true }));
        else if (kind === "icon") shape.src = iconSvg(icons[i]!.name, icons[i]!.color, Math.round(icons[i]!.px));
        else if (kind === "embed") shape.src = await drawEmbed(page, embeds[i]!, where);
        else if (kind === "shot") shape.src = await drawShot(page, i, shots[i]!, shape, canvas);
      }
    };
    await visit(spec.shapes);
  }
}

// An embed is the agent's HTML: it is drawn as a static page, in a browser context of its own with JavaScript off and
// every request aborted (a meta refresh would otherwise load any address). Its CSS settles, and it is drawn.
async function drawEmbed(page: Page, embed: { html: string; w: number; h: number }, where: string): Promise<string> {
  const context = await page.context().browser()!.newContext({
    javaScriptEnabled: false, viewport: { width: Math.ceil(embed.w), height: Math.ceil(embed.h) }, deviceScaleFactor: 2,   // the measuring page's
  });
  try {
    await context.route("**/*", (route) => route.abort());
    const own = await context.newPage();
    await own.setContent(`<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:">
<style>html,body{margin:0;background:transparent}</style></head><body>${embed.html}</body></html>`, { timeout: EMBED_TIMEOUT_MS });
    await own.waitForTimeout(EMBED_SETTLE_MS);
    return png(await own.screenshot({ type: "png", omitBackground: true, timeout: EMBED_TIMEOUT_MS }));
  } catch (error) {
    if (error instanceof errors.TimeoutError) {
      throw new ContentError(`The x-embed ${where} did not draw within ${EMBED_TIMEOUT_MS / 1000} s: keep an embed to plain HTML and CSS.`);
    }
    throw error;
  } finally {
    await context.close().catch(() => undefined);
  }
}

const png = (data: Buffer): string => `data:image/png;base64,${data.toString("base64")}`;

// The page paints no icon (they come from the icon set): each is painted in, in its colour and box, so a picture of an
// element holding one shows it.
async function paintIcons(page: Page): Promise<void> {
  const wanted = await page.evaluate(() => {
    for (const marked of Array.from(document.querySelectorAll("[data-paint]"))) marked.removeAttribute("data-paint");
    const icons = Array.from(document.querySelectorAll<HTMLElement>("section x-icon")).filter((el) => !el.parentElement?.closest("svg, x-embed"));
    return icons.map((el, i) => {
      el.setAttribute("data-paint", String(i));
      const rgb = (/(\d+), (\d+), (\d+)/.exec(getComputedStyle(el).color) ?? ["", "0", "0", "0"]).slice(1).map((v) => Number(v).toString(16).padStart(2, "0"));
      return { name: el.getAttribute("name")!, color: `#${rgb.join("")}`, px: el.offsetWidth };
    });
  });
  const srcs = wanted.map((icon) => iconSvg(icon.name, icon.color, Math.max(1, Math.round(icon.px))));
  await page.evaluate((list) => Promise.all(list.map((src, i) => {
    const img = document.createElement("img");
    img.src = src;
    img.style.cssText = "display:block;width:100%;height:100%";
    document.querySelector(`[data-paint="${i}"]`)!.replaceChildren(img);
    return img.decode();
  })), srcs);
}

// A shot: the page as a style laid over it shows the marked element (see Shot), over the picture's own box. Everything
// it changes is put back.
async function drawShot(page: Page, n: number, kind: Shot, shape: { x: number; y: number; w: number; h: number }, canvas: Canvas): Promise<string> {
  const at = `[data-shot~="${n}"]`;
  const alone = "html, body { background: transparent !important; } section, section * { visibility: hidden !important; }";
  const shown = "visibility: visible !important;";
  const blank = "color: transparent !important; -webkit-text-fill-color: transparent !important; -webkit-text-stroke-color: transparent !important;";
  const css: Record<Shot, string> = {
    element: `${alone} ${at}, ${at} * { ${shown} }`,
    box: `${alone} ${at} { ${shown} ${blank} }`,
    text: `${alone} ${at}, ${at} * { ${shown} ${blank} }`,
    shadow: `${alone} [data-shot-copy] { ${shown} }`,
    region: "",
  };
  // A box-shadow alone is a copy of the element's box, transparent, with its shadow, radii, transform and opacity.
  if (kind === "shadow") {
    await page.evaluate((selector) => {
      const el = document.querySelector<HTMLElement>(selector)!, section = document.querySelector("section")!;
      const cs = getComputedStyle(el), saved = el.style.transform;
      el.style.transform = "none";
      const r = el.getBoundingClientRect(), s = section.getBoundingClientRect(), ss = getComputedStyle(section);
      el.style.transform = saved;
      const copy = document.createElement("div");
      copy.setAttribute("data-shot-copy", "");
      Object.assign(copy.style, {
        position: "absolute", boxSizing: "border-box", left: `${r.left - s.left - parseFloat(ss.borderLeftWidth)}px`, top: `${r.top - s.top - parseFloat(ss.borderTopWidth)}px`,
        width: `${r.width}px`, height: `${r.height}px`, boxShadow: cs.boxShadow, transform: cs.transform, opacity: cs.opacity,
        borderTopLeftRadius: cs.borderTopLeftRadius, borderTopRightRadius: cs.borderTopRightRadius, borderBottomRightRadius: cs.borderBottomRightRadius, borderBottomLeftRadius: cs.borderBottomLeftRadius,
      });
      section.appendChild(copy);
    }, at);
  }
  const style = css[kind] ? await page.addStyleTag({ content: css[kind] }) : null;
  try {
    const e = canvas.emuPerPx;
    return png(await page.screenshot({ type: "png", omitBackground: true, clip: { x: shape.x / e, y: shape.y / e, width: shape.w / e, height: shape.h / e } }));
  } finally {
    await style?.evaluate((node) => (node as Element).remove());
    if (kind === "shadow") await page.evaluate(() => document.querySelector("[data-shot-copy]")?.remove());
  }
}

// The page a slide is measured in. No script of the content runs and nothing it names is fetched (its CSP; the
// context also aborts every request); an x-embed's own content is hidden here and runs on a page of its own. Text is
// laid out as the editor lays it out: no kerning, no ligatures, precise advances, 1.2 lines as single spacing. Tags
// start with Claude Slides' defaults (format.md): a section or div with no display is a column, and one whose style
// sets display takes CSS's own direction (display:flex is a row); h1–h3 and p sizes, weights and line heights; cell
// padding 0.35em 0.6em. A connector given points is placed by them, not laid out, and its border only says how its line
// is drawn.
function documentOf(html: string, canvas: Canvas): string {
  return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:">
<style>
html, body { margin: 0; padding: 0; background: #ffffff; }
* { box-sizing: border-box; margin: 0; font-kerning: none; font-variant-ligatures: none; text-rendering: geometricPrecision; }
section { position: relative; overflow: hidden; display: flex; flex-direction: column; width: ${canvas.width}px !important;
  height: ${canvas.height}px !important; font-family: Arial; font-size: 32px; line-height: 1.4; color: #000000; }
section[hidden] { display: flex !important; }
div { display: flex; flex-direction: column; }
:is(section, div):is([style*="display:"], [style*="display :"]) { flex-direction: row; }
h1 { font-size: 96px; font-weight: 600; line-height: 1.1; } h2 { font-size: 64px; font-weight: 600; line-height: 1.15; }
h3 { font-size: 44px; font-weight: 600; line-height: 1.2; } p { font-size: 32px; font-weight: 400; line-height: 1.4; }
td, th { padding: 0.35em 0.6em; } table { border-collapse: collapse; }
ul, ol { padding-left: 1.2em; }
aside, x-embed > * { display: none; }
x-icon, x-shape, x-connector, x-embed { display: block; }
x-connector { border-color: transparent !important; } x-connector[x1] { position: absolute; }
</style></head><body>${html}</body></html>`;
}
