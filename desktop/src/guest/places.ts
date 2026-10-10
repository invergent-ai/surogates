// A folder's place in the guest (spec, Section 13, "The user's computer"): its history from the
// app's data, mounted for writing, and the folder itself, read-only, for the agent's own git.
// Each is mounted at a path of the folder's key, in the agent's mount namespace alone: a
// root's namespaces leave /run behind (vm/enter-root), so no command of a thread's reaches
// the history, another thread's copy or the real files. Git on a place is the agent's own: it runs
// the history the agent disk carries (vm/history-tree.sh), as the agent's user, one request a run.

import { execFile, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { chmod, mkdir, rmdir } from "node:fs/promises";
import { join } from "node:path";
import { promisify } from "node:util";

import type { Outcome } from "../link/protocol.js";
import { CANCELLED, SANDBOX_STOPPED } from "./command.js";
import { PLACE_KEY, type Share } from "./protocol.js";
import { BOUNDS, TAG } from "./root.js";

export const PLACES = "/run/surogate/places";
const MOUNT_RETRY_MS = 25;
// Nothing in either is a program to run: git's hooks never are, and a file of the user's is data here.
const OPTIONS = "nosuid,nodev,noexec";

// The image's python, and the history's way in on the agent disk. -I: it reads no user's site
// folder and no PYTHON* variable, so that tree is the only code it can import.
const HISTORY = ["/usr/local/bin/python3", "-I", "/run/surogate/agent/history/main.py"];
// One answer's most: a commit's lists for a folder at the cap, well inside the control port's line.
const ANSWER_BYTES = 6 * 1024 ** 2;
const MESSAGE_UNITS = 2_000;
const CODE_UNITS = 64;
const GONE_RETRY_MS = 10;
// Why not, where the history ended without saying: the agent's own code, beside the history's (vm/history.ts).
const NO_ANSWER: Outcome = { error: { type: "history", code: "no_answer", message: "This folder's history did not answer" } };
const NOT_HERE = "This folder's history is not in the sandbox";
const HELD = "This folder's history is held by a request before this one, which has not ended";

const execute = promisify(execFile);

// What the history is asked: the place as it is mounted here, the thread whose copy it is, the
// user it is made for, and the action with its arguments.
export interface Asked {
  store: string;
  folder: string;
  thread: string;
  user: string;
  action: string;
  args: Record<string, unknown>;
}

// Settles once no process of the group *leader* led is left, the leader itself having ended. Its
// number names another process only once the group is empty: until then nothing else is given it.
async function emptied(leader: number): Promise<void> {
  for (;;) {
    if (existsSync(`/proc/${leader}`)) return;
    try {
      process.kill(-leader, 0);
    } catch {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, GONE_RETRY_MS));
  }
}

/**
 * The history's answer to *asked*, as it wrote it; rejects when it ended without one. The request
 * is its input alone: nothing of it is on the command line or in the environment, and git's
 * variables, with every other of the agent's, stay here. Everything it starts is one process
 * group, the history its leader. At *signal*, past an answer's size, and when the history itself
 * ends, the whole group is ended, and this settles only once none of it is left: a git that
 * outlived its request would write the thread's repository under the place's next one.
 */
export function askHistory(asked: Asked, signal: AbortSignal, command: readonly string[] = HISTORY): Promise<string> {
  return new Promise((resolve, reject) => {
    const [program = "", ...args] = command;
    const child = spawn(program, args, {
      cwd: "/", detached: true, stdio: ["pipe", "pipe", "ignore"],
      env: { PATH: "/usr/local/bin:/usr/bin:/bin", HOME: "/nonexistent", LANG: "C.UTF-8" },
    });
    const pieces: Buffer[] = [];
    let size = 0;
    // The history itself has ended, and its group with it.
    let left = false;
    const end = () => {
      try {
        if (child.pid !== undefined && !left) process.kill(-child.pid, "SIGKILL");
      } catch {
        // The group has gone.
      }
    };
    signal.addEventListener("abort", end, { once: true });
    if (signal.aborted) end();
    child.stdout.on("data", (piece: Buffer) => {
      size += piece.length;
      if (size > ANSWER_BYTES) end();
      else pieces.push(piece);
    });
    child.stdin.on("error", () => {});
    // One that could not be started closes with no exit: it is answered there.
    child.on("error", () => {});
    // The history has ended, however it did: what it left running goes with it. Once its group is
    // empty the group's number may be another's, so nothing signals it after this.
    child.once("exit", () => {
      signal.removeEventListener("abort", end);
      end();
      left = true;
    });
    child.once("close", (code) => {
      signal.removeEventListener("abort", end);
      void (child.pid === undefined ? Promise.resolve() : emptied(child.pid)).then(() => {
        if (code === 0 && size <= ANSWER_BYTES) resolve(Buffer.concat(pieces).toString("utf8"));
        else reject(new Error("the history ended without an answer"));
      });
    });
    child.stdin.end(JSON.stringify(asked));
  });
}

