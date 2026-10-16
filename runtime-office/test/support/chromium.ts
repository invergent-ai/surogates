import { existsSync } from "node:fs";
import { chromium, type Page } from "playwright-core";
import type { FontSource } from "../../src/slides/measure.js";

/** Whether Playwright's Chromium is installed: the measuring tests skip without it. */
export const chromiumThere = (() => {
  try {
    return existsSync(chromium.executablePath());
  } catch {
    return false;
  }
})();

// Fonts the workstation's Chromium has by name stand in for the document server's here: no file is loaded.
export const systemFonts: FontSource = {
  list: async () => ({
    base: "",
    faces: new Map(["Arial", "DejaVu Sans", "Georgia", "Times New Roman", "Courier New"].map((name) => [name.toLowerCase(), { name, regular: "x", italic: null, bold: null, boldItalic: null }])),
  }),
  css: async () => "",
};

/** A page like runtime-office's measuring page: 1920 by 1080 at device scale 2, every request aborted. */
export async function measuringPage(): Promise<{ page: Page; close: () => Promise<void> }> {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 2 });
  await context.route("**/*", (route) => route.abort());
  return { page: await context.newPage(), close: () => browser.close() };
}
