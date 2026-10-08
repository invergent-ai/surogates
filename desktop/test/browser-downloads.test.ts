import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, symlinkSync, truncateSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { type ApprovalAnswer, type ApprovalRequest, Approvals } from "../src/binding/approvals.js";
import { BOOT_ID } from "../src/binding/folder.js";
import { downloadSaver, quoted, type Saver, saveDownload, savedName, type StagedDownload, tooLarge, UNSAVED } from "../src/browser/downloads.js";
import { MAX_WRITE_BYTES } from "../src/files/answers.js";
import { perform } from "../src/files/operations.js";
import type { Mode } from "../src/journal/bindings.js";
import { OperationJournal } from "../src/journal/journal.js";

const ROOT = "44444444-4444-4444-8444-444444444444";
const CHILD = "66666666-6666-4666-8666-666666666666";

let base: string;
let folder: string;
let downloads: string;
let journal: OperationJournal;
let asked: ApprovalRequest[];
// What the chat's user answers, and what happens on this computer while their prompt is open.
let answer: ApprovalAnswer;
let meanwhile: (request: ApprovalRequest) => Promise<void>;
let saver: Saver;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "downloads-")));
  folder = join(base, "notes");
  downloads = join(folder, "Downloads");
  mkdirSync(folder);
  mkdirSync(join(base, "staged"));
  journal = new OperationJournal(join(base, "journal.sqlite"));
  asked = [];
  answer = "allow";
  meanwhile = () => Promise.resolve();
  const approvals = new Approvals({
    bindings: journal.bindings, agent: "Research assistant",
    prompts: {
      approve: async (request) => {
        asked.push(request);
        await meanwhile(request);
        return answer;
      },
      confirmFreeMode: () => Promise.resolve(false),
    },
  });
  // The device's binder, as far as a download sees it: the chat's approvals, then the file helper's own operations.
  saver = {
    admit: (operation, signal, download) => approvals.admit(operation, signal, download),
    run: (operation, signal) => perform(operation.kind, operation.args, { folder, home: base, env: {} }, signal),
  };
});

afterEach(() => {
  journal.close();
  rmSync(base, { recursive: true, force: true });
});

const bind = (mode: Mode) => journal.bindings.add({ root: ROOT, nonce: "n", folder, dev: 1, ino: 1, boot: BOOT_ID, mode, boundAt: 1 });
const read = (name: string) => readFileSync(join(downloads, name), "utf8");

// A file the browser host staged for the chat's sub-agent, as the page named it; *user*: its user's own.
let staged = 0;
function stage(name: string, data = "report", user = false): StagedDownload {
  const path = join(base, "staged", `download-${(staged += 1)}`);
  writeFileSync(path, data);
  return { root: ROOT, session: CHILD, name, path, user };
}

