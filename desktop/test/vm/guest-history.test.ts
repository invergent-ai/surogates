// A folder's history in the guest, under QEMU and KVM: git as the agent's own user over the
// folder's place, a thread's copy at the folder's path, and the folder untouched. The image
// built by images/guest/build.sh, the agent disk from this package (npm run build first).
// Behind SUROGATE_VM_TESTS=1.

import { spawnSync } from "node:child_process";
import {
  cpSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { type Context, perform } from "../../src/files/operations.js";
import { CANCELLED, SANDBOX_STOPPED } from "../../src/guest/command.js";
import type { Outcome } from "../../src/link/protocol.js";
import { checked, forgettable, type HistoryRequest } from "../../src/vm/history.js";
import { bootLinux, readonlyFlag } from "../../src/vm/linux.js";
import { Guest, type Place, VmManager, type VmOptions } from "../../src/vm/manager.js";
import { VIRTIOFSD } from "../../src/vm/qemu.js";
import { agentDisk, folderOf, IMAGE, KVM, median, needsKvm, signal, until, USER, withHistory } from "./guest-support.js";

beforeAll(needsKvm);

const KEY = "0123456789abcdef";
const ONE = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const TWO = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
const THREE = "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f";
const NO_ID = "b".repeat(40);
const YOU = { name: "u1", email: "user:u1@surogate" };
// In the history's place on a guest's agent disk: each write to the folder its root can try, its
// outcome in a commit's answer, whose excluded names are text.
const PROBE = `
import errno, json, subprocess, sys
request = json.load(sys.stdin)
folder, store, said = request["folder"], request["store"], []
def attempt(what, act):
    try:
        act()
        said.append(what + ": done")
    except OSError as error:
        said.append(what + ": " + errno.errorcode.get(error.errno, str(error.errno)))
import os
attempt("make", lambda: open(folder + "/planted.txt", "w").close())
attempt("replace", lambda: open(folder + "/Report.docx", "w").close())
attempt("remove", lambda: os.unlink(folder + "/Report.docx"))
said.append("remount: " + str(subprocess.run(["/usr/bin/mount", "-o", "remount,rw", folder]).returncode))
attempt("make again", lambda: open(folder + "/planted.txt", "w").close())
attempt("history", lambda: open(store + "/probe.txt", "w").close())
json.dump({"commit": None, "base": "0" * 40, "changes": [], "overlapped": [], "excluded": said, "repositories": [], "not_taken": []}, sys.stdout)
`;
// In the same place: what a guest that is not ours can leave in the place it was given, for the next
// boot's git to find. A repository of its own, whose config has a program run for every file git reads
// or writes, and the thread's own repository's worktrees made a link into it. The program's mark is a
// file in the place, which this computer sees.
const PLANT = `
import json, os, shutil, sys
request = json.load(sys.stdin)
store, thread = request["store"], request["thread"]
repo, spare = store + "/clones/" + thread, store + "/spare"
shutil.copytree(repo, spare, symlinks=True)
program = "echo ran >> " + store + "/ran; cat"
with open(spare + "/config", "a") as config:
    config.write('[filter "x"]\\n\\tclean = "' + program + '"\\n\\tsmudge = "' + program + '"\\n')
with open(spare + "/info/attributes", "w") as attributes:
    attributes.write("* filter=x\\n")
with open(spare + "/worktrees/" + thread + "/commondir", "w") as commondir:
    commondir.write(spare + "\\n")
shutil.rmtree(repo + "/worktrees")
os.symlink(spare + "/worktrees", repo + "/worktrees")
json.dump({"commit": None, "base": "0" * 40, "changes": [], "overlapped": [], "excluded": ["planted"], "repositories": [], "not_taken": []}, sys.stdout)
`;
// In the same place: what such a guest can leave among a thread's own objects, for the next boot's git
// to read as that thread's. Every loose object of the folder's other threads, a snapshot's commit and
// its trees, each by a link at the name git gives the object. Its answer names the objects it linked.
const LINK = `
import json, os, sys
request = json.load(sys.stdin)
store, thread, linked = request["store"], request["thread"], []
mine = store + "/clones/" + thread + "/objects"
for other in sorted(os.listdir(store + "/clones")):
    theirs = store + "/clones/" + other + "/objects"
    if other == thread or not os.path.isdir(theirs):
        continue
    for folder in sorted(name for name in os.listdir(theirs) if len(name) == 2):
        for name in sorted(os.listdir(theirs + "/" + folder)):
            if not os.path.lexists(mine + "/" + folder + "/" + name):
                os.makedirs(mine + "/" + folder, exist_ok=True)
                os.symlink("../../../" + other + "/objects/" + folder + "/" + name, mine + "/" + folder + "/" + name)
                linked.append(folder + name)
json.dump({"commit": None, "base": "0" * 40, "changes": [], "overlapped": [], "excluded": linked, "repositories": [], "not_taken": []}, sys.stdout)
`;
// In the same place: a history that starts a writer of its own in the place, as a git it runs and does
// not wait for, and never answers; asked what changed, it says whether the writer of the request before
// it still lives as it starts.
const WRITER = `
import json, os, subprocess, sys, time
request = json.load(sys.stdin)
store = request["store"]
if request["action"] == "changed":
    try:
        os.kill(int(open(store + "/writer.pid").read()), 0)
        left = ["a writer lives"]
    except (OSError, ValueError):
        left = []
    json.dump({"paths": left}, sys.stdout)
else:
    writer = subprocess.Popen(["/bin/sh", "-c", "while :; do echo beat >> " + store + "/beat; sleep 0.05; done"])
    with open(store + "/writer.pid", "w") as pid:
        pid.write(str(writer.pid))
    time.sleep(3600)
`;
const author = (thread: string) => ({ name: thread === ONE ? "Draft A" : "Draft B", email: `thread:${thread}@surogate` });
const ok = (outcome: Outcome) => {
  expect(outcome).toHaveProperty("ok");
  return (outcome as { ok: Record<string, unknown> }).ok;
};

interface Change {
  path: string;
  before: string | null;
  after: string | null;
}

// A folder, its place in the app's data and the disks of a guest for it, in a folder of the test's own.
function world(prefix: string) {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), prefix)));
  const folder = join(dir, "Documents");
  mkdirSync(join(folder, "notes"), { recursive: true });
  mkdirSync(join(dir, "store"));
  writeFileSync(join(folder, "Report.docx"), "the report, v1\n");
  writeFileSync(join(folder, "notes", "todo.txt"), "one\n");
  // Under $XDG_RUNTIME_DIR: a vhost-user socket's path must fit in 108 bytes.
  const disks = (at: string, disk = agentDisk(at)): VmOptions => ({
    kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: disk, sessions: join(at, "sessions.img"),
    run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(at, "console.log"), user: USER, kvm: KVM,
  });
  const place: Place = { key: KEY, history: join(dir, "store"), real: folderOf(folder) };
  return { dir, folder, place, disks, copyOf: (thread: string) => join(dir, "store", "threads", thread) };
}

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("a folder's history in the guest", { timeout: 120_000 }, () => {
  let dir: string;
  let folder: string;
  let options: VmOptions;
  let manager: VmManager;
  let place: Place;
  let copyOf: (thread: string) => string;
  let disks: (at: string, disk?: string) => VmOptions;
  const history = async (thread: string, action: string, args: Record<string, unknown> = {}) => {
    const request: HistoryRequest = { place, thread, user: "u1", action, args };
    return ok(await manager.history(request, signal()));
  };
  // A command of the thread's, in its copy at the folder's path.
  const run = async (thread: string, command: string) => ok(await manager.perform({
    id: `run-${Math.random()}`, root: thread, folder: folderOf(copyOf(thread)), at: folder, kind: "run", args: { command, workdir: null, timeout: 30 },
  }, signal()));
  // A landing's steps as its saga runs them: the applies are the host's, here a copy of each file.
  const land = async (thread: string, saga: string) => {
    const trailers = [["Surogate-Saga", saga]];
    const picked = await history(thread, "pickup", { author: YOU, trailers });
    const turn = await history(thread, "commit", { author: author(thread), trailers, pickup: picked.commit });
    const changes = turn.changes as Change[];
    for (const change of changes) {
      if (change.after === null) rmSync(join(folder, change.path));
      else cpSync(join(copyOf(thread), change.path), join(folder, change.path), { recursive: false });
    }
    const recorded = await history(thread, "record", {
      turn: turn.commit, applied: changes, author: author(thread), trailers, main: picked.main, pickup: picked.commit,
    });
    return { picked, turn, changes, recorded, landing: recorded.commit as string };
  };
  // One request to a guest whose agent runs *program* in the history's place, as its root: its answer, a
  // commit's, whose excluded names are text. The guest is stopped before this returns.
  const subverted = async (program: string) => {
    const at = mkdtempSync(join(dir, "subverted-"));
    const other = disks(at, withHistory(agentDisk(at), program));
    const guest = new VmManager(other);
    try {
      return ok(await guest.history({ place, thread: ONE, user: "u1", action: "commit", args: {} }, signal()));
    } finally {
      await guest.stop();
      rmSync(other.run, { recursive: true, force: true });
    }
  };
  // The next boot: another guest, with our agent, on other disks, and the place as the last one left it.
  const nextGuest = async () => {
    await manager.stop();
    rmSync(options.run, { recursive: true, force: true });
    options = disks(mkdtempSync(join(dir, "next-")));
    manager = new VmManager(options);
  };

  beforeAll(() => {
    ({ dir, folder, place, copyOf, disks } = world("vm-history-"));
    options = disks(dir);
    manager = new VmManager(options);
  });

  afterAll(async () => {
    await manager.stop();
    rmSync(options.run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("makes each thread's copy in the app's data, as this user's files, and leaves the folder as it was", async () => {
    expect(await history(ONE, "open")).toEqual({ copy: "made" });
    expect(await history(TWO, "open")).toEqual({ copy: "made" });
    expect(readdirSync(join(dir, "store")).sort()).toEqual(["clones", "threads"]);
    expect(readdirSync(copyOf(ONE)).sort()).toEqual(["Report.docx", "notes"]);
    expect(readFileSync(join(copyOf(TWO), "notes", "todo.txt"), "utf8")).toBe("one\n");
    // Written by the guest's root, they are this user's here; and no git state reaches the copy or the folder.
    expect(lstatSync(join(copyOf(ONE), "Report.docx")).uid).toBe(USER.uid);
    expect(existsSync(join(copyOf(ONE), ".git")) || existsSync(join(folder, ".git"))).toBe(false);
    expect(readdirSync(folder).sort()).toEqual(["Report.docx", "notes"]);
  });

  it("commits what a thread's command wrote, and puts the copy back to a snapshot at a stop", async () => {
    const before = (await history(ONE, "snapshot", { reason: "before a step" })).hash as string;
    const wrote = await run(ONE, "pwd; echo \"A's report\" > Report.docx; mkdir -p out && echo made > out/new.txt; id -u");
    expect(wrote).toMatchObject({ output: `${folder}\n10000\n`, returncode: 0 });
    // The copy's files are the command's own user's in the guest, and git's as its root reads them the moment they are written.
    const after = (await history(ONE, "snapshot", { reason: "before a step" })).hash as string;
    expect(after).not.toBe(before);
    expect(await history(ONE, "restore", { commit: before })).toEqual({});
    expect(await run(ONE, "cat Report.docx; ls")).toMatchObject({ output: "the report, v1\nReport.docx\nnotes\n" });
    // The folder itself never changed.
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe("the report, v1\n");
  });

  it("lands one thread's turn, and names the other's file that changed since it started", async () => {
    await run(ONE, "echo \"A's report\" > Report.docx; echo A > A.md");
    await run(TWO, "echo \"B's report\" > Report.docx; echo B > B.md");
    expect(readdirSync(folder).sort()).toEqual(["Report.docx", "notes"]);
    const first = await land(ONE, "saga-1");
    expect(first.changes.map((change) => change.path)).toEqual(["A.md", "Report.docx"]);
    expect(first.picked).toMatchObject({ main: null, commit: null, picked_up: [] });
    expect(first.recorded).toEqual({ commit: first.landing, set_aside: null });
    const second = await land(TWO, "saga-2");
    expect(second.changes.map((change) => change.path)).toEqual(["B.md"]);
    expect(second.turn.overlapped).toEqual([expect.objectContaining({
      path: "Report.docx", reason: "changed", by: { kind: "thread", id: ONE, title: "Draft A" },
    })]);
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe("A's report\n");
    expect(readdirSync(folder).sort()).toEqual(["A.md", "B.md", "Report.docx", "notes"]);
    // The folder's history holds both landings, as packs and packed-refs, and says where each landed.
    expect(readdirSync(join(dir, "store", "history.git")).sort()).toEqual(["HEAD", "config", "index", "objects", "packed-refs", "refs"]);
    expect(await history(ONE, "fetch", { saga: "saga-2" })).toMatchObject({ main: second.landing, landing: second.landing, hidden: false, missing: [] });
    expect(await history(TWO, "fetch", { saga: "saga-1", since: first.landing })).toMatchObject({ landing: first.landing });
    expect(await history(TWO, "fetch", { saga: "saga-none", commits: [first.landing, NO_ID] })).toMatchObject({ landing: null, hidden: false, missing: [NO_ID] });
  });

  it("picks up a save of yours the moment it is made, with no wait", async () => {
    writeFileSync(join(folder, "notes", "todo.txt"), "one\ntwo, saved by you\n");
    const picked = await history(ONE, "pickup", { author: YOU, trailers: [["Surogate-Saga", "saga-3"]] });
    expect(picked.picked_up).toEqual([expect.objectContaining({ path: "notes/todo.txt" })]);
    // And a clean copy moves to the folder as it is now at its next turn.
    expect(await history(ONE, "open")).toEqual({ copy: "moved" });
    expect(await run(ONE, "cat notes/todo.txt; ls")).toMatchObject({ output: "one\ntwo, saved by you\nA.md\nB.md\nReport.docx\nnotes\n" });
  });

  it("takes a place again that is still being let go, and its history answers", async () => {
    const gone = manager.unplace(place);
    // Asked of at once: the place is added again with shares of its own, mounted once the old mounts have gone.
    expect(await history(ONE, "changed")).toEqual({ paths: [] });
    expect(await gone).toBe(true);
    expect(await history(TWO, "changed")).toEqual({ paths: [] });
  });

  it("finds a thread's copy and its history again in the next guest, booted on other disks, by what this computer keeps", async () => {
    await run(ONE, "echo draft > Draft.md");
    const drafted = (await history(ONE, "snapshot", { reason: "before a step" })).hash as string;
    await run(ONE, "echo more >> Draft.md");
    await nextGuest();
    // Nothing of the guest that went is the next one's: the place is the folder's key, its history's path and the folder, as this computer has them.
    expect(await history(ONE, "changed")).toEqual({ paths: ["Draft.md"] });
    expect(await history(ONE, "open")).toEqual({ copy: "kept" });
    expect(await history(ONE, "restore", { commit: drafted })).toEqual({});
    expect(await run(ONE, "cat Draft.md")).toMatchObject({ output: "draft\n" });
    // The other thread's, and the folder's landings, as they were.
    expect(await history(TWO, "open")).toEqual({ copy: "moved" });
    expect(await history(TWO, "fetch", { saga: "saga-1" })).toMatchObject({ landing: expect.stringMatching(/^[0-9a-f]{40}$/) });
    expect(readdirSync(folder).sort()).toEqual(["A.md", "B.md", "Report.docx", "notes"]);
  });

  it("refuses the guest's own root every write to the folder, a remount aside where this computer's virtiofsd cannot", async () => {
    try {
      // A guest whose agent runs this in place of the history: what a guest that is not ours could try.
      const answer = await subverted(PROBE);
      const refuses = readonlyFlag(spawnSync(VIRTIOFSD, ["--help"], { encoding: "utf8" }).stdout);
      expect(answer.excluded).toEqual([
        "make: EROFS", "replace: EROFS", "remove: EROFS", "remount: 0",
        // Past the guest's own mount option, only the daemon on this computer refuses: 1.11 and later.
        `make again: ${refuses ? "EROFS" : "done"}`,
        "history: done",
      ]);
      expect(existsSync(join(folder, "planted.txt"))).toBe(!refuses);
      expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe("A's report\n");
      expect(existsSync(join(dir, "store", "probe.txt"))).toBe(true);
    } finally {
      rmSync(join(folder, "planted.txt"), { force: true });
    }
  });

  it("makes a copy of 5,000 files, and takes a snapshot of it before a step", async () => {
    mkdirSync(join(dir, "big", "data"), { recursive: true });
    for (let n = 0; n < 5_000; n += 1) writeFileSync(join(dir, "big", "data", `${n}.txt`), `file ${n}\n`);
    mkdirSync(join(dir, "big-store"));
    const big: Place = { key: "aaaaaaaaaaaaaaaa", history: join(dir, "big-store"), real: folderOf(join(dir, "big")) };
    const ask = async (action: string, args: Record<string, unknown> = {}) => ok(await manager.history({ place: big, thread: ONE, user: "u1", action, args }, signal()));
    const began = performance.now();
    expect(await ask("open")).toEqual({ copy: "made" });
    const opened = performance.now() - began;
    const times: number[] = [];
    for (let n = 0; n < 5; n += 1) {
      writeFileSync(join(dir, "big-store", "threads", ONE, "data", `${n}.txt`), `changed ${n}\n`);
      const start = performance.now();
      await ask("snapshot", { reason: "before a step" });
      times.push(performance.now() - start);
    }
    console.log(`a copy of 5,000 files made in ${Math.round(opened)} ms; a snapshot of it, one file changed: median ${Math.round(median(times))} ms (${times.map(Math.round).join(", ")})`);
    // Loose bounds, for a loaded computer: the numbers are the log's.
    expect(opened).toBeLessThan(90_000);
    expect(median(times)).toBeLessThan(5_000);
    expect(statSync(join(dir, "big-store", "threads", ONE, "data", "4999.txt")).size).toBe(10);
    expect(await manager.unplace(big)).toBe(true);
  });

  it("runs nothing an earlier boot's guest left in the place: a repository it redirected to its own is made again", async () => {
    expect((await subverted(PLANT)).excluded).toEqual(["planted"]);
    expect(lstatSync(join(dir, "store", "clones", ONE, "worktrees")).isSymbolicLink()).toBe(true);
    await nextGuest();
    writeFileSync(join(copyOf(ONE), "Report.docx"), "changed before the next boot\n");
    // Whatever request comes first runs no git in the repository: the planted program's mark would be in
    // the place, where this computer sees it. The open makes the repository again.
    const first = await manager.history({ place, thread: ONE, user: "u1", action: "snapshot", args: { reason: "before a step" } }, signal());
    expect(existsSync(join(dir, "store", "ran"))).toBe(false);
    expect(first).toEqual({
      error: { type: "history", code: "no_whole_copy", message: "refused the request: this thread has no whole copy, and its next open makes one" },
    });
    expect(await history(ONE, "open")).toEqual({ copy: "made" });
    expect(lstatSync(join(dir, "store", "clones", ONE, "worktrees")).isSymbolicLink()).toBe(false);
    await run(ONE, "echo 'after the next boot' > Report.docx");
    expect((await history(ONE, "snapshot", { reason: "before a step" })).hash).toMatch(/^[0-9a-f]{40}$/);
    expect(await history(ONE, "changed")).toEqual({ paths: ["Report.docx"] });
    expect(existsSync(join(dir, "store", "ran"))).toBe(false);
  });

  it("reads no link an earlier boot's guest left among a thread's objects as one of them: its repository is made again", async () => {
    // The other thread's work, not landed: its snapshot's commit and tree are loose objects of its own repository.
    await run(TWO, "echo \"B's, not landed\" > Secret.md");
    const unlanded = (await history(TWO, "snapshot", { reason: "before a step" })).hash as string;
    const linked = (await subverted(LINK)).excluded as string[];
    expect(linked).toContain(unlanded);
    const at = (id: string) => join(dir, "store", "clones", ONE, "objects", id.slice(0, 2), id.slice(2));
    for (const id of linked) expect(lstatSync(at(id)).isSymbolicLink()).toBe(true);
    await nextGuest();
    // Its first request puts this thread's copy back to the other's snapshot. Through the links git would
    // read that commit and its tree, and say which of the other's files it could not write.
    const restore: HistoryRequest = { place, thread: ONE, user: "u1", action: "restore", args: { commit: unlanded } };
    const first = await manager.history(restore, signal());
    expect(first).toEqual({
      error: { type: "history", code: "no_whole_copy", message: "refused the request: this thread has no whole copy, and its next open makes one" },
    });
    expect(await history(ONE, "open")).toEqual({ copy: "made" });
    for (const id of linked) expect(lstatSync(at(id), { throwIfNoEntry: false })).toBeUndefined();
    // The other thread's snapshot is no object of this repository: git cannot go to it, and names no file of it.
    const again = await manager.history(restore, signal());
    expect(again).toMatchObject({ error: { type: "history", code: "failed" } });
    expect(JSON.stringify(again)).not.toContain("Secret.md");
    expect(existsSync(join(copyOf(ONE), "Secret.md"))).toBe(false);
    // The other thread's repository and its copy are as they were.
    expect(await history(TWO, "changed")).toEqual({ paths: ["Secret.md"] });
  });
});

// The history itself, with nothing between it and this computer's check: what the guest's agent sends for each
// action, as the class on the agent disk answers it, and each of those passed through the check. A field or a code
// the class has and this computer does not know fails here.
describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("each answer of a folder's history, as the history in the guest gives it", { timeout: 120_000 }, () => {
  let dir: string;
  let folder: string;
  let options: VmOptions;
  let guest: Guest;
  let place: Place;
  let copyOf: (thread: string) => string;
  // Each answer the history gave, by its action, and each code it refused with.
  const answered = new Map<string, unknown[]>();
  const refused = new Set<string>();
  // What the agent answered *action* with, unchecked; and that the check passes all of it on.
  const raw = async (thread: string, action: string, args: Record<string, unknown> = {}, at = KEY) => {
    const reply = await guest.request({ type: "history", key: at, thread, user: "u1", action, args });
    expect(reply).toMatchObject({ type: "result" });
    const { outcome } = reply as { outcome: Outcome };
    expect(checked(action, outcome, thread), `${action} answered ${JSON.stringify(outcome)}`).toEqual(outcome);
    if ("ok" in outcome) answered.set(action, [...(answered.get(action) ?? []), outcome.ok]);
    else refused.add(String(outcome.error.code));
    return outcome;
  };
  const said = async (thread: string, action: string, args: Record<string, unknown> = {}) => ok(await raw(thread, action, args));
  const code = async (thread: string, action: string, args: Record<string, unknown> = {}) => {
    const outcome = await raw(thread, action, args);
    expect(outcome).toHaveProperty("error");
    return "error" in outcome ? String(outcome.error.code) : "";
  };
  // The applies of a landing, here a copy of each file.
  const apply = (thread: string, changes: Change[]) => {
    for (const change of changes) {
      if (change.after === null) rmSync(join(folder, change.path), { force: true });
      else cpSync(join(copyOf(thread), change.path), join(folder, change.path), { recursive: false });
    }
  };
  // A file whose name is not UTF-8, in *at*.
  const odd = (at: string) => Buffer.concat([Buffer.from(`${at}/odd-`), Buffer.from([0xff, 0xfe])]);

  beforeAll(async () => {
    let disks: (at: string) => VmOptions;
    ({ dir, folder, place, copyOf, disks } = world("vm-history-answers-"));
    writeFileSync(join(folder, "notes", "old.txt"), "old\n");
    options = disks(dir);
    guest = await Guest.boot(bootLinux, options);
    expect(await guest.place(place)).toBeNull();
  }, 120_000);

  afterAll(async () => {
    await guest?.stop();
    if (options) rmSync(options.run, { recursive: true, force: true });
    if (dir) rmSync(dir, { recursive: true, force: true });
  });

  it("answers an open, what changed, a snapshot and a put-back, each as this computer knows it", async () => {
    expect(await code(THREE, "snapshot", { reason: "before its first open" })).toBe("no_whole_copy");
    expect(await said(ONE, "open")).toEqual({ copy: "made" });
    expect(await said(TWO, "open")).toEqual({ copy: "made" });
    writeFileSync(join(copyOf(ONE), "Report.docx"), "A's report\n");
    writeFileSync(join(copyOf(ONE), "A.md"), "A\n");
    rmSync(join(copyOf(ONE), "notes", "old.txt"));
    expect(await said(ONE, "changed")).toEqual({ paths: ["A.md", "Report.docx", "notes/old.txt"] });
    const snapshot = await said(ONE, "snapshot", { reason: "before a step" });
    expect(snapshot).toEqual({ hash: expect.stringMatching(/^[0-9a-f]{40}$/) });
    expect(await said(ONE, "restore", { commit: snapshot.hash })).toEqual({});
    expect(await said(ONE, "open")).toEqual({ copy: "kept" });
    // Inside a turn, as the app opens a copy before a step: it stays where its branch is.
    expect(await said(TWO, "open", { moves: false })).toEqual({ copy: "kept" });
    expect(await code(TWO, "open", { moves: "yes" })).toBe("not_a_request");
    // A request that is none of a folder's history, an id that is none, a commit the repository does not hold.
    expect(await code(ONE, "prune", { keep: [] })).toBe("not_a_request");
    expect(await code(ONE, "apply", { path: "A.md", before: null, after: NO_ID })).toBe("not_a_request");
    expect(await code(ONE, "restore", { commit: "--upload-pack=/x" })).toBe("not_a_request");
    expect(await code(ONE, "restore", { commit: NO_ID })).toBe("failed");
    // A file whose name history cannot record, made in the copy after the copy was.
    writeFileSync(odd(copyOf(ONE)), "x");
    try {
      expect(await code(ONE, "changed")).toBe("name_not_utf8");
    } finally {
      rmSync(odd(copyOf(ONE)));
    }
  });

  it("answers a landing's steps, each as this computer knows it", async () => {
    const before = (await said(ONE, "snapshot", { reason: "before a landing" })).hash;
    const trailers = [["Surogate-Saga", "saga-1"]];
    // A file the thread deleted, you delete too before the landing looks.
    rmSync(join(folder, "notes", "old.txt"));
    const picked = await said(ONE, "pickup", { author: YOU, trailers });
    expect(picked).toEqual({
      main: null, commit: expect.stringMatching(/^[0-9a-f]{40}$/), packs: expect.any(Number),
      picked_up: [{ path: "notes/old.txt", before: expect.stringMatching(/^[0-9a-f]{40}$/), after: null }],
    });
    const turn = await said(ONE, "commit", { author: author(ONE), trailers, pickup: picked.commit });
    const changes = turn.changes as Change[];
    // The turn's deletion of it is a change with nothing to do: there is no file, before or after.
    expect(changes.map((change) => change.path)).toEqual(["A.md", "Report.docx", "notes/old.txt"]);
    expect(changes[2]).toEqual({ path: "notes/old.txt", before: null, after: null });
    apply(ONE, changes);
    // Neither recorded nor put back: what it kept is not to be forgotten.
    expect(await code(ONE, "forget", { saga: "saga-1", applied: changes })).toBe("landing_unsettled");
    const recorded = await said(ONE, "record", { turn: turn.commit, applied: changes, author: author(ONE), trailers, main: picked.main, pickup: picked.commit });
    expect(recorded).toEqual({ commit: expect.stringMatching(/^[0-9a-f]{40}$/), set_aside: null });
    expect(await said(ONE, "forget", { saga: "saga-1", applied: changes })).toEqual({ landing: recorded.commit });
    expect(await said(ONE, "forget", { saga: "saga-none", applied: [] })).toEqual({ landing: null });
    expect(await said(ONE, "fetch", { saga: "saga-1", commits: [turn.commit, NO_ID], since: null })).toEqual({
      main: recorded.commit, landing: recorded.commit, hidden: false, packs: expect.any(Number), missing: [NO_ID],
    });
    // A snapshot from before the landing is on another base than the copy's is now.
    expect(await code(ONE, "restore", { commit: before })).toBe("not_on_base");
  });

  it("answers a landing that leaves files out, one whose history moved, a kept turn and a copy set aside, each as this computer knows it", async () => {
    // The second thread changed the file the first landed, and one you save now; it wrote one history leaves out, and one late.
    writeFileSync(join(copyOf(TWO), "Report.docx"), "B's report\n");
    writeFileSync(join(copyOf(TWO), "B.md"), "B\n");
    writeFileSync(join(copyOf(TWO), "notes", "todo.txt"), "one\nB's\n");
    writeFileSync(join(copyOf(TWO), "notes.tmp"), "left out\n");
    writeFileSync(join(folder, "notes", "todo.txt"), "one\ntwo, saved by you\n");
    const trailers = [["Surogate-Saga", "saga-2"]];
    const picked = await said(TWO, "pickup", { author: YOU, trailers });
    expect(picked.picked_up).toEqual([{ path: "notes/todo.txt", before: expect.any(String), after: expect.any(String) }]);
    const turn = await said(TWO, "commit", { author: author(TWO), trailers, pickup: picked.commit });
    expect(turn).toMatchObject({
      changes: [{ path: "B.md", before: null }],
      overlapped: [
        { path: "Report.docx", reason: "changed", by: { kind: "thread", id: ONE, title: "Draft A" } },
        { path: "notes/todo.txt", reason: "changed", by: { kind: "you" } },
      ],
      excluded: ["notes.tmp"], repositories: [], not_taken: [],
    });
    const changes = turn.changes as Change[];
    apply(TWO, changes);
    writeFileSync(join(copyOf(TWO), "late.md"), "written after the turn was committed\n");
    // Asked with the main the landing did not begin on: the history moved, which is no failure of git's.
    const record = { turn: turn.commit, applied: changes, author: author(TWO), trailers, pickup: picked.commit };
    expect(await code(TWO, "record", { ...record, main: null })).toBe("conflict");
    const recorded = await said(TWO, "record", { ...record, main: picked.main });
    expect(recorded).toEqual({ commit: expect.stringMatching(/^[0-9a-f]{40}$/), set_aside: expect.stringMatching(/^[0-9a-f]{40}$/) });
    expect(await said(TWO, "open")).toEqual({ copy: "moved", set_asides: [recorded.set_aside] });
    // A turn that failed, kept on its branch.
    writeFileSync(join(copyOf(ONE), "Kept.md"), "kept\n");
    expect(await said(ONE, "keep", { author: author(ONE), trailers: [["Surogate-Saga", "saga-3"]], base: false })).toEqual({
      commit: expect.stringMatching(/^[0-9a-f]{40}$/), not_taken: [],
    });
  });

  it("answers a folder history cannot record, and a history that is not the platform's, each as this computer knows it", async () => {
    // Another folder, with a file whose name is not UTF-8: it gets no history, and says why.
    for (const name of ["odd", "odd-store"]) mkdirSync(join(dir, name));
    writeFileSync(odd(join(dir, "odd")), "x");
    const other: Place = { key: "aaaaaaaaaaaaaaaa", history: join(dir, "odd-store"), real: folderOf(join(dir, "odd")) };
    try {
      expect(await guest.place(other)).toBeNull();
      expect(ok(await raw(ONE, "open", {}, other.key))).toEqual({ history: "off", reason: "names" });
    } finally {
      rmSync(odd(join(dir, "odd")));
    }
    // A link in the folder's history: refused whole, for every thread.
    symlinkSync("/etc", join(dir, "store", "history.git", "planted"));
    try {
      expect(await code(ONE, "changed")).toBe("history_refused");
      expect(await code(TWO, "fetch", {})).toBe("history_refused");
    } finally {
      rmSync(join(dir, "store", "history.git", "planted"));
    }
    expect(await said(ONE, "changed")).toEqual({ paths: ["Kept.md"] });
  });

  it("was asked every action a folder's history takes, and refused with each code a test can bring about", () => {
    const actions = /^_ACTIONS[^]*?^\}/m.exec(readFileSync(new URL("../../../surogates/sandbox/local_history.py", import.meta.url), "utf8"))![0];
    expect([...answered.keys()].sort()).toEqual([...actions.matchAll(/^ {4}"([a-z_]+)": \(/gm)].map(([, action]) => action).sort());
    // record_unfinished and move_unfinished need a copy git cannot write: the history's own tests make one.
    expect([...refused].sort()).toEqual([
      "conflict", "failed", "history_refused", "landing_unsettled", "name_not_utf8", "no_whole_copy", "not_a_request", "not_on_base",
    ]);
  });
});

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("a request to a folder's history, ended in the guest", { timeout: 120_000 }, () => {
  let dir: string;
  let options: VmOptions;
  let manager: VmManager;
  let place: Place;
  const ask = (action: string, stop = signal()) => manager.history({ place, thread: ONE, user: "u1", action, args: {} }, stop);
  const beat = () => statSync(join(dir, "store", "beat"), { throwIfNoEntry: false })?.size ?? 0;
  // The writer of a request under way, once it writes: how much it has written.
  const writing = async () => {
    const from = beat();
    await until(() => beat() > from, 30_000);
  };
  // Whether anything in the guest still writes the place.
  const written = async () => {
    const from = beat();
    await new Promise((resolve) => setTimeout(resolve, 600));
    return beat() - from;
  };

  beforeAll(() => {
    let disks: (at: string, disk?: string) => VmOptions;
    ({ dir, place, disks } = world("vm-history-ended-"));
    options = disks(dir, withHistory(agentDisk(dir), WRITER));
    manager = new VmManager(options);
  });

  afterAll(async () => {
    await manager.stop();
    rmSync(options.run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("ends everything a cancelled request started, a writer it left running too, before the place's next request starts", async () => {
    const cancel = new AbortController();
    const first = ask("open", cancel.signal);
    await writing();
    cancel.abort();
    expect(await first).toEqual(CANCELLED);
    // The next request starts only once nothing of the first is left: it finds no writer alive, and none writes after.
    expect(await ask("changed")).toEqual({ ok: { paths: [] } });
    expect(await written()).toBe(0);
  });

  it("lets a place go only once nothing of a request it stopped writes it, and takes it again", async () => {
    const cancel = new AbortController();
    const first = ask("open", cancel.signal);
    await writing();
    cancel.abort();
    const gone = manager.unplace(place);
    expect(await first).toEqual(CANCELLED);
    expect(await gone).toBe(true);
    expect(await written()).toBe(0);
    expect(await ask("changed")).toEqual({ ok: { paths: [] } });
  });

  it("answers a request the guest stopped under as stopped by the sandbox, with nothing of it left writing", async () => {
    const running = ask("open");
    await writing();
    await manager.stop();
    expect(await running).toEqual(SANDBOX_STOPPED);
    expect(await written()).toBe(0);
  });
});

// The file helper's land kind keeps each file a landing replaces, and forgets them on its caller's word. Whether it
// may is the history's to say, and this computer's to hold the land kind to: here with the history in the guest, the
// land kind on the real folder, and the one function that joins them.
describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("what a landing kept of a folder's files, forgotten only by the history's word", { timeout: 120_000 }, () => {
  let dir: string;
  let folder: string;
  let options: VmOptions;
  let manager: VmManager;
  let place: Place;
  let copyOf: (thread: string) => string;
  let kept: string;
  const history = (thread: string, action: string, args: Record<string, unknown> = {}) => manager.history({ place, thread, user: "u1", action, args }, signal());
  // The landing's helper for *thread*: on the real folder, given the thread's copy and where its landings keep.
  const helper = (thread: string, args: Record<string, unknown>) => {
    const context: Context = { folder, home: USER.home, env: {}, landing: { copy: copyOf(thread), kept } };
    return perform("land", args, context, signal());
  };
  // A landing of *thread*'s turn up to its applies, each through the land kind at the revision its look saw.
  const applied = async (thread: string, saga: string) => {
    const trailers = [["Surogate-Saga", saga]];
    const { paths } = ok(await history(thread, "changed")) as { paths: string[] };
    const seen = Object.fromEntries(ok(await helper(thread, { action: "revisions", paths })).revisions as Array<[string, string]>);
    const picked = ok(await history(thread, "pickup", { author: YOU, trailers }));
    const turn = ok(await history(thread, "commit", { author: author(thread), trailers, pickup: picked.commit }));
    const changes = turn.changes as Change[];
    for (const [step, change] of changes.entries()) ok(await helper(thread, { action: "apply", saga, step, ...change, expected: seen[change.path] }));
    return { trailers, picked, turn, changes };
  };
  // The land kind's forgetting of *saga*, asked only where the history's answer lets it be: what was answered.
  const forget = async (thread: string, saga: string, changes: Change[]) => {
    const refusal = forgettable(await history(thread, "forget", { saga, applied: changes }));
    return refusal ?? helper(thread, { action: "forget", saga });
  };
  const UNSETTLED = {
    error: {
      type: "history", code: "landing_unsettled",
      message: "refused the request: this landing was neither recorded nor put back whole: a file it applied is not what was there before it, and what it replaced is kept for its put-back",
    },
  };
  let unsettled: Change[];

  beforeAll(async () => {
    let disks: (at: string) => VmOptions;
    ({ dir, folder, place, copyOf, disks } = world("vm-history-forget-"));
    kept = join(dir, "landings");
    options = disks(dir);
    manager = new VmManager(options);
    for (const thread of [ONE, TWO]) expect(ok(await history(thread, "open"))).toEqual({ copy: "made" });
  }, 120_000);

  afterAll(async () => {
    await manager.stop();
    rmSync(options.run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("forgets what a recorded landing kept", async () => {
    writeFileSync(join(copyOf(ONE), "Report.docx"), "A's report\n");
    const { trailers, picked, turn, changes } = await applied(ONE, "saga-1");
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe("A's report\n");
    expect(readdirSync(join(kept, "saga-1")).sort()).toEqual(["0", "0.json"]);
    // Applied, and not recorded yet: the replaced file is all that could put the folder back.
    expect(await forget(ONE, "saga-1", changes)).toEqual(UNSETTLED);
    expect(readFileSync(join(kept, "saga-1", "0"), "utf8")).toBe("the report, v1\n");
    const recorded = ok(await history(ONE, "record", { turn: turn.commit, applied: changes, author: author(ONE), trailers, main: picked.main, pickup: picked.commit }));
    expect(await history(ONE, "forget", { saga: "saga-1", applied: changes })).toEqual({ ok: { landing: recorded.commit } });
    expect(await forget(ONE, "saga-1", changes)).toEqual({ ok: {} });
    expect(existsSync(join(kept, "saga-1"))).toBe(false);
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe("A's report\n");
  });

  it("forgets nothing of a landing neither recorded nor put back, whatever else the history says of it", async () => {
    expect(ok(await history(TWO, "open"))).toEqual({ copy: "moved" });
    writeFileSync(join(copyOf(TWO), "Report.docx"), "B's report\n");
    writeFileSync(join(copyOf(TWO), "B.md"), "B\n");
    ({ changes: unsettled } = await applied(TWO, "saga-2"));
    expect(unsettled.map((change) => change.path)).toEqual(["B.md", "Report.docx"]);
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe("B's report\n");
    expect(await forget(TWO, "saga-2", unsettled)).toEqual(UNSETTLED);
    // A look at the history names a landing too, and null there is one that did not land: it is no word to forget by.
    const looked = await history(TWO, "fetch", { saga: "saga-2" });
    expect(looked).toMatchObject({ ok: { landing: null, hidden: false } });
    expect(forgettable(looked)).toEqual({
      error: { type: "value", message: "This is no answer of a folder's history to forgetting a landing, so what the landing kept was not forgotten" },
    });
    expect(readFileSync(join(kept, "saga-2", "1"), "utf8")).toBe("A's report\n");
  });

  it("forgets nothing of a landing whose put-back is not whole", async () => {
    // The file it made is taken back; the one it replaced is still the landing's.
    expect(ok(await helper(TWO, { action: "unapply", saga: "saga-2", step: 0, path: "B.md" }))).toMatchObject({ path: "B.md" });
    expect(existsSync(join(folder, "B.md"))).toBe(false);
    expect(await forget(TWO, "saga-2", unsettled)).toEqual(UNSETTLED);
    expect(readFileSync(join(kept, "saga-2", "1"), "utf8")).toBe("A's report\n");
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe("B's report\n");
  });

  it("forgets what a landing kept once it is put back whole, each file as it was before", async () => {
    expect(ok(await helper(TWO, { action: "unapply", saga: "saga-2", step: 1, path: "Report.docx" }))).toMatchObject({ path: "Report.docx" });
    expect(readFileSync(join(folder, "Report.docx"), "utf8")).toBe("A's report\n");
    expect(await history(TWO, "forget", { saga: "saga-2", applied: unsettled })).toEqual({ ok: { landing: null } });
    expect(await forget(TWO, "saga-2", unsettled)).toEqual({ ok: {} });
    expect(existsSync(join(kept, "saga-2"))).toBe(false);
    expect(readdirSync(folder).sort()).toEqual(["Report.docx", "notes"]);
  });
});
