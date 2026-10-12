// Surogate Desktop's release bucket at https://surogate.ai/desktop/: a Cloudflare Worker on the route
// surogate.ai/desktop/*, with the bucket bound as BUCKET. What publish.sh puts under desktop/ is
// served by its path, read-only: /desktop/latest.json is the object desktop/latest.json, with the
// type and the cache control it was sent with. The install script and the app ask for nothing else.
// Bundled to one module for its deploy: npx esbuild release/worker.ts --bundle --format=esm.

// The part of the R2 binding this Worker reads: an object's body, its size, its etag, and the
// metadata it was stored with.
export interface ReleaseObject {
  readonly size: number;
  readonly httpEtag: string;
  writeHttpMetadata(headers: Headers): void;
}
export interface ReleaseBucket {
  get(key: string): Promise<(ReleaseObject & { readonly body: ReadableStream }) | null>;
  head(key: string): Promise<ReleaseObject | null>;
}

const PREFIX = "desktop/";

export default {
  async fetch(request: Request, env: { BUCKET: ReleaseBucket }): Promise<Response> {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response(null, { status: 405, headers: { allow: "GET, HEAD" } });
    }
    // Keys as publish.sh writes them: a file's path under desktop/, never a folder's.
    const key = new URL(request.url).pathname.slice(1);
    if (!key.startsWith(PREFIX) || key.endsWith("/")) return new Response("Not found\n", { status: 404 });
    const headers = new Headers();
    if (request.method === "HEAD") {
      const object = await env.BUCKET.head(key);
      if (object === null) return new Response(null, { status: 404 });
      object.writeHttpMetadata(headers);
      headers.set("etag", object.httpEtag);
      headers.set("content-length", String(object.size));
      return new Response(null, { headers });
    }
    const object = await env.BUCKET.get(key);
    if (object === null) return new Response("Not found\n", { status: 404 });
    object.writeHttpMetadata(headers);
    headers.set("etag", object.httpEtag);
    return new Response(object.body, { headers });
  },
};
