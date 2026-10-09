// The host's side of the way into a chat's sandbox from outside it (spec, Section 5 and Section 11,
// Network, "Later"). The guest's inbound port carries one HTTP/2 session, the host its client: a
// CONNECT stream for each connection, to a port of the loopback's address, with the root in a header,
// which the agent has that root's runner dial in the root's own namespaces (guest/inbound.ts). The
// guest is not trusted: its answer is a status, a short reason, and then bytes.

import { type ClientHttp2Session, type ClientHttp2Stream, connect as connectH2, constants } from "node:http2";
import type { Duplex } from "node:stream";

// How long the agent has to answer a stream: past its own bound on a runner's dial (guest/network.ts).
export const REACH_MS = 10_000;
// Why the guest took no connection, as it may say it: an errno's name, or that the root has no sandbox there.
const REASON = /^([A-Z]{1,16}|sandbox)$/;

/**
 * Lets go of a connection into a root, so that the guest lets go of it too. A stream is reset with a code
 * first: destroyed alone, it is reset with none, and the agent's end, which may still hold bytes of the
 * server's answer it could not write, is then never closed, nor the root's connection behind it (measured).
 */
export function letGo(reached: Duplex): void {
  const stream = reached as Partial<Pick<ClientHttp2Stream, "close" | "closed">>;
  if (typeof stream.close === "function" && stream.closed === false) stream.close(constants.NGHTTP2_CANCEL);
  reached.destroy();
}

/** The host's end of the guest's inbound port: a stream for each connection into a root. */
export class Carrier {
  private readonly session: ClientHttp2Session;

  constructor(channel: Duplex, private readonly reachMs = REACH_MS) {
    this.session = connectH2("http://guest", { createConnection: () => channel, settings: { enablePush: false } });
    // The guest ends it only by going: a stream opened after answers that the root has no sandbox.
    this.session.on("error", () => {});
  }

  /**
   * A connection to *port* of *root*'s own loopback in the guest, or why there is none: what the
   * agent said, when it is a reason an agent gives, "unreachable" for anything else, and
   * "ETIMEDOUT" when it said nothing in time. Never rejects.
   */
  open(root: string, port: number): Promise<ClientHttp2Stream | string> {
    return new Promise((resolve) => {
      let stream: ClientHttp2Stream;
      try {
        stream = this.session.request({ ":method": "CONNECT", ":authority": `127.0.0.1:${port}`, "surogate-root": root });
      } catch {
        return resolve("sandbox");
      }
      const timer = setTimeout(() => settle("ETIMEDOUT"), this.reachMs);
      const settle = (answer: ClientHttp2Stream | string) => {
        clearTimeout(timer);
        if (typeof answer === "string") letGo(stream);
        resolve(answer);
      };
      stream.on("error", () => {});
      stream.once("close", () => settle("sandbox"));
      stream.once("response", (headers) => {
        if (headers[":status"] === 200) return settle(stream);
        const reason = String(headers["surogate-reason"] ?? "");
        settle(REASON.test(reason) ? reason : "unreachable");
      });
    });
  }

  close(): void {
    this.session.destroy();
  }
}