describe("a download the agent's page started", () => {
  it("is saved under Downloads in the chat's folder, under its own name or the next free one, and leaves nothing staged", async () => {
    bind("free");
    // The first makes the folder.
    const first = stage("report.txt", "first");
    expect(await saveDownload(first, journal.bindings, saver)).toBe(
      'The page downloaded "report.txt". It is saved in the chat\'s folder as Downloads/report.txt.',
    );
    expect(await saveDownload(stage("report.txt", "second"), journal.bindings, saver)).toContain("as Downloads/report (2).txt.");
    expect([read("report.txt"), read("report (2).txt")]).toEqual(["first", "second"]);
    expect(existsSync(first.path)).toBe(false);
    // Beside the chat's files, never among them: a page's conftest.py is not at the top of the folder for the agent's next test run.
    await saveDownload(stage("conftest.py", "raise SystemExit"), journal.bindings, saver);
    expect(readdirSync(folder)).toEqual(["Downloads"]);
    // A chat that works freely is asked nothing.
    expect(asked).toEqual([]);
  });

  it("asks a chat that asks every time, saying the page downloaded it, and saves nothing its user denies", async () => {
    bind("ask");
    answer = "deny";
    const download = stage("invoice.pdf", "%PDF");
    expect(await saveDownload(download, journal.bindings, saver)).toBe(
      'The page downloaded "invoice.pdf", but it was not saved: The user denied this change on this computer.',
    );
    expect(asked).toMatchObject([{
      kind: "change", action: "write", path: join(downloads, "invoice.pdf"), bytes: 4, download: "page", chat: { root: ROOT, calling: CHILD },
    }]);
    expect(existsSync(join(downloads, "invoice.pdf"))).toBe(false);
    expect(existsSync(download.path)).toBe(false);
  });

  it("saves two downloads of one name as two files, however long their prompts stay open, and tells each its own name", async () => {
    bind("ask");
    meanwhile = () => new Promise((done) => setTimeout(done, 200));
    const save = downloadSaver(journal.bindings, saver);
    const [first, second] = await Promise.all([save(stage("report.txt", "first")), save(stage("report.txt", "second"))]);
    expect(first).toBe('The page downloaded "report.txt". It is saved in the chat\'s folder as Downloads/report.txt.');
    expect(second).toBe('The page downloaded "report.txt". It is saved in the chat\'s folder as Downloads/report (2).txt.');
    expect([read("report.txt"), read("report (2).txt")]).toEqual(["first", "second"]);
    // One at a time for the chat: each prompt names the file its download becomes.
    expect(asked.map((request) => request.kind === "change" && request.path)).toEqual([join(downloads, "report.txt"), join(downloads, "report (2).txt")]);
  });

  it("goes on to a chat's next download after one whose save failed outright, which it says was not saved", async () => {
    bind("free");
    const save = downloadSaver(journal.bindings, saver);
    // As a browser host gone wrong would stage one: with no name, and no path; and with a file, but no name.
    const broken = save({ root: ROOT, session: CHILD, user: false } as unknown as StagedDownload);
    const nameless = stage("report.txt");
    const unnamed = save({ ...nameless, name: undefined } as unknown as StagedDownload);
    const next = save(stage("report.txt"));
    expect([await broken, await unnamed]).toEqual([UNSAVED, UNSAVED]);
    expect(UNSAVED).toBe("The page downloaded a file, but it was not saved: this computer could not save it.");
    // What was staged for it goes all the same.
    expect(existsSync(nameless.path)).toBe(false);
    expect(await next).toBe('The page downloaded "report.txt". It is saved in the chat\'s folder as Downloads/report.txt.');
    // And to the one after: the line is the chat's for as long as the device runs.
    expect(await save(stage("report.txt"))).toContain("as Downloads/report (2).txt.");
  });

  it("never replaces a file made at its name while its prompt was open: the next name is taken, and nobody is asked again", async () => {
    bind("ask");
    // The agent's own write, allowed before this one, lands while the download's prompt is open.
    meanwhile = async () => {
      mkdirSync(downloads, { recursive: true });
      writeFileSync(join(downloads, "report.txt"), "the agent's own");
    };
    expect(await saveDownload(stage("report.txt", "the page's"), journal.bindings, saver)).toBe(
      'The page downloaded "report.txt". It is saved in the chat\'s folder as Downloads/report (2).txt.',
    );
    expect([read("report.txt"), read("report (2).txt")]).toEqual(["the agent's own", "the page's"]);
    expect(asked).toHaveLength(1);
    // Two saved at once, with no line to wait in: neither replaces the other.
    meanwhile = () => new Promise((done) => setTimeout(done, 50));
    const said = await Promise.all(["third", "fourth"].map((data) => saveDownload(stage("notes.txt", data), journal.bindings, saver)));
    expect(said.map((notice) => notice.split(" as ")[1]).sort()).toEqual(["Downloads/notes (2).txt.", "Downloads/notes.txt."]);
    expect([read("notes.txt"), read("notes (2).txt")].sort()).toEqual(["fourth", "third"]);
  });

  it("writes through no link at its name, to a file or to nothing: the name is passed over", async () => {
    bind("free");
    mkdirSync(downloads);
    writeFileSync(join(base, "outside.txt"), "the user's own, outside the folder");
    symlinkSync(join(base, "outside.txt"), join(downloads, "report.txt"));
    symlinkSync(join(folder, "nowhere.txt"), join(downloads, "notes.txt"));
    expect(await saveDownload(stage("report.txt", "first"), journal.bindings, saver)).toContain("as Downloads/report (2).txt.");
    expect(await saveDownload(stage("notes.txt", "second"), journal.bindings, saver)).toContain("as Downloads/notes (2).txt.");
    expect(readFileSync(join(base, "outside.txt"), "utf8")).toBe("the user's own, outside the folder");
    expect(existsSync(join(folder, "nowhere.txt"))).toBe(false);
  });

  it("saves nothing where Downloads leads out of the chat's folder, or is no folder, and says why", async () => {
    bind("free");
    mkdirSync(join(base, "elsewhere"));
    symlinkSync(join(base, "elsewhere"), downloads);
    expect(await saveDownload(stage("report.txt"), journal.bindings, saver)).toContain(
      `but it was not saved: Path traversal blocked: '${downloads}' resolves to '${join(base, "elsewhere")}'`,
    );
    expect(readdirSync(join(base, "elsewhere"))).toEqual([]);
    rmSync(downloads);
    writeFileSync(downloads, "a file of the user's");
    expect(await saveDownload(stage("report.txt"), journal.bindings, saver)).toBe(
      `The page downloaded "report.txt", but it was not saved: File exists: '${downloads}'.`,
    );
    expect(readFileSync(downloads, "utf8")).toBe("a file of the user's");
  });

  it("answers at once where Downloads is no folder, asking nobody and trying no write; and tries one write, and no other name, where it stopped being one meanwhile", async () => {
    bind("ask");
    writeFileSync(downloads, "a file of the user's");
    const ran: string[] = [];
    const counting: Saver = { admit: saver.admit, run: (operation, signal) => (ran.push(operation.kind), saver.run(operation, signal)) };
    const refused = `The page downloaded "report.txt", but it was not saved: File exists: '${downloads}'.`;
    expect(await saveDownload(stage("report.txt"), journal.bindings, counting)).toBe(refused);
    expect([asked, ran]).toEqual([[], ["resolve", "stat"]]);
    // Not there at the look, and a file once its user has allowed the save: the write's refusal is not a name taken.
    rmSync(downloads);
    ran.length = 0;
    meanwhile = async () => writeFileSync(downloads, "made meanwhile");
    expect(await saveDownload(stage("report.txt"), journal.bindings, counting)).toBe(refused);
    // The write's refusal, then one more look at Downloads, which says why.
    expect([asked.length, ran]).toEqual([1, ["resolve", "stat", "resolve", "stat", "write", "resolve", "stat"]]);
    expect(readFileSync(downloads, "utf8")).toBe("made meanwhile");
    // A file host that refuses the look at Downloads says why, in its own words.
    const failing: Saver = {
      admit: saver.admit,
      run: (operation, signal) => (operation.kind === "stat"
        ? Promise.resolve({ error: { type: "sandbox", message: "This chat's folder was replaced since it was bound" } })
        : saver.run(operation, signal)),
    };
    expect(await saveDownload(stage("report.txt"), journal.bindings, failing)).toBe(
      'The page downloaded "report.txt", but it was not saved: This chat\'s folder was replaced since it was bound.',
    );
  });

  it("says why when the file host resolves no name of it, as for a folder replaced since it was bound", async () => {
    bind("free");
    const refusing: Saver = {
      admit: saver.admit,
      run: () => Promise.resolve({ error: { type: "sandbox", message: "This chat's folder was replaced since it was bound" } }),
    };
    const download = stage("report.txt");
    expect(await saveDownload(download, journal.bindings, refusing)).toBe(
      'The page downloaded "report.txt", but it was not saved: This chat\'s folder was replaced since it was bound.',
    );
    expect(existsSync(download.path)).toBe(false);
  });

  it("says why by the error's code, in the file tools' words, when its staged file cannot be read, as after the browser closed: no path of this computer, and no text of the error's own", async () => {
    bind("free");
    const staging = join(base, "staged");
    // Gone, as Playwright removes what it staged when the browser closes; and something else there than a file.
    const gone = { ...stage("report.txt"), path: join(staging, "gone") };
    const folder = { ...stage("notes.txt"), path: staging };
    const said = [await saveDownload(gone, journal.bindings, saver), await saveDownload(folder, journal.bindings, saver)];
    expect(said).toEqual([
      'The page downloaded "report.txt", but it was not saved: the file the browser kept could not be read (No such file or directory).',
      'The page downloaded "notes.txt", but it was not saved: the file the browser kept could not be read (Is a directory).',
    ]);
    // Whatever else goes wrong on this computer is not told in the error's words, which can name its paths.
    const broken = stage("plan.txt");
    const unread = { get: () => { throw new Error(`unable to open database file: ${join(base, "journal.sqlite")}`); } };
    const refusing: Saver = { admit: saver.admit, run: () => Promise.reject(Object.assign(new Error(`EACCES: permission denied, open '${broken.path}'`), { code: "EACCES" })) };
    said.push(await saveDownload(broken, unread, saver), await saveDownload(stage("plan.txt"), journal.bindings, refusing));
    // Nor is a code the file tools have no words for: a path that is no path, here.
    said.push(await saveDownload({ ...stage("plan.txt"), path: `${staging}/no\0path` }, journal.bindings, saver));
    expect(said.slice(2)).toEqual([
      'The page downloaded "plan.txt", but it was not saved: this computer could not save it.',
      'The page downloaded "plan.txt", but it was not saved: this computer could not save it.',
      'The page downloaded "plan.txt", but it was not saved: this computer could not save it.',
    ]);
    for (const notice of said) expect(notice).not.toContain(base);
    expect([existsSync(broken.path), existsSync(staging)]).toEqual([false, true]);
    // A link where the staged file was is not read through: what it leads to is neither saved nor removed.
    writeFileSync(join(base, "outside.txt"), "the user's own");
    symlinkSync(join(base, "outside.txt"), join(staging, "link"));
    expect(await saveDownload({ ...stage("keys.txt"), path: join(staging, "link") }, journal.bindings, saver)).toBe(
      'The page downloaded "keys.txt", but it was not saved: the file the browser kept could not be read (Too many levels of symbolic links).',
    );
    expect([readFileSync(join(base, "outside.txt"), "utf8"), existsSync(downloads), existsSync(join(staging, "link"))]).toEqual(["the user's own", false, false]);
  });

  it("looks at a staged file's size before it reads it: one over what a write may carry is not read, and nobody is asked about it", async () => {
    bind("ask");
    const ran: string[] = [];
    const counting: Saver = { admit: saver.admit, run: (operation, signal) => (ran.push(operation.kind), saver.run(operation, signal)) };
    // Its size without its bytes: a hole.
    const over = stage("over.bin", "");
    truncateSync(over.path, MAX_WRITE_BYTES + 1);
    expect(await saveDownload(over, journal.bindings, counting)).toBe(tooLarge("over.bin", MAX_WRITE_BYTES + 1));
    expect(tooLarge("over.bin", MAX_WRITE_BYTES + 1)).toBe(
      'The page downloaded "over.bin" (52428801 bytes), too large to save in the chat\'s folder at once (at most 52428800 bytes), so it was not saved.',
    );
    expect([asked, ran, existsSync(over.path)]).toEqual([[], [], false]);
    // One of exactly that much is saved whole.
    journal.bindings.setMode(ROOT, "free");
    const most = stage("most.bin", "");
    truncateSync(most.path, MAX_WRITE_BYTES);
    expect(await saveDownload(most, journal.bindings, saver)).toContain("as Downloads/most.bin.");
    expect(statSync(join(downloads, "most.bin")).size).toBe(MAX_WRITE_BYTES);
  });

  it("stops asking about a download once it is told to stop, as when its chat is deleted: its prompt goes, nothing is saved, and what was staged goes", async () => {
    bind("ask");
    // A prompt nobody answers.
    meanwhile = () => new Promise(() => {});
    const stop = new AbortController();
    const download = stage("report.txt");
    const save = downloadSaver(journal.bindings, saver);
    const saving = save(download, stop.signal);
    // Another of the chat's, behind it in the chat's line, told to stop by the same.
    const behind = stage("notes.txt");
    const waiting = save(behind, stop.signal);
    await expect.poll(() => asked.length).toBe(1);
    stop.abort();
    expect(await saving).toBe('The page downloaded "report.txt", but it was not saved: its chat was deleted.');
    expect(await waiting).toContain("but it was not saved");
    expect([asked.length, existsSync(downloads), existsSync(download.path), existsSync(behind.path)]).toEqual([1, false, false, false]);
    // In a chat that works freely nobody is asked, and nothing is written either once it is told to stop.
    journal.bindings.setMode(ROOT, "free");
    expect(await saveDownload(stage("free.txt"), journal.bindings, saver, stop.signal)).toBe(
      'The page downloaded "free.txt", but it was not saved: its chat was deleted.',
    );
    expect(existsSync(downloads)).toBe(false);
  });

  it("saves nothing for a chat this computer did not bind", async () => {
    const download = stage("report.txt");
    expect(await saveDownload(download, journal.bindings, saver)).toBe(
      'The page downloaded "report.txt", but this chat has no folder on this computer, so it was not saved.',
    );
    expect(existsSync(download.path)).toBe(false);
  });

  it("asks before it saves a download its user made while they held the browser, in a chat that works freely too, and offers no stop asking", async () => {
    bind("free");
    expect(await saveDownload(stage("statement.pdf", "%PDF", true), journal.bindings, saver)).toContain("as Downloads/statement.pdf.");
    expect(asked).toMatchObject([{ kind: "change", action: "write", path: join(downloads, "statement.pdf"), download: "user" }]);
    // An answer its prompt does not offer denies, and frees no chat.
    journal.bindings.setMode(ROOT, "ask");
    answer = "stop_asking";
    expect(await saveDownload(stage("payslip.pdf", "%PDF", true), journal.bindings, saver)).toContain("but it was not saved");
    expect([journal.bindings.get(ROOT)?.mode, existsSync(join(downloads, "payslip.pdf"))]).toEqual(["ask", false]);
  });

  it("saves nothing through a link at Downloads, wherever in the chat's folder it leads, and asks nobody", async () => {
    bind("ask");
    const refused = 'The page downloaded "ci.yml", but it was not saved: Downloads in the chat\'s folder is a link, and nothing is saved through one.';
    // To the folder's top, where a page's file would lie among the chat's own; to a folder whose files run by
    // themselves; and to one the agent's own writes are not asked about.
    for (const target of [".", ".github/workflows", ".surogates-results"]) {
      mkdirSync(join(folder, target), { recursive: true });
      symlinkSync(join(folder, target), downloads);
      expect(await saveDownload(stage("ci.yml"), journal.bindings, saver), target).toBe(refused);
      expect([asked, readdirSync(join(folder, target)).includes("ci.yml")], target).toEqual([[], false]);
      rmSync(downloads);
    }
    // Made a link while the prompt is open, its user having allowed the save: nothing is written through it either.
    meanwhile = async () => symlinkSync(folder, downloads);
    expect(await saveDownload(stage("ci.yml"), journal.bindings, saver)).toBe(refused);
    expect([asked.length, readdirSync(folder).includes("ci.yml")]).toEqual([1, false]);
  });

  it("asks about a write that saves a download wherever it lands, also where the agent's own writes are not asked about", async () => {
    bind("ask");
    answer = "deny";
    const write = {
      id: "write-1", sessionId: ROOT, callingSessionId: ROOT, invocationId: "download", ordinal: 0, kind: "write",
      args: { key: join(folder, ".surogates-results", "out.txt"), data: "" }, digest: "",
    };
    const signal = new AbortController().signal;
    // The agent's own there is let through unasked; one that saves a download is asked about, whosever it is.
    expect(await saver.admit(write, signal, undefined as never)).toBeNull();
    expect(await saver.admit(write, signal, "page")).toMatchObject({ error: { code: "EACCES" } });
    expect(await saver.admit(write, signal, "user")).toMatchObject({ error: { code: "EACCES" } });
    expect(asked).toMatchObject([{ kind: "change", download: "page" }, { kind: "change", download: "user" }]);
  });

  it("passes over a name where a link was made while its prompt was open, and takes the next, asking nobody again", async () => {
    bind("ask");
    // A link to a file outside the folder, not there yet: written through, it would make that file.
    meanwhile = async () => {
      mkdirSync(downloads, { recursive: true });
      symlinkSync(join(base, "outside.txt"), join(downloads, "report.txt"));
    };
    expect(await saveDownload(stage("report.txt", "the page's"), journal.bindings, saver)).toBe(
      'The page downloaded "report.txt". It is saved in the chat\'s folder as Downloads/report (2).txt.',
    );
    expect([asked.length, read("report (2).txt"), existsSync(join(base, "outside.txt"))]).toEqual([1, "the page's", false]);
    // A write its file host refuses for another reason is no name to pass over: the save ends, and says why.
    const refusing: Saver = {
      admit: saver.admit,
      run: (operation, signal) => (operation.kind === "write"
        ? Promise.resolve({ error: { type: "os", code: "ENOSPC", message: `No space left on device: '${String(operation.args.key)}'` } })
        : saver.run(operation, signal)),
    };
    meanwhile = () => Promise.resolve();
    expect(await saveDownload(stage("notes.txt"), journal.bindings, refusing)).toBe(
      `The page downloaded "notes.txt", but it was not saved: No space left on device: '${join(downloads, "notes.txt")}'.`,
    );
    // Nor is its user's denial: no other name is asked about.
    answer = "deny";
    expect(await saveDownload(stage("notes.txt"), journal.bindings, saver)).toContain("The user denied this change on this computer.");
    expect(asked.length).toBe(3);
  });

  it("saves none once a hundred files of its name are there: none is replaced, and nobody is asked", async () => {
    bind("ask");
    mkdirSync(downloads);
    for (let n = 1; n <= 100; n += 1) writeFileSync(join(downloads, n === 1 ? "report.txt" : `report (${n}).txt`), "kept");
    const download = stage("report.txt", "the page's");
    expect(await saveDownload(download, journal.bindings, saver)).toBe(
      'The page downloaded "report.txt", but it was not saved: the chat\'s folder has 100 files of that name already.',
    );
    expect([readdirSync(downloads).length, [...new Set(readdirSync(downloads).map(read))]]).toEqual([100, ["kept"]]);
    expect([asked, existsSync(download.path)]).toEqual([[], false]);
  });

  it("names it as one file of the folder: never a path, a hidden file or an invisible character, and within what a name may take", () => {
    expect(savedName("_.._escape.txt")).toBe("_.._escape.txt");
    expect(savedName("../../etc/passwd")).toBe("passwd");
    expect(savedName("a\\b.txt")).toBe("b.txt");
    expect(savedName(".envrc")).toBe("_envrc");
    expect(savedName("in‮gnp.exe")).toBe("ingnp.exe");
    expect(savedName(" \u0007 ")).toBe("download");
    expect(savedName("..")).toBe("download");
    const long = savedName(`${"x".repeat(300)}.tar.gz`);
    expect([long.length, long.endsWith(".gz")]).toEqual([200, true]);
    // Counted in bytes, as the file system counts a name, with room for " (100)"; cut between characters, never inside one.
    const japanese = savedName(`${"報告書".repeat(31)}.pdf`);
    expect([Buffer.byteLength(japanese), japanese]).toEqual([199, `${"報告書".repeat(21)}報告.pdf`]);
    expect(savedName("😀".repeat(60))).toBe("😀".repeat(50));
    expect(quoted(`${"x".repeat(197)}😀😀`)).toBe(JSON.stringify("x".repeat(197)));
  });

  it("leaves in a name no character that does not show, or that a file's name does not hold as it is: every other space is a plain one", () => {
    // Separators of lines and of paragraphs.
    expect(savedName("re\u2028po\u2029rt.pdf")).toBe("report.pdf");
    // Half a character, which the file system would write as another: a whole one stays.
    expect(savedName("re\ud800port\udfff 😀.pdf")).toBe("report 😀.pdf");
    // Private-use and unassigned characters, in any plane.
    expect(savedName("re\ue000po\u{f0000}r\u0378t\uffff.pdf")).toBe("report.pdf");
    // The letters that draw nothing.
    expect(savedName("r\u115fe\u1160p\u2800o\u3164r\uffa0t.pdf")).toBe("report.pdf");
    // A no-break, an ideographic, an em and a narrow space.
    expect(savedName("my\u00a0annual\u3000report\u2003final\u202f2.pdf")).toBe("my annual report final 2.pdf");
    // What a script joins a letter with, or picks its shape by, shows: it stays.
    expect(savedName("re\u0301sume\ufe0f.pdf")).toBe("re\u0301sume\ufe0f.pdf");
    // Once they went: nothing left is "download", a space left at an end goes, and a dot that leads hides nothing.
    expect(savedName("\u3164\u2800\u2028")).toBe("download");
    expect(savedName("\u3164.env\u00a0")).toBe("_env");
  });

  it("writes the name it asked about, byte for byte", async () => {
    bind("ask");
    const names = [
      "re\ud800port.pdf", "re\u2028port\u3164.pdf", "my\u00a0annual\u3000report.pdf", "re\u0301sume\ufe0f.pdf", `${"報告書".repeat(31)}.pdf`,
      `${"x".repeat(199)}\ud83d.txt`, "in\u202egnp.exe", "\ue000.envrc",
    ];
    const said: string[] = [];
    for (const name of names) said.push(await saveDownload(stage(name), journal.bindings, saver));
    const about = asked.map((request) => (request.kind === "change" ? basename(request.path) : ""));
    // Each is text a file's name holds as it is, and is the name on disk, read back as bytes.
    expect(about.map((name) => name.isWellFormed())).toEqual(names.map(() => true));
    expect(readdirSync(downloads, { encoding: "buffer" }).map((name) => name.toString("hex")).sort())
      .toEqual(about.map((name) => Buffer.from(name).toString("hex")).sort());
    // And the name its agent is told.
    expect(said).toEqual(about.map((name, n) => `The page downloaded ${quoted(names[n]!)}. It is saved in the chat's folder as Downloads/${name}.`));
  });

  it("saves one whose name is as long as a name may be, in another script too", async () => {
    bind("free");
    const name = `${"報告書".repeat(31)}.pdf`;
    expect(await saveDownload(stage(name), journal.bindings, saver)).toContain(`It is saved in the chat's folder as Downloads/${"報告書".repeat(21)}報告.pdf.`);
    expect(readdirSync(downloads)).toEqual([`${"報告書".repeat(21)}報告.pdf`]);
  });
});
