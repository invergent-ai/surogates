// The Outside layer: a chat's grants of folders outside its own, and its reads in them, done by the app itself.

import { chmodSync, constants, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync, symlinkSync, truncateSync, writeFileSync, writeSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BOOT_ID, type FolderGuards } from "../src/binding/folder.js";
import { MAX_READ_BYTES } from "../src/files/answers.js";
import { type FdPaths, fdPaths, type Where } from "../src/files/fd-paths.js";
import { perform } from "../src/files/operations.js";
import { OperationJournal } from "../src/journal/journal.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import { numberedName, Outside } from "../src/outside/executor.js";
import { BIN } from "../src/platform.js";
import type { ToolLayer } from "../src/shell/device-stack.js";

const ROOT = "root-1";

let base: string;
let desktop: string;
let work: string;
let guards: FolderGuards;
let journal: OperationJournal;
let tools: ToolLayer & { ran: Operation[] };
let chosenFor: ReturnType<typeof vi.fn>;
let bin: string;
// The Recycle Bin, as a folder a trashed file is moved into by its path.
let trash: (path: string) => Promise<void>;

const getPath = (name: string) => join(base, "home", name === "desktop" ? "Desktop" : name);

let next = 0;
const op = (kind: string, args: Record<string, unknown>): Operation => {
  next += 1;
  return { id: `${kind}-${next}`, sessionId: ROOT, callingSessionId: ROOT, invocationId: "1:call", ordinal: next, kind, args, digest: "d" };
};
const never = () => new AbortController().signal;
const layer = (fd: FdPaths = fdPaths) => new Outside({ tools, bindings: journal.bindings, approvals: { chosenFor }, fd, getPath, trash: (path) => trash(path) });

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
  // The chat's folder's own identity, as the binder confirms it: the layer tells the chat's folder by it.
  const { dev, ino } = statSync(work);
  journal.bindings.add({ root: ROOT, nonce: "n", folder: work, dev, ino, boot: BOOT_ID, mode: "free", boundAt: 1 });
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
  bin = join(base, "bin");
  mkdirSync(bin);
  trash = async (path) => renameSync(path, join(bin, basename(path)));
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
  });
});

