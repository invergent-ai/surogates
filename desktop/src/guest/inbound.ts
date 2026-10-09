// The agent's side of the guest's inbound port (spec, Section 5 and Section 11, Network,
// "Later"): one HTTP/2 session on ai.surogate.inbound, the agent its server, in which the
// host opens a CONNECT stream for each connection the agent's browser makes to a server of
// a root's own. Its authority is 127.0.0.1 and a port, and nothing else is taken: the
// connection is made by the root's runner, to its own loopback, inside the root's
// namespaces (root.ts, Roots.reach). The agent opens no stream here, and the host no
// stream on the net port: neither way's rules read the other's requests.

import { performServerHandshake, type ServerHttp2Stream } from "node:http2";
import type { Socket } from "node:net";
import type { Duplex } from "node:stream";

import { MAX_TUNNELS } from "./network.js";
import { MAX_SHARES, ROOT_ID } from "./protocol.js";

// A stream's authority: this, and a port from 1 to 65535 as its digits spell it.
const AUTHORITY = /^127\.0\.0\.1:([1-9]\d{0,4})$/;
// Why nothing took a connection, as the agent's own parts say it: an errno's name, or "sandbox".
const REASON = /^([A-Z]{1,16}|sandbox)$/;

const refuse = (stream: ServerHttp2Stream, status: 400 | 502, why: string): void => {
  if (!stream.destroyed && !stream.headersSent) stream.respond({ ":status": status, "surogate-reason": why }, { endStream: true });
};

export class Inbound {
  // *reach*: a connection to a port of a root's own loopback, or why there is none (Roots.reach).
  constructor(port: Duplex, reach: (root: string, port: number) => Promise<Socket | string>) {
    const session = performServerHandshake(port, { settings: { maxConcurrentStreams: MAX_SHARES * MAX_TUNNELS, enablePush: false } });
    // The host is the one client: an error ends the session, and its streams with it.
    session.on("error", () => {});
    session.on("stream", (stream, headers) => {
      stream.on("error", () => {});
      const root = headers["surogate-root"];
      const to = Number(AUTHORITY.exec(String(headers[":authority"] ?? ""))?.[1] ?? 0);
      if (headers[":method"] !== "CONNECT" || typeof root !== "string" || !ROOT_ID.test(root) || to < 1 || to > 65_535) {
        return refuse(stream, 400, "invalid");
      }
      void Promise.resolve().then(() => reach(root, to)).catch(() => "unreachable").then((reached) => {
        if (typeof reached === "string") return refuse(stream, 502, REASON.test(reached) ? reached : "unreachable");
        // Given up by the host while its root's runner dialed.
        if (stream.destroyed || stream.aborted) return void reached.destroy();
        reached.on("error", () => stream.destroy());
        stream.once("close", () => reached.destroy());
        // And at the host's reset, whatever its code: a stream that still holds bytes of the server's answer
        // is not closed by a reset with no code, and the root's connection would stay open behind it.
        stream.once("aborted", () => reached.destroy());
        stream.respond({ ":status": 200 });
        reached.pipe(stream);
        stream.pipe(reached);
      });
    });
  }
}