// What the history wrote, as an outcome. An answer with an error is a refusal only as a code, which
// whoever asked goes by, and its own words, both text: an error that is not both is no answer, and
// never taken for the action's own.
function read(said: string): Outcome {
  let answer: unknown;
  try {
    answer = JSON.parse(said);
  } catch {
    return NO_ANSWER;
  }
  if (typeof answer !== "object" || answer === null || Array.isArray(answer)) return NO_ANSWER;
  if (!("error" in answer)) return { ok: answer };
  const { code, message } = (answer.error ?? {}) as { code?: unknown; message?: unknown };
  if (typeof code !== "string" || typeof message !== "string") return NO_ANSWER;
  return { error: { type: "history", code: code.slice(0, CODE_UNITS), message: message.slice(0, MESSAGE_UNITS) } };
}

export interface PlacesOptions {
  folder?: string; // where the places are mounted: PLACES
  mount?(args: string[]): Promise<unknown>; // mount(8), by its arguments
  unmount?(args: string[]): Promise<unknown>; // umount(8)
  // A share the host has just added is there once the guest's kernel has found its device: until then its mount is tried again.
  mountMs?: number;
  // The history's answer to one request, as it wrote it; rejects when it ended without one. At *signal*
  // it ends everything it started, and it settles only once none of that is left.
  ask?(asked: Asked, signal: AbortSignal): Promise<string>;
  // How long one request has from when it starts.
  historyMs?: number;
  // How long one request waits for its turn: as long as the one before it has, and as long again as what that ran is given to end.
  waitMs?: number;
}

interface Mounted {
  history: Share;
  real: Share;
}

export class Places {
  private readonly mounted = new Map<string, Mounted>();
  // Each place's mounting and letting go, one after another: the end of the last one asked.
  private readonly changes = new Map<string, Promise<void>>();
  // Each place's requests, one after another: the end of the last one asked, with everything it ran.
  private readonly turns = new Map<string, Promise<void>>();
  // Each place's requests that have not ended, by what ends each.
  private readonly asked = new Map<string, Set<AbortController>>();
  // The guest is stopping: no request starts.
  private stopped = false;
  private readonly ask: (asked: Asked, signal: AbortSignal) => Promise<string>;
  private readonly historyMs: number;
  private readonly waitMs: number;
  private readonly folder: string;
  private readonly run: { mount(args: string[]): Promise<unknown>; unmount(args: string[]): Promise<unknown> };
  private readonly mountMs: number;

  constructor(options: PlacesOptions = {}) {
    this.folder = options.folder ?? PLACES;
    this.run = {
      mount: options.mount ?? ((args) => execute("/usr/bin/mount", args)),
      unmount: options.unmount ?? ((args) => execute("/usr/bin/umount", args)),
    };
    this.mountMs = options.mountMs ?? BOUNDS.mountMs;
    this.ask = options.ask ?? ((asked, signal) => askHistory(asked, signal));
    this.historyMs = options.historyMs ?? BOUNDS.historyMs;
    this.waitMs = options.waitMs ?? this.historyMs + BOUNDS.killedMs;
  }

  /** Where the place of *key* is mounted: its history, and the folder. Throws for one that is not. */
  paths(key: string): { store: string; folder: string } {
    if (!this.mounted.has(key)) throw new Error(NOT_HERE);
    return { store: join(this.folder, key, "history"), folder: join(this.folder, key, "real") };
  }

  /**
   * The place of *key* from the host's two shares, once: a second thread on the folder finds it
   * mounted. One that could not be mounted is asked for again.
   */
  mount(key: string, history: Share, real: Share): Promise<void> {
    if (typeof key !== "string" || !PLACE_KEY.test(key)) return Promise.reject(new Error(`not a folder's key: ${String(key).slice(0, 64)}`));
    for (const share of [history, real]) if (!TAG.test(share.tag)) return Promise.reject(new Error(`not a share tag: ${share.tag.slice(0, 64)}`));
    return this.after(key, async () => {
      const known = this.mounted.get(key);
      if (known) {
        if (known.history.tag !== history.tag || known.real.tag !== real.tag) throw new Error("This folder's history is already mounted from other shares");
        return;
      }
      await this.make(key, history, real);
      this.mounted.set(key, { history, real });
    });
  }

  // *work* on the place of *key*, once whatever was asked of it before has ended. A lazy unmount
  // takes whatever is mounted at its path: one still under way would take the mounts of the same
  // place, asked for again meanwhile, with it.
  private after(key: string, work: () => Promise<void>): Promise<void> {
    const done = (this.changes.get(key) ?? Promise.resolve()).then(work);
    const ended = done.catch(() => {});
    this.changes.set(key, ended);
    void ended.then(() => {
      if (this.changes.get(key) === ended) this.changes.delete(key);
    });
    return done;
  }

