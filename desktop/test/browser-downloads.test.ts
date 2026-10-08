import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { type ApprovalAnswer, type ApprovalRequest, Approvals } from "../src/binding/approvals.js";
import { BOOT_ID } from "../src/binding/folder.js";
import { downloadSaver, quoted, type Saver, saveDownload, savedName, type StagedDownload } from "../src/browser/downloads.js";
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

  it("asks about one whose Downloads leads where the agent's own writes are not asked about", async () => {
    bind("ask");
    mkdirSync(join(folder, ".surogates-results"));
    symlinkSync(join(folder, ".surogates-results"), downloads);
    answer = "deny";
    expect(await saveDownload(stage("report.txt"), journal.bindings, saver)).toBe(
      'The page downloaded "report.txt", but it was not saved: The user denied this change on this computer.',
    );
    expect(asked).toMatchObject([{ kind: "change", action: "write", path: join(folder, ".surogates-results", "report.txt"), download: "page" }]);
    expect(readdirSync(join(folder, ".surogates-results"))).toEqual([]);
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
