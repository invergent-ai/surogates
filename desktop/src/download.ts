// A download the app makes from where it was installed from: the guest image's files, and
// an update's tarball. Resumed from its .partial file with a Range, bounded while nothing
// comes, and checked by its size and hash as it comes: what is not the file is never kept.

import { createHash } from "node:crypto";
import { closeSync, constants, createReadStream, existsSync, openSync, readSync, rmSync, statSync } from "node:fs";
import { open } from "node:fs/promises";

export type Fetch = (url: string, init: { headers: Record<string, string>; signal?: AbortSignal }) => Promise<Response>;

// A file to download: where from, its name as the user is told it, and what it must be.
export interface Download {
  url: string;
  name: string;
  size: number;
  sha256: string;
  // How the file begins: a page in its place, as a captive portal's, does not.
  magic: Buffer;
}

export interface DownloadOptions {
  fetch?: Fetch;
  signal?: AbortSignal;
  stallMs?: number; // how long no bytes may come before the download stops: STALL_MS
  headersMs?: number; // how long its headers may take: HEADERS_MS
}

// Chromium's network bounds no body that stops coming, as from a peer gone over a sleep or a
// proxy that holds a large download: a download that nothing comes for in this long stops.
const STALL_MS = 30_000;
// A proxy that scans a download may send its headers only once it has all of it: they get longer.
const HEADERS_MS = 120_000;

export const sizeOf = (path: string) => (existsSync(path) ? statSync(path).size : 0);

// Whether *path* begins with *magic*.
function begins(path: string, magic: Buffer): boolean {
  const head = Buffer.alloc(magic.length);
  const fd = openSync(path, "r");
  try {
    return readSync(fd, head, 0, head.length, 0) === head.length && head.equals(magic);
  } finally {
    closeSync(fd);
  }
}

// The sha256 of *path*'s first *bytes*, or all of it.
export async function hashOf(path: string, bytes?: number): Promise<ReturnType<typeof createHash>> {
  const hash = createHash("sha256");
  if (bytes === 0) return hash;
  for await (const chunk of createReadStream(path, bytes === undefined ? {} : { end: bytes - 1 })) hash.update(chunk as Buffer);
  return hash;
}

/**
 * *file*'s download into *partial*, resumed from what it holds, and checked by its hash.
 * *got* is told how many of its bytes are here. Rejects with why not, in words for the user.
 */
