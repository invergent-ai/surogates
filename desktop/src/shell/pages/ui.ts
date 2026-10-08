/// <reference lib="dom" />
/// <reference lib="dom.iterable" />
// What the desktop's own pages share: the theme mark, and their icons.
//
// Icons ported from Lucide (lucide-static 0.544.0), ISC License, Copyright (c) Lucide Contributors 2022.
// Each icon is its SVG elements as data, built with createElementNS: no markup is parsed.

type Shape = [tag: string, attributes: Record<string, string>];

export const ICONS = {
  menu: [["path", { d: "M4 5h16" }], ["path", { d: "M4 12h16" }], ["path", { d: "M4 19h16" }]],
  "panel-left": [["rect", { width: "18", height: "18", x: "3", y: "3", rx: "2" }], ["path", { d: "M9 3v18" }]],
  "panel-right": [["rect", { width: "18", height: "18", x: "3", y: "3", rx: "2" }], ["path", { d: "M15 3v18" }]],
  "arrow-left": [["path", { d: "m12 19-7-7 7-7" }], ["path", { d: "M19 12H5" }]],
  "arrow-right": [["path", { d: "M5 12h14" }], ["path", { d: "m12 5 7 7-7 7" }]],
  search: [["path", { d: "m21 21-4.34-4.34" }], ["circle", { cx: "11", cy: "11", r: "8" }]],
  "square-pen": [["path", { d: "M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" }], ["path", { d: "M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z" }]],
  "layout-grid": [["rect", { width: "7", height: "7", x: "3", y: "3", rx: "1" }], ["rect", { width: "7", height: "7", x: "14", y: "3", rx: "1" }], ["rect", { width: "7", height: "7", x: "14", y: "14", rx: "1" }], ["rect", { width: "7", height: "7", x: "3", y: "14", rx: "1" }]],
  inbox: [["polyline", { points: "22 12 16 12 14 15 10 15 8 12 2 12" }], ["path", { d: "M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" }]],
  target: [["circle", { cx: "12", cy: "12", r: "10" }], ["circle", { cx: "12", cy: "12", r: "6" }], ["circle", { cx: "12", cy: "12", r: "2" }]],
  sparkles: [["path", { d: "M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z" }], ["path", { d: "M20 2v4" }], ["path", { d: "M22 4h-4" }], ["circle", { cx: "4", cy: "20", r: "2" }]],
  folder: [["path", { d: "M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" }]],
  bot: [["path", { d: "M12 8V4H8" }], ["rect", { width: "16", height: "12", x: "4", y: "8", rx: "2" }], ["path", { d: "M2 14h2" }], ["path", { d: "M20 14h2" }], ["path", { d: "M15 13v2" }], ["path", { d: "M9 13v2" }]],
  laptop: [["path", { d: "M18 5a2 2 0 0 1 2 2v8.526a2 2 0 0 0 .212.897l1.068 2.127a1 1 0 0 1-.9 1.45H3.62a1 1 0 0 1-.9-1.45l1.068-2.127A2 2 0 0 0 4 15.526V7a2 2 0 0 1 2-2z" }], ["path", { d: "M20.054 15.987H3.946" }]],
  settings: [["path", { d: "M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915" }], ["circle", { cx: "12", cy: "12", r: "3" }]],
  "ellipsis-vertical": [["circle", { cx: "12", cy: "12", r: "1" }], ["circle", { cx: "12", cy: "5", r: "1" }], ["circle", { cx: "12", cy: "19", r: "1" }]],
  "messages-square": [["path", { d: "M16 10a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 14.286V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" }], ["path", { d: "M20 9a2 2 0 0 1 2 2v10.286a.71.71 0 0 1-1.212.502l-2.202-2.202A2 2 0 0 0 17.172 19H10a2 2 0 0 1-2-2v-1" }]],
  library: [["path", { d: "m16 6 4 14" }], ["path", { d: "M12 6v14" }], ["path", { d: "M8 8v12" }], ["path", { d: "M4 4v16" }]],
  clock: [["path", { d: "M12 6v6l4 2" }], ["circle", { cx: "12", cy: "12", r: "10" }]],
  x: [["path", { d: "M18 6 6 18" }], ["path", { d: "m6 6 12 12" }]],
  "chevron-down": [["path", { d: "m6 9 6 6 6-6" }]],
  "chevron-up": [["path", { d: "m18 15-6-6-6 6" }]],
  monitor: [["rect", { width: "20", height: "14", x: "2", y: "3", rx: "2" }], ["line", { 'x1': "8", 'x2': "16", 'y1': "21", 'y2': "21" }], ["line", { 'x1': "12", 'x2': "12", 'y1': "17", 'y2': "21" }]],
  sun: [["circle", { cx: "12", cy: "12", r: "4" }], ["path", { d: "M12 2v2" }], ["path", { d: "M12 20v2" }], ["path", { d: "m4.93 4.93 1.41 1.41" }], ["path", { d: "m17.66 17.66 1.41 1.41" }], ["path", { d: "M2 12h2" }], ["path", { d: "M20 12h2" }], ["path", { d: "m6.34 17.66-1.41 1.41" }], ["path", { d: "m19.07 4.93-1.41 1.41" }]],
  moon: [["path", { d: "M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401" }]],
  "circle-user-round": [["path", { d: "M18 20a6 6 0 0 0-12 0" }], ["circle", { cx: "12", cy: "10", r: "4" }], ["circle", { cx: "12", cy: "12", r: "10" }]],
  "folder-lock": [["rect", { width: "8", height: "5", x: "14", y: "17", rx: "1" }], ["path", { d: "M10 20H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H20a2 2 0 0 1 2 2v2.5" }], ["path", { d: "M20 17v-2a2 2 0 1 0-4 0v2" }]],
  gauge: [["path", { d: "m12 14 4-4" }], ["path", { d: "M3.34 19a10 10 0 1 1 17.32 0" }]],
  languages: [["path", { d: "m5 8 6 6" }], ["path", { d: "m4 14 6-6 2-3" }], ["path", { d: "M2 5h12" }], ["path", { d: "M7 2h1" }], ["path", { d: "m22 22-5-10-5 10" }], ["path", { d: "M14 18h6" }]],
  "circle-help": [["circle", { cx: "12", cy: "12", r: "10" }], ["path", { d: "M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" }], ["path", { d: "M12 17h.01" }]],
  "credit-card": [["rect", { width: "20", height: "14", x: "2", y: "5", rx: "2" }], ["line", { 'x1': "2", 'x2': "22", 'y1': "10", 'y2': "10" }]],
  "external-link": [["path", { d: "M15 3h6v6" }], ["path", { d: "M10 14 21 3" }], ["path", { d: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" }]],
  "log-out": [["path", { d: "m16 17 5-5-5-5" }], ["path", { d: "M21 12H9" }], ["path", { d: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" }]],
  "refresh-cw": [["path", { d: "M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" }], ["path", { d: "M21 3v5h-5" }], ["path", { d: "M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" }], ["path", { d: "M8 16H3v5" }]],
  plug: [["path", { d: "M12 22v-5" }], ["path", { d: "M9 8V2" }], ["path", { d: "M15 8V2" }], ["path", { d: "M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z" }]],
} satisfies Record<string, Shape[]>;

export type IconName = keyof typeof ICONS;

const SVG = "http://www.w3.org/2000/svg";

export function icon(name: IconName, size = 16): SVGSVGElement {
  const svg = document.createElementNS(SVG, "svg");
  for (const [key, value] of Object.entries({
    width: String(size), height: String(size), viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "1.5", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true",
  })) svg.setAttribute(key, value);
  for (const [tag, attributes] of ICONS[name] as Shape[]) {
    const shape = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attributes)) shape.setAttribute(key, value);
    svg.append(shape);
  }
  return svg;
}

// Surogate's mark (web/public/favicon.svg): its disc, and the figure on it cut to the disc. The
// registered sign is left out at this size.
const FIGURE = "M49.71,46.83c0,.23.02.45.07.67,2.1.49,4.16,1.07,6.18,1.74.58-.64.94-1.48.94-2.42,0-1.98-1.61-3.59-3.59-3.59s-3.59,1.61-3.59,3.59M33.95,78.3c3.1-5.1,6.72-8.89,11.64-12.21.61-.42,1.24-.8,1.88-1.17.44-.25.61-.66.58-1.05,0-.53-.49-.91-.93-1.12-1.34-.62-2.72-1.5-4.06-2.54-1.57-1.22-3.01-2.59-4.36-4.06-.95-1.04-2.02-2.38-3-3.81-.81-1.18-.63-2.77.43-3.73,1.1-1,2.26-1.92,3.48-2.76.36-.25.72-.49,1.1-.73,2.43-1.55,5.05-2.79,7.79-3.7,3.23-1.07,6.62-1.66,10.03-1.74.59-.35,1.19-.69,1.8-1.01,1.66-.88,3.38-1.62,5.17-2.21,1.58-.54,3.2-.95,4.85-1.24,3.28-.59,6.64-.7,9.95-.32,1.23.14,2.45.34,3.68.62,2.87.64,5.59,1.62,8.09,2.9.54.27.71.95.36,1.45-1.58,2.22-3.46,4.25-5.5,6.05-3.01,2.65-6.51,4.75-10.26,6.17-2.32.88-4.75,1.53-7.22,1.91-.62.1-.78.9-.24,1.22,0,0,.01,0,.02.01,6.97,4.05,13.1,9.22,18.36,15.32.72.83,1.42,1.68,2.1,2.53,3.36,4.22,6.23,8.82,8.56,13.68,1.8,3.73,3.27,7.63,4.39,11.62.73,2.6,1.34,5.24,1.76,7.91.11.76-.87,1.16-1.3.53-1.15-1.69-2.31-3.35-3.61-4.93-1.2-1.46-2.46-2.86-3.75-4.24-5.44-5.8-11.84-10.7-18.84-14.48-7.32-3.94-15.09-6.38-23.44-6.53-2.23-.04-4.46.08-6.68.3-4.05.43-8.02,1.28-11.93,2.4-.03,0-.05.01-.08.02,0,0-.01,0-.02,0-.07.02-.14.03-.21.03-.53,0-.91-.6-.61-1.09M50.95,12.97c-1.31,1.16-2.2,2.71-2.9,4.29-1.66,3.74-2.85,7.74-3.52,11.77-.32,1.92-.56,3.87-1.28,5.69-.17.44-.37.87-.6,1.28-1.18,2.1-3.37,3.19-5.6,3.86-4.96,1.47-10.14,1.97-15.25,2.65-2.37.31-8.03,1.12-7.34,4.69.46,2.36,2.26,4.16,4.08,5.6,2.66,2.11,5.44,4.09,8.33,5.88,1.87,1.15,3.85,2.11,5.6,3.44,1.74,1.33,3.15,3.05,3.58,5.25.22,1.16-.18,2.35-1,3.2-1.57,1.63-3,3.41-4.25,5.29-1.32,2.1-2.38,4.35-3.25,6.65-.59,1.44.97,2.98,2.41,2.37.53-.21,1.07-.41,1.61-.61,2.26-.82,4.68.73,4.93,3.12.17,1.61.38,3.17.63,4.57.46,2.57,3.14,3.63,5.32,1.41,2.84-2.88,4.66-6.31,7.84-8.86,2.53-2.03,6.26-4.17,9.71-3.59,3.17.56,6.27,1.52,9.23,2.85,5.32,2.32,10.36,5.29,14.95,8.86,9.17,7.1,16.56,16.51,21.27,27.1.93,2.07,4.03,1.44,4.1-.83.55-17.71-5.72-35.46-17.11-49.01-2.87-3.42-6.07-6.65-9.59-9.44-.88-.8-1.54-1.7-1.38-2.93.38-2.97,2-4.32,4.04-5.85,2.51-1.89,5.02-3.85,7.16-6.21.03-.03.06-.06.09-.1,1.24-1.37,2.38-2.83,3.4-4.37,1.2-1.81.6-4.26-1.3-5.31,0,0-.96-.52-1.45-.76-2.72-1.34-5.61-2.36-8.6-3.03-2.76-.62-5.58-.93-8.38-.93-2.38,0-4.76.22-7.09.67-1.96.37-3.97-.31-5.25-1.84-.28-.34-.54-.69-.79-1.04-.58-.84-1.09-1.71-1.63-2.58-1.29-2.06-1.87-3.63-2.89-5.84-1.06-2.29-1.85-4.73-3.26-6.86-.73-1.11-1.53-1.54-2.32-1.54s-1.54.41-2.24,1.02";

/** Surogate's mark, *size* pixels wide, drawn as data as the icons are: no markup is parsed, and no image loaded. */
export function mark(size = 64): SVGSVGElement {
  const svg = document.createElementNS(SVG, "svg");
  for (const [key, value] of Object.entries({ width: String(size), height: String(size), viewBox: "0 0 113 113", "aria-hidden": "true" })) {
    svg.setAttribute(key, value);
  }
  const disc = (): SVGCircleElement => {
    const circle = document.createElementNS(SVG, "circle");
    for (const [key, value] of Object.entries({ cx: "56.5", cy: "56.5", r: "56.5" })) circle.setAttribute(key, value);
    return circle;
  };
  const clip = document.createElementNS(SVG, "clipPath");
  clip.setAttribute("id", "surogate-mark");
  clip.append(disc());
  const shown = disc();
  shown.setAttribute("fill", "#ffaf10");
  const figure = document.createElementNS(SVG, "path");
  for (const [key, value] of Object.entries({ d: FIGURE, fill: "#2a102d", "clip-path": "url(#surogate-mark)" })) figure.setAttribute(key, value);
  svg.append(clip, shown, figure);
  return svg;
}

// Every element that names an icon gets it, ahead of its text.
export function fillIcons(root: ParentNode = document): void {
  for (const element of root.querySelectorAll<HTMLElement>("[data-icon]")) element.prepend(icon(element.dataset.icon as IconName));
}

// The theme in effect, as the main process set it for every page (nativeTheme.themeSource):
// the CSS reads data-theme, and the first frame already follows prefers-color-scheme.
export function markTheme(): void {
  const dark = matchMedia("(prefers-color-scheme: dark)");
  const mark = () => {
    document.documentElement.dataset.theme = dark.matches ? "dark" : "light";
  };
  mark();
  dark.addEventListener("change", mark);
}

export const byId = <T extends HTMLElement = HTMLElement>(id: string): T => document.getElementById(id) as T;

const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

/** How long ago *when* was: "17m" in a row, or "17 minutes ago" on a card. A time ahead of *now* is now. */
export function ago(when: string, now = Date.now(), form: "short" | "long" = "short"): string {
  const minutes = Math.floor((now - Date.parse(when)) / 60_000);
  if (minutes < 1) return form === "short" ? "now" : "just now";
  const [count, unit]: [number, Intl.RelativeTimeFormatUnit] = minutes < 60 ? [minutes, "minute"]
    : minutes < 24 * 60 ? [Math.floor(minutes / 60), "hour"] : [Math.floor(minutes / (24 * 60)), "day"];
  return form === "short" ? `${count}${unit[0]}` : RELATIVE.format(-count, unit);
}

// What a prompt shows as its code point rather than as itself: controls and format characters (the
// bidi controls among them), private-use, unassigned and lone surrogate code points, line and
// paragraph separators, what draws nothing (the blank symbols among it), and every space but U+0020.
const SPECIAL = /[\p{C}\p{Zl}\p{Zp}\p{Default_Ignorable_Code_Point}\u2800\uFFFC\u{1D159}]|(?! )\p{Zs}/gu;

// Three or more line breaks with nothing but spaces and tabs between them: blank lines, which
// would push what follows them out of view.
const BLANK_LINES = /\n(?:[ \t]*\n){2,}/g;

type Run = { text: string; special: boolean };

/**
 * *text* in runs: plain text, and each special character as its code point (U+202E). *keep*
 * holds the special characters shown as themselves. Where it keeps newlines, blank lines
 * show as one mark of how many line breaks they are (U+000A ×40), then one newline.
 */
export function segments(text: string, keep = ""): Run[] {
  const runs: Run[] = [];
  let last = 0;
  if (keep.includes("\n")) {
    for (const match of text.matchAll(BLANK_LINES)) {
      marked(text.slice(last, match.index), keep, runs);
      runs.push({ text: `U+000A ×${match[0].split("\n").length - 1}`, special: true }, { text: "\n", special: false });
      last = match.index + match[0].length;
    }
  }
  marked(text.slice(last), keep, runs);
  return runs;
}

// *text*'s runs onto *runs*, one push each: a text of any number of runs.
function marked(text: string, keep: string, runs: Run[]): void {
  let last = 0;
  for (const match of text.matchAll(SPECIAL)) {
    const [found] = match;
    if (keep.includes(found)) continue;
    if (match.index > last) runs.push({ text: text.slice(last, match.index), special: false });
    runs.push({ text: `U+${found.codePointAt(0)!.toString(16).toUpperCase().padStart(4, "0")}`, special: true });
    last = match.index + found.length;
  }
  if (last < text.length) runs.push({ text: text.slice(last), special: false });
}

/** *text* as showText shows it, in one string: what names it for a screen reader, or in a native box. */
export const asShown = (text: string, keep = ""): string => segments(text, keep).map((run) => run.text).join("");

/** Set *element*'s text to *text*, whole, with each special character marked as its code point: never markup. */
export function showText(element: HTMLElement, text: string, keep = ""): void {
  // Built apart and set at once: a text of any number of runs, where one argument per run would overflow the stack.
  const runs = document.createDocumentFragment();
  for (const { text: run, special } of segments(text, keep)) {
    if (!special) {
      runs.append(run);
      continue;
    }
    const mark = document.createElement("span");
    mark.className = "special";
    mark.textContent = run;
    runs.append(mark);
  }
  element.replaceChildren(runs);
}
