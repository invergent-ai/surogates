// Two project threads on one folder of this computer, end to end on the computer's side: the
// folder's place as the app's data gives it (history/place.ts), each thread in its copy in the
// guest, git as the agent's own user, and each landing written by the file helper's land kind, in
// a helper of its own started as the app starts one (dist/files/helper.js, outside its sandbox:
// the kind's checks are its own). The steps are a landing saga's, in its order. Around each, the
// real folder is compared with what the step may have changed: every name, mode, time and byte.
// Behind SUROGATE_VM_TESTS=1.

import { type ChildProcess, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { BOOT_ID } from "../../src/binding/folder.js";
import { copyOf, FolderReplaced, keptOf, placeOf, type PlaceOptions } from "../../src/history/place.js";
import type { Outcome } from "../../src/link/protocol.js";
import { forgettable } from "../../src/vm/history.js";
import { type Place, VmManager, type VmOptions } from "../../src/vm/manager.js";
import { agentDisk, alive, folderOf, IMAGE, KVM, needsKvm, until, USER } from "./guest-support.js";

beforeAll(needsKvm);

const HELPER = fileURLToPath(new URL("../../dist/files/helper.js", import.meta.url));
const ONE = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const TWO = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
const YOU = { name: "u1", email: "user:u1@surogate" };
const TITLES: Record<string, string> = { [ONE]: "Tidy the report", [TWO]: "Check the totals" };
const NOTHING_CUT = { restored: [], beside: [], lost: [], unread: [] };
// What a new file and a new folder get on this computer.
const NEW_FILE = 0o666 & ~process.umask();
const NEW_FOLDER = 0o777 & ~process.umask();

interface Change {
  path: string;
  before: string | null;
  after: string | null;
}

// One name in a folder, as its user has it: what it is, its mode, which file it is, its times and its bytes.
interface Entry {
  kind: "file" | "folder" | "other";
  mode: number;
  ino: string;
  links: number;
  size: number;
  mtime: string;
  ctime: string;
  sha: string | null;
}
type Picture = Record<string, Entry>;

const sha = (data: Buffer | string) => createHash("sha256").update(data).digest("hex");
/** Everything in the folder *top*, itself as ".", by its path from there. */
function picture(top: string): Picture {
  const seen: Picture = {};
  const look = (path: string, name: string) => {
    const found = lstatSync(path, { bigint: true });
    const kind = found.isFile() ? "file" : found.isDirectory() ? "folder" : "other";
    seen[name] = {
      kind, mode: Number(found.mode & 0o7777n), ino: String(found.ino), links: kind === "file" ? Number(found.nlink) : 0,
      size: kind === "file" ? Number(found.size) : 0, mtime: String(found.mtimeNs), ctime: String(found.ctimeNs),
      sha: kind === "file" ? sha(readFileSync(path)) : null,
    };
    if (kind === "folder") for (const child of readdirSync(path).sort()) look(join(path, child), name === "." ? child : `${name}/${child}`);
  };
  look(top, ".");
  return seen;
}
/** Each path that came, went, or is not in every way as it was. */
const differing = (before: Picture, after: Picture): string[] =>
  [...new Set([...Object.keys(before), ...Object.keys(after)])].filter((path) => JSON.stringify(before[path]) !== JSON.stringify(after[path])).sort();
const up = (path: string) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ".");
// The folders a file's coming or going shows in: each one above it that is not there yet, and the one they are made in.
function above(path: string, before: Picture): string[] {
  const folders: string[] = [];
  for (let folder = up(path); ; folder = up(folder)) {
    folders.push(folder);
    if (before[folder] !== undefined || folder === ".") return folders;
  }
}
// A folder a file came to or went from: the same folder, with the same mode, at another time.
const sameFolder = (before: Entry, after: Entry) => expect([after.kind, after.mode, after.ino]).toEqual([before.kind, before.mode, before.ino]);

