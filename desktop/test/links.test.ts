import { execFileSync } from "node:child_process";
import syncFs, {
  chmodSync, type Dirent, linkSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, rmSync, statSync, symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { monitorEventLoopDelay } from "node:perf_hooks";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { scanLinks } from "../src/binding/links.js";

// The calls are real unless a test holds one lstat, hands out large inode numbers, or lists folders without types.
const calls = vi.hoisted(() => ({
  // An lstat of this path waits for release(), as one on a hung mount never returns.
  hung: "",
  release: () => {},
  // Inode numbers past 2^53 a large filesystem hands out, by path.
  inodes: new Map<string, bigint>(),
  // Folder listings give no entry's type, as on XFS without ftype, some FUSE filesystems and NFSv3.
  untyped: false,
  // The paths lstat was asked for.
  looked: [] as string[],
  // Requests in flight, and the most there were at once.
  inFlight: 0,
  most: 0,
}));

vi.mock("node:fs/promises", async (original) => {
  const fs = await original<typeof import("node:fs/promises")>();
  const counted = async <T>(call: () => Promise<T>): Promise<T> => {
    calls.inFlight += 1;
    calls.most = Math.max(calls.most, calls.inFlight);
    try {
      return await call();
    } finally {
      calls.inFlight -= 1;
    }
  };
  // An entry as Node hands one out when the listing has no type, before it looks it up on the main thread.
  const untyped = (name: string) => ({ name, isFile: () => false, isDirectory: () => false, isSymbolicLink: () => false });
  return {
    ...fs,
    lstat: (...args: Parameters<typeof fs.lstat>) => counted(async () => {
      const path = String(args[0]);
      calls.looked.push(path);
      if (path === calls.hung) {
        await new Promise<void>((_resolve, reject) => {
          calls.release = () => reject(new Error("released"));
        });
      }
      const stats = await fs.lstat(...args);
      const ino = calls.inodes.get(path);
      // As Node reads it: whole as a bigint, rounded to the nearest double as a number.
      if (ino !== undefined) Object.assign(stats, { ino: typeof stats.ino === "bigint" ? ino : Number(ino) });
      return stats;
    }),
    readdir: (...args: Parameters<typeof fs.readdir>) => counted(async () => {
      const listed = await fs.readdir(...args);
      const typed = typeof args[1] === "object" && args[1] !== null && "withFileTypes" in args[1] && args[1].withFileTypes;
      return calls.untyped && typed ? (listed as unknown as Dirent[]).map((entry) => untyped(entry.name)) : listed;
    }),
    opendir: async (...args: Parameters<typeof fs.opendir>) => {
      const dir = await fs.opendir(...args);
      if (!calls.untyped) return dir;
      return (async function* () {
        for await (const entry of dir) yield untyped(entry.name);
      })();
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

afterEach(async () => {
  calls.hung = "";
  calls.release();
  calls.inodes.clear();
  calls.untyped = false;
  calls.looked = [];
  calls.most = 0;
  // A walk the release let go of ends: the next test's scan is not refused as one at a time.
  await new Promise(setImmediate);
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
    // The monitor records a stall when its next timer runs, after it.
    await new Promise((resolve) => setTimeout(resolve, 20));
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

  it("answer at once, as incomplete, while an earlier walk is still held by a look that never returns", async () => {
    linkedIn("a.txt");
    linkedIn("sub/slow.txt");
    calls.hung = join(folder, "sub", "slow.txt");
    expect((await scanLinks(folder, { deadlineMs: 200 }))?.complete).toBe(false);
    const started = performance.now();
    expect(await scanLinks(folder)).toEqual({ count: 0, examples: [], complete: false });
    expect(performance.now() - started).toBeLessThan(50);
    calls.hung = "";
    calls.release();
    // Once that walk has let go, the next is a whole one.
    await vi.waitFor(async () => {
      expect(await scanLinks(folder)).toEqual({ count: 2, examples: ["a.txt", "sub/slow.txt"], complete: true });
    });
  });

  it("keep at most two requests in flight, so a hung mount cannot hold all of libuv's threads", async () => {
    for (const sub of ["a", "b", "c"]) {
      mkdirSync(join(folder, sub));
      for (let i = 0; i < 20; i += 1) writeFileSync(join(folder, sub, `f${i}`), "");
    }
    linkedIn("b/x.txt");
    expect(await scanLinks(folder)).toEqual({ count: 1, examples: ["b/x.txt"], complete: true });
    expect(calls.most).toBeGreaterThan(0);
    expect(calls.most).toBeLessThanOrEqual(2);
  });

  // Node looks an entry of unknown type up with lstatSync, on the main thread, where a hung mount freezes the app.
  it("learn each entry's type from an lstat of its own, when the folder listing gives none", async () => {
    linkedIn("sub/a.txt");
    calls.untyped = true;
    const looked = vi.spyOn(syncFs, "lstatSync");
    try {
      expect(await scanLinks(folder)).toEqual({ count: 1, examples: ["sub/a.txt"], complete: true });
      expect(looked).not.toHaveBeenCalled();
    } finally {
      looked.mockRestore();
    }
    expect(calls.looked).toEqual(expect.arrayContaining([join(folder, "sub"), join(folder, "sub", "a.txt")]));
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
