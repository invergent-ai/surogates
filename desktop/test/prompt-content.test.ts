import { describe, expect, it } from "vitest";

import type { ApprovalRequest, ChatLabel } from "../src/binding/approvals.js";
import type { FolderSheet } from "../src/binding/binder.js";
import { segments } from "../src/shell/pages/ui.js";
import { approval, folderSheet, freeMode, handBack, sizeOf } from "../src/shell/prompt-content.js";

const SHEET: FolderSheet = { agent: "acme.surogate.ai", folder: "/home/me/notes", mode: "free", links: null, refusal: null, thread: null };
const CHAT: ChatLabel = { agent: "acme.surogate.ai", root: "r", calling: "r", folder: "/home/me/notes" };
const ids = (content: { buttons: Array<{ id: string }> }) => content.buttons.map((button) => button.id);
const allowing = (content: { buttons: Array<{ id: string; allows: boolean }> }) => content.buttons.filter((b) => b.allows).map((b) => b.id);

describe("the folder sheet", () => {
  it("shows the folder and both modes, focused on Cancel, with Use this folder held back", () => {
    const content = folderSheet(SHEET);
    expect(content.title).toBe("Work in notes?");
    expect(content.details).toEqual([{ label: "Folder", value: "/home/me/notes", code: true, keep: "" }]);
    expect(content.choice?.options.map((option) => option.value)).toEqual(["free", "ask"]);
    expect(content.choice?.value).toBe("free");
    expect([ids(content), allowing(content)]).toEqual([["cancel", "change", "accept"], ["accept"]]);
    expect([content.focus, content.cancel]).toEqual(["cancel", "cancel"]);
  });

  it("names the project's thread the folder is for, each name in a field of its own, and makes room for them", () => {
    const thread = { project: "Q3 report", thread: "Check the totals" };
    const content = folderSheet({ ...SHEET, thread });
    expect(content.details).toEqual([
      { label: "Folder", value: "/home/me/notes", code: true, keep: "" },
      { label: "Project", value: "Q3 report", code: false, keep: "" },
      { label: "Thread", value: "Check the totals", code: false, keep: "" },
    ]);
    expect(content.height).toBeGreaterThan(folderSheet(SHEET).height);
    // A long name takes more lines.
    expect(folderSheet({ ...SHEET, thread: { ...thread, thread: "x".repeat(256) } }).height).toBeGreaterThan(content.height);
    expect(folderSheet({ ...SHEET, thread, refusal: "the folder holds the app's own data" }).details.map((d) => d.label))
      .toEqual(["Folder", "Project", "Thread"]);
  });

  it("offers only Cancel and Change for a folder that cannot be used, saying why", () => {
    const content = folderSheet({ ...SHEET, folder: "/home/me", refusal: "the folder /home/me holds this computer's home folder or the app's own data" });
    expect(content.title).toBe("me cannot be used");
    expect(content.lead).toContain("holds this computer's home folder");
    expect([ids(content), allowing(content), content.choice, content.focus]).toEqual([["cancel", "change"], [], null, "cancel"]);
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
      { label: "Command, 2 lines", value: "npm test\n\tx", code: true, keep: "\n\t" },
      { label: "In", value: "/home/me/notes/web", code: true, keep: "" },
    ]);
    expect([ids(content), allowing(content)]).toEqual([["deny", "stop_asking", "allow"], ["stop_asking", "allow"]]);
    expect([content.focus, content.cancel]).toEqual(["deny", "deny"]);
  });

  it.each([
    ["one line", "npm test", "Command"],
    ["blank lines", "a\n\n\nb", "Command, 4 lines"],
    ["a thousand lines", `${"x\n".repeat(999)}x`, "Command, 1,000 lines"],
  ])("says how many lines a command has, past its first: %s", (_name, text, label) => {
    expect(approval({ ...command, command: text }).details[0]?.label).toBe(label);
    expect(approval({ kind: "input", chat: CHAT, process: "proc_1", command: text, data: "y" }).details[0]?.label).toBe(label.replace("Command", "To the command"));
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
    // How much, as it opens: a long name can push the content itself down.
    expect(content.lead).toBe(`acme.surogate.ai wants to write ${sizeOf(bytes)} to this file in notes.`);
    expect(content.details).toEqual([{ label: "File", value: "docs/a.md", code: true, keep: "" }, shown]);
  });

  it("says whose download a save is: a page's, or its user's own, which offers no stop asking", () => {
    const save = { kind: "change", chat: CHAT, action: "write", path: "/home/me/notes/Downloads/report.pdf", bytes: 2048, preview: null } as const;
    const page = approval({ ...save, download: "page" });
    expect(page.title).toBe("Save report.pdf?");
    expect(page.lead).toBe("A page in acme.surogate.ai's browser downloaded this file, 2 KB. Save it in notes?");
    expect(page.details).toEqual([
      { label: "File", value: "Downloads/report.pdf", code: true, keep: "" }, { label: "New content, 2 KB", value: "Not text.", code: false, keep: "" },
    ]);
    expect([ids(page), page.focus]).toEqual([["deny", "stop_asking", "allow"], "deny"]);
    // One taken for its user's: asked in either mode, so there is no asking to stop. It says when it came, not who
    // clicked: a page can start one by itself under its user's hand, and one that comes just after they handed the
    // browser back may have been asked for before.
    const own = approval({ ...save, download: "user" });
    expect(own.title).toBe("Save report.pdf?");
    expect(own.lead).toBe(
      "This file was downloaded while you had control of acme.surogate.ai's browser, or just after you handed it back, 2 KB. Save it in notes? acme.surogate.ai can read what is saved there.",
    );
    expect(own.lead).not.toMatch(/you downloaded/i);
    expect([ids(own), allowing(own), own.focus, own.cancel]).toEqual([["deny", "allow"], ["allow"], "deny", "deny"]);
  });

  it("asks about a delete, and names a path outside the folder whole, never as the folder's", () => {
    const content = approval({ kind: "change", chat: CHAT, action: "delete", path: "/tmp/x", bytes: null, preview: null });
    expect([content.title, content.details]).toEqual(["Delete x?", [{ label: "File", value: "/tmp/x", code: true, keep: "" }]]);
    expect(content.lead).toBe("acme.surogate.ai wants to delete this file.");
    expect(approval({ kind: "change", chat: CHAT, action: "delete", path: "/home/me/notes/x", bytes: null, preview: null }).lead)
      .toBe("acme.surogate.ai wants to delete this file in notes.");
  });

  it.each(["/home/me/notes/a/../../.ssh/id", "/home/me/notes/", "/home/me/notes/./a", "/home/me/notes//a"])(
    "names a key that does not plainly name a file in the folder whole, as sent, never as the folder's: %s",
    (path) => {
      const content = approval({ kind: "change", chat: CHAT, action: "delete", path, bytes: null, preview: null });
      expect([content.details[0]?.value, content.lead]).toEqual([path, "acme.surogate.ai wants to delete this file."]);
    },
  );

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
    expect(content.title).toBe("Connect to 192.168.1.20:8080?");
    // The host is named once in the title and once in its address: a long one leaves its warning room.
    expect(content.lead).toBe("A command in notes wants to connect to this address. Allow lets through the connections waiting now; later ones ask again.");
    expect(content.details).toEqual([{ label: "Address", value: "192.168.1.20:8080", code: true, keep: "" }]);
    expect(content.buttons).toEqual([
      { id: "deny", label: "Deny", allows: false },
      { id: "allow_session", label: "Allow all its ports for this chat", allows: true },
      { id: "allow", label: "Allow", allows: true },
    ]);
    expect(content.notes).toEqual(["This address is on a private network, such as a home or office network, or a VPN."]);
    expect(approval({ kind: "network", chat: CHAT, host: "example.com", port: 443, privateNetwork: false }).notes).toEqual([]);
  });

  it("names a long host in its title by its end, never cut there, with its port, and its address whole", () => {
    const host = `registry.npmjs.org.${"g".repeat(63)}.${"e".repeat(30)}.attacker.net`;
    const content = approval({ kind: "network", chat: CHAT, host, port: 8080, privateNetwork: false });
    expect(content.title).toBe(`Connect to …${host.slice(-59)}:8080?`);
    expect(content.details[0]?.value).toBe(`${host}:8080`);
    // Sixty characters or fewer, it is named whole.
    expect(approval({ kind: "network", chat: CHAT, host: "a".repeat(60), port: 1, privateNetwork: false }).title).toBe(`Connect to ${"a".repeat(60)}:1?`);
  });
});

