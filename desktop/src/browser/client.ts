// The main process's side of the browser host (spec, Section 1): one host per agent identity,
// started at its first browser operation and again after one that went. In the app it is an
// Electron utility process; in the tests, a Node child process. Its browser goes with it.

import { fork } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { Operation, Outcome } from "../link/protocol.js";
import type { Launch } from "./host.js";

// The same from src/browser and from dist/browser.
const PACKAGE = fileURLToPath(new URL("../..", import.meta.url));
export const BROWSER_HOST = join(PACKAGE, "dist", "browser", "main.js");
// Above the host's own bound on its browser's close (RELEASE_MS).
const STOP_MS = 10_000;
// How long a host may take to say whether it showed a page: above its own bound on bringing one to the front (SHOW_MS).
const SHOWN_MS = 5_000;

export const CANCELLED: Outcome = {
  error: { type: "cancelled", message: "The session stopped this before the computer finished it" },
};
// A second operation under the id of one still running: its answer would be taken for the first's.
export const DUPLICATE: Outcome = {
  error: { type: "browser", message: "The computer's browser is already running an operation under this id" },
};
// A browser operation of a chat its user took the browser over (surogates/devices/browser.py).
export const PAUSED: Outcome = {
  error: { type: "paused_by_user", message: "The user took over the agent's browser on this computer" },
};
export const BROWSER_STOPPED: Outcome = {
  error: {
    type: "interrupted",
    message: "interrupted: the computer's browser stopped while this ran. Check the page before repeating it.",
  },
};

// The address of a tab a session's next operation would open.
export const NEW_TAB = "about:blank";

export type ToBrowser =
  // *root* is the chat's root session, *session* the calling one: each calling session has a tab.
  | { type: "op"; id: string; launch: Launch; root: string; session: string; kind: string; args: Record<string, unknown> }
  | { type: "cancel"; id: string }
  // A deleted chat: every tab of its sessions closes.
  | { type: "forget"; root: string }
  // A browser the user picked, launched once to see that it runs: answered {version}, or why not.
  | { type: "try"; id: string; executable: string }
  // The address of the page the session's next operation acts in.
  | { type: "address"; id: string; session: string }
  // A chat its user took the browser over, or handed back: an operation of it waiting its turn is answered paused.
  | { type: "pause"; root: string; paused: boolean }
  // The chat's newest page brought to the front: answered whether there was one.
  | { type: "show"; id: string; root: string }
  | { type: "stop" };

export type FromBrowser =
  | { type: "result"; id: string; outcome: Outcome }
  // A try's answer: its ids are the client's own, apart from the link's operation ids.
  | { type: "tried"; id: string; outcome: Outcome }
  | { type: "address"; id: string; url: string }
  | { type: "shown"; id: string; shown: boolean }
  // Its last word at a stop, after every answer: a utility process's postMessage has no callback.
  | { type: "stopped" };

export interface BrowserProcess {
  send(message: ToBrowser): void;
  onMessage(listener: (message: FromBrowser) => void): void;
  onExit(listener: () => void): void;
  kill(): void;
}

export function forkBrowserHost(script = BROWSER_HOST): BrowserProcess {
  // Its stdout goes to stderr: the app's stdout may carry other things.
  const child = fork(script, [], { stdio: ["ignore", 2, 2, "ipc"] });
  child.on("error", () => {});
  let closed = false;
  child.once("close", () => {
    closed = true;
  });
  return {
    send: (message) => void child.send(message, (error) => error),
    onMessage: (listener) => void child.on("message", (message) => listener(message as FromBrowser)),
    onExit: (listener) => {
      if (closed) listener();
      else child.once("close", () => listener());
    },
    kill: () => void child.kill("SIGKILL"),
  };
}

export class BrowserClient {
  private host: BrowserProcess | null = null;
  private readonly pending = new Map<string, (outcome: Outcome) => void>();
  private readonly trying = new Map<string, (outcome: Outcome) => void>();
  private readonly addressing = new Map<string, (url: string) => void>();
  private readonly showing = new Map<string, (shown: boolean) => void>();
  private stopping: Promise<void> | null = null;
  private tries = 0;
  private addresses = 0;
  private shows = 0;

  constructor(private readonly spawn: () => BrowserProcess = forkBrowserHost) {}

  /** One browser operation of a session's, in its tab. A cancel is answered at once; the host is told. Never rejects. */
  perform(launch: Launch, operation: Operation, signal: AbortSignal): Promise<Outcome> {
    if (signal.aborted) return Promise.resolve(CANCELLED);
    // No host, no tab: a close has nothing to close, and starts none.
    if (operation.kind === "browser.close" && !this.host && !this.stopping) return Promise.resolve({ ok: { closed: false } });
    return this.ask(this.pending, operation.id, {
      type: "op", id: operation.id, launch, root: operation.sessionId, session: operation.callingSessionId, kind: operation.kind, args: operation.args,
    }, signal);
  }

