// Updates inside the app: the signed latest.json of the base the app was installed from, checked
// in Node against the release keys the root helper lists, and a newer release's tarball
// downloaded into the user's cache, resumed. A local HTTP server serves the base.

import { createHash, generateKeyPairSync, type KeyObject, randomBytes, sign } from "node:crypto";
import { chmodSync, existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { type Applied, AS_ROOT, helperRun, installedUpdates, newer, releaseKeys, updateLine, Updates, type UpdatesOptions } from "../src/shell/updates.js";

const sha256 = (data: Buffer) => createHash("sha256").update(data).digest("hex");
const keys = generateKeyPairSync("ed25519");
const next = generateKeyPairSync("ed25519");
const pem = (key: KeyObject) => key.export({ type: "spki", format: "pem" }).toString().trim();
// The root helper: the install script, with *trusted* in its release keys' place, which installs *channel*'s releases.
const SCRIPT = fileURLToPath(new URL("../release/install.sh", import.meta.url));
const helperWith = (trusted: KeyObject[], channel = "stable") => readFileSync(SCRIPT, "utf8")
  .replace(/RELEASE_KEYS=\(\n[^)]*\)/, `RELEASE_KEYS=(\n${trusted.map((key) => `    '${pem(key)}'`).join("\n")}\n  )`)
  .replace(/^  CHANNEL=stable$/m, `  CHANNEL=${channel}`);

let dir: string;
let server: Server;
let base: string;
let served: Map<string, Buffer>;
let heard: Array<{ url: string; range: string | undefined }>;
let answer: (request: IncomingMessage, response: ServerResponse, body: Buffer) => void;

const ranged = (request: IncomingMessage, response: ServerResponse, body: Buffer) => {
  const from = Number(/^bytes=(\d+)-$/.exec(request.headers.range ?? "")?.[1] ?? 0);
  if (from > 0) response.writeHead(206, { "content-range": `bytes ${from}-${body.length - 1}/${body.length}`, "content-length": body.length - from });
  else response.writeHead(200, { "content-length": body.length });
  response.end(body.subarray(from));
};

// Release *version* on the base, as the release job publishes it: its tarball, and latest.json
// signed by *key*, with *fields* in place of its own. The tarball.
function publish(version: string, fields: Record<string, unknown> = {}, key: KeyObject = keys.privateKey): Buffer {
  const tarball = gzipSync(randomBytes(300_000));
  const url = `releases/${version}/surogate-desktop-${version}-linux-x64.tar.gz`;
  const manifest = Buffer.from(`${JSON.stringify({
    version, channel: "stable", platform: "linux", arch: "x64", url, sha256: sha256(tarball), size: tarball.length, stateSchema: 1, ...fields,
  })}\n`);
  served.set(`/desktop/${url}`, tarball);
  served.set("/desktop/latest.json", manifest);
  served.set("/desktop/latest.json.sig", sign(null, manifest, key));
  return tarball;
}

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), "updates-"));
  served = new Map();
  heard = [];
  answer = ranged;
  server = createServer((request, response) => {
    heard.push({ url: request.url ?? "", range: request.headers.range });
    const body = served.get(request.url ?? "");
    if (!body) return void response.writeHead(404).end();
    answer(request, response, body);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  writeFileSync(join(dir, "install.json"), JSON.stringify({ base, channel: "stable" }));
  writeFileSync(join(dir, "surogate-apply-update"), helperWith([keys.publicKey]));
});

afterEach(async () => {
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
  rmSync(dir, { recursive: true, force: true });
});

const cache = () => join(dir, "cache", "surogate", "updates");
const updates = (options: Partial<UpdatesOptions> = {}, changed = () => {}) => new Updates({
  version: "1.2.3", record: join(dir, "install.json"), rootOwned: false, helper: join(dir, "surogate-apply-update"), installed: null,
  cache: cache(), fetch: (url, init) => fetch(url, init), signal: new AbortController().signal, apply: () => Promise.resolve({ code: 0, said: "" }), ...options,
}, changed);