export async function download(file: Download, partial: string, options: DownloadOptions = {}, got: (have: number) => void = () => {}): Promise<void> {
  let have = sizeOf(partial);
  // Whether more came than its size: then it is not the file, whatever its start.
  let past = false;
  // More than the file: it starts again, and the open below empties what is here.
  if (have > file.size) have = 0;
  let hash = await hashOf(partial, have);
  got(have);
  if (have < file.size) {
    const { url } = file;
    const said = (error: unknown) => (error instanceof Error ? error.message : String(error));
    // No headers for headersMs, or no next bytes for stallMs, stops it, whatever the fetch bounds.
    const stallMs = options.stallMs ?? STALL_MS;
    const quiet = new AbortController();
    let timer: NodeJS.Timeout | undefined;
    const heard = (ms = stallMs) => {
      clearTimeout(timer);
      timer = setTimeout(() => quiet.abort(new Error(`nothing came for ${ms / 1000} s`)), ms);
    };
    const stalled = new Promise<never>((_resolve, reject) => quiet.signal.addEventListener("abort", () => reject(quiet.signal.reason), { once: true }));
    stalled.catch(() => {});
    const signal = options.signal ? AbortSignal.any([options.signal, quiet.signal]) : quiet.signal;
    heard(options.headersMs ?? HEADERS_MS);
    try {
      let response: Response;
      try {
        response = await Promise.race([(options.fetch ?? fetch)(url, { headers: have > 0 ? { range: `bytes=${have}-` } : {}, signal }), stalled]);
      } catch (error) {
        options.signal?.throwIfAborted();
        if (quiet.signal.aborted) throw new Error(`the download of ${file.name} stopped: ${said(quiet.signal.reason)}`);
        throw new Error(`could not reach ${new URL(url).host}: ${said(error)}`);
      }
      // Its first bytes get the whole idle bound, not what is left of the headers'.
      heard();
      if (response.status === 200 && have > 0) {
        // Not the rest of the file: the whole of it, from a server that ignores a Range, starts it
        // again. Anything else, as a captive portal's page, is not the file, and leaves what is here.
        if (Number(response.headers.get("content-length")) !== file.size) {
          void response.body?.cancel().catch(() => {});
          throw new Error(`${file.name} was not the file the app expects`);
        }
        have = 0;
        hash = createHash("sha256");
        got(have);
      } else if ((response.status === 206 && !response.headers.get("content-range")?.startsWith(`bytes ${have}-`)) || (response.status === 416 && have > 0)) {
        // A range, but not from where it stopped, or none at all, as for an object shorter than what is
        // kept: asked again, it would be the same, so the next try starts it afresh.
        void response.body?.cancel().catch(() => {});
        rmSync(partial, { force: true });
        throw new Error(`the download of ${file.name} did not resume where it stopped`);
      } else if (response.status !== 200 && response.status !== 206) {
        void response.body?.cancel().catch(() => {});
        throw new Error(`${new URL(url).host} answered ${response.status} for ${file.name}`);
      }
      // Opened by its own name, never through a link: one put in its place since its folder was
      // looked at fails the open (ELOOP), and nothing is written where it leads. The open is also
      // what empties a file that starts again, so nothing is emptied through a link either.
      // A resume opens the file that was counted and hashed, and makes none: one that went since
      // would be made again from the rest alone, with a count and a hash that say it is whole.
      // And the open waits for nothing: a pipe put in the file's place would hold it, and with it
      // every later download, until someone read from it. What was opened is then a file, or is refused.
      const flags = constants.O_WRONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK | (have > 0 ? constants.O_APPEND : constants.O_CREAT | constants.O_TRUNC);
      const refused = (why: string) => {
        void response.body?.cancel().catch(() => {});
        return new Error(`the download of ${file.name} stopped: ${partial} ${why}`);
      };
      const out = await open(partial, flags, 0o600).catch((error: NodeJS.ErrnoException) => {
        if (error.code === "ELOOP") throw refused("is a link");
        if (error.code === "ENXIO") throw refused("is no file");
        if (error.code === "ENOENT" && have > 0) throw refused("went while the rest of it was asked for");
        void response.body?.cancel().catch(() => {});
        throw error;
      });
      if (!(await out.stat()).isFile()) {
        await out.close();
        throw refused("is no file");
      }
      const reader = response.body?.getReader();
      const next = () => reader && Promise.race([reader.read(), stalled]);
      try {
        for (let read = await next(); read && !read.done; read = await next()) {
          heard();
          past = have + read.value.length > file.size;
          if (past) break;
          hash.update(read.value);
          // A write may take less than it is given: what is hashed is what is on disk.
          for (let at = 0; at < read.value.length;) at += (await out.write(read.value, at)).bytesWritten;
          have += read.value.length;
          got(have);
        }
      } catch (error) {
        options.signal?.throwIfAborted();
        throw new Error(`the download of ${file.name} stopped: ${said(error)}`);
      } finally {
        // Not waited for: a body that stalled need not answer its cancel.
        void reader?.cancel().catch(() => {});
        await out.close();
      }
      // Ended short, as a server that caps a range sends: what came is kept for the next try's Range.
      // A page in its place, as a captive portal's, does not begin as the file does.
      if (!past && have < file.size && begins(partial, file.magic)) {
        throw new Error(`the download of ${file.name} stopped: it ended after ${have} of ${file.size} bytes`);
      }
    } finally {
      clearTimeout(timer);
    }
  }
  if (past || have !== file.size || hash.digest("hex") !== file.sha256) {
    rmSync(partial, { force: true });
    throw new Error(`${file.name} was not the file the app expects`);
  }
}
