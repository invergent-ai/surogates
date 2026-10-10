// A file helper whose folder is a thread's copy (spec, Section 13, "The user's computer"): it is asked, and answers,
// by the path of the folder the copy is of, and every check runs on the copy. The kinds run in this process, as
// land.test.ts runs its own: outside any sandbox, their own checks are all there is. Around each of them, everything
// outside the copy is looked at, the folder itself first: its names, modes, times and bytes are as they were.

import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync, existsSync, linkSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, readlinkSync, realpathSync, renameSync, rmSync,
  symlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { searched } from "../src/files/edge.js";
import { type Context, kinds, perform } from "../src/files/operations.js";
import { fileToolsMissing } from "../src/hosts/policy.js";

const KEY = "0123456789abcdef";
const THREAD = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const OTHER = "7d2f9a10-3c4b-4e5d-8f60-1a2b3c4d5e6f";
const HELPER = fileURLToPath(new URL("../dist/files/helper.js", import.meta.url));
const dist = (file: string) => new URL(`../dist/${file}`, import.meta.url).href;

let base: string;
let at: string; // the folder, as the server and the model name it
let place: string; // where the app keeps the folder's history and its threads' copies
let copy: string; // the thread's copy of the folder, where the helper works
let context: Context;

// The folder and the thread's copy of it, each holding the same names with its own words in them.
function lay(folder = join("home", "Reports"), data = "data"): void {
  at = join(base, folder);
  place = join(base, data, "history", KEY);
  copy = join(place, "threads", THREAD);
  for (const [root, who] of [[at, "the folder's"], [copy, "the copy's"]] as const) {
    mkdirSync(join(root, "sub"), { recursive: true });
    writeFileSync(join(root, "a.txt"), `${who} alpha\n`);
    writeFileSync(join(root, "sub", "b.txt"), `${who} beta\n`);
  }
  context = { folder: copy, at, home: join(base, "home"), env: { PATH: "/usr/bin:/bin" } };
}

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "edge-")));
  lay();
});
afterEach(() => rmSync(base, { recursive: true, force: true }));

// What lies outside the thread's copy, the folder itself among it: each name with its mode, its inode, its size and its
// times, a file's bytes and a link's target. Under *roots*, or everything the test made.
function outside(...roots: string[]): string[] {
  const seen: string[] = [];
  const look = (path: string): void => {
    if (path === copy) return;
    const st = lstatSync(path, { bigint: true });
    const holds = st.isSymbolicLink() ? readlinkSync(path) : st.isFile() ? createHash("sha256").update(readFileSync(path)).digest("hex") : "";
    seen.push([path, st.mode.toString(8), st.ino, st.nlink, st.size, st.mtimeNs, st.ctimeNs, holds].join(" "));
    if (st.isDirectory()) for (const name of readdirSync(path).sort()) look(join(path, name));
  };
  for (const root of roots.length > 0 ? roots : [base]) look(root);
  return seen;
}

type Answer = { ok: unknown } | { error: { type: string; message: string; code?: string } };

// One operation, asked of the helper on the copy; nothing outside the copy is other than it was before it.
async function run(kind: string, args: Record<string, unknown>, on: Context = context): Promise<Answer> {
  const before = outside();
  const outcome = (await perform(kind, args, on, new AbortController().signal)) as Answer;
  expect(outside(), `${kind} touched what lies outside the thread's copy`).toEqual(before);
  return outcome;
}
const data = (text: string) => Buffer.from(text).toString("base64");
const search = (mode: string, pattern: string, key = at) => run("ripgrep", { key, mode, pattern, glob: null, context: 0 });
const message = (answer: Answer): string => ("error" in answer ? answer.error.message : `it answered ${JSON.stringify(answer.ok)}`);
const leaves = (asked: string, leads: string) =>
  ({ error: { type: "sandbox", message: `Path traversal blocked: '${asked}' resolves to '${leads}' which is outside the workspace '${at}'.` } });
const noPath = (key: string) => ({ error: { type: "sandbox", message: `Not a path in this folder: '${key}'` } });
const whole = { skip: [], skip_top: [], skip_hidden: false, since: null };
const lines = { encoding: "utf-8", offset: 1, limit: 10, max_bytes: 1024 };

