import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync, existsSync, linkSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync,
  symlinkSync, truncateSync, utimesSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { landable } from "../src/files/land.js";
import { fileToolsMissing } from "../src/hosts/policy.js";
import { type Context, OWN_FILE, perform, revisionOf } from "../src/files/operations.js";

const SAGA = "0f6d1c5e-7a3b-4c2d-9e1f-0a1b2c3d4e5f";
const HELPER = fileURLToPath(new URL("../dist/files/helper.js", import.meta.url));
// Loaded into a helper before its own code: it ends the process as a kill does, at the call of an fs function, by its
// number among those whose argument matches. Nothing after that call runs, no handler and no cleanup. Given something
// to do there instead, it does that before the call: what another program did meanwhile, or what the filesystem answers.
const CUT = `data:text/javascript,${encodeURIComponent(`
  import fs from "node:fs";
  import { syncBuiltinESMExports } from "node:module";
  const [name, argument, pattern, nth, instead] = JSON.parse(process.env.LAND_CUT);
  const real = fs[name];
  let seen = 0;
  fs[name] = (...args) => {
    if (new RegExp(pattern).test(String(args[argument])) && (++seen === nth || nth === 0)) {
      if (instead) new Function("fs", "refuse", instead)(fs, (code) => { throw Object.assign(new Error(code), { code, errno: -1, syscall: name }); });
      else {
        process.kill(process.pid, "SIGKILL");
        for (;;);
      }
    }
    return real(...args);
  };
  syncBuiltinESMExports();
`)}`;
// Where a helper is cut: before the call of this fs function, numbered from 1 among those whose argument at this place matches.
// With *instead*, the helper lives, and runs that there: `refuse(code)` answers the call as the filesystem would. 0 is every such call.
type Cut = [name: string, argument: number, pattern: string, nth: number, instead?: string];

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
const unapply = (step: number, path: string) => ({ action: "unapply", saga: SAGA, step, path });

// A landing's helper as the app starts one, outside any sandbox: on the folder, given the thread's copy and where
// replaced files are kept, or what *given* says instead. It is asked *requests* once it is ready, and runs until it has
// answered them or is ended: what it answered, and the signal that ended it.
async function helper(
  requests: Array<Record<string, unknown>>, cut?: Cut, given: Record<string, string> = { SUROGATE_COPY: copy, SUROGATE_KEPT: kept },
): Promise<{ answers: unknown[]; signal: NodeJS.Signals | null }> {
  const child = spawn(process.execPath, [...(cut ? ["--import", CUT] : []), HELPER], {
    env: { SUROGATE_FOLDER: folder, HOME: base, PATH: "/usr/bin:/bin", ...given, ...(cut ? { LAND_CUT: JSON.stringify(cut) } : {}) },
    stdio: ["pipe", "pipe", "inherit"],
  });
  // A helper that was killed takes no more.
  child.stdin.on("error", () => {});
  const answers: unknown[] = [];
  const ended = new Promise<NodeJS.Signals | null>((resolve) => child.once("exit", (_code, signal) => resolve(signal)));
  createInterface({ input: child.stdout }).on("line", (line) => {
    const said = JSON.parse(line) as { ready?: boolean; outcome?: unknown };
    if (said.ready) for (const [id, args] of requests.entries()) child.stdin.write(`${JSON.stringify({ id: String(id), kind: "land", args })}\n`);
    else answers.push(said.outcome);
    if (answers.length === requests.length) child.stdin.end();
  });
  const bound = setTimeout(() => child.kill("SIGKILL"), 20_000);
  const signal = await ended;
  clearTimeout(bound);
  return { answers, signal };
}
// A landing's helper that stays, asked one thing at a time, so that the folder can change between two answers.
function session(cut?: Cut) {
  const child = spawn(process.execPath, [...(cut ? ["--import", CUT] : []), HELPER], {
    env: { SUROGATE_FOLDER: folder, HOME: base, PATH: "/usr/bin:/bin", SUROGATE_COPY: copy, SUROGATE_KEPT: kept, ...(cut ? { LAND_CUT: JSON.stringify(cut) } : {}) },
    stdio: ["pipe", "pipe", "inherit"],
  });
  child.stdin.on("error", () => {});
  const waiting: Array<(outcome: unknown) => void> = [];
  const ready = new Promise<void>((resolve) => {
    createInterface({ input: child.stdout }).on("line", (line) => {
      const said = JSON.parse(line) as { ready?: boolean; outcome?: unknown };
      if (said.ready) resolve();
      else waiting.shift()?.(said.outcome);
    });
  });
  const bound = setTimeout(() => child.kill("SIGKILL"), 30_000);
  return {
    async ask(args: Record<string, unknown>): Promise<unknown> {
      await ready;
      const answered = new Promise<unknown>((resolve) => waiting.push(resolve));
      child.stdin.write(`${JSON.stringify({ id: "1", kind: "land", args })}\n`);
      return answered;
    },
    async end(): Promise<void> {
      clearTimeout(bound);
      const ended = new Promise((resolve) => child.once("exit", resolve));
      child.stdin.end();
      await ended;
    },
  };
}
// The helper's next start, asked what it put back.
const restart = async () => (await helper([{ action: "recover" }])).answers[0];

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