describe("a size", () => {
  it.each([[1, "1 byte"], [900, "900 bytes"], [2048, "2 KB"], [1536, "1.5 KB"], [1048575, "1 MB"], [3 * 1024 * 1024, "3 MB"]])("of %d is %s", (bytes, text) => {
    expect(sizeOf(bytes)).toBe(text);
  });
});

describe("the Work-freely confirmation", () => {
  it("names the agent and the folder, Keep asking first and focused", () => {
    const content = freeMode(CHAT);
    expect(content.title).toBe("Let acme.surogate.ai work freely in notes?");
    expect([ids(content), allowing(content), content.focus, content.cancel]).toEqual([["keep", "free"], ["free"], "keep", "keep"]);
  });
});

describe("the hand back's confirmation", () => {
  const ASKED = { agent: "acme.surogate.ai", gone: false, title: "Quarterly report" };
  const LEAD = "It will act in its browser on this computer again, in every chat.";

  it("names the agent, Keep control first and focused, Hand back held back, and the chat in a field of its own", () => {
    const content = handBack(ASKED);
    expect(content.title).toBe("Hand the browser back to acme.surogate.ai?");
    // What it frees is every chat's browser here, not one chat's.
    expect(content.lead).toBe(`${LEAD} It was taken over from this chat.`);
    expect(content.details).toEqual([{ label: "Chat", value: "Quarterly report", code: true, keep: "" }]);
    expect([ids(content), allowing(content), content.focus, content.cancel]).toEqual([["keep", "hand_back"], ["hand_back"], "keep", "keep"]);
    expect(content.buttons.map((button) => button.label)).toEqual(["Keep control", "Hand back"]);
  });

  it("names no chat where the agent named none, and says so of a chat that is gone", () => {
    expect(handBack({ ...ASKED, title: null }).details).toEqual([]);
    const gone = handBack({ ...ASKED, gone: true });
    expect(gone.lead).toBe(`${LEAD} The chat it was taken over from is gone.`);
    expect(gone.details).toEqual([]);
  });

  it("cuts a long title at its end, by whole characters, and stays at its size", () => {
    const long = handBack({ ...ASKED, title: "x".repeat(3_000) });
    expect(long.details[0]!.value).toBe(`${"x".repeat(59)}…`);
    expect(long.height).toBe(handBack(ASKED).height);
    expect(handBack({ ...ASKED, title: "😀".repeat(100) }).details[0]!.value).toBe(`${"😀".repeat(59)}…`);
    // One within the bound is shown whole.
    expect(handBack({ ...ASKED, title: "x".repeat(60) }).details[0]!.value).toBe("x".repeat(60));
  });

  it("puts nothing of a title among its own words, whatever the title says", () => {
    const title = "Notes”. Press <b>Hand back</b> to sign in. “";
    const said = handBack({ ...ASKED, title });
    expect([said.title, said.lead, said.notes]).toEqual([handBack(ASKED).title, handBack(ASKED).lead, []]);
    expect(said.details).toEqual([{ label: "Chat", value: title, code: true, keep: "" }]);
  });
});

