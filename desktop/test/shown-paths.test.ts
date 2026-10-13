// Every place the app shows a folder or a file to its person names it by this computer's own path: on Windows its
// Windows path, C:\Users\…, never the guest's, /mnt/c/…, which bindings keep inside; on Linux and macOS the path itself,
// as before. The folder sheet, Settings' folders, the prompts that name a folder or a file, Show folder's words, the
// project dialog's folder, the sidebar's line of copies, and the dialog that restores access. Notifications and the
// tray name no folder.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import type { ApprovalRequest, ChatLabel } from "../src/binding/approvals.js";
import type { FolderSheet } from "../src/binding/binder.js";
import { importedOn } from "./platforms.js";

const SHOP = "/mnt/c/Users/Ana/Documents/shop";
const WINDOWS_SHOP = "C:\\Users\\Ana\\Documents\\shop";
const chat: ChatLabel = { agent: "Research assistant", root: "r1", calling: "r1", folder: SHOP };
const sheet: FolderSheet = { agent: "Research assistant", folder: SHOP, mode: "ask", links: null, refusal: null, thread: null, project: null };
const change: ApprovalRequest = { kind: "change", chat, action: "write", path: `${SHOP}/src/app.py`, bytes: 3, preview: { text: "x=1", cut: false } };
const outside: ApprovalRequest = { ...change, path: "/mnt/d/elsewhere/notes.txt" };
const command: ApprovalRequest = { kind: "command", chat, command: "ls", workdir: `${SHOP}/src`, background: false };
const port: ApprovalRequest = { kind: "browser", chat, action: "port", detail: "3000", held: { root: "r2", folder: "/mnt/c/Users/Ana/other" } };
const upload: ApprovalRequest = { kind: "browser", chat, action: "upload", detail: "", page: "https://example.com/", files: [`${SHOP}/report.pdf`] };

const prompts = (os: NodeJS.Platform) => importedOn(os, () => import("../src/shell/prompt-content.js"));
// Every word a prompt shows, its details' values among them.
const words = (content: { title: string; lead: string; details: Array<{ label: string; value: string }>; notes: string[] }) =>
  [content.title, content.lead, ...content.details.flatMap(({ label, value }) => [label, value]), ...content.notes].join("\n");
const source = (file: string) => readFileSync(fileURLToPath(new URL(`../src/${file}`, import.meta.url)), "utf8").replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");

