import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync, existsSync, linkSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync,
  symlinkSync, utimesSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { landable } from "../src/files/land.js";
import { type Context, perform, revisionOf } from "../src/files/operations.js";

const SAGA = "0f6d1c5e-7a3b-4c2d-9e1f-0a1b2c3d4e5f";

let base: string;
let folder: string;
let copy: string;
let kept: string;
let context: Context;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "land-")));
  folder = join(base, "Documents");
  copy = join(base, "data", "history", "k", "threads", "t1");
  kept = join(base, "data", "landings", "k");
  for (const dir of [folder, copy]) mkdirSync(dir, { recursive: true });
  context = { folder, home: join(base, "home"), env: {}, landing: { copy, kept } };
});

afterEach(() => {
  rmSync(base, { recursive: true, force: true });
});

// A file's blob id, as git names its bytes.
const blob = (data: string | Buffer) => createHash("sha1").update(`blob ${Buffer.byteLength(data)}\0`).update(data).digest("hex");
const land = (args: Record<string, unknown>, on = context) => perform("land", args, on, new AbortController().signal);
const ok = async (args: Record<string, unknown>) => {
  const outcome = await land(args);
  expect(outcome).toHaveProperty("ok");
  return (outcome as { ok: Record<string, unknown> }).ok;
};
const refused = async (args: Record<string, unknown>, on = context) => {
  const outcome = await land(args, on);
  expect(outcome).toHaveProperty("error");
  return (outcome as { error: { type: string; message: string } }).error;
};
// What the landing's look answers for *paths*, as [path, token] pairs.
const looked = async (...paths: string[]) => Object.fromEntries((await ok({ action: "revisions", paths })).revisions as Array<[string, string]>);
// The copy's version of *path*, and its apply over what the look saw.
const turn = (path: string, data: string) => {
  mkdirSync(join(copy, path, ".."), { recursive: true });
  writeFileSync(join(copy, path), data);
  return blob(data);
};
const apply = (step: number, path: string, before: string | null, after: string | null, expected: string) =>
  ({ action: "apply", saga: SAGA, step, path, before, after, expected });
// Nothing of a landing is left in the folder: no file of its own beside the user's.
const leftovers = (dir = folder): string[] => readdirSync(dir, { recursive: true, encoding: "utf8" }).filter((name) => name.includes(".surogate-"));

describe("a landing's look at the folder", () => {
  it("answers each file's revision, absent for none, and other for what is no plain file of the folder's", async () => {
    writeFileSync(join(folder, "Report.docx"), "v1");
    mkdirSync(join(folder, "notes"));
    symlinkSync(join(folder, "Report.docx"), join(folder, "link.docx"));
    writeFileSync(join(folder, "twice.txt"), "x");
    linkSync(join(folder, "twice.txt"), join(folder, "twice-again.txt"));
    symlinkSync(base, join(folder, "out"));
    expect(await looked("Report.docx", "new.txt", "notes/deep/new.txt", "notes", "link.docx", "twice.txt", "out/escaped.txt")).toEqual({
      "Report.docx": revisionOf(statSync(join(folder, "Report.docx"), { bigint: true })),
      "new.txt": "absent", "notes/deep/new.txt": "absent", notes: "other", "link.docx": "other", "twice.txt": "other", "out/escaped.txt": "other",
    });
  });

  it("is refused for a path that is not one inside the folder, and by a helper that is no landing's", async () => {
    for (const path of ["/etc/passwd", "../Documents/x", "a/../../x", "a//b", "./a", "", "a\0b", "a/"]) {
      expect(await refused({ action: "revisions", paths: [path] })).toMatchObject({ type: "value" });
    }
    const plain: Context = { folder, home: join(base, "home"), env: {} };
    expect(await refused({ action: "revisions", paths: ["a"] }, plain)).toEqual({ type: "unsupported", message: "This computer cannot do 'land' yet" });
  });
});