describe("a file helper on a thread's copy", () => {
  it("names the copy's files by the folder's path, and works in the copy alone", async () => {
    expect(await run("resolve", { path: "a.txt" })).toEqual({ ok: join(at, "a.txt") });
    expect(await run("resolve", { path: join(at, "sub", "..", "sub", "b.txt") })).toEqual({ ok: join(at, "sub", "b.txt") });
    expect(await run("resolve", { path: "~/Reports/new/c.txt" })).toEqual({ ok: join(at, "new", "c.txt") });
    expect(await run("read", { key: join(at, "a.txt"), max_bytes: null })).toEqual({ ok: data("the copy's alpha\n") });
    expect(await run("list_dir", { key: at })).toMatchObject({ ok: expect.arrayContaining(["a.txt", "sub"]) });
    expect(await run("stat", { key: join(at, "sub") })).toMatchObject({ ok: { is_dir: true } });
    expect(await run("write", { key: join(at, "new", "c.txt"), data: data("made by the thread\n") })).toEqual({ ok: null });
    expect(await run("delete", { key: join(at, "sub", "b.txt") })).toEqual({ ok: null });
    expect(await run("walk", { key: at, ...whole })).toMatchObject({ ok: { files: expect.arrayContaining([["a.txt", 17], ["new/c.txt", 19]]) } });
    expect(readFileSync(join(copy, "new", "c.txt"), "utf8")).toBe("made by the thread\n");
    expect(existsSync(join(copy, "sub", "b.txt"))).toBe(false);
    // The folder itself, which holds the same names, is as it was.
    expect(readFileSync(join(at, "sub", "b.txt"), "utf8")).toBe("the folder's beta\n");
    expect(existsSync(join(at, "new"))).toBe(false);
  });

  it("takes the folder's path however it is spelled, out of the folder and back in too", async () => {
    for (const path of [
      join(at, "a.txt"), `${at}/../Reports/a.txt`, `${dirname(at)}/./Reports//a.txt`, "../Reports/a.txt", "sub/../a.txt",
      // Up to the root and down again by the folder's own path: the folder's files are the copy's, whichever way they are reached.
      `${"../".repeat(at.split("/").length + 2)}${at.slice(1)}/a.txt`,
    ]) {
      expect(await run("resolve", { path }), path).toEqual({ ok: join(at, "a.txt") });
    }
  });

  it("takes no key and no path but the folder's: the copy's own path names nothing", async () => {
    const own = join(copy, "a.txt");
    const refused = noPath(own);
    expect(await run("read", { key: own, max_bytes: null })).toEqual(refused);
    expect(await run("read_lines", { key: own, ...lines })).toEqual(refused);
    expect(await run("write", { key: own, data: data("x") })).toEqual(refused);
    expect(await run("delete", { key: own })).toEqual(refused);
    expect(await run("list_dir", { key: copy })).toEqual(noPath(copy));
    expect(await run("walk", { key: copy, ...whole })).toEqual(noPath(copy));
    expect(await search("files", "*.txt", copy)).toEqual(noPath(copy));
    expect(await run("stat", { key: own })).toEqual({ ok: null });
    expect(await run("resolve", { path: own })).toEqual(leaves(own, own));
    // However it is spelled: tidied, it is still the copy's own.
    for (const path of [`${copy}/sub/../a.txt`, `${copy}//a.txt`, `${at}/${"../".repeat(at.split("/").length)}${own.slice(1)}`, `${"../".repeat(at.split("/").length)}${own.slice(1)}`]) {
      expect(await run("resolve", { path }), path).toEqual(leaves(path, own));
    }
    // A name that is protected in the folder is not protected there: it is no name of the folder's.
    expect(await run("check_write", { path: join(copy, ".git", "config") })).toEqual({ ok: null });
    expect(readFileSync(own, "utf8")).toBe("the copy's alpha\n");
  });

  it("answers its own path as it answers any path outside the folder: nothing in the words says the copy is there", async () => {
    const nowhere = join(base, "nowhere", "at", "all");
    // A link in the copy, too: asked by the copy's own path, it is not looked at.
    symlinkSync("/etc", join(copy, "system"));
    const asks: Array<[string, (path: string) => Record<string, unknown>]> = [
      ["resolve", (path) => ({ path: join(path, "a.txt") })], ["resolve", (path) => ({ path: join(path, "missing.txt") })],
      ["resolve", (path) => ({ path: join(path, "system", "hosts") })], ["check_write", (path) => ({ path: join(path, "system", "passwd") })],
      ["check_write", (path) => ({ path: join(path, ".vscode", "settings.json") })], ["stat", (key) => ({ key: join(key, "a.txt") })],
      ["read", (key) => ({ key: join(key, "a.txt"), max_bytes: null })], ["read_lines", (key) => ({ key: join(key, "a.txt"), ...lines })],
      ["write", (key) => ({ key: join(key, "a.txt"), data: data("x") })], ["delete", (key) => ({ key: join(key, "a.txt") })],
      ["list_dir", (key) => ({ key })], ["walk", (key) => ({ key, ...whole })],
      ["ripgrep", (key) => ({ key, mode: "files", pattern: "*.txt", glob: null, context: 0 })],
    ];
    for (const [kind, args] of asks) {
      const told = JSON.stringify(await run(kind, args(copy)));
      expect(told.replaceAll(copy, nowhere), kind).toBe(JSON.stringify(await run(kind, args(nowhere))));
    }
  });

  it("says where a path that leaves the copy leads as the folder's own path has it", async () => {
    expect(await run("resolve", { path: "../other/x.txt" })).toEqual(leaves("../other/x.txt", join(dirname(at), "other", "x.txt")));
    symlinkSync("/etc", join(copy, "system"));
    expect(await run("resolve", { path: "system/hosts" })).toEqual(leaves("system/hosts", "/etc/hosts"));
    // And no key reaches through such a link.
    expect(await run("read", { key: join(at, "system", "hosts"), max_bytes: null })).toEqual(noPath(join(at, "system", "hosts")));
  });

  it("follows a link in the copy that names the folder's path, as the thread's commands do in the guest", async () => {
    symlinkSync(join(at, "sub"), join(copy, "linked"));
    symlinkSync("../Reports/sub", join(copy, "around"));
    // A file only the folder itself holds: the link leads into the copy, where there is none.
    writeFileSync(join(at, "only-here.txt"), "the folder's own\n");
    symlinkSync(join(at, "only-here.txt"), join(copy, "only"));
    expect(await run("resolve", { path: "linked/b.txt" })).toEqual({ ok: join(at, "sub", "b.txt") });
    expect(await run("resolve", { path: "around/b.txt" })).toEqual({ ok: join(at, "sub", "b.txt") });
    expect(await run("read", { key: join(at, "sub", "b.txt"), max_bytes: null })).toEqual({ ok: data("the copy's beta\n") });
    expect(await run("resolve", { path: "only" })).toEqual({ ok: join(at, "only-here.txt") });
    expect(await run("read", { key: join(at, "only-here.txt"), max_bytes: null })).toEqual({
      error: { type: "os", code: "ENOENT", message: `No such file or directory: '${join(at, "only-here.txt")}'` },
    });
    // A key is a file's one name: none goes through a link, wherever the link leads.
    expect(await run("read", { key: join(at, "linked", "b.txt"), max_bytes: null })).toEqual(noPath(join(at, "linked", "b.txt")));
    expect(await run("write", { key: join(at, "only"), data: data("x") })).toEqual(noPath(join(at, "only")));
  });

  it("follows no link out of the copy: not to the copy beside it, not to the folder's place, and its own path leads back into it", async () => {
    const other = join(place, "threads", OTHER);
    mkdirSync(join(other, "sub"), { recursive: true });
    writeFileSync(join(other, "sub", "b.txt"), "another thread's beta\n");
    mkdirSync(join(place, "history.git"));
    writeFileSync(join(place, "history.git", "config"), "the history's own\n");
    symlinkSync(join(other, "sub"), join(copy, "to-other"));
    symlinkSync(`../${OTHER}/sub`, join(copy, "beside"));
    symlinkSync(place, join(copy, "to-place"));
    symlinkSync("../../history.git", join(copy, "above"));
    symlinkSync(join(copy, "sub"), join(copy, "to-itself"));
    symlinkSync(join(place, "threads"), join(copy, "to-threads"));
    const answers: Answer[] = [];
    const ask = async (kind: string, args: Record<string, unknown>) => {
      answers.push(await run(kind, args));
      return answers.at(-1);
    };
    // Each link's own words lead out of the folder, and are said as they are: they are what the copy holds.
    expect(await ask("resolve", { path: "to-other/b.txt" })).toEqual(leaves("to-other/b.txt", join(other, "sub", "b.txt")));
    expect(await ask("resolve", { path: "to-place/history.git/config" })).toEqual(leaves("to-place/history.git/config", join(place, "history.git", "config")));
    // By a path from the copy, a link leads from the folder's own path: what lies beside the copy is not there.
    expect(await ask("resolve", { path: "beside/b.txt" })).toEqual(leaves("beside/b.txt", join(dirname(at), OTHER, "sub", "b.txt")));
    expect(await ask("resolve", { path: "above/config" })).toEqual(leaves("above/config", join(dirname(dirname(at)), "history.git", "config")));
    // The copy's own path, in a link, is the folder's: it leads into the copy, and is answered by the folder's name.
    expect(await ask("resolve", { path: "to-itself/b.txt" })).toEqual({ ok: join(at, "sub", "b.txt") });
    expect(await ask("resolve", { path: `to-threads/${THREAD}/sub/b.txt` })).toEqual({ ok: join(at, "sub", "b.txt") });
    // From there on it is the folder's path that is followed: a link past it is one of the copy's, and a loop is a loop.
    symlinkSync(copy, join(copy, "to-root"));
    symlinkSync(join(at, "sub"), join(copy, "linked"));
    symlinkSync(join(copy, "pong"), join(copy, "ping"));
    symlinkSync(join(copy, "ping"), join(copy, "pong"));
    expect(await ask("resolve", { path: "to-root/linked/b.txt" })).toEqual({ ok: join(at, "sub", "b.txt") });
    expect(await ask("resolve", { path: "ping/x" })).toEqual({
      error: { type: "os", code: "ELOOP", message: `Too many levels of symbolic links: '${join(at, "ping", "x")}'` },
    });
    for (const name of ["to-other", "beside", "to-place", "above", "to-itself"]) {
      const key = join(at, name, name.includes("place") ? "history.git/config" : name === "above" ? "config" : "b.txt");
      expect(await ask("read", { key, max_bytes: null }), name).toEqual(noPath(key));
      expect(await ask("write", { key, data: data("planted") }), name).toEqual(noPath(key));
      expect(await ask("delete", { key }), name).toEqual(noPath(key));
      expect(await ask("check_write", { path: key }), name).toEqual({ ok: null });
    }
    // Nothing of what the links name was read, and no answer holds this copy's path.
    for (const answer of answers) {
      const told = JSON.stringify(answer);
      expect(told).not.toContain(copy);
      expect(told).not.toContain(data("another thread's beta\n"));
      expect(told).not.toContain(data("the history's own\n"));
    }
    expect(readFileSync(join(other, "sub", "b.txt"), "utf8")).toBe("another thread's beta\n");
  });

  it("never says the copy's path for a link that spells it through a loop", async () => {
    symlinkSync("round", join(copy, "round"));
    symlinkSync(`round/${"../".repeat(at.split("/").length + 1)}${copy.slice(1)}/sub`, join(copy, "spelled"));
    const told = await run("resolve", { path: "spelled/b.txt" });
    expect(JSON.stringify(told)).not.toContain(copy);
    expect(await run("resolve", { path: "round/x" })).toEqual({
      error: { type: "os", code: "ELOOP", message: `Too many levels of symbolic links: '${join(at, "round", "x")}'` },
    });
  });

  it("works nowhere once its copy is no folder of the app's own: a link in the copy's stead, or on its way, is not followed", async () => {
    const refused = { error: { type: "sandbox", message: "This thread's copy is not a folder of the app's own" } };
    const asks: Array<[string, Record<string, unknown>]> = [
      ["resolve", { path: "a.txt" }], ["check_write", { path: "a.txt" }], ["read", { key: join(at, "a.txt"), max_bytes: null }],
      ["read_lines", { key: join(at, "a.txt"), ...lines }], ["write", { key: join(at, "a.txt"), data: data("planted") }],
      ["write", { key: join(at, "planted.txt"), data: data("planted") }], ["delete", { key: join(at, "a.txt") }],
      ["list_dir", { key: at }], ["walk", { key: at, ...whole }], ["ripgrep", { key: at, mode: "files", pattern: "*", glob: null, context: 0 }],
    ];
    const kept = `${copy}.kept`;
    renameSync(copy, kept);
    // In the copy's stead: the folder itself, which the copy stands for, and then the copy beside it.
    for (const target of [at, kept]) {
      symlinkSync(target, copy);
      for (const [kind, args] of asks) expect(await run(kind, args), kind).toEqual(refused);
      expect(await run("stat", { key: join(at, "a.txt") })).toEqual({ ok: null });
      rmSync(copy);
    }
    // On its way: the folder that holds the threads' copies.
    renameSync(kept, copy);
    const threads = dirname(copy);
    renameSync(threads, `${threads}.kept`);
    symlinkSync(`${threads}.kept`, threads);
    for (const [kind, args] of asks) expect(await run(kind, args), kind).toEqual(refused);
    expect(readFileSync(join(`${threads}.kept`, THREAD, "a.txt"), "utf8")).toBe("the copy's alpha\n");
    expect(existsSync(join(at, "planted.txt"))).toBe(false);
  });

  it("refuses a path that holds a NUL before it looks at anything, in the cloud's words", async () => {
    const refused = { error: { type: "value", message: "A path, a command or a search pattern cannot hold a NUL character" } };
    for (const path of [`${at}/a\0.txt`, `a.txt\0${copy}`, `${copy}/a.txt\0`, `\0`, `${at}\0/../../a.txt`]) {
      expect(await run("resolve", { path })).toEqual(refused);
      expect(await run("check_write", { path })).toEqual(refused);
      for (const [kind, more] of [["read", { max_bytes: null }], ["read_lines", lines], ["write", { data: data("x") }], ["delete", {}], ["list_dir", {}], ["walk", whole]] as const) {
        expect(await run(kind, { key: path, ...more }), kind).toEqual(refused);
      }
      expect(await search("files", "*", path)).toEqual(refused);
      expect(await run("stat", { key: path })).toEqual({ ok: null });
    }
    expect(await search("json", `beta\0${copy}`)).toEqual(refused);
    expect(readdirSync(copy).sort()).toEqual(["a.txt", "sub"]);
  });

  it("judges a write by the copy's protected names, asked by the folder's path", async () => {
    const denied = (path: string) => `Write denied: '${path}' is protected in this folder: a change to it could run code outside the sandbox.`;
    expect(await run("check_write", { path: join(at, ".vscode", "settings.json") })).toEqual({ ok: denied(join(at, ".vscode", "settings.json")) });
    expect(await run("check_write", { path: ".git/hooks/pre-commit" })).toEqual({ ok: denied(".git/hooks/pre-commit") });
    expect(await run("check_write", { path: join(at, "sub", "b.txt") })).toEqual({ ok: null });
    expect(await run("check_write", { path: "/etc/passwd" })).toEqual({ ok: "Write denied: '/etc/passwd' is a protected system/credential file." });
    expect(await run("check_write", { path: "~/.ssh/config" })).toEqual({ ok: "Write denied: '~/.ssh/config' is a protected system/credential file." });
    // Through a link in the copy that names the folder's path: the name it leads to is the one judged.
    symlinkSync(join(at, ".git"), join(copy, "repo"));
    expect(await run("check_write", { path: "repo/config" })).toEqual({ ok: denied("repo/config") });
    expect(existsSync(join(copy, ".git"))).toBe(false);
  });

  it("is a plain chat's helper without the folder's path: its folder is named by its own", async () => {
    context = { folder: copy, home: join(base, "home"), env: { PATH: "/usr/bin:/bin" } };
    expect(await run("resolve", { path: "a.txt" })).toEqual({ ok: join(copy, "a.txt") });
    expect(await run("read", { key: join(copy, "a.txt"), max_bytes: null })).toEqual({ ok: data("the copy's alpha\n") });
    expect(await run("read", { key: join(copy, "missing.txt"), max_bytes: null })).toEqual({
      error: { type: "os", code: "ENOENT", message: `No such file or directory: '${join(copy, "missing.txt")}'` },
    });
    expect(await run("read", { key: join(at, "a.txt"), max_bytes: null })).toEqual(noPath(join(at, "a.txt")));
  });
});