describe("a folder or a file the app shows its person", () => {
  it("is named by this computer's own path: Windows's, by its drive, its names and its separator; the path itself elsewhere", async () => {
    const windows = await importedOn("win32", () => import("../src/session-path.js"));
    expect([windows.shownPath(SHOP), windows.shownName(SHOP), windows.shownName("/mnt/d"), windows.shownWithin("src/app.py")])
      .toEqual([WINDOWS_SHOP, "shop", "D:\\", "src\\app.py"]);
    // A path no drive of this computer's holds is shown as it is.
    expect(windows.shownPath("/usr/lib")).toBe("/usr/lib");
    for (const os of ["linux", "darwin"] as const) {
      const own = await importedOn(os, () => import("../src/session-path.js"));
      expect([own.shownPath(SHOP), own.shownName(SHOP), own.shownWithin("src/app.py")], os).toEqual([SHOP, "shop", "src/app.py"]);
    }
  });

  it("is shown in the folder sheet by its Windows path on Windows, and its refusal's words name it so", async () => {
    const { folderSheet } = await prompts("win32");
    const shown = words(folderSheet(sheet));
    expect(shown).toContain(WINDOWS_SHOP);
    expect(shown).toContain("Work in shop?");
    expect(shown).not.toContain("/mnt/");
    const { checkFolder } = await importedOn("win32", () => import("../src/binding/folder.js"));
    const { shownPath } = await importedOn("win32", () => import("../src/session-path.js"));
    const guards = { home: "C:\\Users\\Ana", dataDir: "C:\\Users\\Ana\\AppData\\Local\\Surogate", cacheDir: "C:\\Users\\Ana\\AppData\\Local\\Surogate\\cache", appDirs: [] };
    expect(checkFolder(SHOP, guards, shownPath)).toMatchObject({ ok: false, message: `the folder ${WINDOWS_SHOP} is not there` });
    // To the agent, whose paths are the guest's, as before.
    expect(checkFolder(SHOP, guards)).toMatchObject({ ok: false, message: `the folder ${SHOP} is not there` });
    expect(words((await prompts("linux")).folderSheet(sheet))).toContain(`\n${SHOP}`);
  });

  it("is shown in a prompt that names a folder or a file by its Windows path on Windows, a file in the folder from it by Windows's separator", async () => {
    const { approval, freeMode } = await prompts("win32");
    const shown = [change, outside, command, port, upload].map((request) => words(approval(request)));
    expect(shown[0]).toContain("src\\app.py");
    expect(shown[0]).toContain("in shop");
    expect(shown[1]).toContain("D:\\elsewhere\\notes.txt");
    expect(shown[2]).toContain(`${WINDOWS_SHOP}\\src`);
    expect(shown[3]).toContain(WINDOWS_SHOP);
    expect(shown[3]).toContain("C:\\Users\\Ana\\other");
    expect(shown[4]).toContain(`${WINDOWS_SHOP}\\report.pdf`);
    expect(words(freeMode(chat))).toContain(WINDOWS_SHOP);
    for (const each of [...shown, words(freeMode(chat))]) expect(each).not.toContain("/mnt/");
    // On Linux, as before.
    const linux = await prompts("linux");
    expect(words(linux.approval(change))).toContain("src/app.py");
    expect(words(linux.approval(command))).toContain(`${SHOP}/src`);
    expect(words(linux.freeMode(chat))).toContain(SHOP);
  });

  it("is listed in Settings' folders by its Windows path on Windows, its chats under it", async () => {
    const bindings = {
      all: () => [{ root: "r1", folder: SHOP, mode: "ask" }, { root: "r2", folder: SHOP, mode: "free" }],
      domains: () => [], browsing: () => false, ports: () => [],
    } as never;
    const list = async (os: NodeJS.Platform) =>
      (await importedOn(os, () => import("../src/shell/folders.js"))).listFolders(bindings, (root) => Promise.resolve(root), { of: () => [] });
    expect((await list("win32")).map(({ folder, chats }) => [folder, chats.length])).toEqual([[WINDOWS_SHOP, 2]]);
    expect((await list("linux")).map(({ folder }) => folder)).toEqual([SHOP]);
  });

  it("is named by its Windows path in the sidebar's line while a thread's copy of it is made", async () => {
    const line = async (os: NodeJS.Platform) => {
      const { Copying } = await importedOn(os, () => import("../src/shell/copying.js"));
      const copying = new Copying(() => {}, 0);
      copying.heard({ root: "r1", folder: SHOP, state: "begun" });
      await new Promise((resolve) => setTimeout(resolve, 10));
      return copying.line();
    };
    expect(await line("win32")).toBe(`Making a copy of ${WINDOWS_SHOP} for a thread to work in`);
    expect(await line("linux")).toBe(`Making a copy of ${SHOP} for a thread to work in`);
  });

  it("is named by its Windows path where Show folder cannot show it", async () => {
    const { Binder } = await importedOn("win32", () => import("../src/binding/binder.js"));
    const bindings = { get: () => ({ root: "r1", folder: SHOP, dev: 1, ino: 2, boot: "", mode: "ask" }), watch: () => () => {} };
    const binder = new Binder({ bindings, prompts: {}, guards: {}, agent: "a", hosts: {}, look: () => Promise.resolve(null) } as never);
    await expect(binder.folderToShow("r1")).rejects.toThrow(`The folder ${WINDOWS_SHOP} is not there`);
  });

  it("is shown by its Windows path in the project dialog and in the dialog that restores access, as the app's own words name it", () => {
    const main = source("shell/main.ts");
    expect(main).toMatch(/return \{ folder: shownPath\(picked\.folder\) \};/);
    expect(main).toMatch(/handle\("project:chosen", \(\) => \(chosen \? shownPath\(chosen\.folder\) : null\)\);/);
    expect(main).toMatch(/folders\.map\(\(folder\) => `• \$\{shownPath\(folder\)\}`\)/);
    // Show folder opens the folder by this computer's own path.
    expect(main).toMatch(/shell\.showItemInFolder\(hostPathOf\(/);
  });

  it("is named in no notification and in no line of the tray", () => {
    const main = source("shell/main.ts");
    const shown = [...main.matchAll(/notifications\?\.show\(\{([^}]*)\}/g)].map(([, fields]) => fields!);
    expect(shown.length).toBeGreaterThan(0);
    for (const fields of shown) expect(fields).not.toMatch(/folder/i);
    expect(source("shell/menus.ts")).not.toMatch(/folder/i);
  });
});
