// How a folder is looked at apart on Linux: coreutils' stat, by its whole path, following links as
// the checks do, printing nothing, with the folder after the end of its options.

import { expect, it } from "vitest";

import { lookCommand } from "../src/shell/folders-held-linux.js";

it("looks with coreutils' stat, following links and printing nothing, at a folder whatever its name begins with", () => {
  expect(lookCommand("/home/ana/-notes")).toEqual(["/usr/bin/stat", ["-L", "--printf=", "--", "/home/ana/-notes"]]);
});
