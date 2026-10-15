// Updates as a Mac's app has them (src/shell/updates.ts and vm/image.ts, their rows for macOS), here on any OS: the
// channel is the install record's alone, and the release keys the list beside the app, the bundle's own; a newer
// release that this user may not install where the app is says an administrator needs to, with the disk image to
// download, and nothing of it is downloaded; one that nobody can install where the app runs says why; the app quits
// for its helper, and how the helper's apply ended is said at the next start, as it kept it; the installed version is
// the bundle's own package; and the install record is an administrator's alone, without which the public base and
// channel built into the app are used.

import { randomBytes, sign } from "node:crypto";
import { chmodSync, existsSync, linkSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

import { describe, expect, it } from "vitest";

import type { UpdatesOptions } from "../src/shell/updates.js";
import { importedOn } from "./platforms.js";
import { keys, pem, servedBase, sha256 } from "./updates-base.js";

const base = servedBase();
// The updater and the install record's reader, imported as a Mac's app imports them.
const mac = () => importedOn("darwin", async () => ({ updates: await import("../src/shell/updates.js"), image: await import("../src/vm/image.js") }));
const linux = () => importedOn("linux", async () => ({ updates: await import("../src/shell/updates.js"), image: await import("../src/vm/image.js") }));

// Release *version* for macOS on the base, as the release job publishes it, with *fields* in place of its own.
function publish(version: string, fields: Record<string, unknown> = {}): Buffer {
  const tarball = gzipSync(randomBytes(300_000));
  const url = `releases/${version}/darwin-arm64/surogate-desktop-${version}-darwin-arm64.tar.gz`;
  base.served.set(`/desktop/${url}`, tarball);
  const manifest = Buffer.from(`${JSON.stringify({
    version, channel: "stable", platform: "darwin", arch: "arm64", url, sha256: sha256(tarball), size: tarball.length, stateSchema: 1, ...fields,
  })}\n`);
  base.served.set("/desktop/darwin-arm64/latest.json", manifest);
  base.served.set(`/desktop/releases/${version}/darwin-arm64/manifest.json.sig`, sign(null, manifest, keys.privateKey));
  return tarball;
}

// The release keys beside a Mac's app, Contents/Resources/release-keys: the install script's list, and nothing else.
function releaseKeys(): string {
  const path = join(base.dir, "release-keys");
  rmSync(path, { force: true });
  writeFileSync(path, `RELEASE_KEYS=(\n    '${pem(keys.publicKey)}'\n  )\n`);
  chmodSync(path, 0o644);
  return path;
}

// A Mac's app at 1.2.3 that reads the test's record and the list beside it, with *options* in place of its own.
async function onMac(options: Partial<UpdatesOptions> = {}, changed = () => {}) {
  const { updates } = await mac();
  return new updates.Updates({
    version: "1.2.3", record: base.record, rootOwned: false, helper: releaseKeys(), installed: null, cache: base.cache(), fetch: (url, init) => fetch(url, init),
    signal: new AbortController().signal, apply: () => Promise.resolve({ code: 0, said: "" }), target: "darwin-arm64", ...options,
  }, changed);
}

const DISK_IMAGE = "darwin-arm64/Surogate.dmg";

describe("a Mac's update", () => {
  it("takes the channel from the install record alone, and the release keys from the list beside the app, which names none", async () => {
    publish("1.2.4");
    const found = await onMac();
    await found.check();
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
    // Linux's app asks its root helper for the channel it installs, which a list of keys alone does not name.
    const { updates } = await linux();
    const onLinux = new updates.Updates({
      version: "1.2.3", record: base.record, rootOwned: false, helper: releaseKeys(), installed: null, cache: base.cache(), fetch: (url, init) => fetch(url, init),
      signal: new AbortController().signal, apply: () => Promise.resolve({ code: 0, said: "" }), target: "darwin-arm64",
    });
    await expect(onLinux.check()).rejects.toThrow("installs no channel it names");
  });

  it("says an administrator needs to install it where this user may not change the app's folder, with the disk image to download, and downloads nothing", async () => {
    publish("1.2.4");
    let held: { download: string } | null = null;
    const told: string[] = [];
    const found = await onMac({ held: () => held }, () => told.push(found.state.state));
    // Downloaded for an earlier offer, before the folder was another's.
    await found.check();
    expect(existsSync(join(base.cache(), "1.2.4"))).toBe(true);
    held = { download: DISK_IMAGE };
    base.heard = [];
    await found.check();
    expect(found.state).toEqual({ state: "administrator", version: "1.2.4", download: `${base.url}/desktop/${DISK_IMAGE}` });
    expect(told).toEqual(["available", "administrator"]);
    expect(base.heard.map(({ url }) => url)).toEqual(["/desktop/darwin-arm64/latest.json", "/desktop/releases/1.2.4/darwin-arm64/manifest.json.sig"]);
    // Nothing is kept that nobody here would install.
    expect(existsSync(base.cache())).toBe(false);
    const { updates } = await mac();
    expect(updates.updateLine(found.state)).toEqual({ text: "An administrator needs to install this update.", button: "Download" });
    // Its button downloads the disk image (main.ts), and installs nothing.
    await found.install();
    expect(found.state.state).toBe("administrator");
    // The folder this user's again: the release is downloaded and offered.
    held = null;
    await found.check();
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
  });

  it("says why where nobody can install it where the app runs, and downloads nothing", async () => {
    publish("1.2.4");
    const why = "/Volumes/Surogate is on a volume that cannot be written to. Copy Surogate to Applications, and open it from there";
    const found = await onMac({ held: () => ({ why }) });
    await found.check();
    expect(found.state).toEqual({ state: "held", version: "1.2.4", why });
    expect(existsSync(base.cache())).toBe(false);
    const { updates } = await mac();
    expect(updates.updateLine(found.state)).toEqual({ text: `Surogate cannot install 1.2.4 where it runs: ${why}`, button: null });
  });

  it("asks nothing of where the app is while nothing newer is out", async () => {
    publish("1.2.3");
    let asked = 0;
    const found = await onMac({ held: () => (asked += 1, { download: DISK_IMAGE }) });
    await found.check();
    expect([found.state, asked]).toEqual([{ state: "none" }, 0]);
  });

  it("lets the app quit for its helper once it is staged and checked, and says at the next start how the helper's apply ended, as it kept it", async () => {
    publish("1.2.4");
    const logged: string[] = [];
    const app = () => onMac({ apply: () => Promise.resolve({ code: 0, said: "", restarts: true }), log: (words) => logged.push(words) });
    const first = await app();
    await first.check();
    const { files } = first.state as { files: { tarball: string } };
    expect(files.tarball).toBe(join(base.cache(), "1.2.4", "release.tar.gz"));
    await first.install();
    expect(first.state).toEqual({ state: "restarting", version: "1.2.4" });
    // Restarting, a check asks nothing more: the helper swaps the bundle once the app has quit.
    base.heard = [];
    await first.check();
    expect(base.heard).toEqual([]);
    // Started again at its old version: the helper kept how its apply ended, said once and gone.
    const kept = join(base.cache(), "1.2.4", "result.json");
    for (const [code, why] of [
      [3, "it is not a release of Surogate that this app takes, so nothing was installed"],
      [4, "Surogate.app could not be replaced by it, and nothing was changed"],
      [5, "Surogate did not quit in time for it, and nothing was changed"],
      [9, "its install ended 9"],
    ] as const) {
      writeFileSync(kept, `${JSON.stringify({ version: "1.2.4", code, said: `the helper's words for ${code}` })}\n`);
      const again = await app();
      await again.check();
      expect(again.state).toEqual({ state: "failed", version: "1.2.4", files, why });
      expect(existsSync(kept)).toBe(false);
      expect(logged.at(-1)).toBe(`Surogate 1.2.4 was not installed: the helper's words for ${code}`);
    }
    // One the helper cut short, or of another version, is no one's: the update is offered as it is.
    for (const written of ["", '{"version":"1.2.4","co', `${JSON.stringify({ version: "1.2.5", code: 4 })}\n`]) {
      writeFileSync(kept, written);
      const later = await app();
      await later.check();
      expect(later.state, written).toEqual({ state: "available", version: "1.2.4", files });
    }
  });

  it("reads the release keys beside the app as the bundle's own: a regular file of one name that no one else may write, and no link", async () => {
    const { updates } = await mac();
    const path = releaseKeys();
    expect(updates.releaseKeys(path, true)).toHaveLength(1);
    chmodSync(path, 0o664);
    expect(() => updates.releaseKeys(path, true)).toThrow(`${path} is not Surogate's own: others may write it`);
    chmodSync(path, 0o644);
    linkSync(path, join(base.dir, "second-name"));
    expect(() => updates.releaseKeys(path, true)).toThrow(`${path} is not Surogate's own: it is no file of its own`);
    const link = join(base.dir, "keys-link");
    symlinkSync(releaseKeys(), link);
    expect(() => updates.releaseKeys(link, true)).toThrow(`${link} is not Surogate's own: it is a link`);
    mkdirSync(join(base.dir, "keys-folder"));
    expect(() => updates.releaseKeys(join(base.dir, "keys-folder"), true)).toThrow("is not Surogate's own: it is no file of its own");
    // Linux's app takes no list that root does not own.
    const { updates: onLinux } = await linux();
    expect(() => onLinux.releaseKeys(releaseKeys(), true)).toThrow("is not the install script's: only root may write it");
  });

  it("says a newer version is installed where the bundle's own package names one, as another user's update leaves it", async () => {
    publish("1.2.4");
    const mark = join(base.dir, "package.json");
    writeFileSync(mark, `${JSON.stringify({ name: "surogate-desktop", version: "1.2.9", stateSchema: 2 }, null, 2)}\n`);
    chmodSync(mark, 0o644);
    const found = await onMac({ installed: mark, rootOwned: true, record: join(base.dir, "no-record.json") });
    await found.check();
    expect(found.state).toEqual({ state: "installed", version: "1.2.9" });
    // Not one that others may write: the check goes on to the base, as for no mark at all.
    chmodSync(mark, 0o666);
    const other = await onMac({ installed: mark, rootOwned: true, record: join(base.dir, "no-record.json"), fetch: () => Promise.reject(new Error("offline")) });
    await expect(other.check()).rejects.toThrow("could not reach surogate.ai: offline");
    expect(other.state).toEqual({ state: "none" });
  });

  it("takes the install record only as root's word, and without one the public base and channel built into the app", async () => {
    const { image, updates } = await mac();
    const missing = join(base.dir, "no-record.json");
    expect(image.BUILT_IN).toEqual({ base: "https://surogate.ai", channel: "stable" });
    expect(image.installBase(missing, true)).toBe("https://surogate.ai");
    // A development build's record is its test's: one that is not there names nothing.
    expect(() => image.installBase(missing, false)).toThrow("does not know where it was installed from");
    // One that is there, and that another may write, is no administrator's word: nothing is asked of any base.
    expect(() => image.installBase(base.record, true)).toThrow(`${base.record} is not an administrator's: only root may write it`);
    const broken = await onMac({ rootOwned: true });
    await expect(broken.check()).rejects.toThrow(`${base.record} is not an administrator's: only root may write it`);
    expect(updates.updateLine(broken.state)).toEqual({ text: "Surogate cannot update itself. Install Surogate again.", button: null });
    // Without one, the public base's newest release of the public channel: here, the test's base in its place.
    const asked: string[] = [];
    const fromPublic: UpdatesOptions["fetch"] = (url, init) => (asked.push(url), fetch(url.replace("https://surogate.ai", base.url), init));
    publish("1.2.4");
    const found = await onMac({ rootOwned: true, record: missing, fetch: fromPublic });
    await found.check();
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
    expect(asked[0]).toBe("https://surogate.ai/desktop/darwin-arm64/latest.json");
    publish("1.2.5", { channel: "beta" });
    await expect((await onMac({ rootOwned: true, record: missing, fetch: fromPublic })).check()).rejects.toThrow("is not a release of Surogate Desktop for this computer");
    // Linux's app has no base of its own: its install script always leaves a record.
    const { image: onLinux } = await linux();
    expect(onLinux.BUILT_IN).toBeNull();
    expect(() => onLinux.installBase(missing, true)).toThrow("Surogate was not installed by its install script");
  });
});
