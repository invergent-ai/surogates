// What an installed app on macOS does of its install (src/shell/installer-darwin.ts): which bundle it is, and whether
// this user may update it where it is; a release's tarball unpacked beside it by the install script's archive rules,
// checked, and handed to surogate-helper's apply, which the app quits for once it waits; the removal handed to its
// remove; and, once a start reaches its window, what an update left beside it goes. Here with a stand-in for the
// helper, on any OS; on a Mac also with the helper built for it (native/helper/build.sh), on bundles of the test's own
// signed ad hoc, which it takes by the test's requirement, the swap on the Mac's own disk.

import { execFileSync, spawn, spawnSync } from "node:child_process";
import {
  chmodSync, closeSync, constants, copyFileSync, existsSync, linkSync, lstatSync, mkdirSync, mkdtempSync, openSync, readdirSync, readFileSync, realpathSync, rmSync, statSync,
  symlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  applyingUnder, bundleOf, checkStaged, DISK_IMAGE, forgotten, heldAt, installedUpdates, MACHINE_RECORD, reachedWindow, removalAsked, removeRun, stage, stagingOf,
  treeRefusal, updateRun,
} from "../src/shell/installer-darwin.js";
import { bundled } from "../src/shell/updates.js";
import { HELPER } from "../src/native-helper.js";
import { importedOn } from "./platforms.js";

let dir: string;
beforeEach(() => {
  dir = realpathSync(mkdtempSync(join(tmpdir(), "installer-darwin-")));
});
afterEach(() => {
  // A folder a test made read-only is opened again to be removed.
  for (const name of readdirSync(dir)) chmodSync(join(dir, name), 0o755);
  rmSync(dir, { recursive: true, force: true });
});

const uid = process.getuid!();

// A tree as a release's Surogate.app is, at *path*: its program and helper, its Info.plist, its package of *version*,
// and a framework's links, each inside it.
function tree(path: string, version = "1.2.4"): string {
  for (const folder of ["Contents/MacOS", "Contents/Resources/app", "Contents/Frameworks/Kit.framework/Versions/A"]) mkdirSync(join(path, folder), { recursive: true });
  for (const program of ["Surogate", "surogate-helper"]) {
    writeFileSync(join(path, "Contents/MacOS", program), "#!/bin/sh\n");
    chmodSync(join(path, "Contents/MacOS", program), 0o755);
  }
  writeFileSync(join(path, "Contents/Info.plist"), "<plist/>\n");
  writeFileSync(join(path, "Contents/Resources/app/package.json"), `${JSON.stringify({ version, stateSchema: 1 })}\n`);
  writeFileSync(join(path, "Contents/Resources/release-keys"), "RELEASE_KEYS=(\n  )\n");
  writeFileSync(join(path, "Contents/Frameworks/Kit.framework/Versions/A/Kit"), "code");
  symlinkSync("A", join(path, "Contents/Frameworks/Kit.framework/Versions/Current"));
  symlinkSync("Versions/Current/Kit", join(path, "Contents/Frameworks/Kit.framework/Kit"));
  return path;
}

// *top* as a release's tarball, at *path*, its one top folder Surogate.app, with no AppleDouble member of macOS's tar.
function tarball(top: string, path: string, names = ["Surogate.app"]): string {
  execFileSync("tar", ["-czf", path, "-C", top, ...names], { env: { ...process.env, COPYFILE_DISABLE: "1" } });
  return path;
}