describe("an update", () => {
  it("finds a newer signed release for this computer and its channel, downloads it into the user's cache, and is then available", async () => {
    const tarball = publish("1.2.4");
    const told: string[] = [];
    const found: Updates = updates({}, () => told.push(found.state.state));
    await found.check();
    const folder = join(cache(), "1.2.4");
    expect(told).toEqual(["available"]);
    expect(found.state).toEqual({
      state: "available", version: "1.2.4",
      files: { manifest: join(folder, "manifest.json"), signature: join(folder, "manifest.json.sig"), tarball: join(folder, "release.tar.gz") },
    });
    // The helper is handed the exact bytes it verifies again, as root.
    expect(readFileSync(join(folder, "manifest.json")).equals(served.get("/desktop/latest.json")!)).toBe(true);
    expect(readFileSync(join(folder, "manifest.json.sig")).equals(served.get("/desktop/latest.json.sig")!)).toBe(true);
    expect(readFileSync(join(folder, "release.tar.gz")).equals(tarball)).toBe(true);
    expect(readdirSync(folder).sort()).toEqual(["manifest.json", "manifest.json.sig", "release.tar.gz"]);
    expect(statSync(folder).mode & 0o777).toBe(0o700);
    // Checked again, as every 6 hours: what is here is not downloaded again.
    heard = [];
    await found.check();
    expect(heard.map(({ url }) => url)).toEqual(["/desktop/latest.json", "/desktop/latest.json.sig"]);
    expect(found.state.state).toBe("available");
  });

  it("takes none it cannot trust or does not need, says why, and downloads no tarball for it", async () => {
    const unsigned = `${base}/desktop/latest.json is not signed by Surogate's release key`;
    const noRelease = `${base}/desktop/latest.json is not a release of Surogate Desktop for this computer`;
    // Each offer, and why it is not taken: nothing to say of one that is signed and not newer.
    const offers: Array<[string, Record<string, unknown>, string | null, KeyObject?]> = [
      ["1.2.4", {}, unsigned, next.privateKey],
      ["1.2.4", { channel: "beta" }, noRelease],
      ["1.2.4", { arch: "arm64" }, noRelease],
      ["1.2.4", { platform: "darwin" }, noRelease],
      ["1.2.4", { url: "https://elsewhere.example/surogate.tar.gz" }, noRelease],
      ["1.2.4", { sha256: "A".repeat(64) }, noRelease],
      // Its size and its state schema, each a whole number in the helper's own bounds.
      ["1.2.4", { size: 0 }, noRelease],
      ["1.2.4", { size: 1.5 }, noRelease],
      ["1.2.4", { size: 1e15 }, noRelease],
      ["1.2.4", { stateSchema: 0 }, noRelease],
      ["1.2.4", { stateSchema: 1e15 }, noRelease],
      ["1.2.3", {}, null],
      ["1.1.9", {}, null],
    ];
    for (const [version, fields, why, key] of offers) {
      publish(version, fields, key);
      heard = [];
      const found = updates();
      if (why) await expect(found.check(), `${version} ${JSON.stringify(fields)}`).rejects.toThrow(why);
      else await found.check();
      expect(found.state, `${version} ${JSON.stringify(fields)}`).toEqual({ state: "none" });
      expect(heard.map(({ url }) => url)).toEqual(["/desktop/latest.json", "/desktop/latest.json.sig"]);
    }
    // A manifest is one short line, and its signature 64 bytes: more in the place of either is refused unread.
    const refused = updates();
    publish("1.2.4");
    served.set("/desktop/latest.json.sig", randomBytes(65));
    await expect(refused.check()).rejects.toThrow(`${base}/desktop/latest.json.sig is not Surogate's: it is larger than 64 bytes`);
    served.set("/desktop/latest.json", randomBytes(64 * 1024));
    await expect(refused.check()).rejects.toThrow(`${base}/desktop/latest.json is not Surogate's: it is larger than 4096 bytes`);
    // A base that has none says so, and what it sent in its place is not read as one.
    served.delete("/desktop/latest.json");
    await expect(refused.check()).rejects.toThrow(`${new URL(base).host} answered 404 for ${base}/desktop/latest.json`);
    expect(refused.state).toEqual({ state: "none" });
  });

  it("stops asking its base when the app quits", async () => {
    publish("1.2.4");
    // A base that takes the request, and never answers it.
    answer = () => {};
    const quit = new AbortController();
    const checked = updates({ signal: quit.signal }).check();
    await expect.poll(() => heard.length, { timeout: 5_000 }).toBe(1);
    quit.abort(new Error("Surogate quit"));
    await expect(checked).rejects.toThrow("Surogate quit");
  });

  it("trusts each release key the root helper lists, as across a rotation, and no key anywhere else in it", async () => {
    const helper = join(dir, "surogate-apply-update");
    writeFileSync(helper, helperWith([keys.publicKey, next.publicKey]));
    expect(releaseKeys(helper)).toHaveLength(2);
    publish("1.2.4", {}, next.privateKey);
    const found = updates();
    await found.check();
    expect(found.state.state).toBe("available");
    // A key the script holds outside its list, as a rotation's notes might, signs nothing it takes.
    writeFileSync(helper, `${helperWith([keys.publicKey])}\nRETIRED_KEY='${pem(next.publicKey)}'\n`);
    expect(releaseKeys(helper)).toHaveLength(1);
    await expect(updates().check()).rejects.toThrow(`${base}/desktop/latest.json is not signed by Surogate's release key`);
    // An entry that is no Ed25519 key, or no key at all, is skipped, as the helper skips it.
    const curve = generateKeyPairSync("ec", { namedCurve: "P-256" }).publicKey;
    writeFileSync(helper, helperWith([keys.publicKey, curve]).replace("RELEASE_KEYS=(\n", "RELEASE_KEYS=(\n    '-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----'\n"));
    expect(releaseKeys(helper)).toHaveLength(1);
    writeFileSync(helper, helperWith([]));
    expect(() => releaseKeys(helper)).toThrow(`${helper} trusts no release key`);
    // An installed app reads the list of no helper that another than root may write: this one is the test's.
    expect(() => releaseKeys(helper, true)).toThrow(`${helper} is not the install script's: only root may write it`);
    // The repository's own list, read as the installed app reads its version's.
    expect(releaseKeys(SCRIPT)).toHaveLength(1);
  });

  it("resumes a download that stopped, with a Range, and keeps nothing of a tarball that is not the one its manifest names", async () => {
    const tarball = publish("1.2.4");
    const cut = 100_000;
    answer = (request, response, body) => {
      if (request.headers.range || !request.url?.endsWith(".tar.gz")) return ranged(request, response, body);
      // The connection drops partway through, as a laptop's Wi-Fi does.
      response.writeHead(200, { "content-length": body.length });
      response.write(body.subarray(0, cut));
      setTimeout(() => response.socket?.destroy(), 200);
    };
    const found = updates();
    await expect(found.check()).rejects.toThrow(/^the download of Surogate 1\.2\.4 stopped: /);
    expect(found.state).toEqual({ state: "none" });
    expect(statSync(join(cache(), "1.2.4", "release.tar.gz.partial")).size).toBe(cut);
    await found.check();
    expect(readFileSync(join(cache(), "1.2.4", "release.tar.gz")).equals(tarball)).toBe(true);
    expect(heard.filter(({ url }) => url.endsWith(".tar.gz")).map(({ range }) => range)).toEqual([undefined, `bytes=${cut}-`]);
    answer = ranged;
    // One changed since it was downloaded, as by a process of the user's, is downloaded again.
    writeFileSync(join(cache(), "1.2.4", "release.tar.gz"), randomBytes(tarball.length));
    await found.check();
    expect(readFileSync(join(cache(), "1.2.4", "release.tar.gz")).equals(tarball)).toBe(true);
    // A tarball that is not the manifest's is not kept.
    served.set("/desktop/releases/1.2.4/surogate-desktop-1.2.4-linux-x64.tar.gz", gzipSync(randomBytes(300_000)));
    rmSync(cache(), { recursive: true });
    await expect(updates().check()).rejects.toThrow("Surogate 1.2.4 was not the file the app expects");
    expect(existsSync(join(cache(), "1.2.4", "release.tar.gz"))).toBe(false);
    expect(existsSync(join(cache(), "1.2.4", "release.tar.gz.partial"))).toBe(false);
  });

  it("keeps one release's download: an older offer's goes once a newer one is here, and all once none is newer", async () => {
    publish("1.2.4");
    const found = updates();
    await found.check();
    publish("1.2.5");
    await found.check();
    expect(found.state).toMatchObject({ state: "available", version: "1.2.5" });
    expect(readdirSync(cache())).toEqual(["1.2.5"]);
    // Updated to it: the next start's check finds nothing newer than itself.
    const restarted = updates({ version: "1.2.5" });
    await restarted.check();
    expect(restarted.state).toEqual({ state: "none" });
    expect(existsSync(cache())).toBe(false);
  });

  it("says a newer version is installed for every user already, as another user's update leaves it, and downloads nothing", async () => {
    publish("1.2.4");
    writeFileSync(join(dir, "release.json"), `${JSON.stringify({ version: "1.2.4" })}\n`);
    const found = updates({ installed: join(dir, "release.json") });
    await found.check();
    expect(found.state).toEqual({ state: "installed", version: "1.2.4" });
    expect(heard).toEqual([]);
  });

  it("compares versions as numbers, part by part", () => {
    expect([newer("1.2.10", "1.2.9"), newer("2.0.0", "1.99.99"), newer("1.2.3", "1.2.3"), newer("1.2.3", "1.3.0"), newer("1.2", "1.1.0")]).toEqual([true, true, false, false, false]);
  });
});

