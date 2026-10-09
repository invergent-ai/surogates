// What a check refuses, and what bounds it, beyond the offers of updates.test.ts: the manifest's
// exact bytes and its version's form, the helper's own channel, the base that does not answer, the
// quit while a release downloads, and the mark of the version installed now.

import { sign } from "node:crypto";
import { existsSync, lstatSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { CHECK_MS, listedKeys, signedRelease } from "../src/shell/updates.js";
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

  it("names no field twice in any one object, whatever its two values: of the two a reader keeps one, and says nothing of the other", () => {
    const text = signed({ note: "@" })[0].toString();
    // The line with a field of its own, *value* as it is where its value goes; and with that field named twice.
    const own = (value: string) => text.replace('"@"', value);
    const twice = (first: string, second: string) => text.replace('"note":"@"', `"note":${first},"note":${second}`);
    const down = (steps: number, open: string, close: string) => `${open.repeat(steps)}1${close.repeat(steps)}`;
    expect(signedRelease("URL", ...signed(own("1")), [keys.publicKey], "stable").version).toBe("1.2.4");
    const refused: Array<[string, string]> = [
      ["its version, the release's last", text.replace('{"version"', '{"version":"9.9.9","version"')],
      ["its size, the release's last", text.replace('"size":', '"size":1,"size":')],
      ["its state schema, the same both times", text.replace('"stateSchema":1', '"stateSchema":1,"stateSchema":1')],
      ["a field of its own", twice("1", "2")],
      ["a field of its own, the same both times", twice("1", "1")],
      // Dropped unread by a reader that keeps the last, where one jq cannot read it at all.
      ["a field of its own, the first 300 down in lists", twice(down(300, "[", "]"), "1")],
      ["a field of its own, the first 300 down in objects", twice(down(300, '{"n":', "}"), "1")],
      ["a field of its own, the first 1000 down in lists", twice(down(1000, "[", "]"), "1")],
      ["a field of its own, the second 300 down in lists", twice("1", down(300, "[", "]"))],
      ["a field of its own, the second time by an escape", text.replace('"note":"@"', '"note":1,"\\u006eote":2')],
      ["a field in an object of its own", own('{"a":1,"a":2}')],
      ["a field in an object in a list of its own", own('[{"a":1,"a":2}]')],
      ["a field in an object far down", own(`${"[".repeat(40)}{"a":1,"b":2,"a":3}${"]".repeat(40)}`)],
      // jq reads the second half of a pair that is escaped alone as a replacement character: two
      // such names, or one and that character's own escape, are one name to it.
      ["two second halves of a pair, each escaped alone", own('{"\\udc00":1,"\\udfff":2}')],
      ["a second half of a pair escaped alone, and a replacement character's escape", own('{"x\\udc00":1,"x\\ufffd":2}')],
    ];
    for (const [what, form] of refused) expect(() => signedRelease("URL", ...signed(form), [keys.publicKey], "stable"), what).toThrow(`URL ${NO_RELEASE}`);
    // One name in two objects is two fields; and names that only look alike are two names.
    const taken: Array<[string, string]> = [
      ["a field of one name in two objects of its own", own('[{"a":1},{"a":2}]')],
      ["a field of its own name inside it", own('{"note":{"note":1}}')],
      ["a field after an object that has one of its name", twice('{"more":1}', "2").replace(',"note":2', ',"more":2')],
      ["a field after a list of objects that have one of its name", twice('[{"more":1},{"more":2}]', "3").replace(',"note":3', ',"more":3')],
      ["a list's two places", own("[1,1]")],
      ["names in other capitals", own('{"a":1,"A":2}')],
      ["two pairs, each escaped whole", own('{"\\ud83d\\ude00":1,"\\ud83d\\ude01":2}')],
      ["a name and its colon in a word", own('{"a":"\\"a\\":1","b":"a"}')],
    ];
    for (const [what, form] of taken) expect(signedRelease("URL", ...signed(form), [keys.publicKey], "stable").version, what).toBe("1.2.4");
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

describe("a helper's list of release keys", () => {
  it("is read in the one form the install script's own reader takes, and no key of any other spelling: never more than an install would trust", () => {
    const script = helperWith([keys.publicKey, next.publicKey]);
    const list = /^ *RELEASE_KEYS=\(\n[^)]*\)\n/m.exec(script)![0];
    const with_ = (change: (list: string) => string) => script.replace(list, change(list));
    expect(listedKeys(script)).toHaveLength(2);
    const spellings: Array<[string, string, number]> = [
      ["spaces after an entry's quote", with_((text) => text.replace("-----END PUBLIC KEY-----'\n", "-----END PUBLIC KEY-----'  \n")), 0],
      ["a comment in it", with_((text) => text.replace("(\n", "(\n    # the first key, since 2026\n")), 0],
      ["a comment that has an apostrophe in it", with_((text) => text.replace("(\n", "(\n    # Surogate's release key since 2026\n")), 0],
      ["a comment that has a parenthesis in it", with_((text) => text.replace("(\n", "(\n    # the first key (2026)\n")), 0],
      ["a comment between two keys", with_((text) => text.replace("-----END PUBLIC KEY-----'\n", "-----END PUBLIC KEY-----'\n    # the next\n")), 0],
      ["its keys in double quotes", with_((text) => text.replaceAll("'", '"')), 0],
      ["one line", with_((text) => `${text.trim().replace("(\n", "( ").replace(/\n *\)$/, " )")}\n`), 0],
      ["its closing bracket behind the last key", with_((text) => text.replace(/'\n *\)\n$/, "' )\n")), 0],
      ["a key's lines indented", with_((text) => text.replace(/\n(?=[A-Za-z0-9+/=]+\n|-----END)/g, "\n    ")), 0],
      ["an empty line in it", with_((text) => text.replace("(\n", "(\n\n")), 0],
      ["no key in it", with_((text) => `${text.split("\n")[0]}\n  )\n`), 0],
      ["a second list added to it", with_((text) => `${text}${text.replace("RELEASE_KEYS=(", "RELEASE_KEYS+=(")}`), 0],
      ["the list given twice", with_((text) => `${text}${text}`), 0],
      ["a carriage return at each of its lines' ends", with_((text) => text.replaceAll("\n", "\r\n")), 0],
      ["a list that never ends", with_((text) => text.replace(/\n *\)\n$/, "\n")), 0],
      ["a key that is no key", with_((text) => text.replace(/\n[A-Za-z0-9+/=]+\n/, "\nbm90IGEga2V5\n")), 1],
      ["nothing", "", 0],
    ];
    expect(spellings.map(([name, written]) => [name, listedKeys(written).length])).toEqual(spellings.map(([name, , read]) => [name, read]));
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