describe("the app's bundle", () => {
  it("is the .app its program is in, by the program's whole path", () => {
    expect(bundleOf("/Applications/Surogate.app/Contents/MacOS/Surogate")).toBe("/Applications/Surogate.app");
    expect(bundleOf("/Users/ana/Applications/Surogate.app/Contents/MacOS/Surogate")).toBe("/Users/ana/Applications/Surogate.app");
    expect(bundleOf("/Applications/Surogate 2.app/Contents/MacOS/Surogate")).toBe("/Applications/Surogate 2.app");
    for (const exec of ["/opt/surogate/current/surogate", "/Applications/Surogate.app/Contents/Frameworks/x/Surogate", "Surogate.app/Contents/MacOS/Surogate"]) expect(bundleOf(exec), exec).toBeNull();
    expect(stagingOf("/Applications/Surogate.app", "1.2.4")).toBe("/Applications/.Surogate-1.2.4.staging");
  });

  it("is updated where the folder that holds it is this user's to change and the bundle is theirs", () => {
    const app = join(dir, "Surogate.app");
    mkdirSync(app);
    expect(heldAt(app, uid)).toBeNull();
    // Another user's bundle, in a folder this user may change: an administrator's to install.
    expect(heldAt(app, uid + 1)).toEqual({ download: DISK_IMAGE });
    // A link in the bundle's place is no bundle.
    const link = join(dir, "link");
    mkdirSync(link);
    symlinkSync(app, join(link, "Surogate.app"));
    expect(heldAt(join(link, "Surogate.app"), uid)).toEqual({ download: DISK_IMAGE });
  });

  it("is an administrator's to install where this user may not change its folder", () => {
    const parent = join(dir, "Applications");
    mkdirSync(join(parent, "Surogate.app"), { recursive: true });
    chmodSync(parent, 0o555);
    expect(heldAt(join(parent, "Surogate.app"), uid)).toEqual({ download: DISK_IMAGE });
  });

  it("is updated by nobody where it runs from macOS's own copy of it, or under another name", () => {
    const translocated = "/private/var/folders/ab/T/AppTranslocation/0A1B2C3D/d/Surogate.app";
    expect(heldAt(translocated, uid)).toEqual({ why: "macOS runs it from a copy of its own, as it does an app opened where it was downloaded. Move Surogate to Applications, and open it from there" });
    mkdirSync(join(dir, "Surogate 2.app"));
    expect(heldAt(join(dir, "Surogate 2.app"), uid)).toEqual({ why: "it is named Surogate 2.app. Name it Surogate.app again, and it updates itself" });
  });

  // A folder on a disk that cannot be written to: a Mac's system volume, which is sealed; elsewhere, a mount of this
  // computer's that is read-only, where there is one.
  const readOnly = process.platform === "darwin" ? "/System/Library/CoreServices"
    : readFileSync("/proc/self/mounts", "utf8").split("\n").map((line) => line.split(" ")).find(([, at, , options]) => options?.split(",").includes("ro") && at?.startsWith("/snap/"))?.[1];
  it.runIf(readOnly !== undefined)("is updated by nobody where it runs from a read-only disk, as the disk image is", () => {
    expect(heldAt(join(readOnly!, "Surogate.app"), uid)).toEqual({ why: `${readOnly} is on a disk that cannot be written to. Copy Surogate to Applications, and open it from there` });
  });
});