describe("the install record an update reads", () => {
  it("takes the channel the install script recorded beside its base, where it is the one the root helper installs", async () => {
    writeFileSync(join(dir, "install.json"), JSON.stringify({ base }));
    publish("1.2.4");
    await expect(updates().check()).rejects.toThrow(`${join(dir, "install.json")} names no update channel`);
    // A record that names another channel than the helper installs: nothing is asked of the base,
    // as the helper would refuse what came.
    writeFileSync(join(dir, "install.json"), JSON.stringify({ base, channel: "beta" }));
    publish("1.2.4", { channel: "beta" });
    heard = [];
    await expect(updates().check()).rejects.toThrow(`${join(dir, "install.json")} names the channel beta, and ${join(dir, "surogate-apply-update")} installs stable`);
    expect(heard).toEqual([]);
    writeFileSync(join(dir, "surogate-apply-update"), helperWith([keys.publicKey], "beta"));
    const found = updates();
    await found.check();
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
  });

  it("is root's own in an installed app, as the helper is: a record another may write names no base", async () => {
    publish("1.2.4");
    await expect(updates({ rootOwned: true }).check()).rejects.toThrow(`${join(dir, "install.json")} is not the install script's: only root may write it`);
    expect(heard).toEqual([]);
  });
});

describe("an installed app's updates", () => {
  it("read what the install script leaves, each root's alone: its record, the helper pkexec runs, and the installed version's mark", () => {
    const { signal } = new AbortController();
    expect(installedUpdates("1.2.3", "/home/user/.cache/surogate/updates", fetch, signal)).toMatchObject({
      version: "1.2.3", record: "/etc/surogate/install.json", rootOwned: true, helper: "/opt/surogate/bin/surogate-apply-update",
      installed: "/opt/surogate/current/release.json", cache: "/home/user/.cache/surogate/updates", fetch, signal,
    });
    // Where the install script itself puts each.
    const script = readFileSync(SCRIPT, "utf8").split("\n");
    for (const line of ["  ROOT=/opt/surogate", "  RECORD=/etc/surogate/install.json", '  HELPER="$ROOT/bin/surogate-apply-update"']) expect(script).toContain(line);
  });
});