describe("a landing cut short by a kill", () => {
  const V1 = "the report, v1";
  // Where the user's bytes are, wherever a cut left them: at the file's name, beside it under a name of the landing's, or kept.
  const holders = (path: string, data: string): string[] =>
    [join(folder, path), ...leftovers().map((name) => join(folder, name)), join(kept, SAGA, "1")]
      .filter((at) => existsSync(at) && lstatSync(at).isFile() && readFileSync(at, "utf8") === data);

  it.each<[string, Cut, boolean]>([
    ["before anything of it is written down", ["renameSync", 0, "\\.json\\.new$", 1], false],
    ["while its own file is half written", ["writeSync", 0, ".", 2], false],
    ["with its own file staged", ["renameSync", 0, "\\.json\\.new$", 2], false],
    ["before the real file is moved aside", ["renameSync", 0, "/Report\\.docx$", 1], false],
    ["with its own file under a second name, to see that the folder gives one", ["unlinkSync", 0, "\\.surogate-", 1], false],
    ["between its two renames, the real file moved aside and its name empty", ["linkSync", 1, "/Report\\.docx$", 1], true],
    ["with its own file at the name and still beside it", ["unlinkSync", 0, "\\.surogate-", 2], true],
    ["before the replaced file is kept in the app's data", ["renameSync", 0, "\\.surogate-", 1], true],
    ["with it written down that the real file is about to leave its name", ["renameSync", 0, "\\.json\\.new$", 3], false],
    ["before its record says it ended", ["renameSync", 0, "\\.json\\.new$", 4], true],
  ])("loses no file an apply was replacing when it is killed %s: the helper's next start puts the user's back at its name", { timeout: 60_000 }, async (_where, cut, moved) => {
    const target = join(folder, "Report.docx");
    writeFileSync(target, V1);
    chmodSync(target, 0o640);
    const was = lstatSync(target, { bigint: true });
    // Three pieces, so that a cut can fall between two of them.
    const data = Buffer.alloc(3 * 1024 * 1024, "t");
    writeFileSync(join(copy, "Report.docx"), data);
    const seen = await looked("Report.docx");
    const killed = await helper([apply(1, "Report.docx", blob(V1), blob(data), seen["Report.docx"]!)], cut);
    expect(killed).toEqual({ answers: [], signal: "SIGKILL" });
    // The user's file is somewhere the helper finds it, with the user's bytes, whatever the cut.
    expect(holders("Report.docx", V1).length).toBeGreaterThan(0);
    expect(holders("Report.docx", V1).includes(target)).toBe(!moved);
    expect(await restart()).toEqual({ ok: { restored: moved ? ["Report.docx"] : [], beside: [], lost: [], unread: [] } });
    // The very file, not a copy of it: its inode, its time and its mode.
    const now = lstatSync(target, { bigint: true });
    expect([readFileSync(target, "utf8"), now.ino, now.mtimeNs, now.mode, now.nlink]).toEqual([V1, was.ino, was.mtimeNs, was.mode, 1n]);
    expect(readdirSync(folder)).toEqual(["Report.docx"]);
    expect(existsSync(join(kept, SAGA))).toBe(false);
  });

  it.each<[string, Cut]>([
    ["with its file staged", ["renameSync", 0, "\\.json\\.new$", 2]],
    ["with its file at the name and still beside it", ["unlinkSync", 0, "\\.surogate-", 1]],
    ["before its record says it ended", ["renameSync", 0, "\\.json\\.new$", 3]],
  ])("leaves nothing of a new file whose apply is killed %s: neither the file, nor the folders made for it", { timeout: 60_000 }, async (_where, cut) => {
    const after = turn("threads/A/new.md", "new\n");
    expect(await helper([apply(1, "threads/A/new.md", null, after, "absent")], cut)).toEqual({ answers: [], signal: "SIGKILL" });
    expect(await restart()).toEqual({ ok: { restored: [], beside: [], lost: [], unread: [] } });
    expect(readdirSync(folder)).toEqual([]);
    expect(existsSync(join(kept, SAGA))).toBe(false);
  });

  it.each<[string, Cut]>([
    ["before the file is moved aside", ["renameSync", 0, "/old\\.txt$", 1]],
    ["with the file moved aside", ["renameSync", 0, "\\.surogate-", 1]],
    ["before its record says it ended", ["renameSync", 0, "\\.json\\.new$", 3]],
  ])("brings back a file whose deletion is killed %s", { timeout: 60_000 }, async (_where, cut) => {
    mkdirSync(join(folder, "private"), { mode: 0o700 });
    writeFileSync(join(folder, "private", "old.txt"), "to be deleted");
    const was = lstatSync(join(folder, "private", "old.txt"), { bigint: true });
    mkdirSync(join(copy, "private"));
    const seen = await looked("private/old.txt");
    expect(await helper([apply(1, "private/old.txt", blob("to be deleted"), null, seen["private/old.txt"]!)], cut)).toEqual({ answers: [], signal: "SIGKILL" });
    await restart();
    const now = lstatSync(join(folder, "private", "old.txt"), { bigint: true });
    expect([readFileSync(join(folder, "private", "old.txt"), "utf8"), now.ino, now.mtimeNs]).toEqual(["to be deleted", was.ino, was.mtimeNs]);
    expect(leftovers()).toEqual([]);
    expect(existsSync(join(kept, SAGA))).toBe(false);
  });

  it.each<[string, Cut]>([
    ["with what it replaced given a second name beside its own, to see that the folder gives it one", ["unlinkSync", 0, "\\.surogate-", 1]],
    ["before the landing's file is moved out", ["renameSync", 0, "/Report\\.docx$", 1]],
    ["with the landing's file moved out and the name empty", ["linkSync", 1, "/Report\\.docx$", 1]],
    ["with the real file back and the landing's still beside it", ["unlinkSync", 0, "\\.surogate-", 2]],
    ["before what the step kept is dropped", ["unlinkSync", 0, "/1(\\.json)?$", 1]],
    ["with what the step kept dropped, and its record not yet", ["unlinkSync", 0, "/1(\\.json)?$", 2]],
  ])("ends a put-back that is killed %s: the user's file is at its name after the helper's next start", { timeout: 60_000 }, async (_where, cut) => {
    const target = join(folder, "Report.docx");
    writeFileSync(target, V1);
    const was = lstatSync(target, { bigint: true });
    const after = turn("Report.docx", "the report, by the thread");
    const seen = await looked("Report.docx");
    expect((await helper([apply(1, "Report.docx", blob(V1), after, seen["Report.docx"]!)])).answers).toMatchObject([{ ok: { path: "Report.docx" } }]);
    expect(readFileSync(target, "utf8")).toBe("the report, by the thread");
    expect(await helper([unapply(1, "Report.docx")], cut)).toEqual({ answers: [], signal: "SIGKILL" });
    expect(holders("Report.docx", V1).length).toBeGreaterThan(0);
    await restart();
    const now = lstatSync(target, { bigint: true });
    expect([readFileSync(target, "utf8"), now.ino, now.mtimeNs, now.nlink]).toEqual([V1, was.ino, was.mtimeNs, 1n]);
    expect(readdirSync(folder)).toEqual(["Report.docx"]);
    expect(existsSync(join(kept, SAGA))).toBe(false);
  });

  // An apply killed between its two renames: the user's file is beside its name, which holds nothing.
  const cutBetween = async (path = "Report.docx") => {
    mkdirSync(join(folder, path, ".."), { recursive: true });
    writeFileSync(join(folder, path), V1);
    const after = turn(path, "the report, by the thread");
    const seen = await looked(path);
    const name = path.split("/").at(-1)!.replaceAll(".", "\\.");
    expect(await helper([apply(1, path, blob(V1), after, seen[path]!)], ["linkSync", 1, `/${name}$`, 1])).toEqual({ answers: [], signal: "SIGKILL" });
    expect(existsSync(join(folder, path))).toBe(false);
  };

  it("puts nothing back before it is asked, so it is ready at once, and puts the file back in the first thing it is asked, before that looks at the folder", { timeout: 60_000 }, async () => {
    await cutBetween();
    expect(await helper([])).toEqual({ answers: [], signal: null });
    expect([existsSync(join(folder, "Report.docx")), leftovers().length]).toEqual([false, 2]);
    // A look is never answered "absent" for a file that is only beside its name.
    const { answers } = await helper([{ action: "revisions", paths: ["Report.docx"] }]);
    expect(answers).toEqual([{ ok: { revisions: [["Report.docx", revisionOf(statSync(join(folder, "Report.docx"), { bigint: true }))]] } }]);
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe(V1);
    expect(leftovers()).toEqual([]);
  });

  it("puts the file back when the helper that was putting it back is killed too", { timeout: 60_000 }, async () => {
    await cutBetween();
    expect(await helper([{ action: "recover" }], ["linkSync", 1, "/Report\\.docx$", 1])).toEqual({ answers: [], signal: "SIGKILL" });
    expect(await helper([{ action: "recover" }], ["unlinkSync", 0, "\\.surogate-", 1])).toEqual({ answers: [], signal: "SIGKILL" });
    expect(await restart()).toEqual({ ok: { restored: [], beside: [], lost: [], unread: [] } });
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe(V1);
    expect(statSync(join(folder, "Report.docx")).nlink).toBe(1);
    expect(readdirSync(folder)).toEqual(["Report.docx"]);
    expect(existsSync(join(kept, SAGA))).toBe(false);
  });

  it("never puts the file back over one the user has made at its name since: it goes beside it, under a name that says what it is, and is reported", { timeout: 60_000 }, async () => {
    await cutBetween("docs/Report.docx");
    writeFileSync(join(folder, "docs", "Report.docx"), "made by you since");
    // The first name that says so is taken too.
    writeFileSync(join(folder, "docs", "Report (kept by Surogate).docx"), "yours as well");
    expect(await restart()).toEqual({ ok: { restored: [], beside: [["docs/Report.docx", "docs/Report (kept by Surogate 2).docx"]], lost: [], unread: [] } });
    expect(Object.fromEntries(readdirSync(join(folder, "docs")).map((name) => [name, readFileSync(join(folder, "docs", name), "utf8")]))).toEqual({
      "Report.docx": "made by you since", "Report (kept by Surogate).docx": "yours as well", "Report (kept by Surogate 2).docx": V1,
    });
    expect(existsSync(join(kept, SAGA))).toBe(false);
    // Once: the next start finds nothing to do, and moves nothing again.
    expect(await restart()).toEqual({ ok: { restored: [], beside: [], lost: [], unread: [] } });
    expect(readdirSync(join(folder, "docs"))).toHaveLength(3);
  });

  it("has no reader of the folder take what a cut left for a file of the user's: not the helper's own listing, and not the history's pickup", { timeout: 60_000 }, async () => {
    writeFileSync(join(folder, "Report.docx"), V1);
    const data = Buffer.alloc(3 * 1024 * 1024, "t");
    writeFileSync(join(copy, "Report.docx"), data);
    const seen = await looked("Report.docx");
    // Killed with both of its own names in the folder: its staged file, and the user's moved aside.
    await helper([apply(1, "Report.docx", blob(V1), blob(data), seen["Report.docx"]!)], ["linkSync", 1, "/Report\\.docx$", 1]);
    const own = readdirSync(folder);
    expect(own).toHaveLength(2);
    expect(own.every((name) => OWN_FILE.test(name))).toBe(true);
    // A chat's own helper on the folder, which puts nothing back: its walk and its listing name neither as a new file.
    const plain: Context = { folder, home: base, env: {} };
    const asked = (kind: string, args: Record<string, unknown>) => perform(kind, args, plain, new AbortController().signal);
    expect(await asked("walk", { key: folder, skip: [], skip_top: [], skip_hidden: false, since: null })).toMatchObject({ ok: { files: [], truncated: false } });
    expect(await asked("list_dir", { key: folder })).toEqual({ ok: [] });
    // The pickup is git's, in the guest, by the history's excludes: "*.tmp" there is what keeps both names out of
    // it, so neither is recorded as a file the user made. That the user's file is not recorded as deleted is the
    // order's: a landing's first step, its hold, puts the file back before its pickup is asked; and where no landing's
    // host is on the folder, the app puts it back before a pickup or a turn's open (hosts/tool-hosts.ts, recoverBefore).
    const history = readFileSync(new URL("../../surogates/sandbox/history.py", import.meta.url), "utf8");
    expect(/^HISTORY_EXCLUDES = [^]*?^\] \+ /m.exec(history)?.[0]).toContain('"*.tmp"');
    expect(await restart()).toEqual({ ok: { restored: ["Report.docx"], beside: [], lost: [], unread: [] } });
    expect(await asked("walk", { key: folder, skip: [], skip_top: [], skip_hidden: false, since: null })).toMatchObject({ ok: { files: [["Report.docx", V1.length]] } });
  });

  it("follows nothing to a file whose folder went, or was made a link, after the cut: it says the file was lost with it, and keeps the record that names it", { timeout: 60_000 }, async () => {
    mkdirSync(join(base, "elsewhere"));
    writeFileSync(join(base, "elsewhere", "Report.docx"), "outside the folder");
    await cutBetween("docs/Report.docx");
    renameSync(join(folder, "docs"), join(base, "docs.taken"));
    symlinkSync(join(base, "elsewhere"), join(folder, "docs"));
    const own = readdirSync(join(base, "docs.taken")).find((name) => readFileSync(join(base, "docs.taken", name), "utf8") === V1);
    expect(await restart()).toEqual({ ok: { restored: [], beside: [], lost: [["docs/Report.docx", own]], unread: [] } });
    expect(readFileSync(join(base, "docs.taken", own!), "utf8")).toBe(V1);
    expect(readdirSync(join(base, "elsewhere"))).toEqual(["Report.docx"]);
    expect(readFileSync(join(base, "elsewhere", "Report.docx"), "utf8")).toBe("outside the folder");
    // Its record is all that names the file: kept, and said again at every start, and its landing is not forgotten.
    expect(readdirSync(join(kept, SAGA))).toEqual(["1.json"]);
    expect(await helper([{ action: "forget", saga: SAGA }, { action: "recover" }])).toMatchObject({
      answers: [{ error: { type: "conflict" } }, { ok: { lost: [["docs/Report.docx", own]] } }],
    });
  });

  it("acts on no record but one of its own writing: a file of the user's that one names as the landing's is left where it is", { timeout: 60_000 }, async () => {
    await cutBetween();
    writeFileSync(join(folder, "Budget.xlsx"), "yours");
    const record = join(kept, SAGA, "1.json");
    const written = JSON.parse(readFileSync(record, "utf8")) as Record<string, unknown>;
    const other = "1b2c3d4e-0000-4000-8000-000000000001";
    mkdirSync(join(kept, other));
    // As a damaged record could read: the user's own file named as the one moved aside, a path out of the folder, and no record at all.
    writeFileSync(join(kept, other, "1.json"), JSON.stringify({ ...written, aside: "Budget.xlsx" }));
    writeFileSync(join(kept, other, "2.json"), JSON.stringify({ ...written, path: "../Documents/Budget.xlsx" }));
    writeFileSync(join(kept, other, "3.json"), "{ not a record");
    writeFileSync(join(kept, other, "3"), "what a step kept");
    // Nor on one that names a file no landing may write, though a file of the landing's shape lies ready beside it.
    mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
    writeFileSync(join(folder, ".git", "hooks", written.aside as string), "#!/bin/sh\n");
    writeFileSync(join(kept, other, "4.json"), JSON.stringify({ ...written, path: ".git/hooks/pre-commit" }));
    // Nor is one that cannot even be opened taken for none.
    mkdirSync(join(kept, other, "5.json"));
    // Nor on a folder that is no saga's, whatever is in it.
    mkdirSync(join(kept, "not a saga"));
    writeFileSync(join(kept, "not a saga", "1.json"), JSON.stringify(written));
    expect(await restart()).toEqual({
      ok: { restored: ["Report.docx"], beside: [], lost: [], unread: [[other, 1, "Report.docx"], [other, 2, null], [other, 3, null], [other, 4, null], [other, 5, null]] },
    });
    expect(readFileSync(join(folder, "Budget.xlsx"), "utf8")).toBe("yours");
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe(V1);
    expect(readdirSync(folder).sort()).toEqual([".git", "Budget.xlsx", "Report.docx"]);
    expect(readdirSync(join(folder, ".git", "hooks"))).toEqual([written.aside]);
    // What it cannot read it does not remove: the file beside such a record may be all that is left of a user's.
    expect(readdirSync(join(kept, other)).sort()).toEqual(["1.json", "2.json", "3", "3.json", "4.json", "5.json"]);
    expect(readdirSync(join(kept, "not a saga"))).toEqual(["1.json"]);
  });

  it("puts the file back before a landing's first look or apply, in a helper that was not started as one", { timeout: 60_000 }, async () => {
    const target = join(folder, "Report.docx");
    writeFileSync(target, V1);
    const after = turn("Report.docx", "the report, by the thread");
    // Not asked of this process: nothing here has looked at the folder yet.
    const seen = revisionOf(statSync(target, { bigint: true }));
    await helper([apply(1, "Report.docx", blob(V1), after, seen)], ["linkSync", 1, "/Report\\.docx$", 1]);
    expect(existsSync(target)).toBe(false);
    expect((await looked("Report.docx"))["Report.docx"]).toBe(revisionOf(statSync(target, { bigint: true })));
    expect(await ok({ action: "recover" })).toEqual({ restored: ["Report.docx"], beside: [], lost: [], unread: [] });
  });

  it("removes what a forgetting was killed in the middle of: nothing of a landing that ended is kept for good", { timeout: 60_000 }, async () => {
    writeFileSync(join(folder, "Report.docx"), V1);
    const after = turn("Report.docx", "the report, by the thread");
    const seen = await looked("Report.docx");
    await helper([apply(1, "Report.docx", blob(V1), after, seen["Report.docx"]!)]);
    expect(await helper([{ action: "forget", saga: SAGA }], ["rmSync", 0, "forgotten", 1])).toEqual({ answers: [], signal: "SIGKILL" });
    expect(readdirSync(kept)).toHaveLength(1);
    await restart();
    expect(readdirSync(kept)).toEqual([]);
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe("the report, by the thread");
  });

  it("leaves no copy of a kept file in the folder when a put-back from another filesystem is killed", { timeout: 60_000 }, async () => {
    const here = kept;
    kept = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/dev/shm", "land-kept-"));
    try {
      if (statSync(kept).dev === statSync(folder).dev) return;
      context = { ...context, landing: { copy, kept } };
      const target = join(folder, "Report.docx");
      writeFileSync(target, V1);
      const after = turn("Report.docx", "the report, by the thread");
      const seen = await looked("Report.docx");
      await helper([apply(1, "Report.docx", blob(V1), after, seen["Report.docx"]!)]);
      // Its second link to the name: the first, from the other filesystem, is refused there, and the copy is the one linked in.
      expect(await helper([unapply(1, "Report.docx")], ["linkSync", 1, "/Report\\.docx$", 2])).toEqual({ answers: [], signal: "SIGKILL" });
      expect(leftovers()).toHaveLength(2);
      expect(await restart()).toEqual({ ok: { restored: ["Report.docx"], beside: [], lost: [], unread: [] } });
      expect([readdirSync(folder), readFileSync(target, "utf8")]).toEqual([["Report.docx"], V1]);
      expect(readdirSync(kept)).toEqual([]);
    } finally {
      rmSync(kept, { recursive: true, force: true });
      kept = here;
    }
  });

  // *test*, with what the folder's landings keep on another filesystem than the folder, where this computer has one: a
  // put-back then copies the file it brings back.
  const keptElsewhere = async (test: () => Promise<void>) => {
    const here = kept;
    kept = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/dev/shm", "land-kept-"));
    try {
      if (statSync(kept).dev === statSync(folder).dev) return;
      context = { ...context, landing: { copy, kept } };
      await test();
    } finally {
      rmSync(kept, { recursive: true, force: true });
      kept = here;
    }
  };
  // A file of the user's, of *bytes* each its own, saved at a time of its own with a mode of its own; the thread's
  // landed over it, and what it replaced kept. Its bytes, and when they were saved.
  const replaced = async (bytes: number): Promise<{ target: string; mine: Buffer; saved: Date }> => {
    const target = join(folder, "Report.bin");
    const mine = Buffer.alloc(bytes);
    for (let at = 0; at < bytes; at += 4) mine.writeUInt32LE(at, at);
    writeFileSync(target, mine);
    chmodSync(target, 0o640);
    const saved = new Date("2020-09-13T12:26:40Z");
    utimesSync(target, saved, saved);
    const after = turn("Report.bin", "the report, by the thread");
    const seen = await looked("Report.bin");
    expect((await helper([apply(1, "Report.bin", blob(mine), after, seen["Report.bin"]!)])).answers).toMatchObject([{ ok: { path: "Report.bin" } }]);
    return { target, mine, saved };
  };
  // The step's record, as its helper last wrote it.
  const recorded = () => JSON.parse(readFileSync(join(kept, SAGA, "1.json"), "utf8")) as { back: string | null; copied: number };

  it("keeps the landing's file at the name while what it replaced is copied back from another filesystem: the name is not empty for as long as the copy takes", { timeout: 60_000 }, () => keptElsewhere(async () => {
    const { target, mine, saved } = await replaced(3 * 1024 * 1024);
    // Killed with the copy made, before it is dated: the user's bytes beside the name, whole, and the landing's at it.
    expect(await helper([unapply(1, "Report.bin")], ["futimesSync", 0, ".", 1])).toEqual({ answers: [], signal: "SIGKILL" });
    expect(readFileSync(target, "utf8")).toBe("the report, by the thread");
    const { back } = recorded();
    expect(readFileSync(join(folder, back!)).equals(mine)).toBe(true);
    expect(await restart()).toEqual({ ok: { restored: ["Report.bin"], beside: [], lost: [], unread: [] } });
    expect(readFileSync(target).equals(mine)).toBe(true);
    expect([statSync(target).mode & 0o777, statSync(target).mtimeMs, statSync(target).nlink]).toEqual([0o640, saved.getTime(), 1]);
    expect([readdirSync(folder), readdirSync(kept)]).toEqual([["Report.bin"], []]);
  }));

  it("goes on with a put-back's copy from another filesystem from as far as its record says it got, when it is cut, and copies none of that again", { timeout: 60_000 }, () => keptElsewhere(async () => {
    const { target, mine, saved } = await replaced(40 * 1024 * 1024);
    // Killed in its copy, once its record says part of the copy is on disk.
    expect(await helper([unapply(1, "Report.bin")], ["renameSync", 0, "\\.json\\.new$", 4])).toEqual({ answers: [], signal: "SIGKILL" });
    const { back, copied } = recorded();
    expect([copied > 0, copied < mine.length, statSync(join(folder, back!)).size >= copied]).toEqual([true, true, true]);
    // Taken up again, it begins where its record says, not from nothing: killed at its first write, its copy holds that much.
    expect(await helper([{ action: "recover" }], ["writeSync", 0, ".", 1])).toEqual({ answers: [], signal: "SIGKILL" });
    expect(statSync(join(folder, back!)).size).toBe(copied);
    expect(readFileSync(target, "utf8")).toBe("the report, by the thread");
    // And then ends: the user's bytes at the name, with the mode and the time they were saved with, and nothing else left.
    expect(await restart()).toEqual({ ok: { restored: ["Report.bin"], beside: [], lost: [], unread: [] } });
    expect(readFileSync(target).equals(mine)).toBe(true);
    expect([statSync(target).mode & 0o777, statSync(target).mtimeMs, statSync(target).nlink]).toEqual([0o640, saved.getTime(), 1]);
    expect([readdirSync(folder), readdirSync(kept)]).toEqual([["Report.bin"], []]);
  }));

  it("begins a put-back's copy again where what its record names is not a copy of its own, and never writes through a second name", { timeout: 60_000 }, () => keptElsewhere(async () => {
    const { target, mine } = await replaced(40 * 1024 * 1024);
    expect(await helper([unapply(1, "Report.bin")], ["renameSync", 0, "\\.json\\.new$", 4])).toEqual({ answers: [], signal: "SIGKILL" });
    const { back } = recorded();
    // Another program gave the half copy a second name: it is no longer the step's own, and nothing is written into it.
    linkSync(join(folder, back!), join(base, "theirs.bin"));
    const theirs = readFileSync(join(base, "theirs.bin"));
    expect(await restart()).toEqual({ ok: { restored: ["Report.bin"], beside: [], lost: [], unread: [] } });
    expect(readFileSync(target).equals(mine)).toBe(true);
    expect(readFileSync(join(base, "theirs.bin")).equals(theirs)).toBe(true);
    expect(readdirSync(folder)).toEqual(["Report.bin"]);
  }));

  it("only puts back what a landing cut short, given what the folder's landings keep and no copy, and refuses every other action before it touches anything", { timeout: 60_000 }, async () => {
    await cutBetween();
    // A recovery's helper, as the app starts one: given what the folder's landings keep, and no copy.
    const recovery = { SUROGATE_KEPT: kept };
    const only = { error: { type: "unsupported", message: "This computer only puts back here what a landing cut short in the folder" } };
    const others = [
      { action: "revisions", paths: ["Report.docx"] }, apply(2, "Other.docx", null, blob("x"), "absent"), unapply(1, "Report.docx"), { action: "forget", saga: SAGA },
      { action: "other" },
    ];
    expect(await helper(others, undefined, recovery)).toEqual({ answers: others.map(() => only), signal: null });
    expect([existsSync(join(folder, "Report.docx")), leftovers().length, readdirSync(join(kept, SAGA))]).toEqual([false, 2, ["1.json"]]);
    // So it is where the land kind is asked as the helper does, with no copy.
    expect(await refused({ action: "revisions", paths: ["Report.docx"] }, { ...context, landing: { kept } })).toEqual(only.error);
    expect(await helper([{ action: "recover" }], undefined, recovery)).toEqual({ answers: [{ ok: { restored: ["Report.docx"], beside: [], lost: [], unread: [] } }], signal: null });
    expect([readFileSync(join(folder, "Report.docx"), "utf8"), leftovers(), existsSync(join(kept, SAGA))]).toEqual([V1, [], false]);
  });

  it("says a file is lost, and keeps its record, where another folder was made at the name of the one it was moved aside in", { timeout: 60_000 }, async () => {
    await cutBetween("docs/Report.docx");
    renameSync(join(folder, "docs"), join(folder, "docs (old)"));
    mkdirSync(join(folder, "docs"));
    const own = readdirSync(join(folder, "docs (old)")).find((name) => readFileSync(join(folder, "docs (old)", name), "utf8") === V1);
    expect(await restart()).toEqual({ ok: { restored: [], beside: [], lost: [["docs/Report.docx", own]], unread: [] } });
    expect(readdirSync(join(folder, "docs"))).toEqual([]);
    expect(readdirSync(join(kept, SAGA))).toEqual(["1.json"]);
    // Once the folder has its name again, the file takes its own.
    rmSync(join(folder, "docs"), { recursive: true });
    renameSync(join(folder, "docs (old)"), join(folder, "docs"));
    expect(await restart()).toEqual({ ok: { restored: ["docs/Report.docx"], beside: [], lost: [], unread: [] } });
    expect([readdirSync(join(folder, "docs")), readFileSync(join(folder, "docs", "Report.docx"), "utf8")]).toEqual([["Report.docx"], V1]);
  });

  it("says nothing is lost where the user deleted the file before the step had moved it", { timeout: 60_000 }, async () => {
    const target = join(folder, "Report.docx");
    writeFileSync(target, V1);
    const after = turn("Report.docx", "the report, by the thread");
    const seen = await looked("Report.docx");
    // Killed with its file staged, the real one still at its name.
    await helper([apply(1, "Report.docx", blob(V1), after, seen["Report.docx"]!)], ["renameSync", 0, "\\.json\\.new$", 3]);
    expect(readFileSync(target, "utf8")).toBe(V1);
    rmSync(target);
    expect(await restart()).toEqual({ ok: { restored: [], beside: [], lost: [], unread: [] } });
    expect(readdirSync(folder)).toEqual([]);
    expect(existsSync(join(kept, SAGA))).toBe(false);
  });

  it.each([
    ["moved away", () => renameSync(join(folder, "docs"), join(base, "docs.taken"))],
    ["made a link", () => {
      renameSync(join(folder, "docs"), join(base, "docs.taken"));
      mkdirSync(join(base, "elsewhere"));
      symlinkSync(join(base, "elsewhere"), join(folder, "docs"));
    }],
  ])("takes no half-made copy for the user's file: killed while it keeps one on another filesystem, with the file's folder then %s, the file is said lost", { timeout: 60_000 }, async (_how, act) => {
    const here = kept;
    kept = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/dev/shm", "land-kept-"));
    try {
      if (statSync(kept).dev === statSync(folder).dev) return;
      context = { ...context, landing: { copy, kept } };
      mkdirSync(join(folder, "docs"));
      writeFileSync(join(folder, "docs", "Report.docx"), V1);
      const after = turn("docs/Report.docx", "the report, by the thread");
      const seen = await looked("docs/Report.docx");
      // Killed in the keeping: the copy made, not yet dated or synced, and the real file still beside its name.
      expect(await helper([apply(1, "docs/Report.docx", blob(V1), after, seen["docs/Report.docx"]!)], ["futimesSync", 0, ".", 1])).toEqual({ answers: [], signal: "SIGKILL" });
      // A kept file has its name only once it is whole.
      expect(readdirSync(join(kept, SAGA)).sort()).toEqual(["1.json", "1.part"]);
      act();
      const own = readdirSync(join(base, "docs.taken")).find((name) => readFileSync(join(base, "docs.taken", name), "utf8") === V1);
      expect(await restart()).toEqual({ ok: { restored: [], beside: [], lost: [["docs/Report.docx", own]], unread: [] } });
      expect(readdirSync(folder).filter((name) => name !== "docs")).toEqual([]);
      expect(existsSync(join(folder, "docs", "Report.docx"))).toBe(false);
      expect(readdirSync(join(kept, SAGA))).toContain("1.json");
    } finally {
      rmSync(kept, { recursive: true, force: true });
      kept = here;
    }
  });

  it("says which record it cannot read, by its landing and step, and neither writes over it, puts it back, nor forgets it", { timeout: 60_000 }, async () => {
    const other = "1b2c3d4e-0000-4000-8000-000000000001";
    for (const [saga, name] of [[SAGA, "A.txt"], [other, "B.txt"]] as const) {
      writeFileSync(join(folder, name), V1);
      const after = turn(name, "the thread's");
      const seen = revisionOf(statSync(join(folder, name), { bigint: true }));
      await helper([{ ...apply(1, name, blob(V1), after, seen), saga }], ["linkSync", 1, `/${name.replace(".", "\\.")}$`, 1]);
      expect(existsSync(join(folder, name))).toBe(false);
      // The first one's record cut off, as a damaged disk or another program leaves it, before any helper starts again.
      if (saga === SAGA) truncateSync(join(kept, SAGA, "1.json"), 20);
    }
    const record = join(kept, SAGA, "1.json");
    const damaged = readFileSync(record);
    const helping = session();
    try {
      // The other landing is put back all the same, and the damaged one is said, with nothing done in the folder for it.
      expect(await helping.ask({ action: "recover" })).toEqual({ ok: { restored: ["B.txt"], beside: [], lost: [], unread: [[SAGA, 1, null]] } });
      expect(existsSync(join(folder, "A.txt"))).toBe(false);
      expect(readdirSync(folder).filter((name) => OWN_FILE.test(name))).toHaveLength(2);
      const unreadable = { error: { type: "os", code: "EIO", message: `The record of step 1 of landing ${SAGA} cannot be read, so nothing is done over it` } };
      // A new file at the name would leave the user's own named by nothing.
      expect(await helping.ask(apply(1, "A.txt", null, blob("the thread's"), "absent"))).toEqual(unreadable);
      expect(await helping.ask(unapply(1, "A.txt"))).toEqual(unreadable);
      expect(await helping.ask({ action: "forget", saga: SAGA })).toEqual(unreadable);
      expect(existsSync(join(folder, "A.txt"))).toBe(false);
      expect(readFileSync(record)).toEqual(damaged);
    } finally {
      await helping.end();
    }
    // Said again at every start, for as long as it is there.
    expect(await restart()).toMatchObject({ ok: { unread: [[SAGA, 1, null]] } });
  });

  it("leaves a file the user saved over the landing's before the helper started again, and puts the one it replaced beside it", { timeout: 60_000 }, async () => {
    const target = join(folder, "Report.docx");
    writeFileSync(target, V1);
    const after = turn("Report.docx", "the report, by the thread");
    const seen = await looked("Report.docx");
    // Killed with its own file at the name, and the user's still beside it.
    await helper([apply(1, "Report.docx", blob(V1), after, seen["Report.docx"]!)], ["unlinkSync", 0, "\\.surogate-", 2]);
    writeFileSync(target, "saved by you over the thread's");
    expect(await restart()).toEqual({ ok: { restored: [], beside: [["Report.docx", "Report (kept by Surogate).docx"]], lost: [], unread: [] } });
    expect(readFileSync(target, "utf8")).toBe("saved by you over the thread's");
    expect(readFileSync(join(folder, "Report (kept by Surogate).docx"), "utf8")).toBe(V1);
    expect(statSync(target).nlink).toBe(1);
    expect(leftovers()).toEqual([]);
  });
});