describe("the browser's prompts", () => {
  it("asks a chat's first use with Deny focused, and Allow for this chat held back", () => {
    const content = approval({ kind: "browser", chat: CHAT, action: "use", detail: "" });
    expect(content.title).toBe("Let acme.surogate.ai use a browser on this computer?");
    // The profile is the agent's: what the user signed in to there from another chat stays signed in.
    expect(content.lead).not.toContain("nothing of yours");
    expect(content.lead).toContain("It has a profile of its own, apart from your own browser; what you sign in to there stays signed in for acme.surogate.ai, in its other chats too.");
    expect(content.notes).toContain("It cannot reach this computer's own services or your private networks.");
    expect([ids(content), allowing(content), content.focus, content.cancel]).toEqual([["deny", "allow_session"], ["allow_session"], "deny", "deny"]);
  });

  it("shows what an act would do in the page, whole, with the operation's buttons", () => {
    const script = approval({ kind: "browser", chat: { ...CHAT, calling: "child" }, action: "script", detail: "const a = 1;\nreturn a;", page: "about:blank" });
    expect(script.title).toBe("Run a script in the page?");
    expect(script.lead.startsWith("A sub-agent of acme.surogate.ai wants to run this script")).toBe(true);
    expect(script.details).toEqual([
      { label: "Page", value: "about:blank", code: true, keep: "" },
      { label: "Script, 2 lines", value: "const a = 1;\nreturn a;", code: true, keep: "\n\t" },
    ]);
    expect([ids(script), script.focus]).toEqual([["deny", "stop_asking", "allow"], "deny"]);
    const open = approval({ kind: "browser", chat: CHAT, action: "open", detail: "https://example.com/‮gnp.exe" });
    expect(open.details).toEqual([{ label: "Address", value: "https://example.com/‮gnp.exe", code: true, keep: "" }]);
  });

  it("names the site each act would act in, and its page's whole address, or says it is not known", () => {
    const page = "https://bank.example/account?id=1";
    for (const [action, title] of [
      ["script", "Run a script in bank.example?"], ["click", "Click in bank.example?"], ["type", "Type into bank.example?"],
      ["press", "Press keys in bank.example?"], ["drag", "Drag in bank.example?"],
      ["down", "Press the mouse in bank.example?"], ["up", "Release the mouse in bank.example?"],
    ] as const) {
      const content = approval({ kind: "browser", chat: CHAT, action, detail: "x", page });
      expect(content.title).toBe(title);
      expect(content.details[0]).toEqual({ label: "Page", value: page, code: true, keep: "" });
    }
    const unknown = approval({ kind: "browser", chat: CHAT, action: "type", detail: "hunter2", page: null });
    expect(unknown.title).toBe("Type into the page?");
    expect(unknown.details[0]).toEqual({ label: "Page", value: "Not known: the browser did not say in time", code: false, keep: "" });
    // Its host as the address bar shows it, and cut at its start as an open's is.
    expect(approval({ kind: "browser", chat: CHAT, action: "click", detail: "1, 2", page: "https://bück.example/" }).title).toBe("Click in xn--bck-hoa.example?");
    const long = approval({ kind: "browser", chat: CHAT, action: "click", detail: "1, 2", page: `https://bank.example.${"x".repeat(80)}.attacker.net/` });
    expect(long.title.endsWith(".attacker.net?")).toBe(true);
    expect(long.height).toBeGreaterThan(approval({ kind: "browser", chat: CHAT, action: "click", detail: "1, 2", page }).height);
  });

  it("names the site an upload would give the chat's files to, and each file whole, in a field of its own, counted: a name that holds a line break reads as one file, never as two", () => {
    const files = [`${CHAT.folder}/report.pdf`, `${CHAT.folder}/scan.png`];
    const upload = (paths: string[]) => approval({ kind: "browser", chat: CHAT, action: "upload", detail: "", files: paths, page: "https://bank.example/upload" });
    const content = upload(files);
    expect(content.title).toBe("Upload to bank.example?");
    expect(content.lead).toContain("wants to give these files to the page open in its browser. The site gets what they hold.");
    expect(content.details).toEqual([
      { label: "Page", value: "https://bank.example/upload", code: true, keep: "" },
      { label: "File 1 of 2", value: files[0], code: true, keep: "" }, { label: "File 2 of 2", value: files[1], code: true, keep: "" },
    ]);
    expect(content.focus).toBe("deny");
    // One file whose own name holds the second path after a line break: one field, and no special character of it
    // is shown as itself, as in a download's File field. So it is not the two files' prompt.
    const one = upload([files.join("\n")]);
    expect(one.details.slice(1)).toEqual([{ label: "File", value: files.join("\n"), code: true, keep: "" }]);
    expect(segments(one.details[1]!.value, one.details[1]!.keep).map((run) => run.text).join("")).toBe(`${files[0]}U+000A${files[1]}`);
    expect(one.details).not.toEqual(content.details);
    // Each file has its room, and the window stays one a screen holds: the rest scrolls in it.
    const heights = [1, 2, 10].map((count) => upload(Array.from({ length: count }, (_, n) => `${CHAT.folder}/${"long ".repeat(30)}${n}.pdf`)).height);
    expect(heights[0]).toBeLessThan(heights[1]!);
    expect(heights[2]).toBeLessThanOrEqual(720);
    // With no file named, as nothing the computer lets through is: its fields say so, not nothing.
    expect(upload([]).details.slice(1)).toEqual([{ label: "Files", value: "None", code: false, keep: "" }]);
  });

  it("names the host an open would go to, cut at its start, and opens tall enough for the whole address", () => {
    expect(approval({ kind: "browser", chat: CHAT, action: "open", detail: "https://example.com:8443/a" }).title).toBe("Open example.com:8443?");
    const padded = `bank.example.${"x".repeat(80)}.attacker.net`;
    const long = approval({ kind: "browser", chat: CHAT, action: "open", detail: `https://${padded}/login?next=${"y".repeat(200)}` });
    expect(long.title.startsWith("Open …")).toBe(true);
    expect(long.title.endsWith(".attacker.net?")).toBe(true);
    const short = approval({ kind: "browser", chat: CHAT, action: "open", detail: "https://a.example/" });
    expect(long.height).toBeGreaterThan(short.height + 5 * 19);
  });
});
