import type { Page } from "playwright-core";
import type { EditorPage } from "../connector.js";
import { ContentError } from "../markdown.js";
import { Fonts, resolveFamily, type Face, type FontList } from "./fonts.js";
import { ICON_NAMES, iconSvg } from "./icons.js";
import { inspect, type InspectArgs, type Inspected } from "./inspect.js";

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
export interface SlideSpec { background: Fill | null; shapes: ShapeSpec[]; notes: string; hidden: boolean; transition: Transition | null }

/** Where a slide's fonts come from: the document server's, in runtime-office. */
export interface FontSource {
  list(editor: EditorPage): Promise<FontList>;
  css(editor: EditorPage, list: FontList, wanted: Face[]): Promise<string>;
}

// Counted without picture data: the worker inlines every workspace picture as a data URL.
const MAX_HTML = 200_000;
export const MIN_TEXT_PX = 24;
const EMBED_SETTLE_MS = 800;

export class Measurer {
  private queue: Promise<unknown> = Promise.resolve();

  constructor(private readonly page: () => Promise<Page>, private readonly fonts: FontSource = new Fonts()) {}

  /** *html*, one slide, checked and measured on *canvas* with the fonts of the document server *editor* loaded from. */
  measure(html: string, canvas: Canvas, editor: EditorPage): Promise<SlideSpec> {
    const run = this.queue.then(() => this.now(html, canvas, editor), () => this.now(html, canvas, editor));
    this.queue = run.catch(() => undefined);
    return run;
  }

  private async now(html: string, canvas: Canvas, editor: EditorPage): Promise<SlideSpec> {
    if (html.replace(/data:[^"')\s]+/g, "data:").length > MAX_HTML) throw new ContentError(`A slide's HTML is at most ${MAX_HTML} characters.`);
    const page = await this.page();
    await page.setViewportSize({ width: canvas.width, height: canvas.height });
    await page.setContent(documentOf(html, canvas));
    const args = (phase: InspectArgs["phase"]): InspectArgs => ({ phase, canvas, minPx: MIN_TEXT_PX, icons: ICON_NAMES });
    const checked = await this.inspect(page, args("check"));
    // Each font stack lays text out in its first family the document server has: Chromium measures with that one.
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
    // Checked again, its faces now the resolved families.
    const faces = ((await this.inspect(page, args("check"))) as { faces: Face[] }).faces;
    const css = await this.fonts.css(editor, list, faces);
    if (css) {
      await page.addStyleTag({ content: css });
      await page.evaluate(() => {
        const loads: Promise<unknown>[] = [];
        document.fonts.forEach((font) => loads.push(font.load()));
        return Promise.all(loads).then(() => true);
      });
    }
    const { spec, icons, embeds } = (await this.inspect(page, args("shapes"))) as Extract<Inspected, { spec: SlideSpec }>;
    await this.drawPictures(page, spec, icons, embeds);
    return spec;
  }

  private async inspect(page: Page, args: InspectArgs): Promise<Inspected> {
    const answer = (await page.evaluate(inspect, args)) as Inspected;
    if ("error" in answer) throw new ContentError(answer.error);
    return answer;
  }

  // Each placeholder picture made what it shows: an svg drawn by Chromium, an icon from the icon set, an embed run on a
  // page of its own (scripts on, network off, as Claude Slides' sandboxed iframe) and drawn.
  private async drawPictures(page: Page, spec: SlideSpec, icons: { name: string; color: string; px: number }[], embeds: { html: string; w: number; h: number }[]): Promise<void> {
    const svgs = page.locator("section svg:not(svg svg)");
    const visit = async (shapes: ShapeSpec[]): Promise<void> => {
      for (const shape of shapes) {
        if (shape.kind === "group") await visit(shape.children);
        if (shape.kind !== "picture" || shape.src.startsWith("data:")) continue;
        const [kind, n] = shape.src.split(":");
        const i = Number(n);
        if (kind === "svg") shape.src = `data:image/png;base64,${(await svgs.nth(i).screenshot({ type: "png", omitBackground: true })).toString("base64")}`;
        else if (kind === "icon") shape.src = iconSvg(icons[i]!.name, icons[i]!.color, Math.round(icons[i]!.px));
        else if (kind === "embed") shape.src = await this.drawEmbed(page, embeds[i]!);
      }
    };
    await visit(spec.shapes);
  }

  private async drawEmbed(page: Page, embed: { html: string; w: number; h: number }): Promise<string> {
    const own = await page.context().newPage();
    try {
      await own.setViewportSize({ width: Math.ceil(embed.w), height: Math.ceil(embed.h) });
      await own.setContent(`<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:">
<style>html,body{margin:0;background:transparent}</style></head><body>${embed.html}</body></html>`);
      await own.waitForTimeout(EMBED_SETTLE_MS);
      return `data:image/png;base64,${(await own.screenshot({ type: "png", omitBackground: true })).toString("base64")}`;
    } finally {
      await own.close();
    }
  }
}

// The page a slide is measured in. No script of the content runs and nothing it names is fetched (its CSP; the
// context also aborts every request); an x-embed's own content is hidden here and runs on a page of its own. Text is
// laid out as the editor lays it out: no kerning, no ligatures, precise advances, 1.2 lines as single spacing. Tags
// start with Claude Slides' defaults (format.md): a section or div with no display is a column; h1–h3 and p sizes,
// weights and line heights; cell padding 0.35em 0.6em.
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
h1 { font-size: 96px; font-weight: 600; line-height: 1.1; } h2 { font-size: 64px; font-weight: 600; line-height: 1.15; }
h3 { font-size: 44px; font-weight: 600; line-height: 1.2; } p { font-size: 32px; font-weight: 400; line-height: 1.4; }
td, th { padding: 0.35em 0.6em; } table { border-collapse: collapse; }
ul, ol { padding-left: 1.2em; }
aside, x-embed > * { display: none; }
x-icon, x-shape, x-connector, x-embed { display: block; }
</style></head><body>${html}</body></html>`;
}