describe("a landing's step, when it fails or is asked again", () => {
  it("never uses a step twice: the file its first use kept stays kept", async () => {
    writeFileSync(join(folder, "a.txt"), "v1");
    writeFileSync(join(folder, "b.txt"), "b, v1");
    const after = turn("a.txt", "the thread's");
    const other = turn("b.txt", "the thread's b");
    const seen = await looked("a.txt", "b.txt");
    const first = apply(1, "a.txt", blob("v1"), after, seen["a.txt"]!);
    const done = await ok(first);
    expect(await refused(apply(1, "b.txt", blob("b, v1"), other, seen["b.txt"]!))).toEqual({ type: "value", message: "land's step was already used for another file of this saga" });
    expect(readFileSync(join(folder, "b.txt"), "utf8")).toBe("b, v1");
    // Asked again as it was, after an answer that was lost: done, and nothing is done twice.
    expect(await ok(first)).toEqual(done);
    expect(readFileSync(join(kept, SAGA, "1"), "utf8")).toBe("v1");
    // Asked again after the user saved over what it wrote: that save is no file of the landing's.
    writeFileSync(join(folder, "a.txt"), "saved by you since");
    expect(await refused(first)).toMatchObject({ type: "conflict" });
    expect(readFileSync(join(folder, "a.txt"), "utf8")).toBe("saved by you since");
    expect(readFileSync(join(kept, SAGA, "1"), "utf8")).toBe("v1");
  });

  it("deletes nothing where the thread's copy is gone: a copy that is not there holds no deletion of the thread's", async () => {
    writeFileSync(join(folder, "old.txt"), "yours");
    const seen = await looked("old.txt");
    rmSync(copy, { recursive: true });
    expect(await refused(apply(1, "old.txt", blob("yours"), null, seen["old.txt"]!))).toEqual({ type: "sandbox", message: "This thread's copy is not a folder of the app's own" });
    expect(readFileSync(join(folder, "old.txt"), "utf8")).toBe("yours");
  });

  it("lands a file in a folder this user may pass through but not list", async () => {
    mkdirSync(join(folder, "closed"));
    writeFileSync(join(folder, "closed", "a.txt"), "v1");
    chmodSync(join(folder, "closed"), 0o311);
    try {
      const after = turn("closed/a.txt", "the thread's");
      const seen = await looked("closed/a.txt");
      await ok(apply(1, "closed/a.txt", blob("v1"), after, seen["closed/a.txt"]!));
      expect(readFileSync(join(folder, "closed", "a.txt"), "utf8")).toBe("the thread's");
      expect(await ok(unapply(1, "closed/a.txt"))).toEqual({ path: "closed/a.txt", put_back: true });
      expect(readFileSync(join(folder, "closed", "a.txt"), "utf8")).toBe("v1");
    } finally {
      chmodSync(join(folder, "closed"), 0o755);
    }
  });

  // Another process, as the user's own programs are: it acts as soon as the landing's own file appears in *dir*.
  const when = (dir: string, act: string) => {
    const acting = spawn(process.execPath, ["-e", `
      const fs = require("node:fs");
      const until = Date.now() + 20000;
      (function wait() {
        if (fs.readdirSync(${JSON.stringify(dir)}).some((name) => name.startsWith(".surogate-"))) {
          ${act}
          process.exit(0);
        }
        if (Date.now() > until) process.exit(3);
        setImmediate(wait);
      })();
    `], { stdio: "ignore" });
    return new Promise<number | null>((resolve) => acting.once("exit", resolve));
  };

  it("follows no link put on a file's way while its apply writes: nothing is written where the link leads, and nothing of the landing's is left", { timeout: 30_000 }, async () => {
    mkdirSync(join(folder, "docs"));
    mkdirSync(join(base, "elsewhere"));
    writeFileSync(join(folder, "docs", "big.bin"), "the file, v1");
    writeFileSync(join(base, "elsewhere", "big.bin"), "outside the folder");
    const data = Buffer.alloc(96 * 1024 * 1024, "t");
    mkdirSync(join(copy, "docs"));
    writeFileSync(join(copy, "docs", "big.bin"), data);
    const seen = await looked("docs/big.bin");
    const swapped = when(join(folder, "docs"), `
      fs.renameSync(${JSON.stringify(join(folder, "docs"))}, ${JSON.stringify(join(folder, "docs.moved"))});
      fs.symlinkSync(${JSON.stringify(join(base, "elsewhere"))}, ${JSON.stringify(join(folder, "docs"))});
    `);
    await new Promise((resolve) => setTimeout(resolve, 300));
    const outcome = await land(apply(1, "docs/big.bin", blob("the file, v1"), blob(data), seen["docs/big.bin"]!));
    expect(await swapped).toBe(0);
    expect(outcome).toEqual({ error: { type: "sandbox", message: "Not a path in this folder: 'docs/big.bin'" } });
    expect(readdirSync(join(base, "elsewhere"))).toEqual(["big.bin"]);
    expect(readFileSync(join(base, "elsewhere", "big.bin"), "utf8")).toBe("outside the folder");
    // The folder that was moved away holds the user's file, and nothing else.
    expect(readdirSync(join(folder, "docs.moved"))).toEqual(["big.bin"]);
    expect(readFileSync(join(folder, "docs.moved", "big.bin"), "utf8")).toBe("the file, v1");
    expect(existsSync(join(kept, SAGA))).toBe(false);
  });

  it("gives a folder back its name when one was made where the file was while its apply writes", { timeout: 30_000 }, async () => {
    const target = join(folder, "big.bin");
    writeFileSync(target, "the file, v1");
    const data = Buffer.alloc(96 * 1024 * 1024, "t");
    writeFileSync(join(copy, "big.bin"), data);
    const seen = await looked("big.bin");
    const made = when(folder, `
      fs.rmSync(${JSON.stringify(target)});
      fs.mkdirSync(${JSON.stringify(target)});
      fs.writeFileSync(${JSON.stringify(join(target, "inside.txt"))}, "yours, in the folder you made");
    `);
    await new Promise((resolve) => setTimeout(resolve, 300));
    const outcome = await land(apply(1, "big.bin", blob("the file, v1"), blob(data), seen["big.bin"]!));
    expect(await made).toBe(0);
    expect(outcome).toMatchObject({ error: { type: "conflict" } });
    expect(readFileSync(join(target, "inside.txt"), "utf8")).toBe("yours, in the folder you made");
    expect(readdirSync(folder)).toEqual(["big.bin"]);
    expect(existsSync(join(kept, SAGA))).toBe(false);
  });

  it("replaces and deletes nothing in a folder that gives a file no second name, as a disk with no hard links does, and says why", { timeout: 60_000 }, async () => {
    writeFileSync(join(folder, "Report.docx"), "the report, v1");
    writeFileSync(join(folder, "old.txt"), "to be deleted");
    const was = ["Report.docx", "old.txt"].map((name) => lstatSync(join(folder, name), { bigint: true }).ino);
    const after = turn("Report.docx", "the report, by the thread");
    const fresh = turn("new.md", "new\n");
    const seen = await looked("Report.docx", "old.txt");
    const { answers } = await helper([
      apply(1, "Report.docx", blob("the report, v1"), after, seen["Report.docx"]!),
      apply(2, "old.txt", blob("to be deleted"), null, seen["old.txt"]!),
      apply(3, "new.md", null, fresh, "absent"),
    ], ["linkSync", 0, ".", 0, 'refuse("EPERM")']);
    const refusal = (path: string) => ({
      error: { type: "os", code: "EPERM", message: `${path} was not written: this folder cannot give a file a second name, which a landing needs to put back what it replaces` },
    });
    expect(answers).toEqual([refusal("Report.docx"), refusal("old.txt"), refusal("new.md")]);
    // Refused before either left its name: the very files, and nothing beside them.
    expect(readdirSync(folder).sort()).toEqual(["Report.docx", "old.txt"]);
    expect(["Report.docx", "old.txt"].map((name) => lstatSync(join(folder, name), { bigint: true }).ino)).toEqual(was);
    expect(existsSync(join(kept, SAGA))).toBe(false);
  });

  it("gives the replaced file its name back when the step fails after it was kept: what it kept is never dropped with its record", { timeout: 60_000 }, async () => {
    const target = join(folder, "Report.docx");
    writeFileSync(target, "the report, v1");
    const was = lstatSync(target, { bigint: true });
    const after = turn("Report.docx", "the report, by the thread");
    const seen = await looked("Report.docx");
    // The disk is full as the step writes down that it ended.
    const { answers } = await helper([apply(1, "Report.docx", blob("the report, v1"), after, seen["Report.docx"]!)], ["renameSync", 0, "\\.json\\.new$", 4, 'refuse("ENOSPC")']);
    expect(answers).toMatchObject([{ error: { type: "os", code: "ENOSPC" } }]);
    const now = lstatSync(target, { bigint: true });
    expect([readFileSync(target, "utf8"), now.ino, now.mode, now.nlink]).toEqual(["the report, v1", was.ino, was.mode, 1n]);
    expect(readdirSync(folder)).toEqual(["Report.docx"]);
  });

  it("leaves a file saved over the landing's while its put-back moves it out, and keeps what the landing replaced", { timeout: 60_000 }, async () => {
    const target = join(folder, "Report.docx");
    writeFileSync(target, "the report, v1");
    const after = turn("Report.docx", "the report, by the thread");
    const seen = await looked("Report.docx");
    await helper([apply(1, "Report.docx", blob("the report, v1"), after, seen["Report.docx"]!)]);
    // Saved in place between the put-back's look and its move.
    const { answers } = await helper([unapply(1, "Report.docx")], ["renameSync", 0, "/Report\\.docx$", 1, `fs.writeFileSync(${JSON.stringify(target)}, "saved by you as it was put back")`]);
    expect(answers).toEqual([{ error: { type: "conflict", message: "Report.docx changed after the landing wrote it, so it was not put back" } }]);
    expect(readFileSync(target, "utf8")).toBe("saved by you as it was put back");
    expect(readdirSync(folder)).toEqual(["Report.docx"]);
    expect(readFileSync(join(kept, SAGA, "1"), "utf8")).toBe("the report, v1");
    expect(JSON.parse(readFileSync(join(kept, SAGA, "1.json"), "utf8"))).toMatchObject({ temp: null, aside: null, out: null, back: null });
    // The step is as it was: nothing for the next start to end, and still there to settle.
    expect(await restart()).toEqual({ ok: { restored: [], beside: [], lost: [], unread: [] } });
    expect(readFileSync(join(kept, SAGA, "1"), "utf8")).toBe("the report, v1");
  });

  it("leaves a file made at the name while a put-back had it empty, and nothing of the landing's beside it", { timeout: 60_000 }, async () => {
    const target = join(folder, "Report.docx");
    writeFileSync(target, "the report, v1");
    const after = turn("Report.docx", "the report, by the thread");
    const seen = await looked("Report.docx");
    await helper([apply(1, "Report.docx", blob("the report, v1"), after, seen["Report.docx"]!)]);
    // Made between the landing's file leaving the name and the replaced one taking it.
    const { answers } = await helper([unapply(1, "Report.docx")], ["linkSync", 1, "/Report\\.docx$", 1, `fs.writeFileSync(${JSON.stringify(target)}, "made by you as it was put back")`]);
    expect(answers).toMatchObject([{ error: { type: "conflict" } }]);
    expect(readFileSync(target, "utf8")).toBe("made by you as it was put back");
    expect(readdirSync(folder)).toEqual(["Report.docx"]);
    expect(readFileSync(join(kept, SAGA, "1"), "utf8")).toBe("the report, v1");
  });

  it("moves a folder back to its name when one took the file's place just as the file was moved aside", { timeout: 60_000 }, async () => {
    const target = join(folder, "Report.docx");
    writeFileSync(target, "the report, v1");
    const after = turn("Report.docx", "the report, by the thread");
    const seen = await looked("Report.docx");
    const swap = `const at = ${JSON.stringify(target)}; fs.rmSync(at); fs.mkdirSync(at); fs.writeFileSync(at + "/inside.txt", "yours, in the folder you made");`;
    const { answers } = await helper([apply(1, "Report.docx", blob("the report, v1"), after, seen["Report.docx"]!)], ["renameSync", 0, "/Report\\.docx$", 1, swap]);
    expect(answers).toMatchObject([{ error: { type: "conflict" } }]);
    expect(readFileSync(join(target, "inside.txt"), "utf8")).toBe("yours, in the folder you made");
    expect(readdirSync(folder)).toEqual(["Report.docx"]);
    expect(existsSync(join(kept, SAGA))).toBe(false);
  });

  it("answers a recovery with nothing where no landing was cut short", async () => {
    expect(await ok({ action: "recover" })).toEqual({ restored: [], beside: [], lost: [], unread: [] });
    expect(existsSync(kept)).toBe(false);
  });
});

