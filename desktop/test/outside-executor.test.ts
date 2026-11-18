// The Outside layer: a chat's grants of folders outside its own, and its reads in them, done by the app itself.

import { mkdirSync, mkdtempSync, realpathSync, renameSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BOOT_ID, type FolderGuards } from "../src/binding/folder.js";
import { type FdPaths, fdPaths } from "../src/files/fd-paths.js";
import { perform } from "../src/files/operations.js";
import { OperationJournal } from "../src/journal/journal.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import { Outside } from "../src/outside/executor.js";
import type { ToolLayer } from "../src/shell/device-stack.js";

const ROOT = "root-1";

let base: string;
let desktop: string;
let work: string;
let guards: FolderGuards;
let journal: OperationJournal;
let tools: ToolLayer & { ran: Operation[] };
let chosenFor: ReturnType<typeof vi.fn>;

const getPath = (name: string) => join(base, "home", name === "desktop" ? "Desktop" : name);

let next = 0;
const op = (kind: string, args: Record<string, unknown>): Operation => {
  next += 1;
  return { id: `${kind}-${next}`, sessionId: ROOT, callingSessionId: ROOT, invocationId: "1:call", ordinal: next, kind, args, digest: "d" };
};
const never = () => new AbortController().signal;
const layer = (fd: FdPaths = fdPaths) => new Outside({ tools, bindings: journal.bindings, approvals: { chosenFor }, fd, getPath });

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "outside-executor-")));
  desktop = join(base, "home", "Desktop");
  work = join(base, "work");
  for (const dir of ["home/.ssh", "home/Desktop/Scan", "data", "work", "elsewhere"]) mkdirSync(join(base, dir), { recursive: true });
  writeFileSync(join(desktop, "note.txt"), "first\nsecond\n");
  writeFileSync(join(desktop, "Scan", "cerere.pdf"), "pdf");
  writeFileSync(join(base, "elsewhere", "secret.txt"), "secret");
  guards = { home: join(base, "home"), dataDir: join(base, "data"), cacheDir: join(base, "home", ".cache", "surogate"), appDirs: [join(base, "app")] };
  journal = new OperationJournal(join(base, "journal.sqlite"));
  journal.bindings.add({ root: ROOT, nonce: "n", folder: work, dev: 1, ino: 1, boot: BOOT_ID, mode: "free", boundAt: 1 });
  const ran: Operation[] = [];
  tools = {
    ran,
    run: async (operation) => {
      ran.push(operation);
      return { ok: "below" };
    },
    guards: () => guards,
    live: () => ["live-root"],
    stop: vi.fn(async () => {}),
    refusal: (operation) => (operation.kind === "run" ? { error: { type: "sandbox", message: "tools refuse" } } : null),
    takeOver: (root) => root === ROOT,
  };
  chosenFor = vi.fn(() => undefined);
});

afterEach(() => {
  journal.close();
  rmSync(base, { recursive: true, force: true });
});

const noPermission = (path: string): Outcome => ({
  error: { type: "sandbox", message: `No permission to read ${path}: ask for it first with request_folder_access.` },
});

