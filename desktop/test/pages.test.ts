// The app's own pages, as their files declare them.

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const PAGES = join(import.meta.dirname, "..", "src", "shell", "pages");
// A page loads only its own files, sets no base address and sends no form anywhere.
const POLICY = "default-src 'none'; style-src 'self'; script-src 'self'; base-uri 'none'; form-action 'none'";

describe("the app's own pages", () => {
  it.each(readdirSync(PAGES).filter((name) => name.endsWith(".html")))("%s declares the one policy, and no other", (name) => {
    const html = readFileSync(join(PAGES, name), "utf8");
    const declared = [...html.matchAll(/<meta http-equiv="Content-Security-Policy" content="([^"]*)"/g)].map((found) => found[1]);
    expect(declared).toEqual([POLICY]);
  });
});
