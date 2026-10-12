// How a folder is looked at apart on macOS: BSD's stat, following links, printing nothing of it.

import { mkdtempSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { lookCommand } from "../src/shell/folders-held-darwin.js";
import { spawnsCleanly } from "./clean-spawns.js";

it("is BSD's stat of the folder, through its links, with a format that prints nothing of it", () => {
  expect(lookCommand("/Users/ana/-Documents")).toEqual(["/usr/bin/stat", ["-L", "-f", "", "--", "/Users/ana/-Documents"]]);
});

// stat started as the app starts it: waits for macOS's clean start of a program (clean-child.ts).
describe.runIf(process.platform === "darwin" && spawnsCleanly)("a look at a folder on a Mac", () => {
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