describe("installing an update", () => {
  it("hands the root helper the files as downloaded, and once it has applied them the update is installed", async () => {
    publish("1.2.4");
    const handed: unknown[] = [];
    const told: string[] = [];
    const found: Updates = updates({ apply: (files) => {
      handed.push(files);
      told.push(found.state.state);
      return Promise.resolve({ code: 0, said: "Surogate Desktop: 1.2.4 is installed" });
    } });
    await found.check();
    const { files } = found.state as { files: unknown };
    await found.install();
    expect(handed).toEqual([files]);
    expect(told).toEqual(["installing"]);
    expect(found.state).toEqual({ state: "installed", version: "1.2.4" });
    // Installed, a later check offers nothing more, and asks its base nothing: the restart runs it.
    heard = [];
    await found.check();
    expect(heard).toEqual([]);
    expect(found.state).toEqual({ state: "installed", version: "1.2.4" });
  });

  it("says an administrator is needed when polkit refuses, and the helper's words when it fails; the files stay for another try", async () => {
    publish("1.2.4");
    let answer = { code: 126 as number | null, said: "" };
    const found = updates({ apply: () => Promise.resolve(answer) });
    await found.check();
    const { files } = found.state as { files: unknown };
    await found.install();
    expect(found.state).toEqual({ state: "refused", version: "1.2.4", files });
    // No authentication agent, or none who could answer: as pkexec says it.
    answer = { code: 127, said: "Error executing command as another user: Not authorized" };
    await found.install();
    expect(found.state).toEqual({ state: "refused", version: "1.2.4", files });
    answer = { code: 1, said: "tar: oops\nSurogate Desktop: the release's archive could not be unpacked" };
    await found.install();
    expect(found.state).toEqual({ state: "failed", version: "1.2.4", files, why: "the release's archive could not be unpacked" });
    answer = { code: null, said: "spawn /usr/bin/pkexec ENOENT" };
    await found.install();
    expect(found.state).toEqual({ state: "failed", version: "1.2.4", files, why: "spawn /usr/bin/pkexec ENOENT" });
    // One that said nothing is said by how it ended.
    answer = { code: 3, said: "" };
    await found.install();
    expect(found.state).toEqual({ state: "failed", version: "1.2.4", files, why: "its helper exited 3" });
    answer = { code: null, said: "tar: oops\nits helper was stopped by SIGTERM" };
    await found.install();
    expect(found.state).toEqual({ state: "failed", version: "1.2.4", files, why: "its helper was stopped by SIGTERM" });
    expect(existsSync((files as { tarball: string }).tarball)).toBe(true);
    answer = { code: 0, said: "" };
    await found.install();
    expect(found.state).toEqual({ state: "installed", version: "1.2.4" });
  });

  it("runs the helper as it is given, with --apply and the files' paths, and none of the app's environment but a PATH", async () => {
    const log = join(dir, "ran");
    writeFileSync(join(dir, "helper"), `#!/bin/sh\nprintf '%s\\n' "$@" >${log}\nenv | sort >>${log}\necho 'Surogate Desktop: it said this' >&2\nexit 3\n`);
    chmodSync(join(dir, "helper"), 0o755);
    const files = { manifest: "/c/m.json", signature: "/c/m.json.sig", tarball: "/c/r.tar.gz" };
    expect(await helperRun([join(dir, "helper"), "--first"])(files)).toEqual({ code: 3, said: "Surogate Desktop: it said this" });
    const ran = readFileSync(log, "utf8").split("\n");
    expect(ran.slice(0, 5)).toEqual(["--first", "--apply", "/c/m.json", "/c/m.json.sig", "/c/r.tar.gz"]);
    expect(ran.slice(5).filter((line) => /^[A-Z_]+=/.test(line) && !/^(PWD|SHLVL|_)=/.test(line))).toEqual(["PATH=/usr/bin:/bin"]);
    expect(await helperRun([join(dir, "missing")])(files)).toMatchObject({ code: null, said: expect.stringContaining("ENOENT") });
    // One that a signal ends has no exit code: how it ended is the last of what is said of it.
    writeFileSync(join(dir, "stopped"), "#!/bin/sh\necho 'Surogate Desktop: it began' >&2\nkill -KILL $$\n");
    chmodSync(join(dir, "stopped"), 0o755);
    expect(await helperRun([join(dir, "stopped")])(files)).toEqual({ code: null, said: "Surogate Desktop: it began\nits helper was stopped by SIGKILL" });
  });

  it("runs the helper under polkit in an installed app: pkexec by its whole path, with no agent of its own on a terminal, on the helper the install script's action names", () => {
    expect(AS_ROOT).toEqual(["/usr/bin/pkexec", "--disable-internal-agent", "/opt/surogate/bin/surogate-apply-update"]);
    expect(readFileSync(SCRIPT, "utf8")).toContain('<annotate key="org.freedesktop.policykit.exec.path">/opt/surogate/bin/surogate-apply-update</annotate>');
  });

  it("leaves an install alone that began while a check was still downloading a later release: its line, and the files its helper reads", async () => {
    publish("1.2.4");
    let applied!: (answer: Applied) => void;
    const told: string[] = [];
    const found: Updates = updates({ apply: () => new Promise((resolve) => {
      applied = resolve;
    }) }, () => told.push(`${found.state.state} ${"version" in found.state ? found.state.version : ""}`.trim()));
    await found.check();
    // A later release, whose tarball the base is slow to send.
    publish("1.2.5");
    let send!: () => void;
    const held = new Promise<void>((resolve) => {
      send = resolve;
    });
    answer = (request, response, body) => {
      if (request.url?.endsWith(".tar.gz")) void held.then(() => ranged(request, response, body));
      else ranged(request, response, body);
    };
    const checked = found.check();
    await expect.poll(() => heard.some(({ url }) => url === "/desktop/releases/1.2.5/surogate-desktop-1.2.5-linux-x64.tar.gz"), { timeout: 5_000 }).toBe(true);
    const installing = found.install();
    send();
    await checked;
    // The check ended while the helper ran: the line still says what installs, and that release's files are where the helper was handed them.
    expect(found.state).toEqual({ state: "installing", version: "1.2.4" });
    expect(readdirSync(cache()).sort()).toEqual(["1.2.4", "1.2.5"]);
    expect(readdirSync(join(cache(), "1.2.4")).sort()).toEqual(["manifest.json", "manifest.json.sig", "release.tar.gz"]);
    applied({ code: 0, said: "" });
    await installing;
    expect(told).toEqual(["available 1.2.4", "installing 1.2.4", "installed 1.2.4"]);
  });

  it("leaves an install alone that began while a check was still asking its base: the files its helper reads stay, whatever the base then answers", async () => {
    publish("1.2.4");
    let applied!: (answer: Applied) => void;
    const found = updates({ apply: () => new Promise((resolve) => {
      applied = resolve;
    }) });
    await found.check();
    // The base is slow to answer, and then names nothing newer: such a check clears the cache.
    publish("1.2.3");
    let send!: () => void;
    const held = new Promise<void>((resolve) => {
      send = resolve;
    });
    answer = (request, response, body) => void held.then(() => ranged(request, response, body));
    heard = [];
    const checked = found.check();
    await expect.poll(() => heard.length, { timeout: 5_000 }).toBe(1);
    const installing = found.install();
    send();
    await checked;
    expect(found.state).toEqual({ state: "installing", version: "1.2.4" });
    expect(readdirSync(join(cache(), "1.2.4")).sort()).toEqual(["manifest.json", "manifest.json.sig", "release.tar.gz"]);
    applied({ code: 0, said: "" });
    await installing;
    expect(found.state).toEqual({ state: "installed", version: "1.2.4" });
  });

  it("says each state in the sidebar, with the button that acts on it", () => {
    const files = { manifest: "m", signature: "s", tarball: "t" };
    expect([
      updateLine({ state: "none" }),
      updateLine({ state: "available", version: "1.2.4", files }),
      updateLine({ state: "installing", version: "1.2.4" }),
      updateLine({ state: "refused", version: "1.2.4", files }),
      updateLine({ state: "failed", version: "1.2.4", files, why: "the release's archive could not be unpacked" }),
      updateLine({ state: "installed", version: "1.2.4" }),
    ]).toEqual([
      null,
      { text: "Update available: Surogate 1.2.4", button: "Restart to update" },
      { text: "Installing Surogate 1.2.4…", button: null },
      { text: "An administrator needs to install this update.", button: "Try again" },
      { text: "Surogate could not install its update: the release's archive could not be unpacked", button: "Try again" },
      { text: "Surogate 1.2.4 is installed.", button: "Restart" },
    ]);
  });
});