describe("what a helper on a thread's copy says when it fails", () => {
  it("words a failure with the folder's path, never the copy's", async () => {
    expect(await run("read", { key: join(at, "missing.txt"), max_bytes: null })).toEqual({
      error: { type: "os", code: "ENOENT", message: `No such file or directory: '${join(at, "missing.txt")}'` },
    });
    expect(await run("write", { key: join(at, "a.txt"), data: data("x"), expected_revision: "1:2:3:4:5" })).toEqual({
      error: { type: "conflict", message: `${join(at, "a.txt")} changed on this computer after it was read, so it was not written. Read it again, then make the change again` },
    });
    expect(await run("write", { key: join(at, ".git", "config"), data: data("x") })).toEqual({
      error: { type: "sandbox", message: `Write denied: '${join(at, ".git", "config")}' is protected in this folder: a change to it could run code outside the sandbox.` },
    });
    expect(await run("read", { key: join(at, "sub"), max_bytes: null })).toMatchObject({ error: { message: `Is a directory: '${join(at, "sub")}'` } });
    expect(await run("delete", { key: at })).toMatchObject({ error: { message: `Is a directory: '${at}'` } });
    expect(existsSync(join(copy, ".git"))).toBe(false);
  });

  it("words it as the cloud would for a file at the folder's path, whatever either path holds", async () => {
    const missing = () => run("read", { key: join(at, "missing.txt"), max_bytes: null });
    const denied = () => run("write", { key: join(at, ".git", "config"), data: data("x") });
    const stale = () => run("write", { key: join(at, "a.txt"), data: data("x"), expected_revision: "1:2:3:4:5" });
    // Python's repr() picks its quote by the path it words: the folder's, not the copy's.
    lay(join("home", "Mom's Reports"));
    expect(message(await missing())).toBe(`No such file or directory: "${at}/missing.txt"`);
    lay(join("home", "Plain"), "o'brien's data");
    expect(message(await missing())).toBe(`No such file or directory: '${at}/missing.txt'`);
    expect(message(await denied())).toBe(`Write denied: '${at}/.git/config' is protected in this folder: a change to it could run code outside the sandbox.`);
    // What repr() escapes in the copy's path is not in the folder's.
    lay(join("home", "Escaped"), "da\\ta here");
    expect(message(await missing())).toBe(`No such file or directory: '${at}/missing.txt'`);
    // A path is words to put in, never a pattern.
    lay(join("home", "Cash $& $' $$ Reports"));
    expect(message(await denied())).toBe(`Write denied: '${at}/.git/config' is protected in this folder: a change to it could run code outside the sandbox.`);
    expect(message(await stale())).toBe(`${at}/a.txt changed on this computer after it was read, so it was not written. Read it again, then make the change again`);
    expect(message(await missing())).toBe(`No such file or directory: "${at}/missing.txt"`);
  });

  it("names a path once, where the folder's own path holds the copy's in its words", async () => {
    lay(join("mirror", copy.slice(1)));
    expect(at.endsWith(copy)).toBe(true);
    expect(message(await run("read", { key: join(at, "missing.txt"), max_bytes: null }))).toBe(`No such file or directory: '${at}/missing.txt'`);
    expect(message(await run("write", { key: join(at, ".git", "config"), data: data("x") })))
      .toBe(`Write denied: '${at}/.git/config' is protected in this folder: a change to it could run code outside the sandbox.`);
    expect(((await search("files", "*.txt")) as { ok: string }).ok.split("\n").filter(Boolean).sort()).toEqual([join(at, "a.txt"), join(at, "sub", "b.txt")]);
    mkdirSync(join(copy, "shut"));
    chmodSync(join(copy, "shut"), 0);
    try {
      expect(message(await search("json", "beta"))).toBe(`rg exited 2: rg: ${at}/shut: Permission denied (os error 13)\n`);
    } finally {
      chmodSync(join(copy, "shut"), 0o700);
    }
  });

  it("names a folder the copy lies in as the folder above the folder's own path", async () => {
    // The copy went, with the folder that held it, and nothing can be made where it was.
    rmSync(dirname(copy), { recursive: true });
    chmodSync(place, 0o500);
    try {
      expect(await run("write", { key: join(at, "new.txt"), data: data("x") })).toEqual({
        error: { type: "os", code: "EACCES", message: `Permission denied: '${dirname(at)}'` },
      });
    } finally {
      chmodSync(place, 0o700);
    }
  });
});

