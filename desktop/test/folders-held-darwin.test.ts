// How a folder is looked at apart on macOS: BSD's test, following links, printing nothing.

import { mkdtempSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { lookCommand } from "../src/shell/folders-held-darwin.js";

it("is BSD's test of whether the folder is there, through its links, which prints nothing", () => {
  expect(lookCommand("/Users/ana/-Documents")).toEqual(["/bin/test", ["-e", "/Users/ana/-Documents"]]);
});

// test started as the app starts it (clean-child.ts).
describe.runIf(process.platform === "darwin")("a look at a folder on a Mac", () => {
  it("finds a folder there, through a link, and none where it is not", async () => {
    const { lookApart } = await import("../src/shell/folders-held.js");
    const dir = mkdtempSync(join(tmpdir(), "looks-"));
    try {
      symlinkSync(dir, join(dir, "link"));
      symlinkSync(join(dir, "gone"), join(dir, "dangling"));
      expect(await Promise.all([dir, join(dir, "link"), join(dir, "gone"), join(dir, "dangling")].map((folder) => lookApart(folder))))
        .toEqual(["there", "there", "gone", "gone"]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
