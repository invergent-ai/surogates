import { execFileSync } from "node:child_process";
import {
  chmodSync, linkSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { monitorEventLoopDelay } from "node:perf_hooks";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { scanLinks } from "../src/binding/links.js";

// The calls are real unless a test holds one lstat or hands out large inode numbers.
const calls = vi.hoisted(() => ({
  // An lstat of this path waits for release(), as one on a hung mount never returns.
  hung: "",
  release: () => {},
  // Inode numbers past 2^53 a large filesystem hands out, by path.
  inodes: new Map<string, bigint>(),
}));

vi.mock("node:fs/promises", async (original) => {
  const fs = await original<typeof import("node:fs/promises")>();
  return {
    ...fs,
    lstat: async (...args: Parameters<typeof fs.lstat>) => {
      const path = String(args[0]);
      if (path === calls.hung) await new Promise<void>((resolve) => { calls.release = resolve; });
      const stats = await fs.lstat(...args);
      const ino = calls.inodes.get(path);
      // As Node reads it: whole as a bigint, rounded to the nearest double as a number.
      if (ino !== undefined) Object.assign(stats, { ino: typeof stats.ino === "bigint" ? ino : Number(ino) });
      return stats;
    },
  };
});

let base: string;
let folder: string;
let outside: string;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "links-")));
  folder = join(base, "folder");
  outside = join(base, "outside");
  mkdirSync(folder);
  mkdirSync(outside);
});

afterEach(() => {
  calls.release();
  calls.hung = "";
  calls.inodes.clear();
  rmSync(base, { recursive: true, force: true });
});

// A file outside the folder, and a hard link to it at *path* in the folder.
function linkedIn(path: string): void {
  const source = join(outside, path.replaceAll("/", "_"));
  writeFileSync(source, "x");
  mkdirSync(join(folder, path, ".."), { recursive: true });
  linkSync(source, join(folder, path));
}