describe("a landing's apply", () => {
  it("puts the copy's file over the real file it looked at, keeps the replaced one outside the folder, and forgets it once recorded", async () => {
    writeFileSync(join(folder, "Report.docx"), "the report, v1");
    chmodSync(join(folder, "Report.docx"), 0o640);
    const before = blob("the report, v1");
    const after = turn("Report.docx", "the report, by the thread");
    const seen = await looked("Report.docx");
    expect(await ok(apply(1, "Report.docx", before, after, seen["Report.docx"]!))).toEqual({ path: "Report.docx", before, after, made: [] });
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe("the report, by the thread");
    // The replaced file's mode, one name, and nothing of the landing's own beside it.
    expect(statSync(join(folder, "Report.docx")).mode & 0o777).toBe(0o640);
    expect(statSync(join(folder, "Report.docx")).nlink).toBe(1);
    expect(readdirSync(folder)).toEqual(["Report.docx"]);
    expect(readFileSync(join(kept, SAGA, "1"), "utf8")).toBe("the report, v1");
    expect(statSync(join(kept, SAGA)).mode & 0o777).toBe(0o700);
    expect(await ok({ action: "forget", saga: SAGA })).toEqual({});
    expect(existsSync(join(kept, SAGA))).toBe(false);
  });

  it("makes a new file with the folders it needs, and deletes one with the folders it empties", async () => {
    const after = turn("threads/Draft A/outline.md", "outline\n");
    const made = await ok(apply(1, "threads/Draft A/outline.md", null, after, "absent"));
    expect(made).toEqual({ path: "threads/Draft A/outline.md", before: null, after, made: ["threads/Draft A", "threads"] });
    expect(readFileSync(join(folder, "threads", "Draft A", "outline.md"), "utf8")).toBe("outline\n");
    const seen = await looked("threads/Draft A/outline.md");
    // The thread deleted it in its copy: only then does its landing delete the folder's.
    rmSync(join(copy, "threads", "Draft A", "outline.md"));
    expect(await ok(apply(2, "threads/Draft A/outline.md", after, null, seen["threads/Draft A/outline.md"]!))).toMatchObject({ after: null });
    expect(readdirSync(folder)).toEqual([]);
    // A deletion of a file already gone is done.
    expect(await ok(apply(3, "threads/Draft A/outline.md", after, null, "absent"))).toMatchObject({ after: null });
  });

  it.each([
    ["saved in place by an editor", (path: string) => writeFileSync(path, "saved by you, in place")],
    ["saved by a rename over it", (path: string) => {
      writeFileSync(`${path}.new`, "saved by you, by rename");
      renameSync(`${path}.new`, path);
    }],
    ["touched, its bytes the same", (path: string) => utimesSync(path, new Date(), new Date(Date.now() + 5_000))],
    ["deleted", (path: string) => rmSync(path)],
    ["replaced by a folder", (path: string) => {
      rmSync(path);
      mkdirSync(path);
    }],
    ["replaced by a link to the file it was", (path: string) => {
      renameSync(path, `${path}.moved`);
      symlinkSync(`${path}.moved`, path);
    }],
  ])("never replaces a file the user %s since the landing looked", async (_how, save) => {
    writeFileSync(join(folder, "Report.docx"), "the report, v1");
    const after = turn("Report.docx", "the report, by the thread");
    const seen = await looked("Report.docx");
    save(join(folder, "Report.docx"));
    const was = existsSync(join(folder, "Report.docx")) && lstatSync(join(folder, "Report.docx")).isFile() ? readFileSync(join(folder, "Report.docx"), "utf8") : null;
    expect(await refused(apply(1, "Report.docx", blob("the report, v1"), after, seen["Report.docx"]!))).toEqual({
      type: "conflict", message: "Report.docx changed in the folder while this landing ran, so it was not replaced",
    });
    if (was !== null) expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe(was);
    expect(leftovers()).toEqual([]);
  });

  it("deletes no file the thread's copy still holds, whatever the landing is told: a landing deletes only what its thread did", async () => {
    writeFileSync(join(folder, "Budget.xlsx"), "yours");
    writeFileSync(join(folder, "kept.txt"), "yours too");
    turn("Budget.xlsx", "yours");
    mkdirSync(join(copy, "kept.txt"));
    const seen = await looked("Budget.xlsx", "kept.txt");
    expect(await refused(apply(1, "Budget.xlsx", blob("yours"), null, seen["Budget.xlsx"]!))).toEqual({
      type: "stale", message: "Budget.xlsx is still in the thread's copy, so it was not deleted from the folder",
    });
    // Anything at its name in the copy counts, a folder too.
    expect(await refused(apply(2, "kept.txt", blob("yours too"), null, seen["kept.txt"]!))).toMatchObject({ type: "stale" });
    expect([readFileSync(join(folder, "Budget.xlsx"), "utf8"), readFileSync(join(folder, "kept.txt"), "utf8")]).toEqual(["yours", "yours too"]);
    expect(existsSync(join(kept, SAGA))).toBe(false);
    expect(leftovers()).toEqual([]);
  });

  it("never writes where a file was made since the landing looked, nor deletes one saved since", async () => {
    const after = turn("new.txt", "the thread's");
    writeFileSync(join(folder, "new.txt"), "made by you meanwhile");
    expect(await refused(apply(1, "new.txt", null, after, "absent"))).toMatchObject({ type: "conflict" });
    expect(readFileSync(join(folder, "new.txt"), "utf8")).toBe("made by you meanwhile");
    const seen = await looked("new.txt");
    writeFileSync(join(folder, "new.txt"), "saved again");
    expect(await refused(apply(2, "new.txt", blob("made by you meanwhile"), null, seen["new.txt"]!))).toMatchObject({ type: "conflict" });
    expect(readFileSync(join(folder, "new.txt"), "utf8")).toBe("saved again");
    expect(leftovers()).toEqual([]);
  });

  it.each([
    ["a file saved", "the file, v1"],
    ["a file made where there was none", null],
  ])("never replaces %s while its apply writes: the user's is in the folder after, and the apply is refused", { timeout: 30_000 }, async (_what, was) => {
    const target = join(folder, "big.bin");
    if (was !== null) writeFileSync(target, was);
    // Large enough that its copy takes a while: the save lands between the apply's first look and its last.
    const data = Buffer.alloc(96 * 1024 * 1024, "t");
    writeFileSync(join(copy, "big.bin"), data);
    const seen = await looked("big.bin");
    // Another process, as the user's editor is: it saves as soon as the landing's own file appears beside the target.
    const saver = spawn(process.execPath, ["-e", `
      const fs = require("node:fs");
      const until = Date.now() + 20000;
      (function wait() {
        if (fs.readdirSync(${JSON.stringify(folder)}).some((name) => name.startsWith(".surogate-"))) {
          fs.writeFileSync(${JSON.stringify(target)}, "saved by you while it landed");
          process.exit(0);
        }
        if (Date.now() > until) process.exit(3);
        setImmediate(wait);
      })();
    `], { stdio: "ignore" });
    const exited = new Promise<number | null>((resolve) => saver.once("exit", resolve));
    await new Promise((resolve) => setTimeout(resolve, 300));
    const outcome = await land(apply(1, "big.bin", was === null ? null : blob(was), blob(data), seen["big.bin"]!));
    expect(await exited).toBe(0);
    expect(outcome).toEqual({ error: { type: "conflict", message: "big.bin changed in the folder while this landing ran, so it was not replaced" } });
    expect(readFileSync(target, "utf8")).toBe("saved by you while it landed");
    expect(leftovers()).toEqual([]);
    expect(existsSync(join(kept, SAGA, "1")) || existsSync(join(kept, SAGA, "1.json"))).toBe(false);
  });

  it("lands only the bytes its turn committed: a copy changed since, or a source that is no plain file of the copy's, is refused", async () => {
    writeFileSync(join(folder, "a.txt"), "v1");
    const after = turn("a.txt", "committed");
    const seen = await looked("a.txt", "b.txt", "c.txt", "d/e.txt");
    writeFileSync(join(copy, "a.txt"), "written after the commit");
    expect(await refused(apply(1, "a.txt", blob("v1"), after, seen["a.txt"]!))).toEqual({
      type: "stale", message: "a.txt changed in the thread's copy after its turn was committed, so it was not landed",
    });
    // A link in the copy to a file outside it, by its last name or by a folder on its way.
    writeFileSync(join(base, "secret"), "outside the copy");
    symlinkSync(join(base, "secret"), join(copy, "b.txt"));
    symlinkSync(base, join(copy, "d"));
    writeFileSync(join(base, "e.txt"), "outside the copy");
    for (const [step, path] of [[2, "b.txt"], [3, "d/e.txt"]] as const) {
      expect(await refused(apply(step, path, null, blob("outside the copy"), "absent"))).toMatchObject({ type: "sandbox" });
    }
    expect(await refused(apply(4, "c.txt", null, blob("nothing"), "absent"))).toMatchObject({ type: "os", code: "ENOENT" });
    expect(readFileSync(join(folder, "a.txt"), "utf8")).toBe("v1");
    expect(readdirSync(folder)).toEqual(["a.txt"]);
  });

  it("reads nothing through a link left where the thread's copy is, to land a file or to delete one", async () => {
    // The guest writes the place the copy is in: a link in the copy's stead names whatever it likes.
    mkdirSync(join(base, "elsewhere"));
    writeFileSync(join(base, "elsewhere", "note.txt"), "outside every copy");
    writeFileSync(join(folder, "old.txt"), "yours");
    const seen = await looked("old.txt");
    rmSync(copy, { recursive: true });
    symlinkSync(join(base, "elsewhere"), copy);
    const refusal = { type: "sandbox", message: "This thread's copy is not a folder of the app's own" };
    expect(await refused(apply(1, "note.txt", null, blob("outside every copy"), "absent"))).toEqual(refusal);
    // Nor is the folder it leads to asked whether the thread deleted a file.
    expect(await refused(apply(2, "old.txt", blob("yours"), null, seen["old.txt"]!))).toEqual(refusal);
    expect(readdirSync(folder)).toEqual(["old.txt"]);
    expect(existsSync(kept)).toBe(false);
  });

  it("never writes through a link in the folder, to a hard-linked file, or to a name whose change runs code on this computer", async () => {
    mkdirSync(join(base, "elsewhere"));
    symlinkSync(join(base, "elsewhere"), join(folder, "linked"));
    writeFileSync(join(base, "elsewhere", "old.txt"), "outside the folder");
    writeFileSync(join(folder, "twice.txt"), "x");
    linkSync(join(folder, "twice.txt"), join(base, "elsewhere", "twice.txt"));
    mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
    const after = turn("f", "the thread's");
    for (const path of ["linked/new.txt", "linked/old.txt"]) {
      turn(path, "the thread's");
      expect(await refused(apply(1, path, null, after, "absent"))).toMatchObject({ type: "sandbox" });
    }
    turn("twice.txt", "the thread's");
    expect(await refused(apply(2, "twice.txt", blob("x"), after, revisionOf(statSync(join(folder, "twice.txt"), { bigint: true }))))).toMatchObject({ type: "conflict" });
    for (const path of [".git/hooks/pre-commit", ".git/config", ".vscode/tasks.json", ".claude/commands/x.md", "sub/.gitmodules", ".bashrc"]) {
      turn(path, "the thread's");
      expect(await refused(apply(3, path, null, after, "absent"))).toMatchObject({ type: "sandbox", message: expect.stringContaining("protected in this folder") });
    }
    expect(readdirSync(join(base, "elsewhere")).sort()).toEqual(["old.txt", "twice.txt"]);
    expect(readFileSync(join(base, "elsewhere", "old.txt"), "utf8")).toBe("outside the folder");
    expect(readFileSync(join(folder, "twice.txt"), "utf8")).toBe("x");
    expect(readdirSync(join(folder, ".git", "hooks"))).toEqual([]);
    expect(existsSync(join(folder, ".vscode")) || existsSync(join(folder, ".claude")) || existsSync(join(folder, "sub"))).toBe(false);
  });

  it("is refused whole for arguments that are not an apply's", async () => {
    const after = turn("a.txt", "x");
    const good = apply(1, "a.txt", null, after, "absent");
    for (const change of [
      { saga: "../x" }, { saga: "" }, { saga: ".." }, { step: -1 }, { step: 1.5 }, { step: "1" }, { path: "../a.txt" }, { path: "/a.txt" },
      { after: "not an id" }, { before: "--x" }, { expected: "whenever" }, { expected: 7 }, { before: null, after: null }, { action: "replace" },
    ]) {
      expect(await refused({ ...good, ...change })).toMatchObject({ type: "value" });
    }
    expect(readdirSync(folder)).toEqual([]);
    expect(existsSync(kept)).toBe(false);
  });
});