describe("outside.copy", () => {
  // The tools below as the chat's folder's own file helper: each operation done in *work*.
  const folderTools = () => {
    tools.run = async (operation) => {
      tools.ran.push(operation);
      return perform(operation.kind, operation.args, { folder: work, home: join(base, "home"), env: {} }, never());
    };
  };
  const copy = async (files: string[], to: string, ticked = files) => {
    chosenFor.mockReturnValueOnce(ticked);
    const operation = op("outside.copy", { files, to });
    const outside = layer();
    expect(outside.refusal(operation)).toBeNull();
    const outcome = await outside.run(operation, never());
    expect(chosenFor).toHaveBeenCalledWith(operation.id);
    return outcome;
  };

  it("numbers a name as name (n).ext", () => {
    expect(numberedName("cerere.pdf", 0)).toBe("cerere.pdf");
    expect(numberedName("cerere.pdf", 1)).toBe("cerere (1).pdf");
    expect(numberedName("raport.final.xlsx", 2)).toBe("raport.final (2).xlsx");
    expect(numberedName(".env", 3)).toBe(".env (3)");
    expect(numberedName("Makefile", 100)).toBe("Makefile (100)");
  });

  it("copies the ticked files into the chat's folder and remembers their originals", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const cerere = join(desktop, "Scan", "cerere.pdf");
    const unticked = join(desktop, "Scan", "other.pdf");
    writeFileSync(unticked, "other");
    const revision = ((await perform("stat", { key: note }, { folder: desktop, home: join(base, "home"), env: {} }, never())) as { ok: { revision: string } }).ok.revision;
    expect(await copy([note, cerere, unticked], work, [note, cerere])).toEqual({
      ok: { copied: [{ from: note, to: join(work, "note.txt") }, { from: cerere, to: join(work, "cerere.pdf") }], skipped: [] },
    });
    expect(readFileSync(join(work, "note.txt"), "utf8")).toBe("first\nsecond\n");
    expect(readFileSync(join(work, "cerere.pdf"), "utf8")).toBe("pdf");
    expect(existsSync(join(work, "other.pdf"))).toBe(false);
    expect(journal.bindings.copyOf(ROOT, join(work, "note.txt"))).toEqual({ original: note, revision });
    expect(journal.bindings.copyOf(ROOT, join(work, "cerere.pdf"))).toMatchObject({ original: cerere });
    // Written by the folder's own tools, as creates only, never asked again.
    expect(tools.ran.filter((ran) => ran.kind === "write").map((ran) => ran.args.create)).toEqual([true, true]);
    // The originals stay as they were.
    expect(readFileSync(note, "utf8")).toBe("first\nsecond\n");
  });

  it("copies from one granted folder into another, and never through the chat's folder", async () => {
    folderTools();
    const documents = join(base, "home", "Documents");
    mkdirSync(join(documents, "PONTAJE"), { recursive: true });
    journal.bindings.grantRead(ROOT, desktop);
    journal.bindings.grantRead(ROOT, documents);
    const cerere = join(desktop, "Scan", "cerere.pdf");
    expect(await copy([cerere], join(documents, "PONTAJE"))).toEqual({
      ok: { copied: [{ from: cerere, to: join(documents, "PONTAJE", "cerere.pdf") }], skipped: [] },
    });
    expect(readFileSync(join(documents, "PONTAJE", "cerere.pdf"), "utf8")).toBe("pdf");
    expect(readFileSync(cerere, "utf8")).toBe("pdf");
    expect(tools.ran).toEqual([]);
    expect(readdirSync(work)).toEqual([]);
    // And out of the chat's folder: read by its own tools, written by the app.
    writeFileSync(join(work, "raport.txt"), "raport");
    expect(await copy([join(work, "raport.txt")], desktop)).toEqual({
      ok: { copied: [{ from: join(work, "raport.txt"), to: join(desktop, "raport.txt") }], skipped: [] },
    });
    expect(readFileSync(join(desktop, "raport.txt"), "utf8")).toBe("raport");
    expect(tools.ran.map((ran) => ran.kind)).not.toContain("write");
  });

  it("numbers a copy whose name is taken instead of overwriting", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    writeFileSync(join(work, "note.txt"), "mine");
    writeFileSync(join(work, "note (1).txt"), "mine too");
    expect(await copy([note], work)).toMatchObject({ ok: { copied: [{ from: note, to: join(work, "note (2).txt") }] } });
    expect(readFileSync(join(work, "note.txt"), "utf8")).toBe("mine");
    expect(readFileSync(join(work, "note (1).txt"), "utf8")).toBe("mine too");
    expect(readFileSync(join(work, "note (2).txt"), "utf8")).toBe("first\nsecond\n");
    // Onto itself: a numbered copy beside it.
    expect(await copy([note], desktop)).toMatchObject({ ok: { copied: [{ from: note, to: join(desktop, "note (1).txt") }] } });
    expect(readFileSync(note, "utf8")).toBe("first\nsecond\n");
    // A link at the name is a name taken: nothing is written through it.
    symlinkSync(join(base, "elsewhere", "secret.txt"), join(desktop, "cerere.pdf"));
    const cerere = join(desktop, "Scan", "cerere.pdf");
    expect(await copy([cerere], desktop)).toMatchObject({ ok: { copied: [{ from: cerere, to: join(desktop, "cerere (1).pdf") }] } });
    expect(readFileSync(join(base, "elsewhere", "secret.txt"), "utf8")).toBe("secret");
  });

  it("refuses a destination that is neither the chat's folder nor granted", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const outside = layer();
    for (const to of [join(base, "elsewhere"), join(base, "home")]) {
      const refused = { error: { type: "denied", message: `The destination ${to} is neither this chat's folder nor a folder it may read.` } };
      expect(outside.refusal(op("outside.copy", { files: [note], to })), to).toEqual(refused);
      // Run as well, for an operation that came without the question.
      chosenFor.mockReturnValueOnce([note]);
      expect(await outside.run(op("outside.copy", { files: [note], to }), never()), to).toEqual(refused);
    }
    // Whatever its files, malformed ones the approvals' to refuse.
    expect(outside.refusal(op("outside.copy", { files: "note.txt", to: join(base, "elsewhere") }))).toMatchObject({ error: { type: "denied" } });
    // A source in the chat's folder is the chat's to copy; one elsewhere is not.
    expect(outside.refusal(op("outside.copy", { files: [join(work, "a.txt")], to: desktop }))).toBeNull();
    expect(outside.refusal(op("outside.copy", { files: [join(base, "elsewhere", "secret.txt")], to: work })))
      .toEqual(noPermission(join(base, "elsewhere", "secret.txt")));
    expect(readdirSync(join(base, "elsewhere"))).toEqual(["secret.txt"]);
    expect(tools.ran).toEqual([]);
  });

  it("reports a file that vanished before the copy as skipped, and copies the rest", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const cerere = join(desktop, "Scan", "cerere.pdf");
    const large = join(desktop, "large.bin");
    writeFileSync(large, "");
    truncateSync(large, MAX_READ_BYTES + 1);
    const outside = layer();
    const operation = op("outside.copy", { files: [note, large, cerere], to: work });
    expect(outside.refusal(operation)).toBeNull();
    rmSync(note);
    chosenFor.mockReturnValueOnce([note, large, cerere]);
    expect(await outside.run(operation, never())).toEqual({
      ok: {
        copied: [{ from: cerere, to: join(work, "cerere.pdf") }],
        skipped: [{ from: note, reason: expect.stringContaining("No such file") }, { from: large, reason: "too large" }],
      },
    });
    expect(readdirSync(work)).toEqual(["cerere.pdf"]);
  });

  it("copies into a folder in the chat's folder by its own tools, even where a grant holds the chat's folder", async () => {
    folderTools();
    // A grant that holds the chat's folder: its subfolder is still the chat's, written by its tools, never by the app.
    journal.bindings.grantRead(ROOT, base);
    const sub = join(work, "sub");
    mkdirSync(sub);
    const note = join(desktop, "note.txt");
    expect(layer().refusal(op("outside.copy", { files: [note], to: sub }))).toBeNull();
    expect(await copy([note], sub)).toEqual({ ok: { copied: [{ from: note, to: join(sub, "note.txt") }], skipped: [] } });
    expect(tools.ran.filter((ran) => ran.kind === "write").map((ran) => ran.args.key)).toEqual([join(sub, "note.txt")]);
    expect(journal.bindings.copyOf(ROOT, join(sub, "note.txt"))).toMatchObject({ original: note });
  });

  it("forgets a copy's record at a key a file of the chat's own is copied to", async () => {
    folderTools();
    // Recorded once, and its file deleted since, without its record.
    journal.bindings.recordCopy(ROOT, join(work, "raport.txt"), join(desktop, "raport.txt"), "1:2:3:4:5");
    mkdirSync(join(work, "drafts"));
    writeFileSync(join(work, "drafts", "raport.txt"), "draft");
    expect(await copy([join(work, "drafts", "raport.txt")], work)).toMatchObject({ ok: { copied: [{ to: join(work, "raport.txt") }] } });
    expect(journal.bindings.copyOf(ROOT, join(work, "raport.txt"))).toBeUndefined();
  });

  it("takes the next name in the chat's folder when one is made there after its look", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const below = tools.run;
    let looked = false;
    // The first look at the name finds nothing; a file is there by the write.
    tools.run = async (operation, signal) => {
      if (operation.kind === "stat" && operation.args.key === join(work, "note.txt") && !looked) {
        looked = true;
        writeFileSync(join(work, "note.txt"), "made meanwhile");
        tools.ran.push(operation);
        return { ok: null };
      }
      return below(operation, signal);
    };
    const note = join(desktop, "note.txt");
    expect(await copy([note], work)).toMatchObject({ ok: { copied: [{ from: note, to: join(work, "note (1).txt") }], skipped: [] } });
    expect(readFileSync(join(work, "note.txt"), "utf8")).toBe("made meanwhile");
  });

  it("removes a copy made outside that does not check out", async () => {
    journal.bindings.grantRead(ROOT, desktop);
    const cerere = join(desktop, "Scan", "cerere.pdf");
    // Something else writes into the new file as it is made: its size is then not the original's.
    const meddling: FdPaths = {
      ...fdPaths,
      open: (where, flags, mode) => {
        const file = fdPaths.open(where, flags, mode);
        if (flags & constants.O_CREAT) writeSync(file, "longer than the original");
        return file;
      },
    };
    chosenFor.mockReturnValueOnce([cerere]);
    const outside = new Outside({ tools, bindings: journal.bindings, approvals: { chosenFor }, fd: meddling, getPath, trash });
    expect(await outside.run(op("outside.copy", { files: [cerere], to: desktop }), never())).toEqual({
      ok: { copied: [], skipped: [{ from: cerere, reason: expect.stringContaining("check") }] },
    });
    expect(readdirSync(desktop).sort()).toEqual(["Scan", "note.txt"]);
    // A file put at the name after the copy was made is not the copy's: it is left alone.
    let created = false;
    const swapping: FdPaths = {
      ...meddling,
      open: (where, flags, mode) => {
        created ||= (flags & constants.O_CREAT) !== 0;
        return meddling.open(where, flags, mode);
      },
      lstat: (where) => {
        // Only the copy's own name, once it was made: the original's look before its read is let be.
        if (created && typeof where !== "string" && where.name === "cerere.pdf") {
          // Made beside it first, then moved over it: a new file of its own, not one reusing the copy's freed inode.
          writeFileSync(join(desktop, "user.tmp"), "the user's");
          renameSync(join(desktop, "user.tmp"), join(desktop, "cerere.pdf"));
        }
        return fdPaths.lstat(where);
      },
    };
    chosenFor.mockReturnValueOnce([cerere]);
    const again = new Outside({ tools, bindings: journal.bindings, approvals: { chosenFor }, fd: swapping, getPath, trash });
    expect(await again.run(op("outside.copy", { files: [cerere], to: desktop }), never())).toEqual({
      ok: { copied: [], skipped: [{ from: cerere, reason: expect.stringContaining("check") }] },
    });
    expect(created).toBe(true);
    expect(readFileSync(cerere, "utf8")).toBe("pdf");
    expect(readFileSync(join(desktop, "cerere.pdf"), "utf8")).toBe("the user's");
  });
});

