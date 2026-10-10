import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

const HELPER = fileURLToPath(new URL("../dist/files/helper.js", import.meta.url));

let base = "";
beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "helper-")));
  writeFileSync(join(base, "a.txt"), "alpha\n");
});
afterEach(() => rmSync(base, { recursive: true, force: true }));

// The helper outside any sandbox: what it says to a line it cannot act on.
async function lines(input: string[], count: number, more: Record<string, string> = {}): Promise<unknown[]> {
  const child = spawn(process.execPath, [HELPER], {
    env: { SUROGATE_FOLDER: base, HOME: base, PATH: "/usr/bin:/bin", ...more },
    stdio: ["pipe", "pipe", "inherit"],
  });
  const heard: unknown[] = [];
  const done = new Promise<void>((resolve) => {
    createInterface({ input: child.stdout }).on("line", (line) => {
      heard.push(JSON.parse(line));
      if (heard.length === count) resolve();
    });
  });
  for (const line of input) child.stdin.write(`${line}\n`);
  await done;
  child.kill("SIGKILL");
  return heard;
}

// The helper as the app starts it, for what it says of its own start: what it wrote, and how it ended once nothing
// more was asked of it.
async function started(more: Record<string, string>): Promise<{ said: string; failed: string; code: number | null }> {
  const child = spawn(process.execPath, [HELPER], {
    env: { SUROGATE_FOLDER: base, HOME: base, PATH: "/usr/bin:/bin", ...more },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let [said, failed] = ["", ""];
  child.stdout.on("data", (chunk: Buffer) => { said += chunk.toString(); });
  child.stderr.on("data", (chunk: Buffer) => { failed += chunk.toString(); });
  child.stdin.on("error", () => {});
  child.stdin.end();
  const bound = setTimeout(() => child.kill("SIGKILL"), 10_000);
  const code = await new Promise<number | null>((resolve) => child.once("exit", resolve));
  clearTimeout(bound);
  return { said, failed, code };
}
const answered = (heard: unknown[]) => (heard.slice(1) as Array<{ id: string }>).sort((a, b) => a.id.localeCompare(b.id));
const read = (id: string, key: string, more: Record<string, unknown> = {}, beside: Record<string, unknown> = {}) =>
  JSON.stringify({ id, kind: "read", args: { key, max_bytes: null, ...more }, ...beside });
const alpha = { ok: Buffer.from("alpha\n").toString("base64") };
const noPath = (key: string) => ({ error: { type: "sandbox", message: `Not a path in this folder: '${key}'` } });

describe("the file helper on a thread's copy", () => {
  it("works in a thread's copy under the path of the folder it is a copy of, when it is given that path", async () => {
    const at = join(dirname(base), "Reports");
    const heard = await lines([
      JSON.stringify({ id: "1", kind: "resolve", args: { path: "a.txt" } }),
      read("2", `${at}/a.txt`),
      read("3", `${base}/a.txt`),
    ], 4, { SUROGATE_AT: at });
    expect(heard[0]).toEqual({ ready: true });
    // In the order asked: each is answered as it ends.
    expect(answered(heard)).toEqual([
      { id: "1", outcome: { ok: `${at}/a.txt` } },
      { id: "2", outcome: alpha },
      { id: "3", outcome: noPath(`${base}/a.txt`) },
    ]);
  });

  it("is given the folder's path at its start, once, and by nothing it is asked", async () => {
    const at = join(dirname(base), "Reports");
    const elsewhere = join(base, "elsewhere");
    mkdirSync(elsewhere);
    writeFileSync(join(elsewhere, "a.txt"), "another folder's\n");
    const named = { at: elsewhere, folder: elsewhere, SUROGATE_AT: elsewhere, SUROGATE_FOLDER: elsewhere, context: { folder: elsewhere, at: elsewhere }, env: { SUROGATE_AT: elsewhere } };
    const heard = await lines([
      // What a request carries, in its arguments or beside them, names no folder for the helper.
      read("1", `${at}/a.txt`, named, named),
      read("2", `${elsewhere}/a.txt`, named, named),
      JSON.stringify({ id: "3", kind: "resolve", args: { path: "a.txt", ...named }, ...named }),
      // And after them it is the same helper: on the copy, named by the folder's path.
      read("4", `${at}/a.txt`),
      read("5", `${base}/a.txt`),
    ], 6, { SUROGATE_AT: at });
    expect(answered(heard)).toEqual([
      { id: "1", outcome: alpha },
      { id: "2", outcome: noPath(`${elsewhere}/a.txt`) },
      { id: "3", outcome: { ok: `${at}/a.txt` } },
      { id: "4", outcome: alpha },
      { id: "5", outcome: noPath(`${base}/a.txt`) },
    ]);
    // Nor does a chat's helper, started with none, take one from a request.
    const plain = await lines([read("1", `${at}/a.txt`, { at }, { at }), read("2", `${base}/a.txt`, { at }, { at })], 3);
    expect(answered(plain)).toEqual([{ id: "1", outcome: noPath(`${at}/a.txt`) }, { id: "2", outcome: alpha }]);
  });

  it("refuses to start, in words, with a path it cannot name its folder by, rather than work as a chat's helper", async () => {
    const whole = (at: string) => `the file helper's SUROGATE_AT must be the whole path of the folder its copy is of: '${at}'\n`;
    const apart = (at: string) => `the file helper's SUROGATE_AT must name a folder that is not the helper's own, neither holds it nor lies in it: '${at}'\n`;
    const cases: Array<[at: string, why: string]> = [
      ["", whole("")], ["Reports", whole("Reports")], ["./Reports", whole("./Reports")], ["/", whole("/")],
      [`${base}/../Reports`, whole(`${base}/../Reports`)], [`${dirname(base)}//Reports`, whole(`${dirname(base)}//Reports`)],
      [`${dirname(base)}/Reports/`, whole(`${dirname(base)}/Reports/`)], [`${dirname(base)}/./Reports`, whole(`${dirname(base)}/./Reports`)],
      [base, apart(base)], [join(base, "sub"), apart(join(base, "sub"))], [dirname(base), apart(dirname(base))],
    ];
    for (const [at, why] of cases) {
      expect(await started({ SUROGATE_AT: at }), JSON.stringify(at)).toEqual({ said: "", failed: why, code: 2 });
    }
    // Nor with a folder of its own that is no whole path: the two could not be told apart by their names.
    expect(await started({ SUROGATE_FOLDER: `${base}/`, SUROGATE_AT: join(dirname(base), "Reports") })).toEqual({
      said: "", failed: "the file helper's SUROGATE_FOLDER must be a whole path for SUROGATE_AT to name it by another\n", code: 2,
    });
    // One that is a whole path of another folder starts, and ends when it is asked no more.
    expect(await started({ SUROGATE_AT: join(dirname(base), "Reports") })).toEqual({ said: '{"ready":true}\n', failed: "", code: 0 });
  });

  it("is no landing's helper: it lands nothing, puts nothing back at its start, and is not started as both", async () => {
    const at = join(dirname(base), "Reports");
    const [copy, kept] = [join(base, "copy"), join(base, "kept")];
    const left = join(kept, ".forgotten-0f6d1c5e");
    const leave = () => {
      mkdirSync(left, { recursive: true });
      writeFileSync(join(left, "1"), "what a forgetting cut short left\n");
    };
    // Nor does a landing's helper put anything back before it is asked: it says it is ready at once.
    leave();
    expect(await started({ SUROGATE_COPY: copy, SUROGATE_KEPT: kept })).toMatchObject({ said: '{"ready":true}\n', code: 0 });
    expect(readdirSync(left)).toEqual(["1"]);
    const both = "the file helper's SUROGATE_AT is for a thread's copy: a landing's helper works in the folder itself, and is given none\n";
    const landings: Array<Record<string, string>> = [{ SUROGATE_COPY: copy, SUROGATE_KEPT: kept }, { SUROGATE_COPY: copy }, { SUROGATE_KEPT: kept }];
    for (const landing of landings) {
      expect(await started({ SUROGATE_AT: at, ...landing }), JSON.stringify(landing)).toEqual({ said: "", failed: both, code: 2 });
    }
    expect(readdirSync(left)).toEqual(["1"]);
    // Started on a copy, it is asked to land, in each of a landing's ways.
    const unsupported = { error: { type: "unsupported", message: "This computer cannot do 'land' yet" } };
    const asks = [
      { action: "recover" }, { action: "revisions", paths: ["a.txt"] }, { action: "forget", saga: "s1" },
      { action: "apply", saga: "s1", step: 1, path: "a.txt", before: null, after: "0".repeat(40), expected: "absent" },
      { action: "unapply", saga: "s1", step: 1, path: "a.txt" },
    ];
    const heard = await lines(asks.map((args, id) => JSON.stringify({ id: String(id), kind: "land", args })), asks.length + 1, { SUROGATE_AT: at });
    expect(answered(heard)).toEqual(asks.map((_, id) => ({ id: String(id), outcome: unsupported })));
    expect(readdirSync(base).sort()).toEqual(["a.txt", "kept"]);
    expect(readdirSync(left)).toEqual(["1"]);
  });
});

describe("the file helper", () => {
  it("says {ready: true} first, then answers a request", async () => {
    const heard = await lines([JSON.stringify({ id: "1", kind: "stat", args: { key: `${base}/a.txt` } })], 2);
    expect(heard[0]).toEqual({ ready: true });
    expect(heard[1]).toMatchObject({ id: "1", outcome: { ok: expect.anything() } });
  });

  it("ignores a line that is not a request, and keeps answering", async () => {
    const heard = await lines(
      ["null", "5", "[]", "not json", JSON.stringify({ id: "1", kind: "stat", args: { key: `${base}/a.txt` } })],
      2,
    );
    expect(heard[1]).toMatchObject({ id: "1", outcome: { ok: expect.anything() } });
  });

  it("answers a request with an id and no usable kind or args, instead of staying silent", async () => {
    const malformed = { error: { type: "value", message: "malformed request" } };
    const heard = await lines(
      [
        JSON.stringify({ id: "1", kind: 5, args: {} }),
        JSON.stringify({ id: "2", kind: "stat", args: null }),
        JSON.stringify({ id: "3", kind: "stat" }),
      ],
      4,
    );
    expect(heard.slice(1)).toEqual([
      { id: "1", outcome: malformed },
      { id: "2", outcome: malformed },
      { id: "3", outcome: malformed },
    ]);
  });

  it("lands only as a landing's helper, which is given a thread's copy and a folder to keep replaced files in", async () => {
    const look = JSON.stringify({ id: "1", kind: "land", args: { action: "revisions", paths: ["a.txt", "b.txt"] } });
    const unsupported = { id: "1", outcome: { error: { type: "unsupported", message: "This computer cannot do 'land' yet" } } };
    // A chat's own helper, and one given only half of a landing's.
    expect((await lines([look], 2))[1]).toEqual(unsupported);
    expect((await lines([look], 2, { SUROGATE_COPY: join(base, "copy") }))[1]).toEqual(unsupported);
    const landing = await lines([look], 2, { SUROGATE_COPY: join(base, "copy"), SUROGATE_KEPT: join(base, "kept") });
    expect(landing[1]).toMatchObject({ id: "1", outcome: { ok: { revisions: [["a.txt", expect.stringMatching(/^\d+:\d+:6:/)], ["b.txt", "absent"]] } } });
  });

  it("only puts back what a landing cut short, as a recovery's helper, given where the folder's landings keep and no copy: when it is asked, and nothing else", async () => {
    const kept = join(base, "kept");
    const left = join(kept, ".forgotten-0f6d1c5e");
    mkdirSync(left, { recursive: true });
    writeFileSync(join(left, "1"), "what a forgetting cut short left\n");
    const only = { error: { type: "unsupported", message: "This computer only puts back here what a landing cut short in the folder" } };
    const asks = [
      { kind: "read", args: { key: `${base}/a.txt`, max_bytes: null } },
      { kind: "write", args: { key: `${base}/b.txt`, data: Buffer.from("over the user's\n").toString("base64") } },
      { kind: "delete", args: { key: `${base}/a.txt` } },
      { kind: "land", args: { action: "revisions", paths: ["a.txt"] } },
      { kind: "land", args: { action: "apply", saga: "s1", step: 1, path: "b.txt", before: null, after: "0".repeat(40), expected: "absent" } },
      { kind: "land", args: { action: "unapply", saga: "s1", step: 1, path: "a.txt" } },
      { kind: "land", args: { action: "forget", saga: "s1" } },
      { kind: "no-such-kind", args: {} },
    ];
    const heard = await lines(asks.map((ask, id) => JSON.stringify({ id: String(id), ...ask })), asks.length + 1, { SUROGATE_KEPT: kept });
    expect(heard[0]).toEqual({ ready: true });
    expect(answered(heard)).toEqual(asks.map((_, id) => ({ id: String(id), outcome: only })));
    // Nothing was touched: not the folder, and not what its landings keep.
    expect([readdirSync(base).sort(), readdirSync(left)]).toEqual([["a.txt", "kept"], ["1"]]);
    const recovered = await lines([JSON.stringify({ id: "r", kind: "land", args: { action: "recover" } })], 2, { SUROGATE_KEPT: kept });
    expect(recovered[1]).toEqual({ id: "r", outcome: { ok: { restored: [], beside: [], lost: [], unread: [] } } });
    expect(existsSync(left)).toBe(false);
  });
});

describe("the folders a file helper is given, as its host found them when it checked them", () => {
  // A folder as its host names the one it checked: its device and its inode.
  const is = (path: string) => {
    const { dev, ino } = statSync(path);
    return `${dev}:${ino}`;
  };
  const NOT_CHECKED = "is not the folder its host checked: another was at its path as its sandbox was made\n";
  let copy = "";
  let kept = "";
  let other = "";
  // What a landing's helper puts right in its kept folder once it is asked shows here: it clears what a forgetting cut short left.
  const left = () => join(kept, ".forgotten-0f6d1c5e");
  beforeEach(() => {
    [copy, kept, other] = [join(base, "copy"), join(base, "kept"), join(base, "other")];
    for (const dir of [copy, other, left()]) mkdirSync(dir, { recursive: true });
    writeFileSync(join(left(), "1"), "what a forgetting cut short left\n");
  });

  it("starts in the folder its host checked, and, for a landing, with the copy and the kept folder its host checked", async () => {
    expect(await started({ SUROGATE_FOLDER_IS: is(base) })).toEqual({ said: '{"ready":true}\n', failed: "", code: 0 });
    expect(await started({ SUROGATE_FOLDER_IS: is(base), SUROGATE_AT: join(dirname(base), "Reports") })).toEqual({ said: '{"ready":true}\n', failed: "", code: 0 });
    const landing = { SUROGATE_FOLDER_IS: is(base), SUROGATE_COPY: copy, SUROGATE_COPY_IS: is(copy), SUROGATE_KEPT: kept, SUROGATE_KEPT_IS: is(kept) };
    expect(await started(landing)).toEqual({ said: '{"ready":true}\n', failed: "", code: 0 });
    expect(readdirSync(left())).toEqual(["1"]);
    const recovered = await lines([JSON.stringify({ id: "1", kind: "land", args: { action: "recover" } })], 2, landing);
    expect([recovered[1], existsSync(left())]).toEqual([{ id: "1", outcome: { ok: { restored: [], beside: [], lost: [], unread: [] } } }, false]);
    // And a recovery's, with the kept folder its host checked and no copy.
    mkdirSync(left(), { recursive: true });
    writeFileSync(join(left(), "1"), "what a forgetting cut short left\n");
    const recovery = { SUROGATE_FOLDER_IS: is(base), SUROGATE_KEPT: kept, SUROGATE_KEPT_IS: is(kept) };
    expect(await started(recovery)).toEqual({ said: '{"ready":true}\n', failed: "", code: 0 });
    expect((await lines([JSON.stringify({ id: "1", kind: "land", args: { action: "recover" } })], 2, recovery))[1]).toMatchObject({ outcome: { ok: { restored: [] } } });
    expect(existsSync(left())).toBe(false);
    // What it is told of them is its own to go by: no request, and nothing it starts, is given it.
    const heard = await lines([JSON.stringify({ id: "1", kind: "ripgrep", args: { key: base, mode: "files", pattern: "*.txt", glob: null, context: 0 } })], 2, { SUROGATE_FOLDER_IS: is(base) });
    expect(heard[1]).toEqual({ id: "1", outcome: { ok: `${base}/a.txt\n` } });
  });

  it("ends before it looks at anything where another folder is at its folder's path, or at the copy's or the kept folder's: by the folder's name, never by a copy's path", async () => {
    const at = join(dirname(base), "Reports");
    const landing = { SUROGATE_FOLDER_IS: is(base), SUROGATE_COPY: copy, SUROGATE_COPY_IS: is(copy), SUROGATE_KEPT: kept, SUROGATE_KEPT_IS: is(kept) };
    symlinkSync(base, join(dirname(base), `${basename(base)}-link`));
    const refusals: Array<[string, Record<string, string>, string]> = [
      ["a chat's folder", { SUROGATE_FOLDER_IS: is(other) }, `the folder ${base} ${NOT_CHECKED}`],
      ["a thread's copy", { SUROGATE_FOLDER_IS: is(other), SUROGATE_AT: at }, `the copy of ${at} this thread works in ${NOT_CHECKED}`],
      ["a landing's folder", { ...landing, SUROGATE_FOLDER_IS: is(other) }, `the folder ${base} ${NOT_CHECKED}`],
      ["the copy a landing lands from", { ...landing, SUROGATE_COPY_IS: is(other) }, `the thread's copy a landing in ${base} lands from ${NOT_CHECKED}`],
      ["the folder a landing keeps in", { ...landing, SUROGATE_KEPT_IS: is(other) }, `the folder a landing in ${base} keeps replaced files in ${NOT_CHECKED}`],
      ["a copy that is not there", { ...landing, SUROGATE_COPY: join(base, "gone") }, `the thread's copy a landing in ${base} lands from ${NOT_CHECKED}`],
      ["a kept folder that is a file", { ...landing, SUROGATE_KEPT: join(base, "a.txt"), SUROGATE_KEPT_IS: is(join(base, "a.txt")) }, `the folder a landing in ${base} keeps replaced files in ${NOT_CHECKED}`],
      ["a folder reached through a link", { SUROGATE_FOLDER: join(dirname(base), `${basename(base)}-link`), SUROGATE_FOLDER_IS: is(base) }, `the folder ${join(dirname(base), `${basename(base)}-link`)} ${NOT_CHECKED}`],
      ["a folder its host named by no device and inode", { SUROGATE_FOLDER_IS: "" }, `the folder ${base} ${NOT_CHECKED}`],
      ["the same inode on another device", { SUROGATE_FOLDER_IS: `${statSync(base).dev + 1}:${statSync(base).ino}` }, `the folder ${base} ${NOT_CHECKED}`],
      ["a copy it is told of and not given", { SUROGATE_FOLDER_IS: is(base), SUROGATE_COPY_IS: is(copy) }, `the thread's copy a landing in ${base} lands from ${NOT_CHECKED}`],
    ];
    try {
      for (const [what, told, why] of refusals) {
        expect(await started(told), what).toEqual({ said: "", failed: why, code: 2 });
        // Nothing of what a landing's helper would put right once asked.
        expect(readdirSync(left()), what).toEqual(["1"]);
      }
      expect(refusals.map(([, , why]) => why).filter((why) => why.includes(copy) || why.includes(kept))).toEqual([]);
    } finally {
      rmSync(join(dirname(base), `${basename(base)}-link`));
    }
  });
});
