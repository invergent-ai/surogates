import { describe, expect, it } from "vitest";

import type { ApprovalRequest, ChatLabel } from "../src/binding/approvals.js";
import type { FolderSheet } from "../src/binding/binder.js";
import { approval, folderSheet, sizeOf } from "../src/shell/prompt-content.js";

const SHEET: FolderSheet = { agent: "acme.surogate.ai", folder: "/home/me/notes", mode: "free", links: null, refusal: null };
const CHAT: ChatLabel = { agent: "acme.surogate.ai", root: "r", calling: "r", folder: "/home/me/notes" };
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

describe("an approval prompt", () => {
  const command: ApprovalRequest = { kind: "command", chat: CHAT, command: "npm test\n\tx", workdir: "/home/me/notes/web", background: false };

  it("asks about a command whole, Deny first and focused, and answers with its buttons' ids", () => {
    const content = approval(command);
    expect(content.title).toBe("Run a command in notes?");
    expect(content.lead).toBe("acme.surogate.ai wants to run this on this computer.");
    expect(content.details).toEqual([
      { label: "Command", value: "npm test\n\tx", code: true, keep: "\n\t" },
      { label: "In", value: "/home/me/notes/web", code: true, keep: "" },
    ]);
    expect([ids(content), allowing(content)]).toEqual([["deny", "stop_asking", "allow"], ["stop_asking", "allow"]]);
    expect([content.focus, content.cancel, content.enter]).toEqual(["deny", "deny", null]);
  });

  it("says a background command starts, and that a sub-agent asks", () => {
    const content = approval({ ...command, chat: { ...CHAT, calling: "child" }, background: true });
    expect(content.title).toBe("Start a background command in notes?");
    expect(content.lead).toBe("A sub-agent of acme.surogate.ai wants to run this on this computer.");
  });

  it.each([
    [{ text: "hello", cut: false }, 5, { label: "New content, 5 bytes", value: "hello", code: true, keep: "\n\t" }],
    [{ text: "aaa", cut: true }, 3 * 1024 * 1024, { label: "The first 8 KB of 3 MB", value: "aaa", code: true, keep: "\n\t" }],
    [null, 2048, { label: "New content, 2 KB", value: "Not text.", code: false, keep: "" }],
  ] as const)("shows a write's file from its folder, and its new content: %o", (preview, bytes, shown) => {
    const content = approval({ kind: "change", chat: CHAT, action: "write", path: "/home/me/notes/docs/a.md", bytes, preview });
    expect(content.title).toBe("Write a.md?");
    expect(content.details).toEqual([{ label: "File", value: "docs/a.md", code: true, keep: "" }, shown]);
  });

  it("asks about a delete, and names a path outside the folder whole, never as the folder's", () => {
    const content = approval({ kind: "change", chat: CHAT, action: "delete", path: "/tmp/x", bytes: null, preview: null });
    expect([content.title, content.details]).toEqual(["Delete x?", [{ label: "File", value: "/tmp/x", code: true, keep: "" }]]);
    expect(content.lead).toBe("acme.surogate.ai wants to delete this file.");
    expect(approval({ kind: "change", chat: CHAT, action: "delete", path: "/home/me/notes/x", bytes: null, preview: null }).lead)
      .toBe("acme.surogate.ai wants to delete this file in notes.");
  });

  it("shows input with every special character marked, and the command it goes to", () => {
    const content = approval({ kind: "input", chat: CHAT, process: "proc_1", command: "python3 manage.py shell", data: "y\n" });
    expect(content.details).toEqual([
      { label: "To the command", value: "python3 manage.py shell", code: true, keep: "\n\t" },
      { label: "Input", value: "y\n", code: true, keep: "" },
    ]);
    expect(approval({ kind: "input", chat: CHAT, process: "proc_1", command: null, data: "y" }).details[0]?.value)
      .toBe("A command Surogate did not start in this session (proc_1).");
  });

  it("asks about a destination with Allow, all its ports for the chat, and Deny, and says when it is private", () => {
    const content = approval({ kind: "network", chat: CHAT, host: "192.168.1.20", port: 8080, privateNetwork: true });
    expect(content.title).toBe("Connect to 192.168.1.20?");
    expect(content.buttons).toEqual([
      { id: "deny", label: "Deny", allows: false },
      { id: "allow_session", label: "Allow all its ports for this chat", allows: true },
      { id: "allow", label: "Allow", allows: true },
    ]);
    expect(content.notes).toEqual(["This address is on a private network, such as a home or office network, or a VPN."]);
    expect(approval({ kind: "network", chat: CHAT, host: "example.com", port: 443, privateNetwork: false }).notes).toEqual([]);
  });
});

describe("a size", () => {
  it.each([[1, "1 byte"], [900, "900 bytes"], [2048, "2 KB"], [1536, "1.5 KB"], [3 * 1024 * 1024, "3 MB"]])("of %d is %s", (bytes, text) => {
    expect(sizeOf(bytes)).toBe(text);
  });
});