describe("a search in a thread's copy", () => {
  it("answers with the folder's paths in each mode, which its caller then asks about", async () => {
    const files = (await search("files", "*.txt")) as { ok: string };
    expect(files.ok.split("\n").filter(Boolean).sort()).toEqual([join(at, "a.txt"), join(at, "sub", "b.txt")]);
    const counts = (await search("count", "copy's")) as { ok: string };
    expect(counts.ok.split("\n").filter(Boolean).sort()).toEqual([`${join(at, "a.txt")}:1`, `${join(at, "sub", "b.txt")}:1`]);
    const found = (await search("json", "beta", join(at, "sub"))) as { ok: string };
    const events = found.ok.split("\n").filter(Boolean).map((line) => JSON.parse(line) as { type: string; data: { path?: { text: string }; lines?: { text: string } } });
    expect(events.filter((event) => event.type !== "summary").map((event) => event.data.path?.text)).toEqual(Array(3).fill(join(at, "sub", "b.txt")));
    expect(events.find((event) => event.type === "match")?.data.lines?.text).toBe("the copy's beta\n");
    // One file, asked by its key.
    expect(await search("files", "*.txt", join(at, "a.txt"))).toEqual({ ok: `${join(at, "a.txt")}\n` });
    // A file's own text that holds the copy's path is the file's: only the names are the folder's.
    writeFileSync(join(copy, "paths.txt"), `see ${copy}/a.txt\n`);
    const text = (await search("json", "see ")) as { ok: string };
    expect(text.ok).toContain(`see ${copy}/a.txt`);
    expect(JSON.parse(text.ok.split("\n")[0] ?? "{}")).toMatchObject({ data: { path: { text: join(at, "paths.txt") } } });
  });

  it("names a file whose name is no text by the folder's path, in its bytes", async () => {
    const name = Buffer.concat([Buffer.from("caf"), Buffer.from([0xe9]), Buffer.from(".txt")]);
    writeFileSync(Buffer.concat([Buffer.from(`${copy}/`), name]), "beta, in a file named in another alphabet\n");
    const found = (await search("json", "another alphabet")) as { ok: string };
    const begin = JSON.parse(found.ok.split("\n")[0] ?? "{}") as { data: { path: { bytes?: string; text?: string } } };
    expect(Buffer.from(begin.data.path.bytes ?? "", "base64")).toEqual(Buffer.concat([Buffer.from(`${at}/`), name]));
    expect(found.ok).not.toContain(Buffer.from(`${copy}/`).toString("base64").slice(0, 40));
  });

  it("names no file by the copy's path in a line of rg's that is no event", () => {
    expect(searched("json", `rg: ${copy}/a.txt went wrong\n`, { at, folder: copy })).toBe(`rg: ${at}/a.txt went wrong\n`);
    expect(searched("json", JSON.stringify(`${copy}/a.txt`), { at, folder: copy })).toBe(JSON.stringify(`${at}/a.txt`));
    // As JSON writes a path: a quote in the copy's is escaped there.
    const quoted = { at, folder: join(base, 'da"ta', THREAD) };
    expect(searched("json", `{"data":{"path":{"text":${JSON.stringify(`${quoted.folder}/a.txt`).slice(0, -1)}`, quoted)).toBe(`{"data":{"path":{"text":"${at}/a.txt`);
  });

  it("says what rg says of a failure by the folder's path, a path the cut fell in gone with what was cut", async () => {
    mkdirSync(join(copy, "shut"));
    chmodSync(join(copy, "shut"), 0);
    try {
      expect(await search("json", "beta")).toEqual({
        error: { type: "ripgrep", message: `rg exited 2: rg: ${at}/shut: Permission denied (os error 13)\n` },
      });
    } finally {
      chmodSync(join(copy, "shut"), 0o700);
    }
    // A copy whose path is long, so that the bytes kept of what rg says end inside it, within the characters answered.
    lay(join("home", "Reports"), join("data", "x".repeat(200), "y".repeat(200)));
    for (const name of ["one", "two", "three"]) {
      mkdirSync(join(copy, name));
      chmodSync(join(copy, name), 0);
    }
    try {
      const said = message(await search("json", "beta"));
      expect(said).toMatch(new RegExp(`^rg exited 2: rg: ${at}/(one|two|three): Permission denied`));
      expect(said).not.toContain(join(base, "data"));
      expect(said).not.toContain("xxx");
    } finally {
      for (const name of ["one", "two", "three"]) chmodSync(join(copy, name), 0o700);
    }
  });

  it("is capped by what it answers: the copy's longer path costs a thread no results, and a folder's longer path is counted", async () => {
    // 3,000 files: over the cap by the copy's path, a third of it by the folder's.
    lay(join("home", "Reports"), join("data", "x".repeat(200)));
    mkdirSync(join(copy, "many"));
    for (let i = 0; i < 3000; i += 1) writeFileSync(join(copy, "many", `f${i}.md`), "");
    const found = (await search("files", "*.md")) as { ok: string };
    expect(found.ok.split("\n").filter(Boolean).length).toBe(3000);
    expect(found.ok).not.toContain(join(base, "data"));
    // And rg's events, each of which names its file: 250 files with a line each.
    mkdirSync(join(copy, "some"));
    for (let i = 0; i < 250; i += 1) writeFileSync(join(copy, "some", `f${i}.md`), "needle\n");
    const events = (await search("json", "needle", join(at, "some"))) as { ok: string };
    expect(events.ok.split("\n").filter((line) => line.startsWith('{"type":"match"')).length).toBe(250);
    expect(events.ok).not.toContain(join(base, "data"));
    // And the other way: 1,500 files under the cap by the copy's path, over it by the folder's.
    lay(join("home", "r".repeat(200), "Reports"), "d");
    mkdirSync(join(copy, "many"));
    for (let i = 0; i < 1500; i += 1) writeFileSync(join(copy, "many", `f${i}.md`), "");
    expect(await search("files", "*.md")).toEqual({
      error: { type: "ripgrep", message: "search output over 262144 characters; narrow the pattern, path or glob" },
    });
  });
});