describe("what a landing keeps, and how much", () => {
  const GIB = 1024 * 1024 * 1024;
  // A file of *size* bytes that takes no room: its size is all a bound looks at.
  const sparse = (path: string, size: number) => {
    writeFileSync(path, "");
    truncateSync(path, size);
  };

  it("refuses a file over a gibibyte before anything is kept or written, and says why", async () => {
    writeFileSync(join(folder, "video.mp4"), "v1");
    sparse(join(copy, "video.mp4"), GIB + 1);
    sparse(join(copy, "new.mp4"), GIB + 1);
    const seen = await looked("video.mp4");
    const refusal = { type: "os", code: "EFBIG", message: "File too large to land in a local folder (over 1 GiB)" };
    expect(await refused(apply(1, "video.mp4", blob("v1"), blob("any"), seen["video.mp4"]!))).toEqual(refusal);
    expect(await refused(apply(2, "new.mp4", null, blob("any"), "absent"))).toEqual(refusal);
    expect(readdirSync(folder)).toEqual(["video.mp4"]);
    expect(readFileSync(join(folder, "video.mp4"), "utf8")).toBe("v1");
    expect(existsSync(kept)).toBe(false);
  });

  it("keeps at most 4 GiB of the files a folder's landings replaced: past it a file is refused before it is touched, until a landing is forgotten", async () => {
    const other = "1b2c3d4e-0000-4000-8000-000000000001";
    for (const name of ["a.bin", "b.bin", "c.bin"]) sparse(join(folder, name), 1.5 * GIB);
    const after = turn("a.bin", "the thread's");
    for (const name of ["b.bin", "c.bin"]) turn(name, "the thread's");
    const seen = await looked("a.bin", "b.bin", "c.bin");
    const any = blob("any");
    await ok(apply(1, "a.bin", any, after, seen["a.bin"]!));
    // A deletion keeps the file it deletes as a replacement does.
    rmSync(join(copy, "b.bin"));
    await ok(apply(2, "b.bin", any, null, seen["b.bin"]!));
    // Whichever landing asks: the bound is the folder's.
    const third = { ...apply(1, "c.bin", any, after, seen["c.bin"]!), saga: other };
    expect(await refused(third)).toEqual({
      type: "os", code: "EDQUOT", message: "c.bin was not replaced: more than 4 GiB would be kept of the files this folder's landings replaced",
    });
    expect(statSync(join(folder, "c.bin")).size).toBe(1.5 * GIB);
    expect(revisionOf(statSync(join(folder, "c.bin"), { bigint: true }))).toBe(seen["c.bin"]);
    expect(existsSync(join(kept, other))).toBe(false);
    expect(leftovers()).toEqual([]);
    await ok({ action: "forget", saga: SAGA });
    await ok(third);
    expect(readFileSync(join(folder, "c.bin"), "utf8")).toBe("the thread's");
  });

  it("keeps a replaced file where this user alone can open it, and gives it its own mode back with its name", async () => {
    writeFileSync(join(folder, "Report.docx"), "the report, v1");
    chmodSync(join(folder, "Report.docx"), 0o664);
    const after = turn("Report.docx", "the report, by the thread");
    const seen = await looked("Report.docx");
    await ok(apply(1, "Report.docx", blob("the report, v1"), after, seen["Report.docx"]!));
    const modes = Object.fromEntries([kept, join(kept, SAGA), join(kept, SAGA, "1"), join(kept, SAGA, "1.json")].map((path) => [path.slice(kept.length), statSync(path).mode & 0o777]));
    expect(modes).toEqual({ "": 0o700, [`/${SAGA}`]: 0o700, [`/${SAGA}/1`]: 0o600, [`/${SAGA}/1.json`]: 0o600 });
    await ok(unapply(1, "Report.docx"));
    expect(statSync(join(folder, "Report.docx")).mode & 0o777).toBe(0o664);
  });
});