describe("the files in a folder linked from elsewhere", () => {
  it("are none in a folder of plain files", async () => {
    writeFileSync(join(folder, "a.txt"), "a");
    mkdirSync(join(folder, "sub"));
    writeFileSync(join(folder, "sub", "b.txt"), "b");
    expect(await scanLinks(folder)).toBeNull();
  });

  it("are counted at any depth, with a few of them by name", async () => {
    for (const path of ["d.txt", "a/b/c.txt", "e.txt", "b.txt"]) linkedIn(path);
    expect(await scanLinks(folder)).toEqual({ count: 4, examples: ["a/b/c.txt", "b.txt", "d.txt"], complete: true });
  });

  it("leave out two links that are both in the folder, and count both of a pair that also has a link outside", async () => {
    writeFileSync(join(folder, "a.txt"), "a");
    linkSync(join(folder, "a.txt"), join(folder, "a-again.txt"));
    linkedIn("b.txt");
    linkSync(join(folder, "b.txt"), join(folder, "b-again.txt"));
    expect(await scanLinks(folder)).toEqual({ count: 2, examples: ["b-again.txt", "b.txt"], complete: true });
  });

  it("leave out node_modules, and what a symbolic link leads to", async () => {
    linkedIn("node_modules/pkg/index.js");
    linkedIn("deep/node_modules/pkg/index.js");
    mkdirSync(join(outside, "tree"));
    writeFileSync(join(outside, "tree", "t.txt"), "t");
    linkSync(join(outside, "tree", "t.txt"), join(outside, "t-again.txt"));
    symlinkSync(join(outside, "tree"), join(folder, "tree"));
    symlinkSync(join(outside, "t-again.txt"), join(folder, "t.txt"));
    expect(await scanLinks(folder)).toBeNull();
  });

  it("leave out a local clone's object store, whose files are hard links into the repository it came from", async () => {
    const git = (...args: string[]) =>
      execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@t", ...args], { stdio: "ignore" });
    git("init", "-q", join(outside, "repo"));
    writeFileSync(join(outside, "repo", "a.txt"), "a");
    git("-C", join(outside, "repo"), "add", "a.txt");
    git("-C", join(outside, "repo"), "commit", "-qm", "a");
    git("clone", "-q", join(outside, "repo"), join(folder, "clone"));
    // The clone's objects are links into the repository outside the folder.
    const objects = join(folder, "clone", ".git", "objects");
    const shared = readdirSync(objects, { recursive: true, encoding: "utf8" })
      .filter((name) => statSync(join(objects, name)).isFile() && statSync(join(objects, name)).nlink > 1);
    expect(shared.length).toBeGreaterThan(0);
    expect(await scanLinks(folder)).toBeNull();
  });

  it("look into a folder named objects that is no git folder's object store", async () => {
    mkdirSync(join(folder, "a", ".git"), { recursive: true });
    writeFileSync(join(folder, "a", ".git", "HEAD"), "ref: refs/heads/main\n");
    linkedIn("a/.git/objects/HEAD");
    linkedIn("b/objects/x");
    expect(await scanLinks(folder)).toEqual({ count: 2, examples: ["a/.git/objects/HEAD", "b/objects/x"], complete: true });
  });

  it("look into the objects folder beside a HEAD outside any .git, and in a folder under .git that holds no HEAD", async () => {
    mkdirSync(join(folder, "proj"));
    writeFileSync(join(folder, "proj", "HEAD"), "ref: refs/heads/main\n");
    linkedIn("proj/objects/x");
    mkdirSync(join(folder, ".git", "sub"), { recursive: true });
    writeFileSync(join(folder, ".git", "HEAD"), "ref: refs/heads/main\n");
    linkedIn(".git/sub/objects/x");
    expect(await scanLinks(folder)).toEqual({ count: 2, examples: [".git/sub/objects/x", "proj/objects/x"], complete: true });
  });

  it("know git's folders by the names git writes, and node_modules in any case", async () => {
    mkdirSync(join(folder, "a", ".GIT"), { recursive: true });
    writeFileSync(join(folder, "a", ".GIT", "HEAD"), "ref: refs/heads/main\n");
    linkedIn("a/.GIT/objects/x");
    mkdirSync(join(folder, "b", ".git"), { recursive: true });
    writeFileSync(join(folder, "b", ".git", "HEAD"), "ref: refs/heads/main\n");
    linkedIn("b/.git/OBJECTS/x");
    linkedIn("Node_Modules/x");
    expect(await scanLinks(folder)).toEqual({ count: 2, examples: ["a/.GIT/objects/x", "b/.git/OBJECTS/x"], complete: true });
  });

  it("tell apart two files whose inode numbers are past 2^53", async () => {
    linkedIn("a.txt");
    linkedIn("b.txt");
    // Neighbours a JavaScript number cannot hold apart.
    calls.inodes.set(join(folder, "a.txt"), 2n ** 53n);
    calls.inodes.set(join(folder, "b.txt"), 2n ** 53n + 1n);
    expect(await scanLinks(folder)).toEqual({ count: 2, examples: ["a.txt", "b.txt"], complete: true });
  });

  it("do not hold up the process while they look through many files", async () => {
    for (let i = 0; i < 50_000; i += 1) writeFileSync(join(folder, `f${i}`), "");
    const delay = monitorEventLoopDelay({ resolution: 10 });
    delay.enable();
    expect(await scanLinks(folder)).toBeNull();
    delay.disable();
    expect(delay.max / 1e6).toBeLessThan(100);
  });

  it("stop at their deadline when a look at a file never returns, with what they found", async () => {
    linkedIn("a.txt");
    linkedIn("sub/slow.txt");
    calls.hung = join(folder, "sub", "slow.txt");
    const started = performance.now();
    expect(await scanLinks(folder, { deadlineMs: 200 })).toEqual({ count: 1, examples: ["a.txt"], complete: false });
    expect(performance.now() - started).toBeLessThan(1_000);
  });

  it("say when the look stopped early, at its cap or its deadline", async () => {
    for (let i = 0; i < 20; i += 1) writeFileSync(join(folder, `f${i}.txt`), "");
    linkedIn("z.txt");
    expect(await scanLinks(folder, { maxEntries: 5 })).toEqual({ count: 0, examples: [], complete: false });
    expect(await scanLinks(folder, { deadlineMs: -1 })).toEqual({ count: 0, examples: [], complete: false });
  });

  it("say when a file in it could not be looked at, such as one whose name is not valid UTF-8", async () => {
    writeFileSync(join(outside, "o.txt"), "o");
    linkSync(join(outside, "o.txt"), Buffer.from([...Buffer.from(`${folder}/bad-`), 0xff]));
    expect(await scanLinks(folder)).toEqual({ count: 0, examples: [], complete: false });
  });

  it("say when they missed a name that is not valid UTF-8, beside a valid name it reads back as", async () => {
    writeFileSync(Buffer.from([...Buffer.from(`${folder}/a`), 0xff]), "");
    linkedIn("a\uFFFD");
    expect(await scanLinks(folder)).toEqual({ count: 0, examples: [], complete: false });
  });

  it("say when a folder in it could not be read", async () => {
    linkedIn("open.txt");
    mkdirSync(join(folder, "locked"));
    chmodSync(join(folder, "locked"), 0o000);
    try {
      expect(await scanLinks(folder)).toEqual({ count: 1, examples: ["open.txt"], complete: false });
    } finally {
      chmodSync(join(folder, "locked"), 0o700);
    }
  });
});
