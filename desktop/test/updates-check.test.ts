// What a check refuses, and what bounds it, beyond the offers of updates.test.ts: the manifest's
// exact bytes and its version's form, the helper's own channel, the base that does not answer, the
// quit while a release downloads, and the mark of the version installed now.

import { sign } from "node:crypto";
import { existsSync, lstatSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { CHECK_MS, signedRelease } from "../src/shell/updates.js";
import { helperWith, keys, next, ranged, servedBase, sha256 } from "./updates-base.js";

const base = servedBase();
const NO_RELEASE = "is not a release of Surogate Desktop for this computer";
const version = () => join(base.cache(), "1.2.4");
// A manifest of *fields*, as the release job writes one, and its signature by the test's key.
function signed(fields: Record<string, unknown> | string): [Buffer, Buffer] {
  const manifest = Buffer.from(typeof fields === "string" ? fields : `${JSON.stringify({
    version: "1.2.4", channel: "stable", platform: "linux", arch: "x64", url: "releases/1.2.4/surogate-desktop-1.2.4-linux-x64.tar.gz",
    sha256: sha256(Buffer.from("a release")), size: 9, stateSchema: 1, ...fields,
  })}\n`);
  return [manifest, sign(null, manifest, keys.privateKey)];
}

describe("a signed manifest", () => {
  it("is checked by its exact bytes: one changed after it was signed is refused, though it reads the same", async () => {
    base.publish("1.2.4");
    // As a mirror that writes its JSON again would serve it: the same release, in other bytes.
    const manifest = base.served.get("/desktop/latest.json")!;
    base.served.set("/desktop/latest.json", Buffer.from(manifest.toString().replace('{"version"', '{ "version"')));
    expect(JSON.parse(base.served.get("/desktop/latest.json")!.toString())).toEqual(JSON.parse(manifest.toString()));
    const found = base.updates();
    await expect(found.check()).rejects.toThrow(`${base.url}/desktop/latest.json is not signed by Surogate's release key`);
    expect(found.state).toEqual({ state: "none" });
    expect(base.heard.map(({ url }) => url)).toEqual(["/desktop/latest.json", "/desktop/latest.json.sig"]);
  });

  it("is taken when the first of two listed keys signed it, as the last is", async () => {
    writeFileSync(base.helper, helperWith([keys.publicKey, next.publicKey]));
    base.publish("1.2.4");
    const found = base.updates();
    await found.check();
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
  });

  it("names a version that is x.y.z in digits, and no other, whatever its url", () => {
    expect(signedRelease("URL", ...signed({}), [keys.publicKey], "stable").version).toBe("1.2.4");
    for (const named of ["1.2.4-beta", "1.2", "1.2.4.5", "v1.2.4", "../../../etc", "1.2.x", "", "1.2.04", "01.2.4", "1.02.4"]) {
      const offer = signed({ version: named, url: `releases/${named}/surogate-desktop-${named}-linux-x64.tar.gz` });
      expect(() => signedRelease("URL", ...offer, [keys.publicKey], "stable"), named).toThrow(`URL ${NO_RELEASE}`);
    }
  });

  it("is refused in the app's own words when it is JSON that is no release: null, a list, a number, a word", () => {
    for (const text of ["null\n", "[]\n", "7\n", '"1.2.4"\n', "true\n", "{}\n", "<html>"]) {
      expect(() => signedRelease("URL", ...signed(text), [keys.publicKey], "stable"), text).toThrow(`URL ${NO_RELEASE}`);
    }
  });

  it("is one line with its newline, of 4096 bytes at most, as its root helper reads one: no other form of the same release is offered", async () => {
    const [manifest] = signed({});
    const text = manifest.toString();
    expect(signedRelease("URL", ...signed(text), [keys.publicKey], "stable").version).toBe("1.2.4");
    const forms = [text.trimEnd(), text.replace(",", ",\n"), `${text}\n`, `\n${text}`, `${text} `, `${text.trimEnd()}${" ".repeat(4097 - text.length)}\n`];
    for (const form of forms) expect(() => signedRelease("URL", ...signed(form), [keys.publicKey], "stable"), JSON.stringify(form.slice(-12))).toThrow(`URL ${NO_RELEASE}`);
    // Through a check: the base's latest.json without its newline is no release, and nothing is downloaded.
    base.publish("1.2.4");
    base.offer(Buffer.from(base.served.get("/desktop/latest.json")!.toString().trimEnd()));
    const found = base.updates();
    await expect(found.check()).rejects.toThrow(`${base.url}/desktop/latest.json ${NO_RELEASE}`);
    expect(base.heard.map(({ url }) => url)).toEqual(["/desktop/latest.json", "/desktop/latest.json.sig"]);
  });
});

describe("a check", () => {
  it("reads the channel its helper installs from the line that sets it, and from no other that names one", async () => {
    // A comment that names a channel, and another variable whose name ends as the channel's does.
    writeFileSync(base.helper, `#!/bin/bash\n# CHANNEL=beta was this script's once\nOLD_CHANNEL=beta\n${helperWith([keys.publicKey])}`);
    base.publish("1.2.4");
    const found = base.updates();
    await found.check();
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
  });

  it("gives up on a base that does not answer for the manifest, or for its signature, once its bound has passed", { timeout: 8_000 }, async () => {
    base.publish("1.2.4");
    // A base that takes the request, and never answers it.
    base.answer = () => {};
    const began = Date.now();
    await expect(base.updates({ askMs: 300 }).check()).rejects.toThrow("The operation was aborted due to timeout");
    expect(base.heard.map(({ url }) => url)).toEqual(["/desktop/latest.json"]);
    base.answer = (request, response, body) => (request.url?.endsWith(".sig") ? undefined : ranged(request, response, body));
    await expect(base.updates({ askMs: 300 }).check()).rejects.toThrow("The operation was aborted due to timeout");
    expect(base.heard.map(({ url }) => url)).toEqual(["/desktop/latest.json", "/desktop/latest.json", "/desktop/latest.json.sig"]);
    expect(Date.now() - began).toBeLessThan(5_000);
  });

  it("stops a release's download when the app quits, and keeps what came for the next start", { timeout: 8_000 }, async () => {
    base.publish("1.2.4");
    // The tarball's first 100 000 bytes come, and no more.
    base.answer = (request, response, body) => {
      if (!request.url?.endsWith(".tar.gz")) return ranged(request, response, body);
      response.writeHead(200, { "content-length": body.length });
      response.write(body.subarray(0, 100_000));
    };
    const quit = new AbortController();
    const partial = join(version(), "release.tar.gz.partial");
    const checked = base.updates({ signal: quit.signal }).check();
    await expect.poll(() => (existsSync(partial) ? statSync(partial).size : 0), { timeout: 5_000 }).toBe(100_000);
    quit.abort(new Error("Surogate quit"));
    await expect(checked).rejects.toThrow("Surogate quit");
    expect(statSync(partial).size).toBe(100_000);
  });

  it("is one at a time: a second asked for while one runs is the same check, and the base is asked once", async () => {
    const tarball = base.publish("1.2.4");
    const found = base.updates();
    const [first, second] = [found.check(), found.check()];
    expect(second).toBe(first);
    await first;
    expect(base.heard.map(({ url }) => url)).toEqual(["/desktop/latest.json", "/desktop/latest.json.sig", base.tarballAt("1.2.4")]);
    expect(statSync(join(version(), "release.tar.gz")).size).toBe(tarball.length);
    // The next is a check of its own.
    const third = found.check();
    expect(third).not.toBe(first);
    await third;
  });

  it("keeps what came of a tarball that ended short, for the next check's Range, and nothing of a page in its place", async () => {
    const tarball = base.publish("1.2.4");
    const partial = join(version(), "release.tar.gz.partial");
    // A server that caps what it sends: the tarball's first 1 000 bytes, ended whole.
    base.answer = (request, response, body) => {
      if (request.headers.range || !request.url?.endsWith(".tar.gz")) return ranged(request, response, body);
      response.writeHead(200, { "content-length": 1_000 }).end(body.subarray(0, 1_000));
    };
    const found = base.updates();
    await expect(found.check()).rejects.toThrow(`the download of Surogate 1.2.4 stopped: it ended after 1000 of ${tarball.length} bytes`);
    expect(statSync(partial).size).toBe(1_000);
    await found.check();
    expect(base.heard.filter(({ url }) => url.endsWith(".tar.gz")).map(({ range }) => range)).toEqual([undefined, "bytes=1000-"]);
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
    // A page where the tarball should be, as a captive portal's, begins as no gzip does.
    base.publish("1.2.5");
    base.served.set(base.tarballAt("1.2.5"), Buffer.from("<html>Sign in to the hotel's Wi-Fi</html>"));
    base.answer = ranged;
    await expect(found.check()).rejects.toThrow("Surogate 1.2.5 was not the file the app expects");
    expect(lstatSync(join(base.cache(), "1.2.5", "release.tar.gz.partial"), { throwIfNoEntry: false })).toBeUndefined();
  });

  it("looks for an update when the version installed for every user is the one that runs, or its mark cannot be read", async () => {
    base.publish("1.2.4");
    const mark = join(base.dir, "release.json");
    // A mark is read as the helper reads one: an object on one line, naming a version with no zero before a part.
    const later = { version: "1.2.9" };
    for (const marked of [`${JSON.stringify({ version: "1.2.3" })}\n`, `${JSON.stringify({ version: "1.2.2" })}\n`, "not a mark\n", "null\n",
      JSON.stringify(later), `${JSON.stringify(later, null, 2)}\n`, `[${JSON.stringify(later)}]\n`, `${JSON.stringify({ version: "1.2.09" })}\n`, `${JSON.stringify(later)}${" ".repeat(4096)}\n`]) {
      writeFileSync(mark, marked);
      const found = base.updates({ installed: mark });
      await found.check();
      expect(found.state, marked).toMatchObject({ state: "available", version: "1.2.4" });
    }
  });

  it("is made every 6 hours", () => {
    expect(CHECK_MS).toBe(6 * 60 * 60 * 1000);
  });
});