// A filesystem that takes "Report.docx" and "report.docx", and two spellings of one "é", for one name, as a Mac's or
// a Windows disk does: tmpfs can, mounted in a user namespace of the test's own. *run* is a shell line given the
// mount's path as $1; null where this computer gives no such mount.
function folding(at: string, run: string, ...more: string[]) {
  return spawnSync("bwrap", [
    "--unshare-user", "--uid", "0", "--gid", "0", "--cap-add", "ALL", "--bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc", "--",
    "sh", "-c", `mount -t tmpfs -o casefold none "$1" && mkdir "$1/Documents" && chattr +F "$1/Documents" && ${run}`, "sh", at, ...more,
  ], { encoding: "utf8", timeout: 30_000 });
}
const probe = mkdtempSync(join(tmpdir(), "land-folding-"));
const folds = folding(probe, 'touch "$1/Documents/A" && test -e "$1/Documents/a"').status === 0;
rmSync(probe, { recursive: true, force: true });

describe("a folder that tells names apart less than the thread's copy does", () => {
  it.skipIf(!folds)("never writes one file twice for two names of a landing, by their case or by how their letters are composed, and says which two", () => {
    const mount = join(base, "mount");
    mkdirSync(mount);
    const [composed, decomposed] = ["café.txt", "café.txt"];
    const data = { "Report.docx": "one", "report.docx": "another", [composed]: "composed", [decomposed]: "decomposed" };
    for (const [name, text] of Object.entries(data)) turn(name, text);
    // The landing runs where the mount is, and says what it was answered and what the folder then holds.
    const script = `
      import { readdirSync, readFileSync, writeFileSync } from "node:fs";
      import { join } from "node:path";
      const { perform } = await import(${JSON.stringify(new URL("../dist/files/operations.js", import.meta.url).href)});
      const folder = join(process.argv[1], "Documents");
      const context = { folder, home: "/nowhere", env: {}, landing: { copy: ${JSON.stringify(copy)}, kept: ${JSON.stringify(kept)} } };
      const land = (args) => perform("land", args, context, new AbortController().signal);
      writeFileSync(join(folder, "Notes.md"), "yours");
      const out = { looked: await land({ action: "revisions", paths: ["Notes.md", "notes.md", "Notes.md", "new.txt"] }), applied: [] };
      for (const [step, [path, after]] of ${JSON.stringify(Object.entries(data).map(([name, text]) => [name, blob(text)]))}.entries()) {
        out.applied.push(await land({ action: "apply", saga: ${JSON.stringify(SAGA)}, step, path, before: null, after, expected: "absent" }));
      }
      out.holds = Object.fromEntries(readdirSync(folder).map((name) => [name, readFileSync(join(folder, name), "utf8")]));
      console.log(JSON.stringify(out));
    `;
    const ran = folding(mount, '"$2" --input-type=module -e "$3" "$1"', process.execPath, script);
    expect(ran.stderr).toBe("");
    const out = JSON.parse(ran.stdout) as { looked: { ok: { revisions: Array<[string, string]> } }; applied: unknown[]; holds: Record<string, string> };
    // Two names of a look that are one file are neither of them replaced; a name asked twice is one name.
    expect(out.looked.ok.revisions.map(([path, token]) => [path, token === "other" ? token : token === "absent" ? token : "a revision"]))
      .toEqual([["Notes.md", "other"], ["notes.md", "other"], ["Notes.md", "other"], ["new.txt", "absent"]]);
    const twice = (path: string, first: string) => ({
      error: { type: "os", code: "EEXIST", message: `${path} and ${first} are one file in this folder, which tells names apart less than the thread's copy does, so it was not written twice` },
    });
    expect(out.applied).toEqual([
      { ok: expect.objectContaining({ path: "Report.docx" }) }, twice("report.docx", "Report.docx"),
      { ok: expect.objectContaining({ path: composed }) }, twice(decomposed, composed),
    ]);
    expect(out.holds).toEqual({ "Notes.md": "yours", "Report.docx": "one", [composed]: "composed" });
  });
});