  private async make(key: string, history: Share, real: Share): Promise<void> {
    const at = join(this.folder, key);
    await mkdir(join(at, "history"), { recursive: true });
    await mkdir(join(at, "real"), { recursive: true });
    await chmod(this.folder, 0o700);
    await this.attach(["-t", "virtiofs", "-o", OPTIONS, history.tag, join(at, "history")]);
    try {
      await this.attach(["-t", "virtiofs", "-o", `ro,${OPTIONS}`, real.tag, join(at, "real")]);
    } catch (error) {
      await this.run.unmount(["-l", join(at, "history")]).catch(() => {});
      throw error;
    }
  }

  private async attach(args: string[]): Promise<void> {
    for (const deadline = performance.now() + this.mountMs; ;) {
      try {
        await this.run.mount(args);
        return;
      } catch (error) {
        if (performance.now() > deadline) throw error;
        await new Promise((resolve) => setTimeout(resolve, MOUNT_RETRY_MS));
      }
    }
  }

  /**
   * One request to the history of *key*, as the agent's own user and outside every root's
   * namespaces. One at a time on a place: a push reads the history's refs and then writes them,
   * and only one writer may be between the two. A request is ended by the host's cancel, at its
   * bound, when its place is let go and at the guest's stop: each is answered at once, and what
   * the request ran has all gone before the place's next one starts. Its wait for its turn is
   * bounded as its run is: where what a request before it ran cannot end, it is answered that
   * the history is held, and never starts. Never rejects.
   */
  history(key: string, request: Pick<Asked, "thread" | "user" | "action" | "args">, signal: AbortSignal): Promise<Outcome> {
    if (this.stopped) return Promise.resolve(SANDBOX_STOPPED);
    let paths: { store: string; folder: string };
    try {
      paths = this.paths(key);
    } catch (error) {
      return Promise.resolve({ error: { type: "unavailable", message: (error as Error).message } });
    }
    if (signal.aborted) return Promise.resolve(CANCELLED);
    const end = new AbortController();
    const cancel = () => end.abort();
    signal.addEventListener("abort", cancel, { once: true });
    const asked = this.asked.get(key) ?? new Set();
    this.asked.set(key, asked.add(end));
    let began = false;
    let held = false;
    const waiting = setTimeout(() => {
      held = true;
      end.abort();
    }, this.waitMs);
    const answered = (this.turns.get(key) ?? Promise.resolve()).then(async () => {
      clearTimeout(waiting);
      if (end.signal.aborted) return null;
      began = true;
      const bound = setTimeout(cancel, this.historyMs);
      try {
        return await this.ask({ ...paths, ...request }, end.signal);
      } finally {
        clearTimeout(bound);
      }
    });
    const turn = answered.then(() => {}, () => {}).then(() => {
      signal.removeEventListener("abort", cancel);
      asked.delete(end);
      if (asked.size === 0 && this.asked.get(key) === asked) this.asked.delete(key);
      if (this.turns.get(key) === turn) this.turns.delete(key);
    });
    this.turns.set(key, turn);
    const outcome = answered.then((said) => (said === null ? NO_ANSWER : read(said)), (): Outcome => NO_ANSWER);
    return new Promise((resolve) => {
      // Ended before it answered. The host's cancel is the session's, and the guest's stop is answered as the
      // host answers a guest that went; one that never started found its place held, or gone.
      const ended = () => {
        clearTimeout(waiting);
        const unstarted: Outcome = { error: { type: "unavailable", message: held ? HELD : NOT_HERE } };
        resolve(signal.aborted ? CANCELLED : this.stopped ? SANDBOX_STOPPED : began ? NO_ANSWER : unstarted);
      };
      end.signal.addEventListener("abort", ended, { once: true });
      void outcome.then((settled) => {
        end.signal.removeEventListener("abort", ended);
        if (end.signal.aborted) ended();
        else resolve(settled);
      });
    });
  }

  /**
   * The guest's stop: every request is ended, and none starts after. Settles once everything they
   * ran has gone, or after *ms*: one that waits on a share that stalled cannot end.
   */
  async stop(ms = BOUNDS.killedMs): Promise<void> {
    this.stopped = true;
    for (const asked of this.asked.values()) for (const request of asked) request.abort();
    let timer: NodeJS.Timeout | undefined;
    await Promise.race([Promise.all(this.turns.values()), new Promise((resolve) => {
      timer = setTimeout(resolve, ms);
    })]);
    clearTimeout(timer);
  }

  /** Both mounts of *key* go, lazily, the folder's first: the host removes its two shares next. */
  unmount(key: string): Promise<void> {
    return this.after(key, async () => {
      if (!this.mounted.delete(key)) return;
      // No request of the place outlives its mounts: each is ended, and what it ran has gone before they do.
      for (const request of this.asked.get(key) ?? []) request.abort();
      await this.turns.get(key);
      const at = join(this.folder, key);
      for (const name of ["real", "history"]) {
        await this.run.unmount(["-l", join(at, name)]).catch(() => {});
        await rmdir(join(at, name)).catch(() => {});
      }
      await rmdir(at).catch(() => {});
    });
  }
}
