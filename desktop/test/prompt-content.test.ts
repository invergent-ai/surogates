import { describe, expect, it } from "vitest";

import type { FolderSheet } from "../src/binding/binder.js";
import { folderSheet } from "../src/shell/prompt-content.js";

const SHEET: FolderSheet = { agent: "acme.surogate.ai", folder: "/home/me/notes", mode: "free", links: null, refusal: null };
const ids = (content: { buttons: Array<{ id: string }> }) => content.buttons.map((button) => button.id);
const allowing = (content: { buttons: Array<{ id: string; allows: boolean }> }) => content.buttons.filter((b) => b.allows).map((b) => b.id);

describe("the folder sheet", () => {
  it("shows the folder and both modes, with Use this folder held back, focused, and on Enter", () => {
    const content = folderSheet(SHEET);
    expect(content.title).toBe("Work in notes?");
    expect(content.details).toEqual([{ label: "Folder", value: "/home/me/notes", code: true, keep: "" }]);
    expect(content.choice?.options.map((option) => option.value)).toEqual(["free", "ask"]);
    expect(content.choice?.value).toBe("free");
    expect([ids(content), allowing(content)]).toEqual([["cancel", "change", "accept"], ["accept"]]);
    expect([content.focus, content.cancel, content.enter]).toEqual(["accept", "cancel", "accept"]);
  });

  it("offers only Cancel and Change for a folder that cannot be used, saying why", () => {
    const content = folderSheet({ ...SHEET, folder: "/home/me", refusal: "the folder /home/me holds this computer's home folder or the app's own data" });
    expect(content.title).toBe("me cannot be used");
    expect(content.lead).toContain("holds this computer's home folder");
    expect([ids(content), allowing(content), content.choice, content.enter]).toEqual([["cancel", "change"], [], null, null]);
  });

  it.each([
    [{ count: 1, examples: ["a.txt"], complete: true }, "1 file in this folder is also linked from elsewhere; commands the agent runs can change those copies too: a.txt."],
    [{ count: 12, examples: ["a", "b", "c"], complete: true }, "12 files in this folder are also linked from elsewhere; commands the agent runs can change those copies too: a, b, c, and others."],
    [{ count: 4, examples: ["a", "b", "c"], complete: false }, "At least 4 files in this folder are also linked from elsewhere; commands the agent runs can change those copies too: a, b, c, and others."],
    [{ count: 0, examples: [], complete: false }, "Surogate could not look through all of this folder for files that are also linked from elsewhere."],
  ])("says what it found of files linked from elsewhere: %o", (links, note) => {
    expect(folderSheet({ ...SHEET, links }).notes).toEqual([note]);
  });
});