// A user's own attribute on a file, set and read by Python: Node has no call for either. Null where this computer's
// filesystem keeps none.
const attribute = (path: string, value?: string): string | null => {
  const code = value === undefined
    ? "import os,sys; print(os.getxattr(sys.argv[1], 'user.note').decode())"
    : "import os,sys; os.setxattr(sys.argv[1], 'user.note', sys.argv[2].encode())";
  const ran = spawnSync("python3", ["-c", code, path, ...(value === undefined ? [] : [value])], { encoding: "utf8", timeout: 10_000 });
  return ran.status === 0 ? ran.stdout.trim() : null;
};
const attributed = (() => {
  const at = mkdtempSync(join(tmpdir(), "land-attribute-"));
  writeFileSync(join(at, "f"), "");
  const kept = attribute(join(at, "f"), "x") !== null;
  rmSync(at, { recursive: true, force: true });
  return kept;
})();

describe("what a landing's file is, and what its put-back gives back", () => {
  it("writes a new file as this user makes one, and a replacement with the mode of the file it replaces, and nothing else of it", async () => {
    const umask = process.umask();
    writeFileSync(join(folder, "run.sh"), "v1");
    chmodSync(join(folder, "run.sh"), 0o750);
    if (attributed) attribute(join(folder, "run.sh"), "yours");
    const before = Date.now();
    // A file the thread made executable in its copy: its mode there is no mode of the folder's.
    const script = turn("new.sh", "#!/bin/sh\n");
    chmodSync(join(copy, "new.sh"), 0o777);
    const replaced = turn("run.sh", "the thread's");
    chmodSync(join(copy, "run.sh"), 0o600);
    const seen = await looked("run.sh");
    await ok(apply(1, "new.sh", null, script, "absent"));
    await ok(apply(2, "run.sh", blob("v1"), replaced, seen["run.sh"]!));
    const [made, over] = [statSync(join(folder, "new.sh")), statSync(join(folder, "run.sh"))];
    expect([made.mode & 0o7777, over.mode & 0o7777]).toEqual([0o666 & ~umask, 0o750]);
    for (const st of [made, over]) {
      expect([st.uid, st.gid, st.nlink]).toEqual([process.getuid!(), process.getgid!(), 1]);
      // Written now: the copy's time is not the folder's.
      expect(st.mtimeMs).toBeGreaterThanOrEqual(before - 1000);
    }
    if (attributed) expect(attribute(join(folder, "run.sh"))).toBeNull();
  });

  it("gives back the very file it replaced: its inode, its times to the nanosecond, its mode, its owner and its attributes", async () => {
    const target = join(folder, "Report.docx");
    writeFileSync(target, "the report, v1");
    chmodSync(target, 0o4750);
    if (attributed) attribute(target, "yours");
    utimesSync(target, 1_600_000_000.123456, 1_500_000_000.654321);
    const was = lstatSync(target, { bigint: true });
    const after = turn("Report.docx", "the report, by the thread");
    const seen = await looked("Report.docx");
    await ok(apply(1, "Report.docx", blob("the report, v1"), after, seen["Report.docx"]!));
    expect(lstatSync(target, { bigint: true }).ino).not.toBe(was.ino);
    expect(await ok(unapply(1, "Report.docx"))).toEqual({ path: "Report.docx", put_back: true });
    const now = lstatSync(target, { bigint: true });
    expect([now.ino, now.mtimeNs, now.atimeNs, now.mode, now.uid, now.gid, now.nlink, now.size])
      .toEqual([was.ino, was.mtimeNs, was.atimeNs, was.mode, was.uid, was.gid, 1n, was.size]);
    if (attributed) expect(attribute(target)).toBe("yours");
  });

  it("gives back a copy of it from another filesystem with its bytes, its mode and the time it was saved to the microsecond, and none of its attributes", async () => {
    const other = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/dev/shm", "land-kept-"));
    try {
      // Only where this computer has a second filesystem to keep it on.
      if (statSync(other).dev === statSync(folder).dev) return;
      context = { ...context, landing: { copy, kept: other } };
      const target = join(folder, "Report.docx");
      writeFileSync(target, "the report, v1");
      chmodSync(target, 0o640);
      if (attributed) attribute(target, "yours");
      // A time whose seconds, as the float Node takes, fall just short of its microsecond.
      utimesSync(target, 1_600_000_000, (1_500_000_000_654_321 + 0.5) / 1e6);
      const was = lstatSync(target, { bigint: true });
      expect(was.mtimeNs / 1000n).toBe(1_500_000_000_654_321n);
      const after = turn("Report.docx", "the report, by the thread");
      await ok(apply(1, "Report.docx", blob("the report, v1"), after, (await looked("Report.docx"))["Report.docx"]!));
      expect(statSync(join(other, SAGA, "1")).mode & 0o777).toBe(0o600);
      expect(await ok(unapply(1, "Report.docx"))).toEqual({ path: "Report.docx", put_back: true });
      const now = lstatSync(target, { bigint: true });
      expect([readFileSync(target, "utf8"), now.mode, now.uid, now.gid, now.nlink, now.mtimeNs / 1000n])
        .toEqual(["the report, v1", was.mode, was.uid, was.gid, 1n, was.mtimeNs / 1000n]);
      // A copy is a new file, and making it reads the old one: neither its attributes nor when it was last read come back with it.
      expect(now.ino).not.toBe(was.ino);
      if (attributed) expect(attribute(target)).toBeNull();
      expect(leftovers()).toEqual([]);
      expect(existsSync(join(other, SAGA))).toBe(false);
    } finally {
      rmSync(other, { recursive: true, force: true });
    }
  });
});