describe("every kind the file helper has, on a thread's copy", () => {
  // How each kind is asked about the copy's files, by the folder's path: so that it works, and in each way its answer can
  // come to hold a path. What it answers, and the path its answer names. A kind the helper gains is asked here before
  // a thread asks it: its answers are looked at for the copy's path as these are.
  type Ask = [args: Record<string, unknown>, answered: string, names?: string];
  const asked = (): Record<string, Ask[]> => ({
    resolve: [
      [{ path: "sub/b.txt" }, "ok", join(at, "sub", "b.txt")], [{ path: "../beside.txt" }, "sandbox", join(dirname(at), "beside.txt")],
      [{ path: "round/x" }, "os", join(at, "round", "x")], [{ path: "out/hosts" }, "sandbox", "/etc/hosts"],
    ],
    check_write: [[{ path: join(at, "sub", "b.txt") }, "ok"], [{ path: join(at, ".git", "config") }, "ok", join(at, ".git", "config")]],
    stat: [[{ key: join(at, "a.txt") }, "ok"], [{ key: join(at, "missing.txt") }, "ok"]],
    read: [
      [{ key: join(at, "a.txt"), max_bytes: null }, "ok"], [{ key: join(at, "missing.txt"), max_bytes: null }, "os", join(at, "missing.txt")],
      [{ key: join(at, "sub"), max_bytes: null }, "os", join(at, "sub")], [{ key: join(at, "pipe"), max_bytes: null }, "os", join(at, "pipe")],
      [{ key: join(at, "out", "hosts"), max_bytes: null }, "sandbox", join(at, "out", "hosts")],
      [{ key: join(at, "shut", "x.txt"), max_bytes: null }, "os", join(at, "shut", "x.txt")],
    ],
    read_lines: [
      [{ key: join(at, "a.txt"), ...lines }, "ok"], [{ key: join(at, "missing.txt"), ...lines }, "os", join(at, "missing.txt")],
      [{ key: join(at, "sub"), ...lines }, "os", join(at, "sub")],
    ],
    write: [
      [{ key: join(at, "new", "c.txt"), data: data("made by the thread\n") }, "ok"],
      [{ key: join(at, "a.txt"), data: data("x"), expected_revision: "1:2:3:4:5" }, "conflict", join(at, "a.txt")],
      [{ key: join(at, ".git", "config"), data: data("x") }, "sandbox", join(at, ".git", "config")],
      [{ key: join(at, "a.txt", "under", "c.txt"), data: data("x") }, "os", join(at, "a.txt")],
      [{ key: join(at, "twice.txt"), data: data("x") }, "os", join(at, "twice.txt")], [{ key: join(at, "sub"), data: data("x") }, "os", join(at, "sub")],
      [{ key: join(at, "shut", "c.txt"), data: data("x") }, "os", join(at, "shut", "c.txt")],
      [{ key: join(at, "a.txt"), data: data("x"), create: true }, "os", join(at, "a.txt")],
    ],
    delete: [
      [{ key: join(at, "gone.txt") }, "os", join(at, "gone.txt")], [{ key: at }, "os", at],
      [{ key: join(at, ".git", "config") }, "sandbox", join(at, ".git", "config")], [{ key: join(at, "spare.txt") }, "ok"],
    ],
    list_dir: [[{ key: at }, "ok"], [{ key: join(at, "missing") }, "os", join(at, "missing")], [{ key: join(at, "shut") }, "os", join(at, "shut")]],
    walk: [[{ key: at, ...whole }, "ok"], [{ key: join(at, "a.txt"), ...whole }, "os", join(at, "a.txt")], [{ key: join(at, "shut"), ...whole }, "os", join(at, "shut")]],
    ripgrep: [
      [{ key: join(at, "sub"), mode: "files", pattern: "*.txt", glob: null, context: 0 }, "ok", join(at, "sub", "b.txt")],
      [{ key: join(at, "sub"), mode: "count", pattern: "beta", glob: null, context: 0 }, "ok", join(at, "sub", "b.txt")],
      [{ key: join(at, "sub"), mode: "json", pattern: "beta", glob: "*.txt", context: 1 }, "ok", join(at, "sub", "b.txt")],
      [{ key: at, mode: "json", pattern: "(", glob: null, context: 0 }, "ripgrep"],
      [{ key: at, mode: "json", pattern: "beta", glob: null, context: 0 }, "ripgrep", join(at, "shut")],
    ],
    // A copy's helper is no landing's: it has no copy to land from, and no folder to land in.
    land: [[{ action: "revisions", paths: ["a.txt"] }, "unsupported"], [{ action: "recover" }, "unsupported"]],
  });

  it("is asked by the folder's path, works in the copy, and names no file by the copy's path in anything it answers", async () => {
    symlinkSync("round", join(copy, "round"));
    symlinkSync("/etc", join(copy, "out"));
    expect(spawnSync("mkfifo", [join(copy, "pipe")]).status).toBe(0);
    writeFileSync(join(copy, "twice.txt"), "one file, two names\n");
    linkSync(join(copy, "twice.txt"), join(copy, "again.txt"));
    writeFileSync(join(copy, "spare.txt"), "to be deleted\n");
    mkdirSync(join(copy, "shut"));
    chmodSync(join(copy, "shut"), 0);
    const asks = asked();
    // The helper's own table: every kind in it is asked, and none is asked that it lacks.
    expect(Object.keys(asks).sort(), "a kind the helper has is not asked about a copy here, or one that is asked is gone").toEqual([...kinds()].sort());
    try {
      for (const kind of kinds()) {
        const worked: string[] = [];
        for (const [args, answered, names] of asks[kind] ?? []) {
          const answer = await run(kind, args);
          const told = JSON.stringify(answer);
          const said = `${kind} ${JSON.stringify(args)} answered ${told}`;
          worked.push("ok" in answer ? "ok" : answer.error.type);
          expect(worked.at(-1), said).toBe(answered);
          // Nothing of the app's data, where the copy lies, is in the answer; and the file it is about is the folder's.
          expect(told, said).not.toContain(join(base, "data"));
          if (names !== undefined) expect(told, said).toContain(names);
        }
        if (kind !== "land") expect(worked, `${kind} never worked on the copy`).toContain("ok");
      }
    } finally {
      chmodSync(join(copy, "shut"), 0o700);
    }
    expect(readFileSync(join(copy, "new", "c.txt"), "utf8")).toBe("made by the thread\n");
    expect(existsSync(join(copy, "spare.txt"))).toBe(false);
    expect(readFileSync(join(copy, "a.txt"), "utf8")).toBe("the copy's alpha\n");
  });
});

