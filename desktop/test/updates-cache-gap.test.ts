// The update's cache, changed between the app's look at it and its use of it: while a release
// downloads, which takes minutes, and in the few system calls between a look and a write, where
// only another process can act. The calls are real; a test acts right after one of the app's own,
// as a running program of the user's could.

import { linkSync, lstatSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { ranged, servedBase } from "./updates-base.js";

const calls = vi.hoisted(() => ({
  // Runs right after the app's *call* of *path* returned: "lstat", "rm" or "open".
  after: (_call: string, _path: string): void => {},
}));

vi.mock("node:fs", async (original) => {
  const fs = await original<typeof import("node:fs")>();
  const told = <Call extends (...args: never[]) => unknown>(name: string, call: Call): Call => ((...args: Parameters<Call>) => {
    const result = call(...args);
    calls.after(name, String(args[0]));
    return result;
  }) as Call;
  return { ...fs, lstatSync: told("lstat", fs.lstatSync), rmSync: told("rm", fs.rmSync), openSync: told("open", fs.openSync) };
});

const base = servedBase();

afterEach(() => {
  calls.after = () => {};
});

// Once, right after the app's *call* of *path*: *act*, as another process would. Whether it has.
function once(call: string, path: string, act: () => void): () => boolean {
  let acted = false;
  calls.after = (made, at) => {
    if (made !== call || at !== path) return;
    calls.after = () => {};
    acted = true;
    act();
  };
  return () => acted;
}

const NOTES = "the user's own notes\n";
const CHAPTER = "the user's own work\n";
// A folder of the user's that is none of the update's, and what it holds.
function documents(): string {
  const folder = join(base.dir, "documents");
  mkdirSync(join(folder, "thesis"), { recursive: true });
  writeFileSync(join(folder, "thesis", "chapter-1.md"), CHAPTER);
  writeFileSync(join(folder, "notes.txt"), NOTES);
  return folder;
}
// What documents() made is as it was, with *more* beside it.
function untouched(folder: string, more: string[] = []): void {
  expect(readdirSync(folder).sort()).toEqual([...more, "notes.txt", "thesis"].sort());
  expect(readFileSync(join(folder, "notes.txt"), "utf8")).toBe(NOTES);
  expect(readdirSync(join(folder, "thesis"))).toEqual(["chapter-1.md"]);
  expect(readFileSync(join(folder, "thesis", "chapter-1.md"), "utf8")).toBe(CHAPTER);
}
const version = () => join(base.cache(), "1.2.4");
const gone = (path: string) => lstatSync(path, { throwIfNoEntry: false }) === undefined;
// The tarball answered in two parts, with *between* run 200 ms after the first: the download is under way.
function midway(between: () => void): void {
  base.answer = (request, response, body) => {
    if (!request.url?.endsWith(".tar.gz")) return ranged(request, response, body);
    response.writeHead(200, { "content-length": body.length });
    response.write(body.subarray(0, 100_000));
    setTimeout(() => {
      between();
      response.end(body.subarray(100_000));
    }, 200);
  };
}

describe("the update's cache, changed while a release downloads", () => {
  it("removes nothing through a link put in the updates folder's place meanwhile", async () => {
    const folder = documents();
    base.publish("1.2.4");
    // The version's folder, with the partial file in it, is moved to where the link then leads: the
    // rest of the download, its rename and the loop that keeps one release would all follow it.
    midway(() => {
      renameSync(version(), join(folder, "1.2.4"));
      rmSync(base.cache(), { recursive: true });
      symlinkSync(folder, base.cache());
    });
    const found = base.updates();
    await expect(found.check()).rejects.toThrow(`${version()} was replaced while Surogate 1.2.4 was downloaded: nothing in it is taken`);
    untouched(folder, ["1.2.4"]);
    expect(found.state).toEqual({ state: "none" });
    // The link went by itself.
    expect(gone(base.cache())).toBe(true);
  });

  it("removes nothing through a link put in the place of surogate meanwhile", async () => {
    const elsewhere = join(base.dir, "elsewhere");
    base.publish("1.2.4");
    // The app's own folder is moved aside whole, and a link to it takes its name: beside the
    // download, its updates folder there then holds a file of the user's.
    midway(() => {
      renameSync(join(base.dir, "cache", "surogate"), elsewhere);
      writeFileSync(join(elsewhere, "updates", "keep.txt"), NOTES);
      symlinkSync(elsewhere, join(base.dir, "cache", "surogate"));
    });
    const found = base.updates();
    await expect(found.check()).rejects.toThrow(`${version()} was replaced while Surogate 1.2.4 was downloaded: nothing in it is taken`);
    expect(readdirSync(join(elsewhere, "updates")).sort()).toEqual(["1.2.4", "keep.txt"]);
    expect(readFileSync(join(elsewhere, "updates", "keep.txt"), "utf8")).toBe(NOTES);
    expect(readdirSync(join(elsewhere, "updates", "1.2.4")).sort()).toEqual(["manifest.json", "manifest.json.sig", "release.tar.gz.partial"]);
    expect(found.state).toEqual({ state: "none" });
    expect(gone(join(base.dir, "cache", "surogate"))).toBe(true);
  });

  it("takes no release through a link put in its version's folder's place meanwhile", async () => {
    const folder = documents();
    base.publish("1.2.4");
    midway(() => {
      renameSync(version(), join(folder, "held"));
      symlinkSync(join(folder, "held"), version());
    });
    const found = base.updates();
    await expect(found.check()).rejects.toThrow(`${version()} was replaced while Surogate 1.2.4 was downloaded: nothing in it is taken`);
    untouched(folder, ["held"]);
    // Nothing there took the tarball's name.
    expect(readdirSync(join(folder, "held")).sort()).toEqual(["manifest.json", "manifest.json.sig", "release.tar.gz.partial"]);
    expect(found.state).toEqual({ state: "none" });
    expect(gone(version())).toBe(true);
  });

  it("writes nothing through a link put in its version's folder's place while the base is asked for the tarball", async () => {
    const folder = documents();
    base.publish("1.2.4");
    const found = base.updates({
      fetch: (url, init) => {
        // Asked for after the folders' look. The base may take as long as its headers' bound to answer.
        if (url.endsWith(".tar.gz")) {
          rmSync(version(), { recursive: true });
          symlinkSync(folder, version());
        }
        return fetch(url, init);
      },
    });
    // In the app's own words, and not as a base that could not be reached.
    expect(await found.check().catch((error: Error) => error.message)).toBe(`${version()} was replaced while Surogate 1.2.4 was downloaded: nothing in it is taken`);
    // No partial file was made where the link led.
    untouched(folder);
    expect(found.state).toEqual({ state: "none" });
    expect(gone(version())).toBe(true);
  });

  it("writes through no link put in the partial file's place once its folder was looked at", async () => {
    const folder = documents();
    base.publish("1.2.4");
    const partial = join(version(), "release.tar.gz.partial");
    const found = base.updates({
      fetch: (url, init) => {
        // Asked for after the folder's look, and before the partial file is opened.
        if (url.endsWith(".tar.gz")) symlinkSync(join(folder, "notes.txt"), partial);
        return fetch(url, init);
      },
    });
    await expect(found.check()).rejects.toThrow(`the download of Surogate 1.2.4 stopped: ${partial} is a link`);
    untouched(folder);
    expect(found.state).toEqual({ state: "none" });
  });
});

describe("the update's cache, changed between a look and a write", () => {
  it("makes the manifest's file, and opens none: what takes its name once the old one is gone is not written to, a link or a file's second name", async () => {
    const folder = documents();
    const tarball = base.publish("1.2.4");
    const manifest = join(version(), "manifest.json");
    for (const plant of [() => symlinkSync(join(folder, "notes.txt"), manifest), () => linkSync(join(folder, "notes.txt"), manifest)]) {
      rmSync(base.cache(), { recursive: true, force: true });
      // The app removes what has the manifest's name once, in a folder it has just made, before it makes the file.
      const planted = once("rm", manifest, plant);
      await expect(base.updates().check()).rejects.toThrow(/^EEXIST: file already exists, open /);
      expect(planted()).toBe(true);
      untouched(folder);
    }
    // The next check finds it there, and takes it for what it is.
    await base.updates().check();
    untouched(folder);
    expect(readFileSync(join(version(), "release.tar.gz")).equals(tarball)).toBe(true);
    expect(statSync(join(folder, "notes.txt")).nlink).toBe(1);
  });

  it("reads no tarball through a link put in its place after the folder's look: the release is downloaded", async () => {
    const tarball = base.publish("1.2.4");
    const elsewhere = join(base.dir, "elsewhere.tar.gz");
    writeFileSync(elsewhere, tarball);
    // The signature is the last thing written before the tarball that is here is looked at.
    const planted = once("open", join(version(), "manifest.json.sig"), () => symlinkSync(elsewhere, join(version(), "release.tar.gz")));
    const found = base.updates();
    await found.check();
    expect(planted()).toBe(true);
    expect(base.heard.map(({ url }) => url)).toEqual(["/desktop/latest.json", base.signatureAt("1.2.4"), base.tarballAt("1.2.4")]);
    expect(lstatSync(join(version(), "release.tar.gz")).isFile()).toBe(true);
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
  });

  it("sets no mode, and looks into no folder, through a link put in a folder's place after its look", async () => {
    const folder = documents();
    base.publish("1.2.4");
    mkdirSync(version(), { recursive: true });
    const before = statSync(folder).mode;
    const planted = once("lstat", version(), () => {
      rmSync(version(), { recursive: true });
      symlinkSync(folder, version());
    });
    // Linux answers ENOTDIR, not ELOOP, for a link opened as a folder that may not be one.
    await expect(base.updates().check()).rejects.toThrow(/^ENOTDIR: not a directory, open /);
    expect(planted()).toBe(true);
    untouched(folder);
    expect(statSync(folder).mode).toBe(before);
  });
});