describe("a landing's helper in the file helper's sandbox", () => {
  const sandboxed = fileToolsMissing(undefined, process.env.PATH ?? "").length === 0;
  const dist = (file: string) => new URL(`../dist/${file}`, import.meta.url).href;

  it.skipIf(!sandboxed)("lands and puts back there, given the thread's copy to read and the kept folder to write", { timeout: 60_000 }, async () => {
    writeFileSync(join(folder, "Report.docx"), "the report, v1");
    const after = turn("Report.docx", "the report, by the thread");
    const fresh = turn("threads/A/new.md", "new\n");
    const work = join(base, "work");
    // srt's own files go where its host points them, as the app's host does: never in the folder.
    for (const dir of [work, join(base, "srt")]) mkdirSync(dir);
    // A host of the test's own: the file helper's policy as a landing's host asks for it, the copy to read and the kept
    // folder to write, around a helper that is asked a landing's steps. It says what it was answered.
    const host = `
      import { spawnSync } from "node:child_process";
      import { SandboxManager } from "@anthropic-ai/sandbox-runtime";
      const { hideSrtTmp, pathOutside, quote, sandboxPolicy } = await import(${JSON.stringify(dist("hosts/policy.js"))});
      const [folder, work, copy, kept, node, home] = process.argv.slice(1);
      const path = pathOutside(process.env.PATH, []);
      const policy = sandboxPolicy({ folder, tmp: work, reads: [copy], writes: [kept], appDirs: ${JSON.stringify([...["../dist", "../node_modules"].map((dir) => fileURLToPath(new URL(dir, import.meta.url))), dirname(dirname(process.execPath))])} });
      await SandboxManager.initialize(policy, () => Promise.resolve(false));
      const asked = ${JSON.stringify(`
        const { perform } = await import(${JSON.stringify(dist("files/operations.js"))});
        const [folder, copy, kept, steps] = process.argv.slice(1);
        const context = { folder, home: "/nowhere", env: {}, landing: { copy, kept } };
        const out = [];
        for (const args of JSON.parse(steps)) {
          if (args.expected === "seen") args.expected = out[0].ok.revisions[0][1];
          out.push(await perform("land", args, context, new AbortController().signal));
        }
        console.log(JSON.stringify(out.slice(1)));
      `)};
      process.chdir(work);
      const words = [node, "--input-type=module", "-e", asked, folder, copy, kept, process.env.STEPS].map(quote).join(" ");
      const [file, flag, line] = (await SandboxManager.wrapWithSandboxArgv(words)).argv;
      const ran = spawnSync(file, ["--norc", "--noprofile", flag, hideSrtTmp(line)], { cwd: folder, env: { HOME: home, PATH: path }, encoding: "utf8", timeout: 30000 });
      await SandboxManager.reset().catch(() => {});
      process.stderr.write(ran.stderr);
      process.stdout.write(ran.stdout);
      process.exit(ran.status ?? 1);
    `;
    const steps = [
      { action: "revisions", paths: ["Report.docx"] },
      { ...apply(1, "Report.docx", blob("the report, v1"), after, "seen") },
      apply(2, "threads/A/new.md", null, fresh, "absent"),
      unapply(2, "threads/A/new.md"),
      unapply(1, "Report.docx"),
    ];
    // In a group of its own, as the app's hosts are: what srt started goes with it.
    const child = spawn(process.execPath, ["--input-type=module", "-e", host, folder, work, copy, kept, process.execPath, base], {
      cwd: fileURLToPath(new URL("..", import.meta.url)), detached: true, stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, TMPDIR: join(base, "srt"), STEPS: JSON.stringify(steps) },
    });
    let [said, failed] = ["", ""];
    child.stdout.on("data", (chunk: Buffer) => { said += chunk.toString(); });
    child.stderr.on("data", (chunk: Buffer) => { failed += chunk.toString(); });
    const bound = setTimeout(() => child.kill("SIGKILL"), 45_000);
    const status = await new Promise<number | null>((resolve) => child.once("exit", resolve));
    clearTimeout(bound);
    try {
      process.kill(-child.pid!, "SIGKILL");
    } catch {
      // Nothing of it was left.
    }
    expect([status, failed]).toEqual([0, ""]);
    expect(JSON.parse(said)).toEqual([
      { ok: { path: "Report.docx", before: blob("the report, v1"), after, made: [] } },
      { ok: { path: "threads/A/new.md", before: null, after: fresh, made: ["threads/A", "threads"] } },
      { ok: { path: "threads/A/new.md", put_back: true } },
      { ok: { path: "Report.docx", put_back: true } },
    ]);
    expect(readdirSync(folder)).toEqual(["Report.docx"]);
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe("the report, v1");
    expect(existsSync(join(kept, SAGA))).toBe(false);
  });
});