// The helper as the app runs it, a process of its own, asked *requests* once it is ready and ended when it has answered
// them all. *loaded*: a module it runs before its own code.
async function helper(requests: Array<[kind: string, args: Record<string, unknown>]>, env: Record<string, string>, loaded?: string): Promise<Answer[]> {
  const child = spawn(process.execPath, [...(loaded ? ["--import", loaded] : []), HELPER], {
    env: { SUROGATE_FOLDER: copy, SUROGATE_AT: at, HOME: join(base, "home"), PATH: "/usr/bin:/bin", ...env },
    stdio: ["pipe", "pipe", "inherit"],
  });
  child.stdin.on("error", () => {});
  const answers = new Map<number, Answer>();
  const ended = new Promise<void>((resolve) => child.once("exit", () => resolve()));
  createInterface({ input: child.stdout }).on("line", (line) => {
    const said = JSON.parse(line) as { ready?: boolean; id?: string; outcome?: Answer };
    if (said.ready) for (const [id, [kind, args]] of requests.entries()) child.stdin.write(`${JSON.stringify({ id: String(id), kind, args })}\n`);
    else if (said.id !== undefined && said.outcome) answers.set(Number(said.id), said.outcome);
    if (answers.size === requests.length) child.stdin.end();
  });
  const bound = setTimeout(() => child.kill("SIGKILL"), 20_000);
  await ended;
  clearTimeout(bound);
  return requests.map((_, id) => answers.get(id) ?? { error: { type: "unanswered", message: "the helper ended before it answered" } });
}

describe("what a helper on a thread's copy looks at", () => {
  // Loaded into a helper before its own code: every call of an fs function, and every program it starts, that names
  // a path at or under EDGE_TAP_ROOT is written down in EDGE_TAP_OUT, with the function's name.
  const TAP = `data:text/javascript,${encodeURIComponent(`
    import child from "node:child_process";
    import fs from "node:fs";
    import { syncBuiltinESMExports } from "node:module";
    const { EDGE_TAP_ROOT: root, EDGE_TAP_OUT: out } = process.env;
    const note = fs.appendFileSync;
    const named = (value) => (typeof value === "string" ? value : Buffer.isBuffer(value) ? value.toString("latin1") : value instanceof URL ? value.pathname : null);
    const under = (value) => { const path = named(value); return path !== null && (path === root || path.startsWith(root + "/")); };
    for (const [name, real] of Object.entries(fs)) {
      if (typeof real !== "function" || /^[A-Z]/.test(name)) continue;
      const tapped = function (...args) {
        if (args.some(under)) note(out, name + " " + args.filter(under).map(named).join(" ") + "\\n");
        return real.apply(this, args);
      };
      fs[name] = Object.defineProperties(tapped, Object.getOwnPropertyDescriptors(real));
    }
    const { spawn } = child;
    child.spawn = (file, args = [], ...rest) => {
      if ([file, ...args].some(under)) note(out, "spawn " + [file, ...args].filter(under).join(" ") + "\\n");
      return spawn(file, args, ...rest);
    };
    syncBuiltinESMExports();
  `)}`;

  it("makes no call on the folder itself, nor on anything beside the copy, whatever it is asked", async () => {
    symlinkSync(join(at, "sub"), join(copy, "linked"));
    symlinkSync("../Reports/sub", join(copy, "around"));
    symlinkSync(`../${OTHER}`, join(copy, "beside"));
    symlinkSync(`${dirname(at)}/reports/sub`, join(copy, "another-case"));
    symlinkSync("round", join(copy, "round"));
    mkdirSync(join(place, "threads", OTHER));
    const up = "../".repeat(at.split("/").length + 2);
    const requests: Array<[string, Record<string, unknown>]> = [
      ["resolve", { path: "a.txt" }], ["resolve", { path: `${at}/../Reports/sub/b.txt` }], ["resolve", { path: `${up}${at.slice(1)}/a.txt` }],
      ["resolve", { path: "linked/b.txt" }], ["resolve", { path: "around/b.txt" }], ["resolve", { path: "beside/a.txt" }],
      ["resolve", { path: "another-case/b.txt" }], ["resolve", { path: `${dirname(at)}/reports/a.txt` }], ["resolve", { path: "round/x" }],
      ["resolve", { path: join(copy, "a.txt") }], ["resolve", { path: "~/Reports/sub" }], ["resolve", { path: "../Reports-old/a.txt" }],
      ["check_write", { path: join(at, ".git", "config") }], ["check_write", { path: "linked/../.vscode/x" }], ["check_write", { path: "~/.bashrc" }],
      ["stat", { key: join(at, "a.txt") }], ["stat", { key: join(at, "linked", "b.txt") }],
      ["read", { key: join(at, "a.txt"), max_bytes: null }], ["read", { key: join(at, "linked", "b.txt"), max_bytes: null }],
      ["read", { key: join(at, "another-case", "b.txt"), max_bytes: null }], ["read_lines", { key: join(at, "sub", "b.txt"), ...lines }],
      ["write", { key: join(at, "new", "c.txt"), data: data("made by the thread\n") }], ["write", { key: join(at, "linked", "c.txt"), data: data("x") }],
      ["delete", { key: join(at, "new", "c.txt") }], ["delete", { key: join(at, "around", "b.txt") }], ["list_dir", { key: at }],
      ["walk", { key: at, ...whole }], ["ripgrep", { key: at, mode: "files", pattern: "*.txt", glob: null, context: 0 }],
      ["ripgrep", { key: join(at, "sub"), mode: "json", pattern: "beta", glob: null, context: 0 }],
      ["land", { action: "revisions", paths: ["a.txt"] }],
    ];
    // The watch's own notes lie beside them: the folder and the place are what is looked at.
    const before = outside(join(base, "home"), place);
    const tapped = async (root: string): Promise<string> => {
      const out = join(base, `tap-${root === at ? "folder" : root === copy ? "copy" : "beside"}.log`);
      writeFileSync(out, "");
      await helper(requests, { EDGE_TAP_ROOT: root, EDGE_TAP_OUT: out }, TAP);
      const noted = readFileSync(out, "utf8");
      rmSync(out);
      return noted;
    };
    // Not the folder; not what lies beside it; not the folder that holds the copies, nor the copy beside this one.
    expect(await tapped(at)).toBe("");
    expect(await tapped(dirname(at))).toBe("");
    expect(await tapped(join(place, "threads", OTHER))).toBe("");
    // And the watch does see what the helper does: it works in the copy.
    const worked = await tapped(copy);
    for (const call of ["lstatSync", "openSync", "readdirSync", "renameSync", "unlinkSync", "spawn"]) expect(worked).toContain(`${call} ${copy}`);
    expect(outside(join(base, "home"), place)).toEqual(before);
  });
});

// A filesystem that takes "Reports" and "reports", and two spellings of one "é", for one name, as a Mac's or a Windows
// disk does: tmpfs can, mounted in a user namespace of the test's own (land.test.ts). *run* is a shell line given the
// mount's path as $1.
function folding(mount: string, run: string, ...more: string[]) {
  return spawnSync("bwrap", [
    "--unshare-user", "--uid", "0", "--gid", "0", "--cap-add", "ALL", "--bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc", "--",
    "sh", "-c", `mount -t tmpfs -o casefold none "$1" && mkdir "$1/disk" && chattr +F "$1/disk" && ${run}`, "sh", mount, ...more,
  ], { encoding: "utf8", timeout: 30_000 });
}
const probe = mkdtempSync(join(tmpdir(), "edge-folding-"));
const folds = folding(probe, 'touch "$1/disk/A" && test -e "$1/disk/a"').status === 0;
rmSync(probe, { recursive: true, force: true });