describe("Outside", () => {
  it("grants a known folder by its OS path and answers where", async () => {
    const outside = layer();
    expect(outside.resolveFolder("desktop", ROOT)).toEqual({ folder: desktop });
    expect(outside.resolveFolder(desktop, ROOT)).toEqual({ folder: desktop });
    // A name that is neither a known folder nor an absolute path, and one that may not be granted, are refused.
    expect(outside.resolveFolder("Music", ROOT)).toMatchObject({ refused: expect.stringContaining("Music") });
    const home = outside.resolveFolder(join(base, "home"), ROOT);
    expect(home).toMatchObject({ refused: expect.stringMatching(/^Surogate cannot read .*home: the folder .* holds/) });
    expect(outside.refusal(op("outside.grant", { folder: "Music" }))).toMatchObject({ error: { type: "denied" } });
    expect(outside.refusal(op("outside.grant", { folder: "Desktop" }))).toBeNull();
    // The approvals granted it, on its user's allow.
    journal.bindings.grantRead(ROOT, desktop);
    expect(await outside.run(op("outside.grant", { folder: "Desktop" }), never())).toEqual({ ok: { folder: desktop } });
    expect(tools.ran).toEqual([]);
  });

  it("refuses reading a folder it was never granted, before anyone is asked", async () => {
    const outside = layer();
    const file = join(desktop, "note.txt");
    for (const operation of [
      op("outside.read", { key: file, max_bytes: null }), op("outside.stat", { key: file }), op("outside.list_dir", { key: desktop }),
      op("outside.resolve", { path: file }),
      op("outside.walk", { key: desktop, skip: [], skip_top: [], skip_hidden: false, since: null }),
    ]) {
      const path = operation.kind === "outside.resolve" ? file : (operation.args.key as string);
      expect(outside.refusal(operation), operation.kind).toEqual(noPermission(path));
      // Run as well, for an operation that came without the question.
      expect(await outside.run(operation, never()), operation.kind).toEqual(noPermission(path));
    }
    expect(outside.refusal(op("outside.copy", { files: [file], to: work }))).toEqual(noPermission(file));
    expect(outside.refusal(op("outside.trash", { files: [file] }))).toEqual(noPermission(file));
    // Granted another folder: still none of this one's.
    journal.bindings.grantRead(ROOT, join(desktop, "Scan"));
    expect(outside.refusal(op("outside.read", { key: file, max_bytes: null }))).toEqual(noPermission(file));
    expect(tools.ran).toEqual([]);
  });

  it("reads, lists and walks inside a grant like the folder's own tools", async () => {
    journal.bindings.grantRead(ROOT, desktop);
    const outside = layer();
    const context = { folder: desktop, home: join(base, "home"), env: {} };
    const file = join(desktop, "note.txt");
    const same = async (kind: string, args: Record<string, unknown>) => {
      expect(outside.refusal(op(`outside.${kind}`, args)), kind).toBeNull();
      const got = await outside.run(op(`outside.${kind}`, args), never());
      expect(got, kind).toEqual(await perform(kind, args, context, never()));
      return got;
    };
    expect(await same("read", { key: file, max_bytes: null })).toEqual({ ok: Buffer.from("first\nsecond\n").toString("base64") });
    await same("read", { key: file, max_bytes: 3 });
    await same("read_lines", { key: file, encoding: "utf-8", offset: 2, limit: 1, max_bytes: 100 });
    expect(await same("stat", { key: file })).toMatchObject({ ok: { is_dir: false, size: 13, revision: expect.any(String) } });
    await same("stat", { key: desktop });
    await same("list_dir", { key: desktop });
    await same("list_dir", { key: join(desktop, "Scan") });
    await same("read", { key: desktop, max_bytes: null }); // EISDIR, in the same words
    await same("read", { key: join(desktop, "gone.txt"), max_bytes: null }); // ENOENT
    expect(await outside.run(op("outside.resolve", { path: `${desktop}/Scan/` }), never())).toEqual({ ok: join(desktop, "Scan") });
    const walked = await outside.run(op("outside.walk", { key: desktop, skip: [], skip_top: [], skip_hidden: false, since: null }), never());
    const theirs = await perform("walk", { key: desktop, skip: [], skip_top: [], skip_hidden: false, since: null }, context, never());
    expect(walked).toMatchObject({ ok: { files: (theirs as { ok: { files: unknown } }).ok.files, truncated: false, cursor: expect.any(String) } });
    expect(tools.ran).toEqual([]);
  });

  it("refuses a path that leads out of the grant through a link", async () => {
    journal.bindings.grantRead(ROOT, desktop);
    const elsewhere = join(base, "elsewhere");
    symlinkSync(elsewhere, join(desktop, "elsewhere"));
    symlinkSync(join(elsewhere, "secret.txt"), join(desktop, "secret.txt"));
    const outside = layer();
    for (const key of [join(desktop, "elsewhere", "secret.txt"), join(desktop, "secret.txt"), join(desktop, "elsewhere")]) {
      expect(outside.refusal(op("outside.read", { key, max_bytes: null })), key).toEqual(noPermission(key));
      expect(await outside.run(op("outside.read", { key, max_bytes: null }), never()), key).toEqual(noPermission(key));
    }
    expect(outside.refusal(op("outside.read", { key: `${desktop}/../.ssh/id`, max_bytes: null }))).toMatchObject({ error: { type: "sandbox" } });
    // A listing names a link as any name, and a walk leaves it out: neither says where it leads.
    expect(await outside.run(op("outside.list_dir", { key: desktop }), never())).toEqual({ ok: expect.arrayContaining(["elsewhere", "secret.txt"]) });
    const walked = await outside.run(op("outside.walk", { key: desktop, skip: [], skip_top: [], skip_hidden: false, since: null }), never());
    expect(JSON.stringify(walked)).not.toContain("secret");
    // A folder swapped for a link after the grant was checked, and before it is read: nothing is read through it.
    const scan = join(desktop, "Scan");
    const swappedAfterCheck = async (kind: string, args: Record<string, unknown>) => {
      const key = args.key as string;
      let swapped = false;
      const swapping: FdPaths = {
        ...fdPaths,
        lstat: (where) => {
          try {
            return fdPaths.lstat(where);
          } finally {
            // The check's last look is at the path itself: the way to it is swapped just after.
            if (where === key && !swapped) {
              swapped = true;
              renameSync(scan, `${scan}.${kind}`);
              symlinkSync(elsewhere, scan);
            }
          }
        },
      };
      const outcome = await layer(swapping).run(op(`outside.${kind}`, args), never());
      rmSync(scan);
      renameSync(`${scan}.${kind}`, scan);
      expect(swapped, kind).toBe(true);
      // What it answers, if anything, holds nothing of elsewhere's: not its file's name, nor its data.
      const answered = JSON.stringify("ok" in outcome ? outcome.ok : null);
      expect(answered, kind).not.toMatch(new RegExp(`secret|${Buffer.from("secret").toString("base64")}`));
      return outcome;
    };
    expect(await swappedAfterCheck("read", { key: join(scan, "secret.txt"), max_bytes: null })).toMatchObject({ error: { type: "os" } });
    await swappedAfterCheck("read_lines", { key: join(scan, "secret.txt"), encoding: "utf-8", offset: 1, limit: 1, max_bytes: 100 });
    // A folder in it, which is elsewhere's once the way to it is swapped.
    mkdirSync(join(elsewhere, "inner"));
    writeFileSync(join(elsewhere, "inner", "secret.txt"), "secret");
    for (const key of [scan, join(scan, "inner")]) {
      expect(await swappedAfterCheck("list_dir", { key }), key).toMatchObject({ error: { type: "os" } });
      await swappedAfterCheck("walk", { key, skip: [], skip_top: [], skip_hidden: false, since: null });
    }
    expect(await swappedAfterCheck("stat", { key: join(scan, "secret.txt") })).toEqual({ ok: null });
  });

  it("passes every other kind to the tools below", async () => {
    const outside = layer();
    const read = op("read", { key: join(work, "a"), max_bytes: null });
    expect(await outside.run(read, never())).toEqual({ ok: "below" });
    expect(tools.ran).toEqual([read]);
    expect(outside.refusal(op("run", {}))).toEqual({ error: { type: "sandbox", message: "tools refuse" } });
    expect(outside.refusal(op("write", {}))).toBeNull();
    expect(outside.guards()).toBe(guards);
    expect(outside.live()).toEqual(["live-root"]);
    await outside.stop();
    expect(tools.stop).toHaveBeenCalled();
    // An optional member is there where the tools below have it, and only there.
    expect(outside.takeOver?.(ROOT)).toBe(true);
    expect(outside.show).toBeUndefined();
    // A copy or a trash is not done here yet: its ticked files are let go all the same.
    journal.bindings.grantRead(ROOT, desktop);
    const copy = op("outside.copy", { files: [join(desktop, "note.txt")], to: work });
    expect(await outside.run(copy, never())).toEqual({ error: { type: "unsupported", message: "This computer cannot do 'outside.copy' yet" } });
    expect(chosenFor).toHaveBeenCalledWith(copy.id);
    expect(tools.ran).toEqual([read]);
  });
});
