// The host's end of the guest's network port (spec, Section 11, Network): one HTTP/2
// session on the byte stream the OS's backend opens for ai.surogate.net, in which the
// agent opens a CONNECT stream for each connection a root's command makes, its
// destination as the stream's authority and the root in a header. Each is judged when it
// is made, and the host dials the addresses it judged, so a name cannot lead elsewhere
// between the two. The proxy keeps, per root, what it refused and what still waits, for
// that root's next run.

import { performServerHandshake, type ServerHttp2Session, type ServerHttp2Stream } from "node:http2";
import { connect as connectTcp, isIPv6, type Socket } from "node:net";
import type { Duplex } from "node:stream";

import { MAX_TUNNELS } from "../guest/network.js";
import { MAX_SHARES, ROOT_ID } from "../guest/protocol.js";
import type { NetworkAsk } from "../hosts/messages.js";
import type { Outcome } from "../link/protocol.js";
import { judge, type ReachOptions } from "./egress.js";

// How many destinations of a kind a notice names.
const NOTICE_NAMES = 20;
// How many streams the guest may have open at once: as many as its roots can have tunnels,
// which binds only a guest whose agent no longer keeps to them.
const MAX_STREAMS = MAX_SHARES * MAX_TUNNELS;

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
// How long an attempt has to connect before the next judged address is tried beside it, the
// earlier one still running (RFC 8305), so an address that never answers holds no other back.
// Node's autoSelectFamily waits as long, then drops the earlier one.
const STAGGER_MS = 250;

// The judged addresses, IPv6 and IPv4 by turns, always IPv6 first, whatever order the lookup
// gave them. Node's autoSelectFamily leads with the family of the lookup's first address.
function inTurn(addresses: readonly string[]): string[] {
  const six = addresses.filter((address) => isIPv6(address));
  const four = addresses.filter((address) => !isIPv6(address));
  return Array.from({ length: Math.max(six.length, four.length) }, (_, at) => [six[at], four[at]]).flat().filter((address) => address !== undefined);
}

export class NetProxy {
  private readonly session: ServerHttp2Session;
  // The ask open for each root's destination: a connection to one being decided waits for it.
  private readonly asking = new Map<string, Promise<boolean>>();
  private readonly met = new Map<string, Met>();

  constructor(channel: Duplex, private readonly options: ProxyOptions) {
    this.session = performServerHandshake(channel, { settings: { maxConcurrentStreams: MAX_STREAMS } });
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

  // A root torn down: what it met goes with it, as a new namespace has met nothing; an ask
  // still open answers only its own connections, and the next namespace's asks anew.
  forget(root: string): void {
    this.met.delete(root);
    for (const id of this.asking.keys()) if (id.startsWith(`${root} `)) this.asking.delete(id);
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
    // Gone while it was judged: its user is not asked about it.
    if (verdict.ask && stream.destroyed) return;
    if (verdict.ask && !(await this.allowed(root, verdict.ask, verdict.key))) return refuse(stream, 403, "denied");
    this.dial(stream, verdict.dial, port);
  }

  // One ask for every connection of *root* to *key* in flight, so one npm install asks once;
  // only of those judged alike, so one judged a private network never rides a prompt that did not say so.
  private allowed(root: string, asked: NetworkAsk, key: string): Promise<boolean> {
    const idOf = (privateNetwork: boolean) => `${root} ${key} ${privateNetwork}`;
    const id = idOf(asked.privateNetwork);
    const open = this.asking.get(id);
    if (open) return open;
    const met = this.metBy(root);
    met.waiting.set(key, false);
    const decided: Promise<boolean> = Promise.resolve()
      .then(() => this.options.egress.ask(root, asked))
      .then((allow) => allow === true, () => false)
      .then((allow) => {
        if (this.asking.get(id) === decided) this.asking.delete(id);
        // Still waiting while the other kind's ask for it is open.
        if (!this.asking.has(idOf(!asked.privateNetwork))) met.waiting.delete(key);
        if (!allow) met.refused.add(key);
        return allow;
      });
    this.asking.set(id, decided);
    return decided;
  }

  // The addresses judged, and no others, in turn (inTurn): the next at once when one fails, or
  // beside it once it has had STAGGER_MS; the first to connect carries the stream, and the rest go.
  private dial(stream: ServerHttp2Stream, addresses: readonly string[], port: number): void {
    if (stream.destroyed) return;
    const order = inTurn(addresses);
    const trying = new Set<Socket>();
    let next = 0;
    let failed = "EHOSTUNREACH";
    let carried: Socket | null = null;
    let stagger: NodeJS.Timeout | undefined;
    stream.once("close", () => {
      clearTimeout(stagger);
      for (const socket of trying) socket.destroy();
    });
    const attempt = (): void => {
      clearTimeout(stagger);
      const address = order[next];
      if (stream.destroyed || carried || address === undefined) return;
      next += 1;
      const socket = (this.options.connect ?? tcp)(address, port);
      trying.add(socket);
      if (next < order.length) stagger = setTimeout(attempt, STAGGER_MS);
      socket.once("connect", () => {
        clearTimeout(stagger);
        carried = socket;
        for (const other of trying) if (other !== socket) other.destroy();
        if (stream.destroyed) return void socket.destroy();
        stream.respond({ ":status": 200 });
        socket.pipe(stream);
        stream.pipe(socket);
      });
      socket.once("error", (error: NodeJS.ErrnoException) => {
        socket.destroy();
        if (socket === carried) return void stream.destroy();
        trying.delete(socket);
        failed = error.code ?? failed;
        if (next < order.length) attempt();
        else if (trying.size === 0) refuse(stream, 502, failed);
      });
    };
    attempt();
  }
}

// A stream answered and ended: 403 for a refusal, 502 for a destination that could not be reached.
function refuse(stream: ServerHttp2Stream, status: 403 | 502, why: string): void {
  if (stream.destroyed || stream.headersSent) return;
  stream.respond({ ":status": status, "surogate-reason": why }, { endStream: true });
}