describe("a folder and a copy on a disk that tells names apart less than their bytes do", () => {
  it.skipIf(!folds)("takes the folder's path by its own bytes alone: another case or composition of it names nothing, and reaches neither the folder nor the copy", () => {
    const mount = join(base, "mount");
    mkdirSync(mount);
    // The helper's kinds run where the mount is, and say what they were answered and what the folder then holds.
    const script = `
      import { createHash } from "node:crypto";
      import { lstatSync, mkdirSync, readdirSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
      import { join } from "node:path";
      const { perform } = await import(${JSON.stringify(dist("files/operations.js"))});
      const disk = join(process.argv[1], "disk");
      const [composed, decomposed] = ["Caf\\u00e9", "Cafe\\u0301"];
      const at = join(disk, "Home", composed, "Reports");
      const copy = join(disk, "Data", "history", "${KEY}", "threads", "${THREAD}");
      for (const [root, who] of [[at, "the folder's"], [copy, "the copy's"]]) {
        mkdirSync(join(root, "sub"), { recursive: true });
        writeFileSync(join(root, "a.txt"), who + " alpha\\n");
        writeFileSync(join(root, "sub", "b.txt"), who + " beta\\n");
      }
      const folder = () => readdirSync(at, { recursive: true }).sort().map((name) => {
        const st = lstatSync(join(at, name), { bigint: true });
        return [name, st.mode, st.ino, st.size, st.mtimeNs, st.ctimeNs, st.isFile() ? createHash("sha256").update(readFileSync(join(at, name))).digest("hex") : ""].join(" ");
      });
      const context = { folder: copy, at, home: join(disk, "Home"), env: { PATH: "/usr/bin:/bin" } };
      const was = folder();
      const out = { at, copy, untouched: true, answers: {} };
      const run = async (name, kind, args) => {
        out.answers[name] = await perform(kind, args, context, new AbortController().signal);
        if (JSON.stringify(folder()) !== JSON.stringify(was)) out.untouched = false;
      };
      const data = Buffer.from("written by the thread\\n").toString("base64");
      const lower = join(disk, "home", composed.toLowerCase(), "reports");
      const other = join(disk, "Home", decomposed, "Reports");
      const owned = join(disk, "data", "HISTORY", "${KEY}", "Threads", "${THREAD}");
      symlinkSync(join(lower, "sub"), join(copy, "another-case"));
      symlinkSync(join(owned, "sub"), join(copy, "its-own"));
      out.one = [lower, other, owned].map((path) => readFileSync(join(path, "a.txt"), "utf8"));
      for (const [name, path] of [["lower", lower], ["other", other], ["owned", owned]]) {
        await run(name + " resolve", "resolve", { path: join(path, "a.txt") });
        await run(name + " read", "read", { key: join(path, "a.txt"), max_bytes: null });
        await run(name + " write", "write", { key: join(path, "a.txt"), data });
        await run(name + " delete", "delete", { key: join(path, "sub", "b.txt") });
        await run(name + " list", "list_dir", { key: path });
        await run(name + " search", "ripgrep", { key: path, mode: "files", pattern: "*", glob: null, context: 0 });
      }
      await run("linked resolve", "resolve", { path: "another-case/b.txt" });
      await run("linked read", "read", { key: join(at, "another-case", "b.txt"), max_bytes: null });
      await run("own link resolve", "resolve", { path: "its-own/b.txt" });
      // In the copy, a file by another case of its name is the copy's one file.
      await run("file read", "read", { key: join(at, "A.TXT"), max_bytes: null });
      await run("file write", "write", { key: join(at, "A.TXT"), data });
      out.holds = readdirSync(copy).sort();
      out.copied = readFileSync(join(copy, "a.txt"), "utf8");
      console.log(JSON.stringify(out));
    `;
    const ran = folding(mount, '"$2" --input-type=module -e "$3" "$1"', process.execPath, script);
    expect(ran.stderr).toBe("");
    const out = JSON.parse(ran.stdout) as { at: string; copy: string; untouched: boolean; one: string[]; answers: Record<string, Answer>; holds: string[]; copied: string };
    const disk = join(mount, "disk");
    // The folder's name in lower case, and with its "é" as two characters; the copy's in another case.
    const [lower, other, owned] = [join(disk, "home", "café", "reports"), join(disk, "Home", "Café", "Reports"), join(disk, "data", "HISTORY", KEY, "Threads", THREAD)];
    // On this disk each of the three is a second name: two of the folder itself, one of the copy.
    expect(out.one).toEqual(["the folder's alpha\n", "the folder's alpha\n", "the copy's alpha\n"]);
    const outsideOf = (asked: string, leads: string) => ({ error: { type: "sandbox", message: `Path traversal blocked: '${asked}' resolves to '${leads}' which is outside the workspace '${out.at}'.` } });
    for (const [name, path] of [["lower", lower], ["other", other], ["owned", owned]] as const) {
      expect(out.answers[`${name} resolve`], name).toEqual(outsideOf(join(path, "a.txt"), join(path, "a.txt")));
      for (const [kind, key] of [["read", join(path, "a.txt")], ["write", join(path, "a.txt")], ["delete", join(path, "sub", "b.txt")], ["list", path], ["search", path]] as const) {
        expect(out.answers[`${name} ${kind}`], `${name} ${kind}`).toEqual(noPath(key));
      }
    }
    expect(out.answers["linked resolve"]).toEqual(outsideOf("another-case/b.txt", join(lower, "sub", "b.txt")));
    expect(out.answers["linked read"]).toEqual(noPath(join(out.at, "another-case", "b.txt")));
    expect(out.answers["own link resolve"]).toEqual(outsideOf("its-own/b.txt", join(owned, "sub", "b.txt")));
    expect(out.answers["file read"]).toEqual({ ok: data("the copy's alpha\n") });
    expect(out.answers["file write"]).toEqual({ ok: null });
    expect(out.holds.map((name) => name.toLowerCase())).toEqual(["a.txt", "another-case", "its-own", "sub"]);
    expect(out.copied).toBe("written by the thread\n");
    // The folder itself, through every one of its names, is as it was; and no answer holds the copy's path.
    expect(out.untouched).toBe(true);
    expect(JSON.stringify(out.answers)).not.toContain(out.copy);
  });
});