// The app died where a landing stood.
class Died extends Error {}
// A step that must be answered before any file is written was not: the landing ends there, and says why.
class Refused extends Error {
  constructor(readonly step: string, readonly refusal: { type: string; code?: string; message: string }) {
    super(`${step}: ${refusal.message}`);
  }
}
// How long a landing waits for one answer of the sandbox's, or of its helper's: none is waited for without a bound.
const ANSWER_MS = 90_000;
const bounded = () => AbortSignal.timeout(ANSWER_MS);

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("two threads on one folder of this computer", { timeout: 180_000 }, () => {
  let dir: string;
  let data: string;
  let folder: string;
  let manager: VmManager;
  let options: VmOptions;
  let place: Place;
  const helpers = new Set<ChildProcess>();
  // Each landing ever begun in the folder, by its saga's id, with its journal: an id names one landing, ever.
  const begun = new Map<string, Map<string, Outcome>>();
  const ok = (outcome: Outcome) => {
    expect(outcome, JSON.stringify(outcome)).toHaveProperty("ok");
    return (outcome as { ok: Record<string, unknown> }).ok;
  };
  // Every request to the folder's history is of the one place the app's data gives the folder, under a bound of its own.
  const asked = (thread: string, action: string, args: Record<string, unknown> = {}) => manager.history({ place, thread, user: "u1", action, args }, bounded());
  // Nothing a request to the folder's history or a thread's command does writes the folder: around each it is as it was.
  const untouched = async (work: Promise<Outcome>) => {
    const before = picture(folder);
    const answer = ok(await work);
    expect(differing(before, picture(folder))).toEqual([]);
    return answer;
  };
  const history = (thread: string, action: string, args: Record<string, unknown> = {}) => untouched(asked(thread, action, args));
  // A command of the thread's, in its copy at the folder's path.
  const run = (thread: string, command: string) => untouched(manager.perform({
    id: `run-${Math.random()}`, root: thread, folder: folderOf(copyOf(place, thread)), at: folder, kind: "run", args: { command, workdir: null, timeout: 30 },
  }, bounded()));
  const real = (path: string) => readFileSync(join(folder, path), "utf8");
  const kept = (...inside: string[]) => join(keptOf(data, place), ...inside);
  // The steps of *saga* that this computer's land helper holds a record of, by their numbers.
  const recordsOf = (saga: string): number[] =>
    (existsSync(kept(saga)) ? readdirSync(kept(saga)) : []).flatMap((name) => /^(0|[1-9][0-9]*)\.json$/.exec(name)?.[1] ?? []).map(Number).sort((a, b) => a - b);

  // A landing's helper for *thread*, as the app starts one: on the real folder, given the thread's copy and where
  // the folder's landings keep what they replace. It puts back what an earlier one was cut short in before it says
  // it is ready; asked one thing at a time.
  const helperOf = (thread: string) => {
    const child = spawn(process.execPath, [HELPER], {
      env: { SUROGATE_FOLDER: folder, HOME: join(dir, "home"), PATH: "/usr/bin:/bin", SUROGATE_COPY: copyOf(place, thread), SUROGATE_KEPT: kept() },
      stdio: ["pipe", "pipe", "inherit"],
    });
    helpers.add(child);
    child.stdin.on("error", () => {});
    const waiting: Array<(outcome: Outcome) => void> = [];
    const exited = new Promise<void>((resolve) => {
      child.once("exit", () => {
        helpers.delete(child);
        resolve();
      });
    });
    const ready = new Promise<void>((resolve) => {
      createInterface({ input: child.stdout }).on("line", (line) => {
        const said = JSON.parse(line) as { ready?: boolean; outcome?: Outcome };
        if (said.ready) resolve();
        else if (said.outcome) waiting.shift()?.(said.outcome);
      });
    });
    let sent = 0;
    return {
      async land(args: Record<string, unknown>): Promise<Outcome> {
        const answered = new Promise<Outcome>((resolve) => waiting.push(resolve));
        void ready.then(() => child.stdin.write(`${JSON.stringify({ id: String(sent += 1), kind: "land", args })}\n`));
        let timer: NodeJS.Timeout | undefined;
        const late = new Promise<never>((_resolve, reject) => {
          timer = setTimeout(() => reject(new Error(`the landing's helper did not answer ${String(args.action)} within ${ANSWER_MS / 1000} s`)), ANSWER_MS);
        });
        return Promise.race([answered, late]).finally(() => clearTimeout(timer));
      },
      async end(): Promise<void> {
        child.stdin.end();
        await exited;
      },
      async kill(): Promise<void> {
        child.kill("SIGKILL");
        await exited;
      },
    };
  };

  // The app is killed: the landing's helper and the sandbox end where they stand, with nothing said to either.
  // Its next start has a sandbox of its own, on the same disks, and the folder's place as the dead one left it.
  const die = async (helper: ReturnType<typeof helperOf>) => {
    await helper.kill();
    const qemu = Number(readFileSync(join(options.run, "qemu.pid"), "utf8"));
    expect(readFileSync(`/proc/${qemu}/cmdline`, "utf8")).toContain(options.run);
    process.kill(qemu, "SIGKILL");
    await until(() => !alive(qemu));
    await manager.stop();
    options = { ...options, run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")) };
    manager = new VmManager(options);
  };

  /**
   * One landing of *thread*'s turn, as its saga runs it. Its helper is started before anything of the folder is
   * read, and asked what it put back. Then the look at the real files the turn changed, your edits picked up, the
   * turn committed, each file applied at the revision the look saw, the landing recorded, and what it kept
   * forgotten. A step that is refused before any file is written ends the landing there, with why (Refused): it is
   * not asked again. An apply that fails has every apply that was sent put back, the newest first, and nothing is
   * recorded. A change with no version on either side (a file both the thread and you deleted) is no apply's, nor is
   * one the folder already has; the record takes both.
   *
   * A record that is not answered is never taken for a landing that did not land: nothing is put back and nothing
   * forgotten, and the landing asked again asks it again. What the landing kept is forgotten only by the history's
   * own leave: its answer to the forgetting of this saga, asked of this folder's place, for all its changes once
   * it is recorded and otherwise for every step the land helper still holds a record of, with no step of the
   * landing's between that answer and the forgetting.
   *
   * *notes* is what the saga's journal keeps of each step's answer: a landing asked again after the app died is
   * answered from it, and a step it has no answer for is asked again. A saga's id names one landing, ever.
   * *between* runs once the files to apply are fixed, before the first apply. *dies* names the step the app is
   * killed before, and the step whose answer it had not kept by then.
   *
   * Around each step the folder is compared with what the step may have changed, and nothing else has.
   */
  const landing = async (
    thread: string, saga: string,
    { notes = new Map<string, Outcome>(), between = () => {}, dies }: { notes?: Map<string, Outcome>; between?: () => void; dies?: { before: string; unkept?: string } } = {},
  ) => {
    expect(begun.get(saga) ?? notes, `${saga} names another landing already`).toBe(notes);
    begun.set(saga, notes);
    const trailers = [["Surogate-Saga", saga]];
    const author = { name: TITLES[thread]!, email: `thread:${thread}@surogate` };
    const copy = copyOf(place, thread);
    const helper = helperOf(thread);
    // Each file as it was before its apply, for its put-back.
    const was = new Map<number, Entry | undefined>();
    // One step: answered from the notes where they hold its answer, else asked, with the folder looked at before
    // and after. *changes* says which paths it may have changed, by the folder as it was; *check* looks at them.
    const step = async (
      name: string, ask: () => Promise<Outcome>, changes: (before: Picture) => string[] = () => [], check: (before: Picture, after: Picture) => void = () => {},
    ) => {
      const noted = notes.get(name);
      if (noted) return noted;
      if (dies?.before === name) {
        if (dies.unkept) notes.delete(dies.unkept);
        await die(helper);
        throw new Died(name);
      }
      const before = picture(folder);
      const outcome = await ask();
      const after = picture(folder);
      const may = "ok" in outcome ? changes(before).sort() : [];
      expect(differing(before, after), `what ${name} changed in the folder`).toEqual(may);
      // A folder a file came to or went from is the folder it was; one made for a file is a folder like any new one.
      for (const path of may.filter((one) => after[one]?.kind === "folder")) {
        if (before[path]) sameFolder(before[path], after[path]!);
        else expect(after[path]!.mode, path).toBe(NEW_FOLDER);
      }
      if (may.length > 0) check(before, after);
      notes.set(name, outcome);
      return outcome;
    };
    const told = (name: string, action: string, args: Record<string, unknown> = {}) => step(name, () => asked(thread, action, args));
    // A step no file is written before: its answer, or the landing's end.
    const first = async (name: string, answer: Promise<Outcome>) => {
      const outcome = await answer;
      if ("error" in outcome) throw new Refused(name, outcome.error);
      return outcome.ok as Record<string, unknown>;
    };
    // What the copy holds at *path*, as the landing writes it.
    const turned = (path: string) => (existsSync(join(copy, path)) ? sha(readFileSync(join(copy, path))) : undefined);
    try {
      // Not the saga's to keep: each helper is asked at its own start, before the folder is read for the pickup.
      notes.delete("recover");
      const recovered = await first("recover", step("recover", () => helper.land({ action: "recover" })));
      const { paths } = await first("changed", told("changed", "changed")) as { paths: string[] };
      const seen = Object.fromEntries((await first("look", step("look", () => helper.land({ action: "revisions", paths })))).revisions as Array<[string, string]>);
      const picked = await first("pickup", told("pickup", "pickup", { author: YOU, trailers }));
      const turn = await first("commit", told("commit", "commit", { author, trailers, pickup: picked.commit }));
      const changes = turn.changes as Change[];
      between();
      // Each apply that was sent, by its step: a step is its file's place among the turn's changes, one for a file.
      const sent: number[] = [];
      let failed: { type: string; message: string } | null = null;
      for (const [nth, change] of changes.entries()) {
        const { path, before: from, after: to } = change;
        if (from === to) continue;
        sent.push(nth);
        const outcome = await step(`apply:${nth}`, () => helper.land({ action: "apply", saga, step: nth, ...change, expected: seen[path] }), (before) => {
          // Asked again after its answer was lost, the file is what the landing wrote, and is not written again.
          const landed = to === null ? before[path] === undefined : before[path]?.sha === turned(path);
          return landed ? [] : [...above(path, before), path];
        }, (before, after) => {
          was.set(nth, before[path]);
          // The file is the thread's, byte for byte, under the mode the one it replaced had; or it is gone.
          const now = after[path];
          if (to === null) expect(now, path).toBeUndefined();
          else expect([now?.kind, now?.sha, now?.mode, now?.links], path).toEqual(["file", turned(path), before[path]?.mode ?? NEW_FILE, 1]);
        });
        if ("error" in outcome) {
          failed = outcome.error;
          break;
        }
      }
      // Every apply that was sent is put back, whatever it answered: one that left no record puts nothing back.
      const putBack: Array<[string, unknown]> = [];
      for (const nth of failed ? [...sent].reverse() : []) {
        const { path } = changes[nth]!;
        const held = recordsOf(saga).includes(nth);
        const outcome = await step(`unapply:${nth}`, () => helper.land({ action: "unapply", saga, step: nth, path }), (before) => (held ? [...above(path, before), path] : []), (_before, after) => {
          // The very file that was there, as it was: its bytes, its mode, its time; or none again.
          const [back, before] = [after[path], was.get(nth)];
          expect(back && [back.kind, back.mode, back.ino, back.size, back.mtime, back.sha, back.links], path)
            .toEqual(before && [before.kind, before.mode, before.ino, before.size, before.mtime, before.sha, before.links]);
        });
        putBack.push([path, "ok" in outcome ? (outcome.ok as { put_back: boolean }).put_back : outcome.error]);
      }
      const record = failed || turn.commit === null ? null : await told("record", "record", {
        turn: turn.commit, applied: changes, author, trailers, main: picked.main, pickup: picked.commit,
      });
      // Not answered, it may have landed all the same: nothing is put back, and nothing of it is forgotten.
      if (record && "error" in record) {
        notes.delete("record");
        return { state: "unsettled" as const, failed, refusal: record, recovered, picked, turn, changes, putBack, forgotten: null, landing: null };
      }
      // Once recorded, all the turn's changes; else each step the helper still holds a record of, by its own
      // records, which name no step this landing did not send.
      expect(recordsOf(saga).filter((nth) => !sent.includes(nth))).toEqual([]);
      const forgotten = record ? changes : recordsOf(saga).map((nth) => changes[nth]!);
      const refusal = forgettable(await told("forgetting", "forget", { saga, applied: forgotten }));
      // Anything but its leave is a refusal: what the landing kept stays, and the forgetting is asked again later.
      if (refusal === null) expect(ok(await step("forget", () => helper.land({ action: "forget", saga })))).toEqual({});
      else notes.delete("forgetting");
      const state = failed ? "compensated" as const : "completed" as const;
      return { state, failed, refusal, recovered, picked, turn, changes, putBack, forgotten, landing: record && (record.ok as Record<string, unknown>) };
    } finally {
      await helper.end();
    }
  };

  beforeAll(async () => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-landing-")));
    data = join(dir, "data");
    folder = join(dir, "Documents");
    for (const made of [join(folder, "notes"), join(dir, "home")]) mkdirSync(made, { recursive: true });
    writeFileSync(join(folder, "Report.md"), "# Report\n\nTotals: 40\n");
    writeFileSync(join(folder, "Budget.csv"), "item,cost\nrent,40\n");
    writeFileSync(join(folder, "Old.md"), "old notes\n");
    writeFileSync(join(folder, "notes", "keep.txt"), "kept as it is\n");
    // Modes of its user's own choosing: a landing that replaces a file leaves it the mode it had.
    chmodSync(join(folder, "Budget.csv"), 0o600);
    chmodSync(join(folder, "notes", "keep.txt"), 0o640);
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"), user: USER, kvm: KVM,
    };
    manager = new VmManager(options);
    ({ place } = await placeOf(data, { folder, ...folderOf(folder), boot: BOOT_ID }, { letGo: (old) => manager.unplace(old) }));
  }, 60_000);

  afterAll(async () => {
    for (const helper of helpers) helper.kill("SIGKILL");
    await manager.stop();
    rmSync(options.run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("work at once, each in its copy, and the folder changes only when one lands", async () => {
    const untouched = picture(folder);
    expect(await Promise.all([history(ONE, "open"), history(TWO, "open")])).toMatchObject([{ copy: "made" }, { copy: "made" }]);
    await Promise.all([
      run(ONE, "printf '# Report\\n\\nTotals: 40\\n\\nTidied by A.\\n' > Report.md; echo 'notes of A' > A.md; mkdir drafts; echo 'a draft' > drafts/a.txt; rm Old.md"),
      run(TWO, "printf '# Report\\n\\nTotals: 42\\n' > Report.md; printf 'item,cost\\nrent,42\\n' > Budget.csv; echo 'notes of B' > B.md"),
    ]);
    // Neither has written the folder: not a name, a mode, a time or a byte of it. Its place is in the app's data.
    expect(differing(untouched, picture(folder))).toEqual([]);
    expect(place.history).toBe(join(data, "history", place.key));
    const first = await landing(ONE, "saga-a1");
    expect([first.state, first.recovered, first.refusal, first.forgotten]).toEqual(["completed", NOTHING_CUT, null, first.changes]);
    // What it wrote first, then what it deleted.
    expect(first.changes.map((change) => change.path)).toEqual(["A.md", "Report.md", "drafts/a.txt", "Old.md"]);
    expect(first.landing).toEqual({ commit: expect.stringMatching(/^[0-9a-f]{40}$/), set_aside: null });
    expect([real("Report.md"), real("A.md"), real("drafts/a.txt")]).toEqual(["# Report\n\nTotals: 40\n\nTidied by A.\n", "notes of A\n", "a draft\n"]);
    // The landing's files, and the folders they came to and went from, are all that is not as it was.
    const landed = picture(folder);
    expect(Object.keys(landed)).toEqual([".", "A.md", "Budget.csv", "Report.md", "drafts", "drafts/a.txt", "notes", "notes/keep.txt"]);
    expect(differing(untouched, landed)).toEqual([".", "A.md", "Old.md", "Report.md", "drafts", "drafts/a.txt"]);
    // The other thread's copy is as it was: it sees the landing at its next turn.
    expect(await run(TWO, "cat Report.md; ls")).toMatchObject({ output: "# Report\n\nTotals: 42\nB.md\nBudget.csv\nOld.md\nReport.md\nnotes\n" });
    // Recorded: each file it replaced is a version in the folder's history, and nothing is kept for a put-back.
    expect(await history(TWO, "fetch", { saga: "saga-a1" })).toMatchObject({ main: first.landing!.commit, landing: first.landing!.commit });
    expect(existsSync(kept("saga-a1"))).toBe(false);
  });

  it("never overwrites a file you save while a landing runs: the landing is put back whole", async () => {
    const before = picture(folder);
    const save = () => writeFileSync(join(folder, "Budget.csv"), "item,cost\nrent,40\npower,5\n");
    const cut = await landing(TWO, "saga-b1", { between: save });
    // The copy's B.md was applied first, then the budget's apply found your save: B.md is taken away again.
    expect(cut).toMatchObject({
      state: "compensated", failed: { type: "conflict", message: "Budget.csv changed in the folder while this landing ran, so it was not replaced" },
    });
    // Each apply that was sent is put back, the newest first: the budget's left nothing to put back.
    expect([cut.changes.map((change) => change.path), cut.putBack]).toEqual([["B.md", "Budget.csv"], [["Budget.csv", false], ["B.md", true]]]);
    expect(real("Budget.csv")).toBe("item,cost\nrent,40\npower,5\n");
    // Your save, in the file it was made in, is all of the folder that is not as it was; a name came and went in it.
    const after = picture(folder);
    expect(differing(before, after)).toEqual([".", "Budget.csv"]);
    sameFolder(before["."]!, after["."]!);
    expect([after["Budget.csv"]!.ino, after["Budget.csv"]!.mode]).toEqual([before["Budget.csv"]!.ino, 0o600]);
    // Nothing of it landed, and the helper holds no record of any step of it by then: it has nothing kept to forget.
    expect(await history(TWO, "fetch", { saga: "saga-b1" })).toMatchObject({ landing: null, hidden: false });
    expect([cut.forgotten, cut.refusal, existsSync(kept("saga-b1"))]).toEqual([[], null, false]);
  });

  it("lands again as a new landing, which names your save and the other thread's change, and leaves both files as they are", async () => {
    const before = picture(folder);
    const again = await landing(TWO, "saga-b2");
    expect([again.state, again.recovered]).toEqual(["completed", NOTHING_CUT]);
    expect(again.picked.picked_up).toEqual([expect.objectContaining({ path: "Budget.csv" })]);
    expect(again.changes.map((change) => change.path)).toEqual(["B.md"]);
    expect(again.turn.overlapped).toEqual([
      expect.objectContaining({ path: "Budget.csv", reason: "changed", by: { kind: "you" } }),
      expect.objectContaining({ path: "Report.md", reason: "changed", by: { kind: "thread", id: ONE, title: "Tidy the report" } }),
    ]);
    expect(real("B.md")).toBe("notes of B\n");
    // Its one new file came; your save and the other thread's file are the files they were.
    expect(differing(before, picture(folder))).toEqual([".", "B.md"]);
    // Its copy now holds the newer files, for its redo.
    expect(await run(TWO, "cat Report.md Budget.csv; ls")).toMatchObject({
      output: "# Report\n\nTotals: 40\n\nTidied by A.\nitem,cost\nrent,40\npower,5\nA.md\nB.md\nBudget.csv\nReport.md\ndrafts\nnotes\n",
    });
  });

  it("redoes its change on the newer file, and both threads' changes reach the folder", async () => {
    const before = picture(folder);
    await run(TWO, "sed -i 's/Totals: 40/Totals: 47/' Report.md; sed -i 's/rent,40/rent,42/' Budget.csv");
    const redone = await landing(TWO, "saga-b3");
    expect([redone.state, redone.turn.overlapped, redone.changes.map((change) => change.path)]).toEqual(["completed", [], ["Budget.csv", "Report.md"]]);
    expect([real("Report.md"), real("Budget.csv")]).toEqual(["# Report\n\nTotals: 47\n\nTidied by A.\n", "item,cost\nrent,42\npower,5\n"]);
    const after = picture(folder);
    expect(differing(before, after)).toEqual([".", "Budget.csv", "Report.md"]);
    // The budget is still its user's alone to read.
    expect([after["Budget.csv"]!.mode, after["Report.md"]!.mode]).toEqual([0o600, before["Report.md"]!.mode]);
    // And the first thread's next turn starts from all of it.
    expect(await history(ONE, "open")).toMatchObject({ copy: "moved" });
    expect(await run(ONE, "cat Report.md; ls")).toMatchObject({ output: "# Report\n\nTotals: 47\n\nTidied by A.\nA.md\nB.md\nBudget.csv\nReport.md\ndrafts\nnotes\n" });
    // Nothing of any landing is left in the folder, and nothing kept once each is recorded or put back.
    expect(Object.keys(after)).toEqual([".", "A.md", "B.md", "Budget.csv", "Report.md", "drafts", "drafts/a.txt", "notes", "notes/keep.txt"]);
    expect(existsSync(kept()) ? readdirSync(kept()) : []).toEqual([]);
  });

  it("leaves your files as landed, each one it replaced kept, when the app is killed after a landing's last apply, and the same landing asked again is recorded", async () => {
    await run(ONE, "echo 'Checked by A.' >> Report.md; rm A.md B.md; echo 'another draft' > drafts/c.txt");
    // You delete a file the thread deleted too, and save another, before the landing looks.
    rmSync(join(folder, "B.md"));
    writeFileSync(join(folder, "Budget.csv"), "item,cost\nrent,42\npower,6\n");
    const before = picture(folder);
    const notes = new Map<string, Outcome>();
    // Killed before the record is asked, the last apply's answer not kept yet.
    await expect(landing(ONE, "saga-a2", { notes, dies: { before: "record", unkept: "apply:2" } })).rejects.toBeInstanceOf(Died);
    expect([...notes.keys()]).toEqual(["recover", "changed", "look", "pickup", "commit", "apply:0", "apply:1"]);
    // As landed: the thread's files are in the folder, and the two it took from there are in the app's data.
    const killed = picture(folder);
    expect(differing(before, killed)).toEqual([".", "A.md", "Report.md", "drafts", "drafts/c.txt"]);
    expect([real("Report.md"), real("drafts/c.txt"), existsSync(join(folder, "A.md"))]).toEqual(["# Report\n\nTotals: 47\n\nTidied by A.\nChecked by A.\n", "another draft\n", false]);
    expect(readdirSync(kept("saga-a2")).sort()).toEqual(["0", "0.json", "1.json", "2", "2.json"]);
    expect([sha(readFileSync(kept("saga-a2", "0"))), sha(readFileSync(kept("saga-a2", "2")))]).toEqual([before["Report.md"]!.sha, before["A.md"]!.sha]);
    // Not recorded: the history holds no landing of it, and would have nothing of it forgotten.
    expect(await history(ONE, "fetch", { saga: "saga-a2" })).toMatchObject({ landing: null, hidden: false });
    const { changes } = ok(notes.get("commit")!) as { changes: Change[] };
    expect(changes).toEqual([
      expect.objectContaining({ path: "Report.md" }), expect.objectContaining({ path: "drafts/c.txt", before: null }),
      expect.objectContaining({ path: "A.md", after: null }), { path: "B.md", before: null, after: null },
    ]);
    expect(forgettable(await asked(ONE, "forget", { saga: "saga-a2", applied: changes }))).toMatchObject({ error: { type: "history", code: "landing_unsettled" } });
    // The landing again, by the app's next start: its helper finds no step cut short, the apply whose answer was
    // lost is answered as done, and the record and the forgetting follow. None of it writes the folder.
    const finished = await landing(ONE, "saga-a2", { notes });
    expect([finished.state, finished.recovered, finished.refusal]).toEqual(["completed", NOTHING_CUT, null]);
    expect(finished.picked.picked_up).toEqual([expect.objectContaining({ path: "B.md", after: null }), expect.objectContaining({ path: "Budget.csv" })]);
    expect(differing(killed, picture(folder))).toEqual([]);
    expect(await history(ONE, "fetch", { saga: "saga-a2" })).toMatchObject({ main: finished.landing!.commit, landing: finished.landing!.commit });
    expect(existsSync(kept("saga-a2"))).toBe(false);
    // The thread's copy is the landing's files, yours among them.
    expect(await run(ONE, "cat Budget.csv; ls . drafts")).toMatchObject({ output: "item,cost\nrent,42\npower,6\n.:\nBudget.csv\nReport.md\ndrafts\nnotes\n\ndrafts:\na.txt\nc.txt\n" });
  });

  it("leaves your files as landed when the app is killed after a landing's record, and the landing asked again forgets what it kept, by the record the history holds", async () => {
    expect(await history(TWO, "open")).toMatchObject({ copy: "moved" });
    await run(TWO, "echo 'water,3' >> Budget.csv; echo 'more notes of B' > B2.md");
    const before = picture(folder);
    const notes = new Map<string, Outcome>();
    // Killed once the record has answered, before its answer was kept or anything forgotten.
    await expect(landing(TWO, "saga-b4", { notes, dies: { before: "forgetting", unkept: "record" } })).rejects.toBeInstanceOf(Died);
    expect([...notes.keys()]).toEqual(["recover", "changed", "look", "pickup", "commit", "apply:0", "apply:1"]);
    const killed = picture(folder);
    expect(differing(before, killed)).toEqual([".", "B2.md", "Budget.csv"]);
    expect([real("Budget.csv"), killed["Budget.csv"]!.mode]).toEqual(["item,cost\nrent,42\npower,6\nwater,3\n", 0o600]);
    // The budget it replaced is still kept, though it is a version in the history by now.
    expect(sha(readFileSync(kept("saga-b4", "1")))).toBe(before["Budget.csv"]!.sha);
    const { landing: pushed } = await history(TWO, "fetch", { saga: "saga-b4" });
    expect(pushed).toMatch(/^[0-9a-f]{40}$/);
    // The landing again: the record, asked a second time, answers the landing the history holds, and writes nothing.
    const finished = await landing(TWO, "saga-b4", { notes });
    expect([finished.state, finished.recovered, finished.refusal, finished.landing]).toEqual(["completed", NOTHING_CUT, null, { commit: pushed, set_aside: null }]);
    expect(differing(killed, picture(folder))).toEqual([]);
    expect(existsSync(kept()) ? readdirSync(kept()) : []).toEqual([]);
    // Each thread's next turn starts from the folder as both left it, with what you saved in it.
    expect(await history(ONE, "open")).toMatchObject({ copy: "moved" });
    expect(await run(ONE, "cat Budget.csv B2.md")).toMatchObject({ output: "item,cost\nrent,42\npower,6\nwater,3\nmore notes of B\n" });
    // And of the whole folder, one file was never anyone's to change: it is the file it was at the start.
    expect(killed["notes/keep.txt"]).toMatchObject({ mode: 0o640, sha: sha("kept as it is\n"), links: 1 });
  });

  it("gives another folder at the same path a place of its own once the sandbox has let the old one go, and the thread of the folder that was there reaches neither", async () => {
    const notes = join(dir, "Notes");
    mkdirSync(notes);
    writeFileSync(join(notes, "first.txt"), "of the first folder\n");
    const sandbox: PlaceOptions = { letGo: (old) => manager.unplace(old) };
    const bound = () => ({ folder: notes, ...folderOf(notes), boot: BOOT_ID });
    const of = (at: Place, thread: string, action: string) => manager.history({ place: at, thread, user: "u1", action, args: {} }, bounded());
    const was = bound();
    const { place: first } = await placeOf(data, was, sandbox);
    expect(ok(await of(first, ONE, "open"))).toMatchObject({ copy: "made" });
    expect(readdirSync(copyOf(first, ONE))).toEqual(["first.txt"]);
    // Its user moves the folder away and makes another at its path; a thread is bound to that one.
    renameSync(notes, join(dir, "Notes.old"));
    mkdirSync(notes);
    writeFileSync(join(notes, "second.txt"), "of the second folder\n");
    // The sandbox still holds the first folder's place: it is let go, and only then renamed. The answer says where.
    const { place: second, aside } = await placeOf(data, bound(), sandbox);
    expect([second.key, second.history, aside?.place]).toEqual([first.key, first.history, first]);
    expect(readdirSync(aside!.history).sort()).toEqual(["clones", "history.git", "threads"]);
    expect(readFileSync(join(aside!.history, "threads", ONE, "first.txt"), "utf8")).toBe("of the first folder\n");
    expect(readdirSync(second.history)).toEqual([]);
    // The new folder's thread works in a copy of the new folder, with a history of its own.
    expect(ok(await of(second, TWO, "open"))).toMatchObject({ copy: "made" });
    expect(readdirSync(copyOf(second, TWO))).toEqual(["second.txt"]);
    // The thread of the folder that was there: no place for it, nothing renamed, and no way to the new folder's.
    const held = readdirSync(join(data, "history")).sort();
    await expect(placeOf(data, was, sandbox)).rejects.toBeInstanceOf(FolderReplaced);
    expect(await of(first, ONE, "changed")).toMatchObject({ error: { type: "unavailable" } });
    expect(readdirSync(join(data, "history")).sort()).toEqual(held);
    expect(existsSync(copyOf(second, ONE))).toBe(false);
    expect(ok(await of(second, TWO, "changed"))).toEqual({ paths: [] });
    // Neither folder was written, and the first one's history is where it was set aside.
    expect([readdirSync(notes), readdirSync(join(dir, "Notes.old"))]).toEqual([["second.txt"], ["first.txt"]]);
    expect(readdirSync(aside!.history).sort()).toEqual(["clones", "history.git", "threads"]);
  });
});
