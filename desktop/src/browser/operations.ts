// The browser's raw operations (spec, Section 5; their contract is the module docstring of
// surogates/devices/browser.py), each on one page, with what the server sent taken as data.
// No code from the server runs here but a page's own JavaScript, in the page (browser.evaluate).

import type { Page } from "playwright-core";

export type PageOperation = (page: Page, args: Record<string, unknown>) => Promise<unknown>;

const NAVIGATION_MS = 60_000;
const WAITS = new Set(["load", "domcontentloaded", "networkidle"]);

function text(value: unknown, name: string): string {
  if (typeof value !== "string") throw new Error(`${name} must be text`);
  return value;
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

export const OPERATIONS: Record<string, PageOperation> = {
  "browser.navigate": navigate,
  "browser.evaluate": evaluate,
};
