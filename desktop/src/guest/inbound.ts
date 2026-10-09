// The agent's side of the guest's inbound port (spec, Section 5 and Section 11, Network,
// "Later"): one HTTP/2 session on ai.surogate.inbound, the agent its server, in which the
// host opens a CONNECT stream for each connection the agent's browser makes to a server of
// a root's own. Its authority is 127.0.0.1 or [::1] and a port, and nothing else is
// taken: the connection is made by the root's runner, to its own loopback, the family
// the authority names first, inside the root's namespaces (root.ts, Roots.reach). The agent opens no stream here, and the host no
// stream on the net port: neither way's rules read the other's requests.

import { constants, performServerHandshake, type ServerHttp2Stream } from "node:http2";
import type { Socket } from "node:net";
import type { Duplex } from "node:stream";

import { MAX_TUNNELS } from "./network.js";
import { MAX_SHARES, ROOT_ID } from "./protocol.js";

// A stream's authority: the loopback's address in the family to try first, as the browser's address
// named it, and a port from 1 to 65535 as its digits spell it.
const AUTHORITY = /^(127\.0\.0\.1|\[::1\]):([1-9]\d{0,4})$/;
// Why nothing took a connection, as the agent's own parts say it: an errno's name, or "sandbox".
const REASON = /^([A-Z]{1,16}|sandbox)$/;

const refuse = (stream: ServerHttp2Stream, status: 400 | 502, why: string): void => {
  if (!stream.destroyed && !stream.headersSent) stream.respond({ ":status": status, "surogate-reason": why }, { endStream: true });
};

export class Inbound {
  // *reach*: a connection to a port of a root's own loopback, the family *first* names tried before the other, or why there is none (Roots.reach).
  constructor(port: Duplex, reach: (root: string, port: number, first: 4 | 6) => Promise<Socket | string>) {
    const session = performServerHandshake(port, { settings: { maxConcurrentStreams: MAX_SHARES * MAX_TUNNELS, enablePush: false } });
    // The host is the one client: an error ends the session, and its streams with it.
    session.on("error", () => {});
    session.on("stream", (stream, headers) => {
      stream.on("error", () => {});
      const root = headers["surogate-root"];
      const authority = AUTHORITY.exec(String(headers[":authority"] ?? ""));
      const to = Number(authority?.[2] ?? 0);
      const first = authority?.[1] === "[::1]" ? 6 : 4;
      if (headers[":method"] !== "CONNECT" || typeof root !== "string" || !ROOT_ID.test(root) || to < 1 || to > 65_535) {
        return refuse(stream, 400, "invalid");
      }
      void Promise.resolve().then(() => reach(root, to, first)).catch(() => "unreachable").then((reached) => {
        if (typeof reached === "string") return refuse(stream, 502, REASON.test(reached) ? reached : "unreachable");
        // Given up by the host while its root's runner dialed.
        if (stream.destroyed || stream.aborted) return void reached.destroy();
        reached.on("error", () => stream.destroy());
        stream.once("close", () => reached.destroy());
        // And at the host's reset, whatever its code: a stream that still holds bytes of the server's answer
        // is not closed by a reset with no code, and the root's connection would stay open behind it.
        stream.once("aborted", () => reached.destroy());
        // The other way, a root's connection that has gone takes its stream with it: one destroyed before its
        // end, as a root's teardown destroys it, by a reset, since nothing else would ever close the stream;
        // one that had ended, with no error and only once every byte of its answer is written.
        reached.once("close", () => {
          if (!stream.destroyed) stream.close(reached.readableEnded ? constants.NGHTTP2_NO_ERROR : constants.NGHTTP2_CANCEL);
        });
        stream.respond({ ":status": 200 });
        reached.pipe(stream);
        stream.pipe(reached);
      });
    });
  }
}