describe("a step whose own clean-up fails", () => {
  // A replacement in a folder that another program makes read-only just as the landing's file is to take the name:
  // the real file is beside its name by then, and nothing in the folder can be moved or removed.
  const stuck = async () => {
    mkdirSync(join(folder, "sub"));
    writeFileSync(join(folder, "sub", "R.txt"), "the report, v1");
    const after = turn("sub/R.txt", "the report, by the thread");
    const seen = await looked("sub/R.txt");
    const helping = session(["linkSync", 1, "/R\\.txt$", 1, `fs.chmodSync(${JSON.stringify(join(folder, "sub"))}, 0o555)`]);
    expect(await helping.ask(apply(1, "sub/R.txt", blob("the report, v1"), after, seen["sub/R.txt"]!))).toMatchObject({ error: { type: "os", code: "EACCES" } });
    expect(existsSync(join(folder, "sub", "R.txt"))).toBe(false);
    return helping;
  };
  afterEach(() => {
    if (existsSync(join(folder, "sub"))) chmodSync(join(folder, "sub"), 0o755);
  });

  it("forgets no landing while a step of it is still cut, in the helper that cut it: the file is put back first, or the forgetting is refused", { timeout: 60_000 }, async () => {
    const helping = await stuck();
    try {
      // Still read-only: nothing can be put back, so nothing is forgotten.
      expect(await helping.ask({ action: "forget", saga: SAGA })).toMatchObject({ error: { type: "os", code: "EACCES" } });
      expect(readdirSync(join(kept, SAGA))).toContain("1.json");
      chmodSync(join(folder, "sub"), 0o755);
      expect(await helping.ask({ action: "forget", saga: SAGA })).toEqual({ ok: {} });
      expect(readdirSync(join(folder, "sub"))).toEqual(["R.txt"]);
      expect(readFileSync(join(folder, "sub", "R.txt"), "utf8")).toBe("the report, v1");
      expect(await helping.ask({ action: "recover" })).toMatchObject({ ok: { restored: ["sub/R.txt"], beside: [], lost: [], unread: [] } });
      expect(readdirSync(kept)).toEqual([]);
    } finally {
      await helping.end();
    }
  });

  it("writes no cut step down as ended by a put-back the user's new file refuses: the file moved aside goes beside it, and is said", { timeout: 60_000 }, async () => {
    const helping = await stuck();
    try {
      chmodSync(join(folder, "sub"), 0o755);
      writeFileSync(join(folder, "sub", "R.txt"), "made by you since");
      expect(await helping.ask(unapply(1, "sub/R.txt"))).toEqual({ ok: { path: "sub/R.txt", put_back: false } });
      expect(await helping.ask({ action: "recover" })).toMatchObject({ ok: { restored: [], beside: [["sub/R.txt", "sub/R (kept by Surogate).txt"]], lost: [], unread: [] } });
      expect(Object.fromEntries(readdirSync(join(folder, "sub")).map((name) => [name, readFileSync(join(folder, "sub", name), "utf8")])))
        .toEqual({ "R.txt": "made by you since", "R (kept by Surogate).txt": "the report, v1" });
      expect(readdirSync(kept)).toEqual([]);
    } finally {
      await helping.end();
    }
  });

  it("drops no record while a file of its own is still in the folder: what it could not remove is removed at the next start", { timeout: 60_000 }, async () => {
    mkdirSync(join(folder, "sub"));
    writeFileSync(join(folder, "sub", "R.txt"), "the report, v1");
    const was = lstatSync(join(folder, "sub", "R.txt"), { bigint: true });
    const after = turn("sub/R.txt", "the report, by the thread");
    const seen = await looked("sub/R.txt");
    // Read-only from the moment its file is staged: the step is refused there, and cannot remove what it staged.
    const { answers } = await helper(
      [apply(1, "sub/R.txt", blob("the report, v1"), after, seen["sub/R.txt"]!)],
      ["linkSync", 1, "\\.surogate-", 1, `fs.chmodSync(${JSON.stringify(join(folder, "sub"))}, 0o555)`],
    );
    expect(answers).toMatchObject([{ error: { type: "os", code: "EACCES" } }]);
    expect(readdirSync(join(folder, "sub")).filter((name) => OWN_FILE.test(name))).toHaveLength(1);
    expect(readdirSync(join(kept, SAGA))).toEqual(["1.json"]);
    chmodSync(join(folder, "sub"), 0o755);
    expect(await restart()).toMatchObject({ ok: { restored: [], beside: [], lost: [], unread: [] } });
    const now = lstatSync(join(folder, "sub", "R.txt"), { bigint: true });
    expect([readdirSync(join(folder, "sub")), now.ino, now.nlink]).toEqual([["R.txt"], was.ino, 1n]);
    expect(existsSync(join(kept, SAGA))).toBe(false);
  });
});
