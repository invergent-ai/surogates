// The browser's raw operations (spec, Section 5; their contract is the module docstring of
// surogates/devices/browser.py), each on one page, with what the server sent taken as data.
// No code from the server runs here but a page's own JavaScript, in the page (browser.evaluate).

import type { Page } from "playwright-core";

import { observe } from "./observe.js";

export type PageOperation = (page: Page, args: Record<string, unknown>) => Promise<unknown>;

const NAVIGATION_MS = 60_000;
const WAITS = new Set(["load", "domcontentloaded", "networkidle"]);
const BUTTONS = new Set(["left", "right", "middle"]);
const MAX_PATH = 1_000;
const MAX_KEYS = 200;

type Button = "left" | "right" | "middle";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function whole(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) throw new Error(`${name} must be a whole number`);
  return value;
}

function text(value: unknown, name: string): string {
  if (typeof value !== "string") throw new Error(`${name} must be text`);
  return value;
}

function buttonOf(value: unknown): Button {
  if (value === undefined) return "left";
  if (typeof value !== "string" || !BUTTONS.has(value)) throw new Error("button is left, right or middle");
  return value as Button;
}

async function navigate(page: Page, args: Record<string, unknown>): Promise<unknown> {
  let url: URL;
  try {
    url = new URL(text(args.url, "url"));
  } catch {
    throw new Error("Not an address the agent's browser can open");
  }
  // goto opens what route interception would not stop: chrome://, edge://, view-source: and file:.
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("The agent's browser opens only http and https addresses");
  const waitUntil = typeof args.wait_until === "string" && WAITS.has(args.wait_until) ? args.wait_until : "load";
  await page.goto(url.href, { waitUntil: waitUntil as "load", timeout: NAVIGATION_MS });
  return { url: page.url(), title: await page.title() };
}

// The page's own JavaScript, run in the page as the cloud runs it: a function body, awaited.
// Its value goes under value, so nothing a page returns is ever read as the link's own framing.
async function evaluate(page: Page, args: Record<string, unknown>): Promise<unknown> {
  return { value: (await page.evaluate(`(async () => {\n${text(args.code, "code")}\n})()`)) ?? null };
}

// As the cloud's click: a moment after it, and the network's quiet if it sent a request.
async function settled(page: Page, act: () => Promise<void>): Promise<void> {
  let sent = false;
  const request = () => {
    sent = true;
  };
  page.on("request", request);
  try {
    await act();
    await page.waitForTimeout(150);
    if (sent) await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {});
  } finally {
    page.off("request", request);
  }
}

async function mouse(page: Page, args: Record<string, unknown>): Promise<unknown> {
  const { action } = args;
  if (action === "drag") {
    const path = args.path;
    if (!Array.isArray(path) || path.length < 2 || path.length > MAX_PATH) throw new Error(`A drag's path has 2 to ${MAX_PATH} points`);
    const points = path.map((point) => {
      if (!Array.isArray(point) || point.length !== 2) throw new Error("A point is [x, y]");
      return [whole(point[0], "x"), whole(point[1], "y")] as const;
    });
    const button = buttonOf(args.button);
    const [first, ...rest] = points;
    await page.mouse.move(first![0], first![1]);
    await page.mouse.down({ button });
    for (const [x, y] of rest) await page.mouse.move(x, y);
    await page.mouse.up({ button });
    return {};
  }
  const x = whole(args.x, "x");
  const y = whole(args.y, "y");
  if (action === "wheel") {
    await page.mouse.move(x, y);
    await page.mouse.wheel(whole(args.delta_x ?? 0, "delta_x"), whole(args.delta_y ?? 0, "delta_y"));
    await page.waitForTimeout(150);
    return page.evaluate(() => ({
      scroll_x: Math.round(window.scrollX),
      scroll_y: Math.round(window.scrollY),
      page_height: Math.round(document.documentElement.scrollHeight),
      viewport_height: Math.round(window.innerHeight),
    }));
  }
  if (action === "move") {
    await page.mouse.move(x, y);
    return {};
  }
  const button = buttonOf(args.button);
  if (action === "click") {
    const clickCount = whole(args.clicks ?? 1, "clicks");
    if (clickCount < 1 || clickCount > 3) throw new Error("clicks is 1, 2 or 3");
    await settled(page, () => page.mouse.click(x, y, { button, clickCount }));
    return {};
  }
  if (action === "down" || action === "up") {
    await page.mouse.move(x, y);
    await page.mouse[action]({ button });
    return {};
  }
  throw new Error(`No mouse action ${JSON.stringify(String(action))}`);
}

async function keyboard(page: Page, args: Record<string, unknown>): Promise<unknown> {
  const delay = whole(args.delay ?? 0, "delay");
  const options = delay > 0 ? { delay: Math.min(delay, 1_000) } : {};
  if (args.action === "type") {
    const typed = text(args.text, "text");
    const { at } = args;
    if (at !== null && at !== undefined) {
      if (!isRecord(at)) throw new Error("at is {x, y}");
      await settled(page, () => page.mouse.click(whole(at.x, "x"), whole(at.y, "y")));
      // Typed once the click has given something the focus, not into whatever had it before.
      await page.waitForFunction(() => document.activeElement !== null && document.activeElement !== document.body, undefined, { timeout: 2_000 })
        .catch(() => {});
    }
    await page.keyboard.type(typed, options);
    return {};
  }
  if (args.action === "press") {
    const keys = text(args.keys, "keys");
    if (keys === "" || keys.length > MAX_KEYS) throw new Error("keys is one chord");
    await page.keyboard.press(keys, options);
    return {};
  }
  throw new Error(`No keyboard action ${JSON.stringify(String(args.action))}`);
}

// The labels drawn over the page for its shot, as the cloud draws them.
function draw(items: Array<{ label: number; x: number; y: number }>): void {
  document.getElementById("surogates-overlay")?.remove();
  const canvas = document.createElement("canvas");
  canvas.id = "surogates-overlay";
  canvas.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647";
  canvas.width = window.innerWidth;
  canvas.height = window.innerHeight;
  document.documentElement.appendChild(canvas);
  const g = canvas.getContext("2d");
  if (!g) return;
  g.font = "bold 14px sans-serif";
  for (const item of items) {
    g.fillStyle = "rgba(255,215,0,0.9)";
    g.fillRect(item.x - 12, item.y - 10, 24, 20);
    g.fillStyle = "black";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(String(item.label), item.x, item.y);
  }
}

function undraw(): void {
  document.getElementById("surogates-overlay")?.remove();
}

async function screenshot(page: Page, args: Record<string, unknown>): Promise<unknown> {
  const labels = (Array.isArray(args.labels) ? args.labels : []).map((item) => {
    if (!isRecord(item)) throw new Error("A label is {label, x, y}");
    return { label: whole(item.label, "label"), x: whole(item.x, "x"), y: whole(item.y, "y") };
  });
  const clip = args.clip;
  const options = isRecord(clip)
    ? { clip: { x: whole(clip.x, "x"), y: whole(clip.y, "y"), width: whole(clip.width, "width"), height: whole(clip.height, "height") } }
    : {};
  if (labels.length > 0) await page.evaluate(draw, labels);
  try {
    return (await page.screenshot(options)).toString("base64");
  } finally {
    if (labels.length > 0) await page.evaluate(undraw).catch(() => {});
  }
}

export const OPERATIONS: Record<string, PageOperation> = {
  "browser.navigate": navigate,
  "browser.observe": observe,
  "browser.evaluate": evaluate,
  "browser.mouse": mouse,
  "browser.keyboard": keyboard,
  "browser.screenshot": screenshot,
};
