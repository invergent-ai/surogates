// The release bucket's Worker (release/worker.ts): what publish.sh puts under desktop/, served at
// https://surogate.ai/desktop/ by its path, read-only, as the install script and the app ask for it.

import { describe, expect, it } from "vitest";

import worker, { type ReleaseBucket } from "../release/worker.js";

// A bucket of *objects* by key, as the R2 binding answers: get and head, each with its stored
// metadata, and every key it was asked for.
const bucket = (objects: Record<string, { body: string; contentType: string; cacheControl?: string }>) => {
  const asked: string[] = [];
  const of = (key: string) => {
    asked.push(key);
    const object = objects[key];
    if (object === undefined) return null;
    return {
      size: new TextEncoder().encode(object.body).byteLength,
      httpEtag: `"etag-of-${key}"`,
      writeHttpMetadata: (headers: Headers) => {
        headers.set("content-type", object.contentType);
        if (object.cacheControl) headers.set("cache-control", object.cacheControl);
      },
      body: new Response(object.body).body!,
    };
  };
  // A head is the object without its body, as R2's is.
  const binding: ReleaseBucket = {
    get: async (key) => of(key),
    head: async (key) => {
      const object = of(key);
      if (object === null) return null;
      const { body: _body, ...head } = object;
      return head;
    },
  };
  return { binding, asked };
};
const ask = (env: { BUCKET: ReleaseBucket }, path: string, method = "GET") => worker.fetch(new Request(`https://surogate.ai${path}`, { method }), env);

describe("the release bucket's Worker", () => {
  const objects = {
    "desktop/latest.json": { body: '{"version":"2.17.1"}', contentType: "application/json", cacheControl: "no-cache" },
    "desktop/install.sh": { body: "#!/usr/bin/env bash\n", contentType: "text/x-shellscript", cacheControl: "no-cache" },
    "desktop/releases/2.17.1/surogate-desktop-2.17.1-linux-x64.tar.gz": { body: "tarball", contentType: "application/gzip" },
  };

  it("serves each object under desktop/ by its path, with the type and cache control publish.sh sent it with", async () => {
    const { binding } = bucket(objects);
    const latest = await ask({ BUCKET: binding }, "/desktop/latest.json");
    expect(latest.status).toBe(200);
    expect(await latest.text()).toBe('{"version":"2.17.1"}');
    expect(Object.fromEntries(["content-type", "cache-control", "etag"].map((name) => [name, latest.headers.get(name)]))).toEqual({
      "content-type": "application/json", "cache-control": "no-cache", etag: '"etag-of-desktop/latest.json"',
    });
    const tarball = await ask({ BUCKET: binding }, "/desktop/releases/2.17.1/surogate-desktop-2.17.1-linux-x64.tar.gz");
    expect([tarball.status, await tarball.text(), tarball.headers.get("content-type")]).toEqual([200, "tarball", "application/gzip"]);
  });

  it("answers a HEAD with the object's headers and size, and no body", async () => {
    const { binding } = bucket(objects);
    const head = await ask({ BUCKET: binding }, "/desktop/install.sh", "HEAD");
    expect([head.status, head.headers.get("content-length"), head.headers.get("content-type"), await head.text()]).toEqual([200, "20", "text/x-shellscript", ""]);
  });

  it("says 404 for an object that is not there, and for any path outside desktop/ without asking the bucket", async () => {
    const { binding, asked } = bucket(objects);
    expect((await ask({ BUCKET: binding }, "/desktop/releases/9.9.9/manifest.json")).status).toBe(404);
    expect(asked).toEqual(["desktop/releases/9.9.9/manifest.json"]);
    for (const path of ["/", "/desktop/", "/desktop/releases/", "/desktopx/latest.json", "/other/latest.json", "/desktop"]) {
      expect((await ask({ BUCKET: binding }, path)).status, path).toBe(404);
    }
    expect(asked).toEqual(["desktop/releases/9.9.9/manifest.json"]);
  });

  it("writes nothing: any method but GET and HEAD is refused, and the bucket is not asked", async () => {
    const { binding, asked } = bucket(objects);
    for (const method of ["PUT", "POST", "DELETE", "PATCH"]) {
      const refused = await ask({ BUCKET: binding }, "/desktop/latest.json", method);
      expect([refused.status, refused.headers.get("allow")], method).toEqual([405, "GET, HEAD"]);
    }
    expect(asked).toEqual([]);
  });
});
