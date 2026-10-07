// The host's end of the guest's network port (spec, Section 11, Network): one HTTP/2
// session on the byte stream the OS's backend opens for ai.surogate.net, in which the
// agent opens a CONNECT stream for each connection a root's command makes, its
// destination as the stream's authority and the root in a header. Each is judged when it
// is made, and the host dials the addresses it judged, so a name cannot lead elsewhere
// between the two. The proxy keeps, per root, what it refused and what still waits, for
// that root's next run.

import { performServerHandshake, type ServerHttp2Session, type ServerHttp2Stream } from "node:http2";
import { connect as connectTcp, type Socket } from "node:net";
import type { Duplex } from "node:stream";

import { ROOT_ID } from "../guest/protocol.js";
import type { NetworkAsk } from "../hosts/messages.js";
import type { Outcome } from "../link/protocol.js";
import { judge, type ReachOptions } from "./egress.js";

// How many destinations of a kind a notice names.
const NOTICE_NAMES = 20;

export interface Egress {
  // Whether the chat's user lets *root* reach *asked* now: a grant, or their answer. Rejecting
  // denies. The app denies a root none of whose work runs, so a root the agent names that is
  // not at work, or not one of this app's chats, reaches nothing past the package hosts.
  ask(root: string, asked: NetworkAsk): Promise<boolean>;
}

export interface ProxyOptions extends ReachOptions {
  egress: Egress;
  // The connection to *address*:*port*, once judged; net.connect by default.
  connect?(address: string, port: number): Socket;
}

// What a root's notice says: this computer's own, refused, still waiting, not looked up.
export interface Noticed {
  own: Iterable<string>;
  refused: Iterable<string>;
  waiting: Iterable<string>;
  unknown: Iterable<string>;
}

// A notice's line about *keys*, naming the first NOTICE_NAMES of them and then how many
// more, so a sweep of ports cannot flood the agent's output; none for no keys.
function line(keys: Iterable<string>, say: (names: string) => string): string | null {
  const all = [...keys];
  if (all.length === 0) return null;
  const more = all.length > NOTICE_NAMES ? ` and ${all.length - NOTICE_NAMES} more` : "";
  return say(`${all.slice(0, NOTICE_NAMES).join(", ")}${more}`);
}

/** The lines that tell the agent what its commands could not reach, or null for none. */
export function networkNotice({ own, refused, waiting, unknown }: Noticed): string | null {
  return [
    line(own, (names) => `This computer does not let a chat reach its own network services (${names})`),
    // Neutral: a denial can also be a prompt that failed or was dismissed, or no one to ask.
    line(refused, (names) => `This computer did not allow network access to ${names}.`),
    line(waiting, (names) => `Still waiting for this computer's user to allow network access to ${names}.`),
    line(unknown, (names) => `This computer could not look up ${names}.`),
  ].filter(Boolean).join("\n") || null;
}

/** *outcome* with *notice* after its output, when it is a command that answered. */
export function withNotice(outcome: Outcome, notice: string | null): Outcome {
  const ok = "ok" in outcome ? (outcome.ok as { output?: unknown }) : null;
  if (!notice || typeof ok?.output !== "string") return outcome;
  return { ok: { ...ok, output: `${ok.output}${ok.output ? "\n" : ""}${notice}` } };
}

// What a root's connections met since its last notice. waiting: each destination asked
// about and not answered yet, and whether a notice has said so.
interface Met {
  own: Set<string>;
  refused: Set<string>;
  unknown: Set<string>;
  waiting: Map<string, boolean>;
}

const tcp = (address: string, port: number) => connectTcp({ host: address, port, allowHalfOpen: true });

export class NetProxy {
  private readonly session: ServerHttp2Session;
  // The ask open for each root's destination: a connection to one being decided waits for it.
  private readonly asking = new Map<string, Promise<boolean>>();
  private readonly met = new Map<string, Met>();