describe("a helper on a thread's copy, in the file helper's sandbox", () => {
  const sandboxed = fileToolsMissing(undefined, process.env.PATH ?? "").length === 0;

  it.skipIf(!sandboxed)("reaches the copy, and neither the folder it is named by, the copy beside it nor the folder's place", { timeout: 60_000 }, async () => {
    const other = join(place, "threads", OTHER);
    mkdirSync(join(other, "sub"), { recursive: true });
    writeFileSync(join(other, "a.txt"), "another thread's alpha\n");
    mkdirSync(join(place, "history.git"));
    symlinkSync(join(at, "sub"), join(copy, "linked"));
    symlinkSync(join(other, "a.txt"), join(copy, "to-other"));
    const work = join(base, "work");
    // srt's own files go where its host points them, as the app's host does: never in the copy.
    for (const dir of [work, join(base, "srt")]) mkdirSync(dir);
    // What a program reaches in that sandbox by itself, whatever the helper would answer: each path read, listed and written.
    const reach = `
      import { readdirSync, readFileSync, writeFileSync } from "node:fs";
      const tried = (call) => { try { return { ok: call() ?? null }; } catch (error) { return { error: error.code }; } };
      console.log(JSON.stringify(Object.fromEntries(process.argv.slice(1).map((path) => [path, {
        read: tried(() => readFileSync(path + "/a.txt", "utf8")), listed: tried(() => readdirSync(path).sort()),
        written: tried(() => writeFileSync(path + "/planted.txt", "planted")),
      }]))));
    `;
    // A host of the test's own: the file helper's policy for the copy, as the app's host makes it for a folder, around
    // that program and then around the helper itself, which is given the folder's path and asked over this process's pipes.
    const host = `
      import { spawn, spawnSync } from "node:child_process";
      import { SandboxManager } from "@anthropic-ai/sandbox-runtime";
      const { hideSrtTmp, pathOutside, quote, sandboxPolicy } = await import(${JSON.stringify(dist("hosts/policy.js"))});
      const [copy, at, work, node, home, helper, reach, ...reached] = process.argv.slice(1);
      const path = pathOutside(process.env.PATH, []);
      await SandboxManager.initialize(sandboxPolicy({ folder: copy, tmp: work, appDirs: ${JSON.stringify([...["../dist", "../node_modules"].map((dir) => fileURLToPath(new URL(dir, import.meta.url))), dirname(dirname(process.execPath))])} }), () => Promise.resolve(false));
      process.chdir(work);
      const wrapped = async (words) => {
        const [file, flag, line] = (await SandboxManager.wrapWithSandboxArgv(words.map(quote).join(" "))).argv;
        return [file, ["--norc", "--noprofile", flag, hideSrtTmp(line)]];
      };
      const [shell, looks] = await wrapped([node, "--input-type=module", "-e", reach, ...reached]);
      const looked = spawnSync(shell, looks, { cwd: copy, env: { HOME: home, PATH: path }, encoding: "utf8", timeout: 30000 });
      process.stderr.write(looked.stderr);
      process.stdout.write(looked.stdout);
      const [file, args] = await wrapped([node, "--disable-sigusr1", helper]);
      const child = spawn(file, args, { cwd: copy, env: { HOME: home, LANG: "C.UTF-8", PATH: path, SUROGATE_FOLDER: copy, SUROGATE_AT: at }, stdio: "inherit" });
      const status = await new Promise((resolve) => child.once("exit", resolve));
      await SandboxManager.reset().catch(() => {});
      process.exit(status ?? 1);
    `;
    const requests: Array<[string, Record<string, unknown>]> = [
      ["resolve", { path: "a.txt" }], ["read", { key: join(at, "a.txt"), max_bytes: null }],
      ["write", { key: join(at, "new", "c.txt"), data: data("made by the thread\n") }], ["list_dir", { key: at }],
      ["ripgrep", { key: at, mode: "files", pattern: "*.txt", glob: null, context: 0 }],
      ["resolve", { path: "linked/b.txt" }], ["read", { key: join(at, "sub", "b.txt"), max_bytes: null }],
      ["read", { key: join(copy, "a.txt"), max_bytes: null }], ["resolve", { path: "to-other" }], ["read", { key: join(at, "to-other"), max_bytes: null }],
      ["read", { key: join(at, "missing.txt"), max_bytes: null }], ["land", { action: "revisions", paths: ["a.txt"] }],
    ];
    // srt's own files lie beside them: the folder and the place are what is looked at.
    const before = outside(join(base, "home"), place);
    // In a group of its own, as the app's hosts are: what srt started goes with it.
    const child = spawn(process.execPath, ["--input-type=module", "-e", host, copy, at, work, process.execPath, join(base, "home"), HELPER, reach, copy, at, dirname(at), other, dirname(copy), place], {
      cwd: fileURLToPath(new URL("..", import.meta.url)), detached: true, stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, TMPDIR: join(base, "srt") },
    });
    child.stdin.on("error", () => {});
    let failed = "";
    child.stderr.on("data", (chunk: Buffer) => { failed += chunk.toString(); });
    let reached: Record<string, { read: { ok?: string; error?: string }; listed: { ok?: string[]; error?: string }; written: { ok?: null; error?: string } }> | undefined;
    const answers = new Map<number, Answer>();
    createInterface({ input: child.stdout }).on("line", (line) => {
      const said = JSON.parse(line) as { ready?: boolean; id?: string; outcome?: Answer };
      if (!reached) reached = said as unknown as typeof reached;
      else if (said.ready) for (const [id, [kind, args]] of requests.entries()) child.stdin.write(`${JSON.stringify({ id: String(id), kind, args })}\n`);
      else if (said.id !== undefined && said.outcome) answers.set(Number(said.id), said.outcome);
      if (answers.size === requests.length) child.stdin.end();
    });
    const bound = setTimeout(() => child.kill("SIGKILL"), 45_000);
    const status = await new Promise<number | null>((resolve) => child.once("exit", resolve));
    clearTimeout(bound);
    try {
      process.kill(-child.pid!, "SIGKILL");
    } catch {
      // Nothing of it was left.
    }
    expect([status, failed]).toEqual([0, ""]);
    // What the sandbox lets a program reach: the copy to read and to write. The folder it is named by, what lies beside
    // that folder and the copy beside this one are not there at all.
    expect(reached?.[copy]).toEqual({ read: { ok: "the copy's alpha\n" }, listed: { ok: expect.arrayContaining(["a.txt", "sub"]) }, written: { ok: null } });
    const absent = { read: { error: "ENOENT" }, listed: { error: "ENOENT" }, written: { error: "ENOENT" } };
    for (const path of [at, dirname(at), other]) expect(reached?.[path], path).toEqual(absent);
    // The folders the copy lies in are the sandbox's own there: empty but for the way to the copy, and what a program
    // writes in one never reaches this computer's.
    expect(reached?.[dirname(copy)]).toEqual({ read: { error: "ENOENT" }, listed: { ok: [THREAD] }, written: { ok: null } });
    expect(reached?.[place]).toEqual({ read: { error: "ENOENT" }, listed: { ok: ["threads"] }, written: { ok: null } });
    expect(readdirSync(dirname(copy)).sort()).toEqual([THREAD, OTHER].sort());
    expect(readdirSync(place).sort()).toEqual(["history.git", "threads"]);
    // And the helper there is asked and answers by the folder's path.
    expect(requests.map((_, id) => answers.get(id))).toEqual([
      { ok: join(at, "a.txt") }, { ok: data("the copy's alpha\n") }, { ok: null }, { ok: expect.arrayContaining(["a.txt", "new", "sub"]) },
      { ok: expect.stringContaining(`${join(at, "new", "c.txt")}\n`) }, { ok: join(at, "sub", "b.txt") }, { ok: data("the copy's beta\n") },
      noPath(join(copy, "a.txt")), leaves("to-other", join(other, "a.txt")), noPath(join(at, "to-other")),
      { error: { type: "os", code: "ENOENT", message: `No such file or directory: '${join(at, "missing.txt")}'` } },
      { error: { type: "unsupported", message: "This computer cannot do 'land' yet" } },
    ]);
    expect((answers.get(4) as { ok: string }).ok).not.toContain(copy);
    expect(readFileSync(join(copy, "new", "c.txt"), "utf8")).toBe("made by the thread\n");
    rmSync(join(copy, "planted.txt"));
    expect(outside(join(base, "home"), place)).toEqual(before);
  });
});
