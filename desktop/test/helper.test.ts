import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
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
    // What a landing's helper does before it is ready shows here: it clears what a forgetting cut short left.
    leave();
    expect(await started({ SUROGATE_COPY: copy, SUROGATE_KEPT: kept })).toMatchObject({ said: '{"ready":true}\n', code: 0 });
    expect(existsSync(left)).toBe(false);
    leave();
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
});