  constructor(channel: Duplex, private readonly options: ProxyOptions) {
    this.session = performServerHandshake(channel);
    // The guest is the one client: an error ends the session, and its streams with it.
    this.session.on("error", () => {});
    this.session.on("stream", (stream, headers) => {
      stream.on("error", () => {});
      const root = headers["surogate-root"];
      const authority = headers[":authority"];
      const at = typeof authority === "string" ? authority.lastIndexOf(":") : -1;
      const port = at < 0 ? "" : (authority as string).slice(at + 1);
      if (headers[":method"] !== "CONNECT" || typeof root !== "string" || !ROOT_ID.test(root) || !/^\d{1,5}$/.test(port)) {
        return refuse(stream, 403, "invalid");
      }
      void this.open(stream, root, (authority as string).slice(0, at), Number(port));
    });
  }

  /** What *root*'s connections met since its last notice, once: each ask still open is told once. */
  takeNotice(root: string): string | null {
    const met = this.met.get(root);
    if (!met) return null;
    const untold = [...met.waiting].filter(([, told]) => !told).map(([key]) => key);
    for (const key of untold) met.waiting.set(key, true);
    const notice = networkNotice({ own: met.own, refused: met.refused, waiting: untold, unknown: met.unknown });
    met.own.clear();
    met.refused.clear();
    met.unknown.clear();
    return notice;
  }

  // A root torn down: what it met goes with it, as a new namespace has met nothing.
  forget(root: string): void {
    this.met.delete(root);
  }

  // The session, and every stream in it, ends: its guest has gone.
  close(): void {
    this.session.destroy();
  }

  private metBy(root: string): Met {
    let met = this.met.get(root);
    if (!met) {
      met = { own: new Set(), refused: new Set(), unknown: new Set(), waiting: new Map() };
      this.met.set(root, met);
    }
    return met;
  }

  private async open(stream: ServerHttp2Stream, root: string, host: string, port: number): Promise<void> {
    const verdict = await judge(host, port, this.options);
    if ("refused" in verdict) {
      if (verdict.refused !== "invalid") this.metBy(root)[verdict.refused].add(verdict.key);
      return refuse(stream, 403, verdict.refused);
    }
    if (verdict.ask && !(await this.allowed(root, verdict.ask, verdict.key))) return refuse(stream, 403, "denied");
    this.dial(stream, verdict.dial, port);
  }

  // One ask for every connection of *root* to *key* in flight, so one npm install asks once.
  private allowed(root: string, asked: NetworkAsk, key: string): Promise<boolean> {
    const id = `${root} ${key}`;
    const open = this.asking.get(id);
    if (open) return open;
    const met = this.metBy(root);
    met.waiting.set(key, false);
    const decided: Promise<boolean> = Promise.resolve()
      .then(() => this.options.egress.ask(root, asked))
      .then((allow) => allow === true, () => false)
      .then((allow) => {
        this.asking.delete(id);
        met.waiting.delete(key);
        if (!allow) met.refused.add(key);
        return allow;
      });
    this.asking.set(id, decided);
    return decided;
  }

  // The addresses judged, in turn, until one takes the connection; the stream carries it from then on.
  private dial(stream: ServerHttp2Stream, addresses: readonly string[], port: number, at = 0, failed = "EHOSTUNREACH"): void {
    const address = addresses[at];
    if (stream.destroyed) return;
    if (address === undefined) return refuse(stream, 502, failed);
    const socket = (this.options.connect ?? tcp)(address, port);
    let connected = false;
    const drop = () => socket.destroy();
    stream.once("close", drop);
    socket.once("connect", () => {
      connected = true;
      if (stream.destroyed) return void socket.destroy();
      stream.respond({ ":status": 200 });
      socket.pipe(stream);
      stream.pipe(socket);
    });
    socket.once("error", (error: NodeJS.ErrnoException) => {
      socket.destroy();
      if (connected) return void stream.destroy();
      stream.off("close", drop);
      this.dial(stream, addresses, port, at + 1, error.code ?? failed);
    });
  }
}

// A stream answered and ended: 403 for a refusal, 502 for a destination that could not be reached.
function refuse(stream: ServerHttp2Stream, status: 403 | 502, why: string): void {
  if (stream.destroyed || stream.headersSent) return;
  stream.respond({ ":status": status, "surogate-reason": why }, { endStream: true });
}
