// How a folder is looked at apart on Windows: surogate-helper.exe's look, which opens it through its links and prints
// nothing; and the app's PATH as Windows writes one, its entries apart by ";" and each on a drive.

import { expect, it } from "vitest";

import { HELPER } from "../src/native-helper.js";
import { lookCommand, PATHS } from "../src/shell/folders-held-win32.js";
import { importedOn } from "./platforms.js";

it("is the helper's look at the folder, through its links, which prints nothing", () => {
  expect(lookCommand("C:\\Users\\Ana\\-Documents")).toEqual([HELPER, ["look", "C:\\Users\\Ana\\-Documents"]]);
});

it("reads a PATH's entries apart by ; and keeps those on a drive alone", () => {
  expect(PATHS.delimiter).toBe(";");
  expect(["C:\\Windows\\System32", "c:/tools", "\\\\server\\bin", "bin", "\\bin", "C:bin", ""].map(PATHS.whole)).toEqual([true, true, false, false, false, false, false]);
});

it("is a Windows PC's, by its PATH's own rules", async () => {
  const { FolderLooks } = await importedOn("win32", () => import("../src/shell/folders-held.js"));
  // Nothing on a drive is looked at here: every entry is left out before a look.
  expect(await new FolderLooks().path("bin;\\\\server\\share;C:")).toBe("");
});