describe("outside.trash and outside.put_back", () => {
  const folderTools = () => {
    tools.run = async (operation) => {
      tools.ran.push(operation);
      return perform(operation.kind, operation.args, { folder: work, home: join(base, "home"), env: {} }, never());
    };
  };
  const trashOf = async (files: string[], ticked = files) => {
    chosenFor.mockReturnValueOnce(ticked);
    const operation = op("outside.trash", { files });
    const outcome = await layer().run(operation, never());
    expect(chosenFor).toHaveBeenCalledWith(operation.id);
    return outcome;
  };
  // *file* copied into the chat's folder, as outside.copy makes and records one.
  const copied = async (file: string) => {
    chosenFor.mockReturnValueOnce([file]);
    const outcome = await layer().run(op("outside.copy", { files: [file], to: work }), never());
    return (outcome as { ok: { copied: Array<{ to: string }> } }).ok.copied[0]!.to;
  };
  const putBack = (copy: string, original: string) => {
    const operation = op("outside.put_back", { copy, original });
    return layer().run(operation, never());
  };
  const changedAt = (path: string) => readdirSync(path).filter((name) => name.includes("surogate-put-back"));

  it("moves only the ticked files to the bin", async () => {
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const cerere = join(desktop, "Scan", "cerere.pdf");
    const other = join(desktop, "other.txt");
    writeFileSync(other, "other");
    expect(layer().refusal(op("outside.trash", { files: [note, cerere, other] }))).toBeNull();
    expect(await trashOf([note, cerere, other], [note, cerere])).toEqual({ ok: { trashed: [note, cerere], kept: [] } });
    expect(readdirSync(bin).sort()).toEqual(["cerere.pdf", "note.txt"]);
    expect(readFileSync(other, "utf8")).toBe("other");
    // Taken up again after a restart, its user's ticks are gone: nothing is trashed that nobody chose.
    expect(await layer().run(op("outside.trash", { files: [other] }), never())).toMatchObject({ error: { type: "denied" } });
    expect(existsSync(other)).toBe(true);
    expect(tools.ran).toEqual([]);
  });

  it("keeps a ticked file that became a link or vanished since it was asked about", async () => {
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const cerere = join(desktop, "Scan", "cerere.pdf");
    const secret = join(base, "elsewhere", "secret.txt");
    rmSync(note);
    symlinkSync(secret, note);
    rmSync(join(desktop, "Scan"), { recursive: true });
    mkdirSync(join(desktop, "Folder"));
    // A folder, the grant's own too, is no file: the windows ask about files.
    expect(await trashOf([note, cerere, join(desktop, "Folder"), desktop])).toEqual({
      ok: {
        trashed: [],
        kept: [
          { file: note, reason: expect.any(String) }, { file: cerere, reason: expect.any(String) },
          { file: join(desktop, "Folder"), reason: "not a file" }, { file: desktop, reason: "not a file" },
        ],
      },
    });
    expect(readdirSync(bin)).toEqual([]);
    expect(readFileSync(secret, "utf8")).toBe("secret");
    // Made a link after the grant's look at it, and before the trash: kept, as a read through it would be refused.
    const plain = join(desktop, "plain.txt");
    writeFileSync(plain, "plain");
    const swapping: FdPaths = {
      ...fdPaths,
      lstat: (where) => {
        // Swapped as the walk reaches it: after every look by its path.
        if (typeof where !== "string" && where.name === "plain.txt" && !existsSync(`${plain}.moved`)) {
          renameSync(plain, `${plain}.moved`);
          symlinkSync(secret, plain);
        }
        return fdPaths.lstat(where);
      },
    };
    chosenFor.mockReturnValueOnce([plain]);
    expect(await layer(swapping).run(op("outside.trash", { files: [plain] }), never())).toEqual({
      ok: { trashed: [], kept: [{ file: plain, reason: expect.stringContaining("link") }] },
    });
    expect(readdirSync(bin)).toEqual([]);
  });

  it("refuses to trash or put back a file of the chat's own folder, even where a grant holds it", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, base);
    const own = join(work, "a.txt");
    writeFileSync(own, "mine");
    const outside = layer();
    for (const file of [own, work, base]) {
      expect(outside.refusal(op("outside.trash", { files: [file] })), file).toMatchObject({ error: { type: "denied" } });
    }
    expect(await trashOf([own, base])).toEqual({
      ok: { trashed: [], kept: [{ file: own, reason: expect.stringContaining("chat's folder") }, { file: base, reason: expect.stringContaining("chat's folder") }] },
    });
    journal.bindings.recordCopy(ROOT, join(work, "b.txt"), own, "1:2:3:4:5");
    writeFileSync(join(work, "b.txt"), "changes");
    expect(outside.refusal(op("outside.put_back", { copy: join(work, "b.txt"), original: own }))).toMatchObject({ error: { type: "denied" } });
    expect(await putBack(join(work, "b.txt"), own)).toMatchObject({ error: { type: "denied" } });
    expect(readFileSync(own, "utf8")).toBe("mine");
    expect(readdirSync(bin)).toEqual([]);
  });

  it("puts the changes back, sends the old version to the bin and deletes the working copy", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const copy = await copied(note);
    writeFileSync(copy, "changed\n");
    expect(layer().refusal(op("outside.put_back", { copy, original: note }))).toBeNull();
    expect(await putBack(copy, note)).toEqual({ ok: { original: note, copyDeleted: true } });
    expect(readFileSync(note, "utf8")).toBe("changed\n");
    expect(readFileSync(join(bin, "note.txt"), "utf8")).toBe("first\nsecond\n");
    expect(existsSync(copy)).toBe(false);
    expect(journal.bindings.copyOf(ROOT, copy)).toBeUndefined();
    expect(changedAt(desktop)).toEqual([]);
    // Deleted by the chat's folder's own tools.
    expect(tools.ran.filter((ran) => ran.kind === "delete").map((ran) => ran.args.key)).toEqual([copy]);
  });

  it("refuses to put back when the original changed since it was copied", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const copy = await copied(note);
    writeFileSync(copy, "changed\n");
    writeFileSync(note, "the user's own edit\n");
    expect(await putBack(copy, note)).toEqual({
      error: { type: "conflict", message: `${note} changed since it was copied. Nothing was replaced; the changes are in ${copy}.` },
    });
    expect(readFileSync(note, "utf8")).toBe("the user's own edit\n");
    expect(readFileSync(copy, "utf8")).toBe("changed\n");
    expect(readdirSync(bin)).toEqual([]);
    expect(changedAt(desktop)).toEqual([]);
  });

  it("leaves everything as it was when the original cannot be replaced", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const copy = await copied(note);
    writeFileSync(copy, "changed\n");
    // Open in Excel: Windows refuses to move it, and Electron's shell.trashItem says so in an Error of its own words, with no code.
    trash = async () => {
      throw new Error("Failed to move item to trash");
    };
    const outcome = await putBack(copy, note);
    expect(outcome).toMatchObject({ error: { type: "os", message: expect.stringContaining("open in another program") } });
    expect((outcome as { error: { message: string } }).error.message).toContain(copy);
    expect(readFileSync(note, "utf8")).toBe("first\nsecond\n");
    expect(readFileSync(copy, "utf8")).toBe("changed\n");
    expect(journal.bindings.copyOf(ROOT, copy)).toMatchObject({ original: note });
    expect(changedAt(desktop)).toEqual([]);
    expect(tools.ran.map((ran) => ran.kind)).not.toContain("delete");
  });

  it("never replaces a file made at the original's name after it went to the bin", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const copy = await copied(note);
    writeFileSync(copy, "changed\n");
    trash = async (path) => {
      renameSync(path, join(bin, basename(path)));
      writeFileSync(path, "saved meanwhile");
    };
    expect(await putBack(copy, note)).toEqual({
      error: { type: "os", message: `The old version is in the ${BIN}; the changes are still in ${copy}.` },
    });
    expect(readFileSync(note, "utf8")).toBe("saved meanwhile");
    expect(readFileSync(copy, "utf8")).toBe("changed\n");
    expect(changedAt(desktop)).toEqual([]);
  });

  it("refuses a put back for a copy it did not make", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const cerere = join(desktop, "Scan", "cerere.pdf");
    writeFileSync(join(work, "note.txt"), "never copied");
    const outside = layer();
    expect(outside.refusal(op("outside.put_back", { copy: join(work, "note.txt"), original: note }))).toMatchObject({ error: { type: "denied" } });
    expect(await putBack(join(work, "note.txt"), note)).toMatchObject({ error: { type: "denied" } });
    // A copy of another file is no copy of this one.
    const copy = await copied(cerere);
    expect(await putBack(copy, note)).toMatchObject({ error: { type: "denied" } });
    expect(readFileSync(note, "utf8")).toBe("first\nsecond\n");
    expect(readdirSync(bin)).toEqual([]);
    expect(tools.ran.map((ran) => ran.kind)).not.toContain("delete");
  });
});