  /** A deleted chat's tabs close; a host that does not run has none. */
  forget(root: string): void {
    this.host?.send({ type: "forget", root });
  }

  /** Launch *executable* once, as Settings' Custom… does before it keeps it. Never rejects. */
  tryBrowser(executable: string): Promise<Outcome> {
    const id = `try-${(this.tries += 1)}`;
    return this.ask(this.trying, id, { type: "try", id, executable });
  }

  /** The address of the page *session*'s next operation acts in: a new tab's, about:blank, where no host runs. Never rejects. */
  address(session: string): Promise<string> {
    const host = this.host;
    if (!host || this.stopping) return Promise.resolve(NEW_TAB);
    const id = `address-${(this.addresses += 1)}`;
    return new Promise((resolve) => {
      this.addressing.set(id, (url) => {
        this.addressing.delete(id);
        resolve(url);
      });
      host.send({ type: "address", id, session });
    });
  }

  /** A chat its user took the browser over, or handed back: a running host is told, for an operation waiting there. */
  pause(root: string, paused: boolean): void {
    this.host?.send({ type: "pause", root, paused });
  }

  /**
   * Bring the chat's newest page to the front: whether there was one. None where no host runs, or
   * where the host does not say within SHOWN_MS. Never rejects.
   */
  show(root: string): Promise<boolean> {
    const host = this.host;
    if (!host || this.stopping) return Promise.resolve(false);
    const id = `show-${(this.shows += 1)}`;
    return new Promise((resolve) => {
      const answer = (shown: boolean) => {
        clearTimeout(late);
        this.showing.delete(id);
        resolve(shown);
      };
      // A host that never says leaves nobody waiting.
      const late = setTimeout(() => answer(false), SHOWN_MS);
      this.showing.set(id, answer);
      host.send({ type: "show", id, root });
    });
  }

  // The host closes its browser and exits; one that does not is killed, and its browser goes with it.
  stop(): Promise<void> {
    this.stopping ??= (async () => {
      const host = this.host;
      if (!host) return;
      const gone = new Promise<void>((resolve) => host.onExit(resolve));
      const timer = setTimeout(() => host.kill(), STOP_MS);
      host.send({ type: "stop" });
      await gone;
      clearTimeout(timer);
    })();
    return this.stopping;
  }

  /** The computer's access ended: the browser closes now, and a later operation starts it again. */
  async end(): Promise<void> {
    await this.stop();
    this.stopping = null;
  }

  private ask(waiting: Map<string, (outcome: Outcome) => void>, id: string, message: ToBrowser, signal?: AbortSignal): Promise<Outcome> {
    if (this.stopping) return Promise.resolve(BROWSER_STOPPED);
    if (waiting.has(id)) return Promise.resolve(DUPLICATE);
    let host: BrowserProcess;
    try {
      host = this.host ?? this.start();
    } catch (error) {
      return Promise.resolve({ error: { type: "unavailable", message: `The computer's browser could not start: ${error instanceof Error ? error.message : String(error)}` } });
    }
    return new Promise((resolve) => {
      const answer = (outcome: Outcome) => {
        signal?.removeEventListener("abort", cancel);
        waiting.delete(id);
        resolve(outcome);
      };
      const cancel = () => {
        host.send({ type: "cancel", id });
        answer(CANCELLED);
      };
      waiting.set(id, answer);
      signal?.addEventListener("abort", cancel, { once: true });
      host.send(message);
    });
  }

  private start(): BrowserProcess {
    const host = this.spawn();
    this.host = host;
    host.onMessage((message) => {
      if (message.type === "result") this.pending.get(message.id)?.(message.outcome);
      else if (message.type === "tried") this.trying.get(message.id)?.(message.outcome);
      else if (message.type === "address") this.addressing.get(message.id)?.(message.url);
      else if (message.type === "shown") this.showing.get(message.id)?.(message.shown);
      else if (message.type === "stopped") host.kill();
    });
    host.onExit(() => {
      if (this.host === host) this.host = null;
      for (const answer of [...this.pending.values(), ...this.trying.values()]) answer(BROWSER_STOPPED);
      // The next operation starts another host, and opens a new tab.
      for (const answer of [...this.addressing.values()]) answer(NEW_TAB);
      for (const answer of [...this.showing.values()]) answer(false);
    });
    return host;
  }
}