describe("a release's unpacked tree", () => {
  it("is taken where it is Surogate's bundle, with links that stay inside it", () => {
    expect(treeRefusal(tree(join(dir, "Surogate.app")))).toBeNull();
  });

  it("is refused where it holds a special file, a set-id file or a hard link", () => {
    const special = "the release's archive holds a special file, a set-id file or a hard link";
    const fifo = tree(join(dir, "fifo", "Surogate.app"));
    execFileSync("mkfifo", [join(fifo, "Contents/Resources/pipe")]);
    expect(treeRefusal(fifo)).toBe(special);
    const setId = tree(join(dir, "set-id", "Surogate.app"));
    chmodSync(join(setId, "Contents/MacOS/surogate-helper"), 0o4755);
    expect(treeRefusal(setId)).toBe(special);
    const hard = tree(join(dir, "hard", "Surogate.app"));
    linkSync(join(hard, "Contents/Info.plist"), join(hard, "Contents/Resources/Info.plist"));
    expect(treeRefusal(hard)).toBe(special);
  });

  it("is refused where a link names a whole path, climbs out as it is written, or leaves it as it resolves", () => {
    const linked = (target: string, at = "Contents/Resources/link") => {
      const top = tree(join(dir, `t${readdirSync(dir).length}`, "Surogate.app"));
      symlinkSync(target, join(top, at));
      return treeRefusal(top);
    };
    expect(linked("/etc/passwd")).toBe("the release's archive holds a link to a whole path: Contents/Resources/link");
    expect(linked("../../../outside")).toBe("the release's archive links outside itself: Contents/Resources/link");
    // The tree's own top is not inside it, as the install script's check has it.
    expect(linked("..", "Contents/up")).toBe("the release's archive links outside itself: Contents/up");
    // Inside as it is written, and outside as it resolves: a link to its own folder, climbed out of from there.
    const through = tree(join(dir, "through", "Surogate.app"));
    symlinkSync(".", join(through, "Contents/here"));
    symlinkSync("here/../../x", join(through, "Contents/out"));
    expect(treeRefusal(through)).toMatch(/^the release's archive links outside itself: Contents\/(?:here|out)$/);
    rmSync(join(through, "Contents/out"));
    expect(treeRefusal(through)).toBeNull();
    // Outside as it is written, and inside as it resolves: climbed out of through a link that leads deeper.
    const deeper = tree(join(dir, "deeper", "Surogate.app"));
    symlinkSync("Resources/app", join(deeper, "Contents/deep"));
    symlinkSync("deep/../..", join(deeper, "Contents/up"));
    expect(treeRefusal(deeper)).toBe("the release's archive links outside itself: Contents/up");
    // Links that lead to each other lead nowhere.
    const loop = tree(join(dir, "loop", "Surogate.app"));
    symlinkSync("b", join(loop, "Contents/a"));
    symlinkSync("a", join(loop, "Contents/b"));
    expect(treeRefusal(loop)).toMatch(/^the release's archive links outside itself: Contents\/[ab]$/);
  });

  it("is refused where it is not Surogate's bundle", () => {
    for (const [what, change] of [
      ["no program", (top: string) => rmSync(join(top, "Contents/MacOS/Surogate"))],
      ["a program that cannot be run", (top: string) => chmodSync(join(top, "Contents/MacOS/surogate-helper"), 0o644)],
      ["a folder for its Info.plist", (top: string) => (rmSync(join(top, "Contents/Info.plist")), mkdirSync(join(top, "Contents/Info.plist")))],
      ["a link for its package", (top: string) => (rmSync(join(top, "Contents/Resources/app/package.json")), symlinkSync("../../Info.plist", join(top, "Contents/Resources/app/package.json")))],
    ] as const) {
      const top = tree(join(dir, what, "Surogate.app"));
      change(top);
      expect(treeRefusal(top), what).toBe("the release's archive is not Surogate Desktop");
    }
  });
});

describe("a release staged beside the app", () => {
  it("is unpacked on the app's volume, under a name that is no app's, and nothing of the unpack is left", async () => {
    const app = join(dir, "Surogate.app");
    mkdirSync(app);
    const release = tarball(dirname(tree(join(dir, "built", "Surogate.app"))), join(dir, "release.tar.gz"));
    // What an earlier try left there goes.
    mkdirSync(join(dir, ".Surogate-1.2.4.staging", "old"), { recursive: true });
    const staged = await stage(release, app, "1.2.4");
    expect(staged).toBe(join(dir, ".Surogate-1.2.4.staging"));
    expect(treeRefusal(staged)).toBeNull();
    expect(existsSync(join(staged, "old"))).toBe(false);
    expect(readdirSync(dir).filter((name) => name.startsWith(".Surogate"))).toEqual([".Surogate-1.2.4.staging"]);
    // Its programs keep their mode, and are the user's.
    expect(statSync(join(staged, "Contents/MacOS/Surogate")).mode & 0o777).toBe(0o755);
    expect(statSync(join(staged, "Contents/MacOS/Surogate")).uid).toBe(uid);
  });

  it("is its user's alone to write, whatever the umask: the next version's release keys are its own, and the tree can be removed", async () => {
    const app = join(dir, "Surogate.app");
    mkdirSync(app);
    const built = tree(join(dir, "built", "Surogate.app"));
    // As a build whose own umask let its group write: and a folder no one may write.
    chmodSync(join(built, "Contents/Resources/release-keys"), 0o664);
    chmodSync(join(built, "Contents/MacOS/Surogate"), 0o775);
    chmodSync(join(built, "Contents/Resources/app"), 0o555);
    const release = tarball(dirname(built), join(dir, "release.tar.gz"));
    chmodSync(join(built, "Contents/Resources/app"), 0o755);
    const umask = process.umask(0o002);
    let staged: string;
    try {
      staged = await stage(release, app, "1.2.4");
    } finally {
      process.umask(umask);
    }
    expect(() => bundled(join(staged, "Contents/Resources/release-keys"))).not.toThrow();
    for (const [path, mode] of [["Contents/Resources/release-keys", 0o644], ["Contents/MacOS/Surogate", 0o755], ["Contents/Resources/app", 0o755], ["Contents", 0o755]] as const) {
      expect(statSync(join(staged, path)).mode & 0o7777, path).toBe(mode);
    }
  });

  it("is refused, leaving nothing, where the archive is no release's", async () => {
    const app = join(dir, "Surogate.app");
    mkdirSync(app);
    const built = dirname(tree(join(dir, "built", "Surogate.app")));
    writeFileSync(join(built, "more"), "more");
    const refused = async (release: string) => {
      const why = await stage(release, app, "1.2.4").then(() => null, (error: unknown) => (error as Error).message);
      expect(readdirSync(dir).filter((name) => name.startsWith(".Surogate"))).toEqual([]);
      return why;
    };
    expect(await refused(tarball(built, join(dir, "two.tar.gz"), ["Surogate.app", "more"]))).toBe("the release's archive holds more than Surogate.app/");
    writeFileSync(join(dir, "page.tar.gz"), "<html>Sign in to the hotel's Wi-Fi</html>");
    expect(await refused(join(dir, "page.tar.gz"))).toBe("the release's archive could not be unpacked");
    symlinkSync("/etc", join(built, "Surogate.app/Contents/Resources/etc"));
    expect(await refused(tarball(built, join(dir, "linked.tar.gz")))).toBe("the release's archive holds a link to a whole path: Contents/Resources/etc");
  });
});

// A stand-in for surogate-helper, a script: it writes each run's arguments down, a line each, in <dir>/ran; its verify
// ends as <dir>/verify holds, a code and words; its apply and remove say they wait, then, once <dir>/hold is gone, keep
// <dir>/result on their descriptor 3 where they have one, and end; and a run of <dir>/early's command says something
// else, and ends 2.
function standIn(): string {
  const script = join(dir, "surogate-helper");
  writeFileSync(script, [
    "#!/bin/sh",
    `printf '%s ' "$@" >>${dir}/ran; echo >>${dir}/ran`,
    `if [ -e ${dir}/early ] && [ "$1" = "$(cat ${dir}/early)" ]; then echo starting; echo "usage: surogate-helper $1 ..." >&2; exit 2; fi`,
    `if [ "$1" = verify ]; then read -r code words <${dir}/verify; [ -z "$words" ] || echo "$words" >&2; exit "$code"; fi`,
    "echo waiting",
    `while [ -e ${dir}/hold ]; do sleep 0.1; done`,
    `if [ -e ${dir}/result ]; then cat ${dir}/result >&3; fi`,
    "",
  ].join("\n"));
  chmodSync(script, 0o755);
  writeFileSync(join(dir, "verify"), "0\n");
  return script;
}

const ran = () => readFileSync(join(dir, "ran"), "utf8").trim().split("\n").map((line) => line.trim());

// A downloaded release of *version*, as a check leaves one in the cache: its manifest, and its tarball.
function downloaded(version = "1.2.4", stateSchema = 1): { manifest: string; signature: string; tarball: string } {
  const folder = join(dir, "cache", version);
  mkdirSync(folder, { recursive: true });
  rmSync(join(dir, "built", version), { recursive: true, force: true });
  writeFileSync(join(folder, "manifest.json"), `${JSON.stringify({ version, stateSchema, platform: "darwin", arch: "arm64" })}\n`);
  writeFileSync(join(folder, "manifest.json.sig"), "signature");
  return { manifest: join(folder, "manifest.json"), signature: join(folder, "manifest.json.sig"), tarball: tarball(dirname(tree(join(dir, "built", version, "Surogate.app"), version)), join(folder, "release.tar.gz")) };
}

describe("an update's install", () => {
  it("is staged and checked, then handed to the helper's apply, which the app quits for once it says it waits", async () => {
    const helper = standIn();
    const app = join(dir, "Surogate.app");
    mkdirSync(app);
    const files = downloaded();
    writeFileSync(join(dir, "result"), '{"version":"1.2.4","code":0}\n');
    expect(await updateRun(helper, app, 4242)(files)).toEqual({ code: 0, said: "", restarts: true });
    const staged = join(dir, ".Surogate-1.2.4.staging");
    expect(ran()).toEqual([`verify --staged ${staged} --app ${app} --version 1.2.4`, `apply --wait 4242 --staged ${staged} --app ${app} --version 1.2.4`]);
    // How its apply ended, on its descriptor 3: the result file in the release's folder of the cache, made afresh.
    await expect.poll(() => readFileSync(join(dir, "cache/1.2.4/result.json"), "utf8")).toBe('{"version":"1.2.4","code":0}\n');
    expect(statSync(join(dir, "cache/1.2.4/result.json")).mode & 0o777).toBe(0o600);
  });

  it("hands the helper no release its check refused, says why, and leaves nothing staged", async () => {
    const helper = standIn();
    const app = join(dir, "Surogate.app");
    mkdirSync(app);
    writeFileSync(join(dir, "verify"), "3 the staged bundle is not signed by this app's team\n");
    expect(await updateRun(helper, app, 4242)(downloaded())).toEqual({ code: null, said: "the staged bundle is not signed by this app's team" });
    expect(ran()).toHaveLength(1);
    expect(existsSync(join(dir, ".Surogate-1.2.4.staging"))).toBe(false);
    // Nor one whose own package is not the release its manifest names: its helper is not asked.
    rmSync(join(dir, "ran"));
    writeFileSync(join(dir, "verify"), "0\n");
    expect(await updateRun(helper, app, 4242)(downloaded("1.2.4", 2))).toEqual({ code: null, said: "the release's Surogate.app is not the release its manifest names" });
    expect(existsSync(join(dir, "ran"))).toBe(false);
    expect(existsSync(join(dir, ".Surogate-1.2.4.staging"))).toBe(false);
  });

  it("stages nothing where this user cannot update the app where it is", async () => {
    const helper = standIn();
    const parent = join(dir, "Applications");
    mkdirSync(join(parent, "Surogate.app"), { recursive: true });
    chmodSync(parent, 0o555);
    const files = downloaded();
    expect(await updateRun(helper, join(parent, "Surogate.app"), 4242)(files)).toEqual({ code: null, said: `this user may not change ${parent}: an administrator installs the update` });
    expect(existsSync(join(dir, "ran"))).toBe(false);
    expect(readdirSync(parent)).toEqual(["Surogate.app"]);
  });

  it("answers how the helper ended where it ends before it waits, and the app does not quit", async () => {
    const helper = standIn();
    const app = join(dir, "Surogate.app");
    mkdirSync(app);
    writeFileSync(join(dir, "early"), "apply");
    expect(await updateRun(helper, app, 4242)(downloaded())).toEqual({ code: 2, said: "usage: surogate-helper apply ..." });
    expect(await updateRun(join(dir, "missing"), app, 4242)(downloaded())).toMatchObject({ code: null });
  });
});

describe("an apply under way, as a start finds it", () => {
  it("is never found on Linux, which takes no lock, though the result file is there", () => {
    mkdirSync(join(dir, "cache", "1.2.4"), { recursive: true });
    writeFileSync(join(dir, "cache", "1.2.4", "result.json"), "");
    expect(applyingUnder(join(dir, "cache"))).toBe(false);
    expect(applyingUnder(join(dir, "none"))).toBe(false);
  });

  it.runIf(process.platform === "darwin")("is the result file locked, as the app locks the one it hands the helper's apply, for as long as the helper holds it", async () => {
    const cache = join(dir, "cache");
    mkdirSync(join(cache, "1.2.3"), { recursive: true });
    writeFileSync(join(cache, "1.2.3", "result.json"), "");
    expect(applyingUnder(cache)).toBe(false);
    // macOS's O_EXLOCK, as the app opens it.
    const held = openSync(join(cache, "1.2.3", "result.json"), constants.O_RDONLY | 0x20);
    expect(applyingUnder(cache)).toBe(true);
    closeSync(held);
    expect(applyingUnder(cache)).toBe(false);
    // Through an install: locked while the helper's apply holds its descriptor 3, and not once it has ended.
    const helper = standIn();
    const app = join(dir, "Surogate.app");
    mkdirSync(app);
    const files = downloaded();
    writeFileSync(join(dir, "hold"), "");
    expect(await updateRun(helper, app, 4242)(files)).toEqual({ code: 0, said: "", restarts: true });
    expect(applyingUnder(join(dir, "cache"))).toBe(true);
    rmSync(join(dir, "hold"));
    await expect.poll(() => applyingUnder(join(dir, "cache")), { timeout: 5_000 }).toBe(false);
  });
});

describe("the app's removal", () => {
  it("hands the helper this app's process, the bundle to move to the Trash where this user may, and the folders to forget, and answers once it waits", async () => {
    const helper = standIn();
    const app = join(dir, "Surogate.app");
    mkdirSync(app);
    const places = await importedOn("darwin", async () => (await import("../src/shell/places.js")).placesFor({ HOME: "/Users/ana" }, "/Users/ana"));
    expect(await removeRun(helper, app, 77)(forgotten(places))).toEqual({ code: 0, said: "", restarts: true });
    expect(ran()).toEqual([`remove --wait 77 --app ${app} --forget /Users/ana/Library/Application Support/Surogate --forget /Users/ana/Library/Caches/Surogate`]);
    // Where this user may not change the bundle's folder, the bundle is not named: an administrator removes it.
    rmSync(join(dir, "ran"));
    expect(await removeRun(helper, join(dir, "Surogate 2.app"), 77)([])).toEqual({ code: 0, said: "", restarts: true });
    expect(ran()).toEqual(["remove --wait 77"]);
    writeFileSync(join(dir, "early"), "remove");
    expect(await removeRun(helper, app, 77)([])).toEqual({ code: 2, said: "usage: surogate-helper remove ..." });
  });

  it("asks first, saying what goes and what stays, as the app is where it is", () => {
    const app = join(dir, "Surogate.app");
    mkdirSync(app);
    const keeps = "The folders your chats work on are left as they are.";
    expect(removalAsked(app)).toEqual({
      message: "Remove Surogate from this Mac?",
      detail: `Surogate quits and goes to the Trash, and its login item and its key in your Keychain are removed. ${keeps}`,
      checkbox: "Also delete your Surogate data on this Mac: your sign-in, your settings, the sandbox's disks and the updates it downloaded",
    });
    chmodSync(dir, 0o555);
    expect(removalAsked(app).detail).toBe(`Surogate's login item and its key in your Keychain are removed. Surogate itself stays in ${dir}: an administrator removes it there. ${keeps}`);
    chmodSync(dir, 0o755);
    expect(removalAsked("/private/var/folders/ab/T/AppTranslocation/0A1B/d/Surogate.app").detail)
      .toBe(`Surogate's login item and its key in your Keychain are removed. Surogate itself runs from where it cannot be changed: drag it to the Trash once it has quit. ${keeps}`);
  });
});

describe("a start that reaches its window", () => {
  it("removes what an update left beside the app, the version it replaced too, and nothing else", async () => {
    const app = join(dir, "Surogate.app");
    mkdirSync(app);
    for (const name of [".Surogate-1.2.3.previous", ".Surogate-1.2.4.staging", ".Surogate-1.2.4.unpack", ".Surogate-x.previous", ".Surogate-01.2.3.previous", "Other.app", ".Surogate-1.2.3.previous.kept"]) {
      mkdirSync(join(dir, name, "Contents"), { recursive: true });
    }
    // A link of the name, and what it leads to, stay; as does a file of the name.
    mkdirSync(join(dir, "elsewhere"));
    symlinkSync(join(dir, "elsewhere"), join(dir, ".Surogate-1.2.2.previous"));
    writeFileSync(join(dir, ".Surogate-1.2.1.previous"), "a file");
    await reachedWindow(app, uid);
    expect(readdirSync(dir).sort()).toEqual([".Surogate-01.2.3.previous", ".Surogate-1.2.1.previous", ".Surogate-1.2.2.previous", ".Surogate-1.2.3.previous.kept", ".Surogate-x.previous", "Other.app", "Surogate.app", "elsewhere"]);
    // Another user's are theirs.
    mkdirSync(join(dir, ".Surogate-1.2.3.previous"));
    await reachedWindow(app, uid + 1);
    expect(existsSync(join(dir, ".Surogate-1.2.3.previous"))).toBe(true);
    // An app in no bundle has nothing beside it.
    await expect(reachedWindow(null, uid)).resolves.toBeUndefined();
  });
});

describe("an installed Mac app's updates", () => {
  it("read the record an administrator placed, the release keys and the version in the bundle, and run its own helper", () => {
    const { signal } = new AbortController();
    const options = installedUpdates("1.2.3", "/Users/ana/Library/Caches/Surogate/updates", fetch, signal, "/Applications/Surogate.app", "/Applications/Surogate.app/Contents/MacOS/surogate-helper");
    expect(options).toMatchObject({
      version: "1.2.3", record: MACHINE_RECORD, rootOwned: true, helper: "/Applications/Surogate.app/Contents/Resources/release-keys",
      installed: "/Applications/Surogate.app/Contents/Resources/app/package.json", cache: "/Users/ana/Library/Caches/Surogate/updates", fetch, signal,
    });
    expect(MACHINE_RECORD).toBe("/Library/Application Support/Surogate/install.json");
    // An app in no bundle installs nothing.
    expect(installedUpdates("1.2.3", "/c", fetch, signal, null).held?.()).toEqual({ why: "it runs from no app bundle" });
  });
});

describe("the README", () => {
  it("lists each place the app writes on a Mac, for a person who drags it to the Trash", async () => {
    const readme = readFileSync(fileURLToPath(new URL("../README.md", import.meta.url)), "utf8");
    const section = readme.slice(readme.indexOf("## What it writes on a Mac"), readme.indexOf("\n## ", readme.indexOf("## What it writes on a Mac") + 1));
    const { root, cache } = await importedOn("darwin", async () => (await import("../src/shell/places.js")).placesFor({ HOME: "/Users/ana" }, "/Users/ana"));
    for (const place of [root, cache].map((path) => path.replace("/Users/ana", "~"))) expect(section).toContain(`\`${place}/\``);
    for (const named of [MACHINE_RECORD, "Surogate Safe Storage", "Remove Surogate from This Mac…", ".Surogate-<version>.previous"]) expect(section).toContain(named);
  });
});

// The helper built for this Mac, on bundles of the test's own: a program built for this Mac, in a bundle with Surogate's
// identifier, signed ad hoc, which the helper takes by the test's requirement since its own signature names no team.
describe.runIf(process.platform === "darwin" && existsSync(HELPER))("surogate-helper's update, on a Mac", () => {
  const REQUIREMENT = { PATH: "/usr/bin:/bin", SUROGATE_TEST_REQUIREMENT: 'identifier "ai.invergent.surogate"' };
  let program: string;
  beforeEach(() => {
    program = join(dir, "program");
    writeFileSync(join(dir, "program.c"), "int main(void) { return 0; }\n");
    execFileSync("/usr/bin/cc", ["-o", program, join(dir, "program.c")]);
  });

  // Surogate.app of *version* at *path*, signed ad hoc, inside out, as *identifier*.
  function bundle(path: string, version: string, identifier = "ai.invergent.surogate"): string {
    tree(path, version);
    for (const name of ["Surogate", "surogate-helper"]) copyFileSync(program, join(path, "Contents/MacOS", name));
    writeFileSync(join(path, "Contents/Info.plist"), [
      '<?xml version="1.0" encoding="UTF-8"?>', '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">', '<plist version="1.0"><dict>',
      ...Object.entries({ CFBundleExecutable: "Surogate", CFBundleIdentifier: identifier, CFBundleName: "Surogate", CFBundlePackageType: "APPL", CFBundleShortVersionString: version, CFBundleVersion: version })
        .map(([key, value]) => `<key>${key}</key><string>${value}</string>`),
      "</dict></plist>", "",
    ].join("\n"));
    rmSync(join(path, "Contents/Frameworks"), { recursive: true });
    execFileSync("/usr/bin/codesign", ["--force", "--sign", "-", join(path, "Contents/MacOS/surogate-helper")]);
    execFileSync("/usr/bin/codesign", ["--force", "--sign", "-", "--identifier", identifier, path]);
    return path;
  }

  it("takes a staged bundle of this app's, of the version asked for and newer, and refuses one changed since it was signed", async () => {
    const app = bundle(join(dir, "Surogate.app"), "1.2.3");
    const staged = bundle(join(dir, ".Surogate-1.2.4.staging"), "1.2.4");
    const release = { version: "1.2.4", stateSchema: 1 };
    await checkStaged(staged, app, release, HELPER, REQUIREMENT);
    // Without the test's requirement, an unsigned helper takes nothing.
    await expect(checkStaged(staged, app, release, HELPER)).rejects.toThrow("this helper is not signed, and no test requirement is named");
    await expect(checkStaged(staged, app, { version: "1.2.3", stateSchema: 1 }, HELPER, REQUIREMENT)).rejects.toThrow("is not the release its manifest names");
    const other = bundle(join(dir, "other", ".Surogate-1.2.4.staging"), "1.2.4", "com.example.other");
    await expect(checkStaged(other, app, release, HELPER, REQUIREMENT)).rejects.toThrow("is not Surogate's, signed whole");
    const older = bundle(join(dir, "older", ".Surogate-1.2.4.staging"), "1.2.4");
    const newer = bundle(join(dir, "newer", "Surogate.app"), "1.2.5");
    await expect(checkStaged(older, newer, release, HELPER, REQUIREMENT)).rejects.toThrow("Surogate 1.2.4 is not newer than the version installed, 1.2.5");
    writeFileSync(join(staged, "Contents/Resources/app/package.json"), `${JSON.stringify({ ...release, changed: true })}\n`);
    await expect(checkStaged(staged, app, release, HELPER, REQUIREMENT)).rejects.toThrow("is not Surogate's, signed whole");
  });

  it("refuses an apply before it says it waits where it may not take its requirement, so the app stays up and says why", () => {
    const app = bundle(join(dir, "Surogate.app"), "1.2.3");
    const staged = bundle(join(dir, ".Surogate-1.2.4.staging"), "1.2.4");
    const quitting = spawn("/bin/sleep", ["30"], { stdio: "ignore" });
    try {
      const done = spawnSync(HELPER, ["apply", "--wait", String(quitting.pid), "--staged", staged, "--app", app, "--version", "1.2.4"], { encoding: "utf8", env: { PATH: "/usr/bin:/bin" }, timeout: 20_000 });
      expect(done).toMatchObject({ status: 3, stdout: "", stderr: "this helper is not signed, and no test requirement is named, so nothing was installed\n" });
    } finally {
      quitting.kill();
    }
  });

  it("swaps the bundle in once the app has quit, keeps the version it replaced beside it, and keeps how it ended for the next start", { timeout: 60_000 }, async () => {
    const app = bundle(join(dir, "Surogate.app"), "1.2.3");
    const before = statSync(app).ino;
    const folder = join(dir, "cache", "1.2.4");
    mkdirSync(folder, { recursive: true });
    writeFileSync(join(folder, "manifest.json"), `${JSON.stringify({ version: "1.2.4", stateSchema: 1 })}\n`);
    bundle(join(dir, "built", "Surogate.app"), "1.2.4");
    const files = { manifest: join(folder, "manifest.json"), signature: join(folder, "manifest.json.sig"), tarball: tarball(join(dir, "built"), join(folder, "release.tar.gz")) };
    // The app: a process that quits a moment after the helper waits.
    const quitting = spawn("/bin/sleep", ["2"], { stdio: "ignore" });
    expect(await updateRun(HELPER, app, quitting.pid!, REQUIREMENT)(files)).toEqual({ code: 0, said: "", restarts: true });
    await expect.poll(() => readFileSync(join(folder, "result.json"), "utf8"), { timeout: 30_000 }).toMatch(/"code":0/);
    expect(JSON.parse(readFileSync(join(folder, "result.json"), "utf8"))).toEqual({ version: "1.2.4", code: 0, said: "" });
    // The staged bundle has the app's name now, and the app's old tree the version's own beside it.
    expect(readFileSync(join(app, "Contents/Resources/app/package.json"), "utf8")).toContain('"version":"1.2.4"');
    expect(lstatSync(join(dir, ".Surogate-1.2.3.previous")).ino).toBe(before);
    expect(existsSync(join(dir, ".Surogate-1.2.4.staging"))).toBe(false);
  });
});