describe("outside put back's edges, and the chat's folder by another name", () => {
  const folderTools = () => {
    tools.run = async (operation) => {
      tools.ran.push(operation);
      return perform(operation.kind, operation.args, { folder: work, home: join(base, "home"), env: {} }, never());
    };
  };
  const copied = async (file: string) => {
    chosenFor.mockReturnValueOnce([file]);
    const outcome = await layer().run(op("outside.copy", { files: [file], to: work }), never());
    return (outcome as { ok: { copied: Array<{ to: string }> } }).ok.copied[0]!.to;
  };
  const temporaries = () => readdirSync(desktop).filter((name) => name.includes("surogate-put-back"));
  // A drive that ignores case, as Windows's and macOS's do: "WORK" is the chat's folder "work". *paths* false spells it
  // so only by a held folder's name, as a look by path finds nothing and the folder is swapped in after.
  const caseless = (paths = true): FdPaths => {
    const plain = (where: Where): Where => typeof where === "string"
      ? (paths ? where.replace(/\/WORK(?=\/|$)/, "/work") : where)
      : where.name === "WORK" ? { ...where, name: "work" } : where;
    return {
      ...fdPaths,
      hold: (where) => fdPaths.hold(plain(where)),
      lstat: (where) => fdPaths.lstat(plain(where)),
      open: (where, flags, mode) => fdPaths.open(plain(where), flags, mode),
      unlink: (where) => fdPaths.unlink(plain(where)),
      link: (from, to) => fdPaths.link(plain(from), plain(to)),
    };
  };

  it("keeps the original's permissions", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    chmodSync(note, 0o600);
    const copy = await copied(note);
    writeFileSync(copy, "changed\n");
    expect(await layer().run(op("outside.put_back", { copy, original: note }), never())).toEqual({ ok: { original: note, copyDeleted: true } });
    expect(statSync(note).mode & 0o777).toBe(0o600);
  });

  it("leaves no second name of the file put back when the rename could not remove it", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const copy = await copied(note);
    writeFileSync(copy, "changed\n");
    let refused = false;
    const sticky: FdPaths = {
      ...fdPaths,
      unlink: (where) => {
        if (!refused && typeof where !== "string" && where.name?.includes("surogate-put-back")) {
          refused = true;
          throw Object.assign(new Error("EBUSY: resource busy or locked"), { code: "EBUSY" });
        }
        fdPaths.unlink(where);
      },
    };
    expect(await layer(sticky).run(op("outside.put_back", { copy, original: note }), never())).toEqual({ ok: { original: note, copyDeleted: true } });
    expect(refused).toBe(true);
    expect(temporaries()).toEqual([]);
    expect(readFileSync(note, "utf8")).toBe("changed\n");
  });

  it("removes its temporary file when its identity cannot be read", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const copy = await copied(note);
    let made = -1;
    const blind: FdPaths = {
      ...fdPaths,
      open: (where, flags, mode) => {
        const file = fdPaths.open(where, flags, mode);
        if (typeof where !== "string" && where.name?.includes("surogate-put-back")) made = file;
        return file;
      },
      fstat: ((file: number, options?: { bigint: true }) => {
        if (file === made) throw Object.assign(new Error("EIO: i/o error"), { code: "EIO", errno: -5 });
        return options ? fdPaths.fstat(file, options) : fdPaths.fstat(file);
      }) as FdPaths["fstat"],
    };
    expect(await layer(blind).run(op("outside.put_back", { copy, original: note }), never())).toMatchObject({ error: { type: "os" } });
    expect(temporaries()).toEqual([]);
    expect(readFileSync(note, "utf8")).toBe("first\nsecond\n");
    expect(readdirSync(bin)).toEqual([]);
  });

  it("says an original that is gone is gone, and keeps the copy", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const copy = await copied(note);
    rmSync(note);
    expect(await layer().run(op("outside.put_back", { copy, original: note }), never())).toEqual({
      error: { type: "conflict", message: `${note} is no longer there; the old version may already be in the ${BIN}. The changes are in ${copy}.` },
    });
    expect(existsSync(copy)).toBe(true);
  });

  it("says when the working copy could not be deleted, and forgets it all the same", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, desktop);
    const note = join(desktop, "note.txt");
    const copy = await copied(note);
    const below = tools.run;
    tools.run = async (operation, signal) => operation.kind === "delete" ? { error: { type: "os", message: "busy" } } : below(operation, signal);
    expect(await layer().run(op("outside.put_back", { copy, original: note }), never())).toEqual({ ok: { original: note, copyDeleted: false } });
    expect(journal.bindings.copyOf(ROOT, copy)).toBeUndefined();
  });

  it("reads the chat's own files through a grant that holds its folder, by any spelling", async () => {
    journal.bindings.grantRead(ROOT, base);
    writeFileSync(join(work, "a.txt"), "mine");
    // By its own path, and, on a drive that ignores case, by another spelling.
    for (const [outside, dir] of [[layer(), work], [layer(caseless()), work], [layer(caseless()), join(base, "WORK")]] as const) {
      const key = join(dir, "a.txt");
      expect(outside.refusal(op("outside.read", { key, max_bytes: null })), key).toBeNull();
      expect(await outside.run(op("outside.read", { key, max_bytes: null }), never()), key).toEqual({ ok: Buffer.from("mine").toString("base64") });
      expect(await outside.run(op("outside.stat", { key }), never()), key).toMatchObject({ ok: { is_dir: false, size: 4 } });
      expect(await outside.run(op("outside.list_dir", { key: dir }), never()), key).toEqual({ ok: ["a.txt"] });
    }
  });

  it("tells the chat's folder by device and inode both, after a reboot too", async () => {
    folderTools();
    // Rebooted since it was bound: the binder would compare the inode alone.
    journal.bindings.replace({ ...journal.bindings.get(ROOT)!, boot: "an earlier boot" });
    journal.bindings.grantRead(ROOT, desktop);
    const scan = join(desktop, "Scan");
    const chat = fdPaths.lstat(work);
    // Scan, on another drive, with the chat's folder's inode number.
    const twin = (st: ReturnType<FdPaths["lstat"]>) => Object.assign(Object.create(Object.getPrototypeOf(st)), st, { ino: chat.ino, dev: chat.dev + 1n });
    const held = new Set<number>();
    const lookalike: FdPaths = {
      ...fdPaths,
      hold: (where) => {
        const dir = fdPaths.hold(where);
        if (where === scan || (typeof where !== "string" && where.name === "Scan")) held.add(dir);
        return dir;
      },
      lstat: (where) => {
        const st = fdPaths.lstat(where);
        return where === scan || (typeof where !== "string" && where.name === "Scan") ? twin(st) : st;
      },
      fstat: ((file: number, options?: { bigint: true }) => {
        if (!options) return fdPaths.fstat(file);
        const st = fdPaths.fstat(file, options);
        return held.has(file) ? twin(st) : st;
      }) as FdPaths["fstat"],
    };
    const cerere = join(scan, "cerere.pdf");
    const outside = layer(lookalike);
    expect(outside.refusal(op("outside.trash", { files: [cerere] }))).toBeNull();
    chosenFor.mockReturnValueOnce([cerere]);
    expect(await outside.run(op("outside.trash", { files: [cerere] }), never())).toEqual({ ok: { trashed: [cerere], kept: [] } });
    expect(held.size).toBeGreaterThan(0);
  });

  it("tells the chat's folder by its identity, whatever its path's spelling", async () => {
    folderTools();
    journal.bindings.grantRead(ROOT, base);
    writeFileSync(join(work, "a.txt"), "mine");
    const other = join(base, "WORK", "a.txt");
    const note = join(desktop, "note.txt");
    const outside = layer(caseless());
    const refused = { error: { type: "denied", message: expect.stringContaining("chat's folder") } };
    // Trash.
    expect(outside.refusal(op("outside.trash", { files: [other] }))).toMatchObject(refused);
    chosenFor.mockReturnValueOnce([other]);
    expect(await outside.run(op("outside.trash", { files: [other] }), never())).toEqual({
      ok: { trashed: [], kept: [{ file: other, reason: expect.stringContaining("chat's folder") }] },
    });
    // Copy, from it and into it.
    expect(outside.refusal(op("outside.copy", { files: [other], to: desktop }))).toMatchObject(refused);
    chosenFor.mockReturnValueOnce([other]);
    expect(await outside.run(op("outside.copy", { files: [other], to: desktop }), never())).toEqual({
      ok: { copied: [], skipped: [{ from: other, reason: expect.stringContaining("another name") }] },
    });
    expect(outside.refusal(op("outside.copy", { files: [note], to: join(base, "WORK") }))).toMatchObject(refused);
    chosenFor.mockReturnValueOnce([note]);
    expect(await outside.run(op("outside.copy", { files: [note], to: join(base, "WORK") }), never())).toMatchObject(refused);
    // Put back over it.
    journal.bindings.recordCopy(ROOT, join(work, "b.txt"), other, "1:2:3:4:5");
    writeFileSync(join(work, "b.txt"), "changes");
    expect(outside.refusal(op("outside.put_back", { copy: join(work, "b.txt"), original: other }))).toMatchObject(refused);
    expect(await outside.run(op("outside.put_back", { copy: join(work, "b.txt"), original: other }), never())).toMatchObject(refused);
    // Spelled so only once its folder is held, as one swapped in after the look: the walk itself refuses it.
    const late = layer(caseless(false));
    chosenFor.mockReturnValueOnce([other]);
    expect(await late.run(op("outside.trash", { files: [other] }), never())).toEqual({
      ok: { trashed: [], kept: [{ file: other, reason: expect.stringContaining("another name") }] },
    });
    chosenFor.mockReturnValueOnce([other]);
    expect(await late.run(op("outside.copy", { files: [other], to: desktop }), never())).toMatchObject({
      ok: { copied: [], skipped: [{ from: other, reason: expect.stringContaining("another name") }] },
    });
    chosenFor.mockReturnValueOnce([note]);
    expect(await late.run(op("outside.copy", { files: [note], to: join(base, "WORK") }), never())).toMatchObject({
      ok: { copied: [], skipped: [{ from: note, reason: expect.stringContaining("another name") }] },
    });
    expect(await late.run(op("outside.put_back", { copy: join(work, "b.txt"), original: other }), never())).toMatchObject(refused);
    expect(readFileSync(join(work, "a.txt"), "utf8")).toBe("mine");
    expect(readdirSync(work).sort()).toEqual(["a.txt", "b.txt"]);
    expect(readdirSync(bin)).toEqual([]);
    expect(tools.ran.map((ran) => ran.kind).filter((kind) => kind === "write" || kind === "delete")).toEqual([]);
  });
});