describe("a landing's put-back", () => {
  it("puts back each file the landing wrote, takes away what it made, and brings back what it deleted", async () => {
    writeFileSync(join(folder, "Report.docx"), "the report, v1");
    writeFileSync(join(folder, "old.txt"), "to be deleted");
    const report = turn("Report.docx", "the report, by the thread");
    const fresh = turn("threads/A/new.md", "new\n");
    const seen = await looked("Report.docx", "old.txt");
    await ok(apply(1, "Report.docx", blob("the report, v1"), report, seen["Report.docx"]!));
    await ok(apply(2, "threads/A/new.md", null, fresh, "absent"));
    await ok(apply(3, "old.txt", blob("to be deleted"), null, seen["old.txt"]!));
    expect(readdirSync(folder).sort()).toEqual(["Report.docx", "threads"]);
    // In reverse, as a saga compensates.
    for (const [step, path] of [[3, "old.txt"], [2, "threads/A/new.md"], [1, "Report.docx"]] as const) {
      expect(await ok({ action: "unapply", saga: SAGA, step, path })).toEqual({ path, put_back: true });
    }
    expect(readdirSync(folder).sort()).toEqual(["Report.docx", "old.txt"]);
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe("the report, v1");
    expect(readFileSync(join(folder, "old.txt"), "utf8")).toBe("to be deleted");
    expect(statSync(join(folder, "Report.docx")).nlink).toBe(1);
    // Safe to repeat, and nothing is kept once it is put back.
    expect(await ok({ action: "unapply", saga: SAGA, step: 1, path: "Report.docx" })).toEqual({ path: "Report.docx", put_back: false });
    expect(existsSync(join(kept, SAGA))).toBe(false);
    expect(leftovers()).toEqual([]);
  });

  it("makes a folder its deletion emptied again as it was, private if it was", async () => {
    mkdirSync(join(folder, "private", "deep"), { recursive: true });
    chmodSync(join(folder, "private"), 0o700);
    chmodSync(join(folder, "private", "deep"), 0o750);
    writeFileSync(join(folder, "private", "deep", "only.txt"), "x");
    const seen = await looked("private/deep/only.txt");
    await ok(apply(1, "private/deep/only.txt", blob("x"), null, seen["private/deep/only.txt"]!));
    expect(readdirSync(folder)).toEqual([]);
    expect(await ok({ action: "unapply", saga: SAGA, step: 1, path: "private/deep/only.txt" })).toEqual({ path: "private/deep/only.txt", put_back: true });
    expect(readFileSync(join(folder, "private", "deep", "only.txt"), "utf8")).toBe("x");
    expect([join(folder, "private"), join(folder, "private", "deep")].map((dir) => statSync(dir).mode & 0o777)).toEqual([0o700, 0o750]);
  });

  it("leaves a file changed since the landing wrote it, and says so", async () => {
    writeFileSync(join(folder, "Report.docx"), "the report, v1");
    const after = turn("Report.docx", "the report, by the thread");
    const fresh = turn("new.md", "new\n");
    const seen = await looked("Report.docx");
    await ok(apply(1, "Report.docx", blob("the report, v1"), after, seen["Report.docx"]!));
    await ok(apply(2, "new.md", null, fresh, "absent"));
    writeFileSync(join(folder, "Report.docx"), "saved by you after the landing wrote it");
    rmSync(join(folder, "new.md"));
    writeFileSync(join(folder, "new.md"), "made again by you");
    for (const [step, path] of [[1, "Report.docx"], [2, "new.md"]] as const) {
      expect(await refused({ action: "unapply", saga: SAGA, step, path })).toEqual({
        type: "conflict", message: `${path} changed after the landing wrote it, so it was not put back`,
      });
    }
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe("saved by you after the landing wrote it");
    expect(readFileSync(join(folder, "new.md"), "utf8")).toBe("made again by you");
    // The version the landing replaced is still kept, for whoever settles it.
    expect(readFileSync(join(kept, SAGA, "1"), "utf8")).toBe("the report, v1");
  });

  it("does nothing for a step that wrote nothing, and refuses one whose record names another file", async () => {
    writeFileSync(join(folder, "a.txt"), "v1");
    expect(await ok({ action: "unapply", saga: SAGA, step: 1, path: "a.txt" })).toEqual({ path: "a.txt", put_back: false });
    const after = turn("a.txt", "the thread's");
    await ok(apply(1, "a.txt", blob("v1"), after, (await looked("a.txt"))["a.txt"]!));
    expect(await refused({ action: "unapply", saga: SAGA, step: 1, path: "b.txt" })).toMatchObject({ type: "value" });
    expect(readFileSync(join(folder, "a.txt"), "utf8")).toBe("the thread's");
  });

  it("keeps and brings back the replaced file, with the time it was saved, when the app's data is on another filesystem than the folder", async () => {
    const other = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/dev/shm", "land-kept-"));
    try {
      // Only where this computer has a second filesystem to keep it on.
      if (statSync(other).dev === statSync(folder).dev) return;
      context = { ...context, landing: { copy, kept: other } };
      writeFileSync(join(folder, "a.txt"), "v1");
      const saved = new Date("2020-09-13T12:26:40Z");
      utimesSync(join(folder, "a.txt"), saved, saved);
      // One the user may only read: a copy of it is as much theirs.
      chmodSync(join(folder, "a.txt"), 0o440);
      const after = turn("a.txt", "the thread's");
      await ok(apply(1, "a.txt", blob("v1"), after, (await looked("a.txt"))["a.txt"]!));
      expect(readFileSync(join(other, SAGA, "1"), "utf8")).toBe("v1");
      expect(statSync(join(other, SAGA, "1")).mtimeMs).toBe(saved.getTime());
      expect(await ok({ action: "unapply", saga: SAGA, step: 1, path: "a.txt" })).toEqual({ path: "a.txt", put_back: true });
      expect(readFileSync(join(folder, "a.txt"), "utf8")).toBe("v1");
      // A copy across filesystems is made now; the file it is of was not.
      expect(statSync(join(folder, "a.txt")).mtimeMs).toBe(saved.getTime());
      expect(statSync(join(folder, "a.txt")).mode & 0o777).toBe(0o440);
      expect(leftovers()).toEqual([]);
    } finally {
      rmSync(other, { recursive: true, force: true });
    }
  });
});

describe("a landing's file, checked before it is asked of the helper", () => {
  it("is what a landing may write: no protected name, and no path but one inside the folder", () => {
    expect(["Report.docx", "threads/Draft A/outline.md", ".github/workflows/ci.yml", "a..b/c", ".gitignore"].map(landable)).toEqual([true, true, true, true, true]);
    expect([".git/hooks/pre-commit", ".vscode/settings.json", "x/.gitmodules", ".claude/agents/a.md", "../x", "/x", "a//b", "", ".git"].map(landable))
      .toEqual([false, false, false, false, false, false, false, false, false]);
  });
});
