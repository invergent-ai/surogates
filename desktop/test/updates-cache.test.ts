// The update's cache, <cache home>/surogate/updates: the folders and files there are the app's
// own, and it reaches nothing through a link. The cache home may be a link, as one moved to
// another disk is. Below it, what has the name of a folder or a file of the update's and is not
// one goes, a link by itself and never what it leads to.

import { randomBytes } from "node:crypto";
import { chmodSync, linkSync, lstatSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { ranged, servedBase } from "./updates-base.js";

const base = servedBase();

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
const mode = (path: string) => lstatSync(path).mode & 0o777;
// The release is here, whole, in files of the update's own.
function downloaded(tarball: Buffer): void {
  expect(lstatSync(base.cache()).isDirectory()).toBe(true);
  expect(lstatSync(version()).isDirectory()).toBe(true);
  expect(readdirSync(version()).sort()).toEqual(["manifest.json", "manifest.json.sig", "release.tar.gz"]);
  for (const name of readdirSync(version())) {
    const { nlink } = lstatSync(join(version(), name));
    expect([name, lstatSync(join(version(), name)).isFile(), nlink]).toEqual([name, true, 1]);
  }
  expect(readFileSync(join(version(), "manifest.json")).equals(base.served.get("/desktop/latest.json")!)).toBe(true);
  expect(readFileSync(join(version(), "manifest.json.sig")).equals(base.served.get("/desktop/latest.json.sig")!)).toBe(true);
  expect(readFileSync(join(version(), "release.tar.gz")).equals(tarball)).toBe(true);
}

describe("the update's cache", () => {
  it("removes nothing outside its own folder: a link in the place of the updates folder goes by itself, and what it leads to stays", async () => {
    const folder = documents();
    mkdirSync(join(base.dir, "cache", "surogate"), { recursive: true });
    symlinkSync(folder, base.cache());
    const tarball = base.publish("1.2.4");
    const found = base.updates();
    await found.check();
    untouched(folder);
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
    downloaded(tarball);
    expect(readdirSync(base.cache())).toEqual(["1.2.4"]);
  });

  it("removes the link alone when a link has the updates folder's name and nothing is newer", async () => {
    const folder = documents();
    mkdirSync(join(base.dir, "cache", "surogate"), { recursive: true });
    symlinkSync(folder, base.cache());
    base.publish("1.2.3");
    const found = base.updates();
    await found.check();
    untouched(folder);
    expect(found.state).toEqual({ state: "none" });
    expect(lstatSync(base.cache(), { throwIfNoEntry: false })).toBeUndefined();
  });

  it("takes its folders below the cache home as real folders only: a link in the place of surogate goes, and a folder named updates where it leads stays", async () => {
    const elsewhere = join(base.dir, "elsewhere");
    const lead = () => {
      rmSync(join(base.dir, "cache"), { recursive: true, force: true });
      mkdirSync(join(elsewhere, "updates"), { recursive: true });
      writeFileSync(join(elsewhere, "updates", "keep.txt"), NOTES);
      mkdirSync(join(base.dir, "cache"));
      symlinkSync(elsewhere, join(base.dir, "cache", "surogate"));
    };
    const kept = () => {
      expect(readdirSync(elsewhere)).toEqual(["updates"]);
      expect(readdirSync(join(elsewhere, "updates"))).toEqual(["keep.txt"]);
      expect(readFileSync(join(elsewhere, "updates", "keep.txt"), "utf8")).toBe(NOTES);
    };
    // With a newer release offered: it is downloaded into a folder of the app's own.
    lead();
    const tarball = base.publish("1.2.4");
    await base.updates().check();
    kept();
    expect(lstatSync(join(base.dir, "cache", "surogate")).isDirectory()).toBe(true);
    downloaded(tarball);
    // With nothing newer, where the earlier offer's download goes: only the link does.
    lead();
    base.publish("1.2.3");
    await base.updates().check();
    kept();
    expect(lstatSync(join(base.dir, "cache", "surogate"), { throwIfNoEntry: false })).toBeUndefined();
  });

  it("follows the cache home itself where it is a link, as a cache moved to another disk, and names its files by their real path", async () => {
    const disk = join(base.dir, "disk", "cache");
    mkdirSync(disk, { recursive: true });
    symlinkSync(disk, join(base.dir, "cache"));
    const tarball = base.publish("1.2.4");
    const found = base.updates();
    await found.check();
    const folder = join(disk, "surogate", "updates", "1.2.4");
    expect(found.state).toEqual({
      state: "available", version: "1.2.4",
      files: { manifest: join(folder, "manifest.json"), signature: join(folder, "manifest.json.sig"), tarball: join(folder, "release.tar.gz") },
    });
    expect(lstatSync(join(base.dir, "cache")).isSymbolicLink()).toBe(true);
    downloaded(tarball);
  });

  it("makes a version's folder afresh where a link has its name, and writes nothing where the link leads", async () => {
    const folder = documents();
    mkdirSync(base.cache(), { recursive: true });
    symlinkSync(folder, version());
    const tarball = base.publish("1.2.4");
    await base.updates().check();
    untouched(folder);
    downloaded(tarball);
  });

  it("writes the manifest and its signature as new files of its own: a link in the place of either goes, and what it leads to is not written over", async () => {
    const folder = documents();
    mkdirSync(version(), { recursive: true });
    symlinkSync(join(folder, "notes.txt"), join(version(), "manifest.json"));
    symlinkSync(join(folder, "thesis", "chapter-1.md"), join(version(), "manifest.json.sig"));
    const tarball = base.publish("1.2.4");
    await base.updates().check();
    untouched(folder);
    downloaded(tarball);
    // A second name of one of the user's files, as a hard link gives it: written over no more than through a link.
    rmSync(join(version(), "manifest.json"));
    linkSync(join(folder, "notes.txt"), join(version(), "manifest.json"));
    await base.updates().check();
    untouched(folder);
    downloaded(tarball);
  });

  it("downloads into a partial file of its own: a link in its place goes, and what it leads to is neither added to nor emptied", async () => {
    const folder = documents();
    const tarball = base.publish("1.2.4");
    const partial = join(version(), "release.tar.gz.partial");
    // A link to a file shorter than the release, which a resume would add to.
    mkdirSync(version(), { recursive: true });
    symlinkSync(join(folder, "notes.txt"), partial);
    await base.updates().check();
    untouched(folder);
    downloaded(tarball);
    // A link to a file longer than the release, which a fresh start would empty: even when nothing then comes.
    rmSync(base.cache(), { recursive: true });
    mkdirSync(version(), { recursive: true });
    writeFileSync(join(folder, "longer.bin"), randomBytes(400_000));
    symlinkSync(join(folder, "longer.bin"), partial);
    base.served.delete(base.tarballAt("1.2.4"));
    await expect(base.updates().check()).rejects.toThrow(`${new URL(base.url).host} answered 404 for Surogate 1.2.4`);
    expect(statSync(join(folder, "longer.bin")).size).toBe(400_000);
    expect(lstatSync(partial, { throwIfNoEntry: false })).toBeUndefined();
    rmSync(join(folder, "longer.bin"));
    // A second name of one of the user's files.
    base.served.set(base.tarballAt("1.2.4"), tarball);
    linkSync(join(folder, "notes.txt"), partial);
    await base.updates().check();
    untouched(folder);
    downloaded(tarball);
  });

  it("takes a tarball that is here only as a regular file: a link to the release elsewhere is not read, and the release is downloaded", async () => {
    const tarball = base.publish("1.2.4");
    const elsewhere = join(base.dir, "elsewhere.tar.gz");
    writeFileSync(elsewhere, tarball);
    mkdirSync(version(), { recursive: true });
    symlinkSync(elsewhere, join(version(), "release.tar.gz"));
    const found = base.updates();
    await found.check();
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
    expect(base.heard.map(({ url }) => url)).toEqual(["/desktop/latest.json", "/desktop/latest.json.sig", base.tarballAt("1.2.4")]);
    downloaded(tarball);
    expect(readFileSync(elsewhere).equals(tarball)).toBe(true);
  });

  it("takes what has a folder's or a file's name and is neither: a file named as the updates folder, a folder named as the manifest", async () => {
    const tarball = base.publish("1.2.4");
    mkdirSync(join(base.dir, "cache", "surogate"), { recursive: true });
    writeFileSync(base.cache(), "not a folder\n");
    await base.updates().check();
    downloaded(tarball);
    rmSync(join(version(), "manifest.json"));
    mkdirSync(join(version(), "manifest.json", "inside"), { recursive: true });
    await base.updates().check();
    downloaded(tarball);
  });

  it("keeps its folders and files the user's alone, 0700 and 0600, whatever was there and whatever the umask leaves", async () => {
    const tarball = base.publish("1.2.4");
    // Folders that are there already, open to others.
    mkdirSync(version(), { recursive: true });
    for (const folder of [join(base.dir, "cache", "surogate"), base.cache(), version()]) chmodSync(folder, 0o777);
    // The download stops partway: its partial file is the user's alone too.
    base.answer = (request, response, body) => {
      if (!request.url?.endsWith(".tar.gz")) return ranged(request, response, body);
      response.writeHead(200, { "content-length": body.length });
      response.write(body.subarray(0, 100_000));
      setTimeout(() => response.socket?.destroy(), 200);
    };
    await expect(base.updates().check()).rejects.toThrow(/^the download of Surogate 1\.2\.4 stopped: /);
    expect(mode(join(version(), "release.tar.gz.partial"))).toBe(0o600);
    base.answer = ranged;
    await base.updates().check();
    downloaded(tarball);
    expect([join(base.dir, "cache", "surogate"), base.cache(), version()].map(mode)).toEqual([0o700, 0o700, 0o700]);
    expect(["manifest.json", "manifest.json.sig", "release.tar.gz"].map((name) => mode(join(version(), name)))).toEqual([0o600, 0o600, 0o600]);
    // A cache made by the check itself.
    rmSync(join(base.dir, "cache"), { recursive: true });
    await base.updates().check();
    expect([join(base.dir, "cache"), join(base.dir, "cache", "surogate"), base.cache(), version()].map(mode)).toEqual([0o700, 0o700, 0o700, 0o700]);
    expect(["manifest.json", "manifest.json.sig", "release.tar.gz"].map((name) => mode(join(version(), name)))).toEqual([0o600, 0o600, 0o600]);
  });

  it("starts a download afresh over a partial file larger than the release", async () => {
    const tarball = base.publish("1.2.4");
    mkdirSync(version(), { recursive: true });
    writeFileSync(join(version(), "release.tar.gz.partial"), randomBytes(tarball.length + 10));
    await base.updates().check();
    downloaded(tarball);
    expect(base.heard.filter(({ url }) => url.endsWith(".tar.gz")).map(({ range }) => range)).toEqual([undefined]);
  });
});
