// The copies of their folders that a project's threads work in, as the app asks for them (spec,
// Section 13, "The user's computer"). A thread's copy is made before its root's first operation:
// git in the guest makes it, in the folder's place in the app's data, and the app takes it only
// as a folder of its own at the path it named. Only the guest can tell a whole copy from one
// whose making was cut short, so the app has the guest open each root's copy, and after that a
// copy that is still the folder the guest left costs no request. The app never walks a place or
// removes anything in it: its guest writes there.
//
// One thing at a time on a root's copy: its making, and each request to its history. A folder's
// place stays in the guest while a copy on it is open or a request runs, and is let go a while
// after the last of them, so a guest that holds nothing else can stop; the next request adds it
// again. Before another folder's place is made under its key, and whenever the place's folder in
// the app's data is not the one the app knew, the place is let go: each host on its copies first,
// then the guest, by the place it was given. What the app knew of its copies goes with it.
//
// A thread whose folder has no history works nowhere: no copy is made, and it is told why. The
// guest's word can deny a thread its copy; nothing it says gives a thread the folder itself.

import { lstat, realpath } from "node:fs/promises";

import { BOOT_ID } from "../binding/folder.js";
import { CANCELLED } from "../guest/command.js";
import { FOLDER_UNAVAILABLE } from "../hosts/messages.js";
import type { BoundFolder } from "../hosts/tool-hosts.js";
import type { Outcome } from "../link/protocol.js";
import type { VmClient } from "../vm/client.js";
import type { HistoryCode } from "../vm/history.js";
import { type Folder, late, type Place } from "../vm/manager.js";
import { copyOf, FolderReplaced, keyOf, placeOf } from "./place.js";

// A thread's copy as the guest left it: the folder's place, the copy's folder with its identity, and
// the folder it is a copy of, by whose path the thread's tools and commands name its files.
export interface Copy {
  place: Place;
  folder: Folder;
  at: string;
}

// One host's hold on its root's copy, given with the copy and let go by close: a hold is closed once.
export interface Handle {
  readonly root: string;
}

// What a root bound to a copy works in, with the hold on it of the host it is given for; or why it works nowhere.
export type Opened = { copy: Copy; handle: Handle } | { failed: Outcome };

// A copy the guest is asked to make, or to make again, for *root*'s work in *folder*: begun, and ended
// however it ended. A first copy of a large folder takes minutes, and whoever shows this tells the person.
export interface Making {
  root: string;
  folder: string;
  state: "begun" | "ended";
}

export interface CopiesOptions {
  dataDir: string;
  // Who this device's threads were started by: a folder's first commit, and its pickups, are theirs.
  user: string;
  vm: Pick<VmClient, "history" | "unplace">;
  // How long a folder's place stays in the guest with no copy on it open and no request running.
  idleMs?: number;
  // How long a place that is to be let go for another waits for the hosts on its copies to let them go.
  closeMs?: number;
  // Told when the copy a root's host works in is that root's copy no more: the guest made it again, left
  // it other than whole, or its place is being let go. The host goes, and its close follows.
  replaced?(root: string): void;
  // Told as the guest is asked to open a copy this computer does not know as whole, which it may make, from
  // nothing or again, and as that ends.
  making?(event: Making): void;
  // How long an open runs before an answer that is no copy is taken as the history's bound cutting it.
  cutMs?: number;
  // Asked before any request to a folder's history that may read the folder itself, *folder*: null where it may be read
  // now, or what the request is answered instead, and nothing is asked. A landing cut short there may have left a file of
  // the user's beside its name, which a read would take for one the user deleted (hosts/tool-hosts.ts, recoverBefore).
  readable?(folder: string, signal: AbortSignal): Promise<Outcome | null>;
}

export const PLACE_IDLE_MS = 120_000;
// A host told to go stops within its own bound (hosts/tool-hosts.ts, STOP_TIMEOUT_MS), with room.
export const CLOSE_MS = 30_000;
// How long a look in the app's data may take: a dead mount never answers.
const LOOK_MS = 5_000;
// How often one operation asks again for its copy where the folder's place was let go under it.
const TURNS = 4;
// The history ends an open at its bound: its git calls at 570 s, the guest's agent the request at 600 s (and
// this computer one the agent does not answer at 1,215 s). An open that ran this long and made no copy was cut.
export const CUT_MS = 500_000;
// A thread whose copy of a folder was cut so often is not made one again: each try starts it from nothing.
const CUTS = 2;

// Why a folder has no history, as its history says it (surogates/sandbox/local_history.py, _off):
// more files than one tracks, or a name in it that is not UTF-8.
type Reason = "cap" | "names";
const OFF: Record<Reason, string> = {
  cap: "holds more files than a project's history on this computer takes, so no thread of the project works in it. Choose a folder inside it that holds fewer",
  names:
    "holds a file whose name a project's history on this computer cannot record, as it is not UTF-8, so no thread of the project works in it. Choose a folder inside it that holds no such name, or rename the file",
};

/** What every operation of a thread whose *folder* has no history is answered, but its turn's own open. */
export const historyOff = (reason: Reason, folder: string): Outcome => ({ error: { type: "history_off", message: `The folder ${folder} ${OFF[reason]}` } });

// What every operation of a thread is answered whose copy of *folder* could not be made within the history's bound, twice.
const tooLarge = (folder: string): Outcome => ({
  error: {
    type: "history_off",
    message: `The folder ${folder} is too large for a thread of the project to have a copy of its own on this computer: its copy could not be made within the time a copy may take, twice. Choose a folder inside it that holds less`,
  },
});

// The history's code for a copy whose making was cut short, or whose repository it had to make again
// (local_history.py, NO_WHOLE_COPY): no request works in such a copy, and its next open makes it whole.
// The words beside the code are a person's, and nothing here reads them.
const NO_WHOLE_COPY: HistoryCode = "no_whole_copy";
const notWhole = (outcome: Outcome): boolean => "error" in outcome && outcome.error.type === "history" && outcome.error.code === NO_WHOLE_COPY;
// A step's refusals after which its copy is as it was: nothing ran, or what ran wrote none of the copy. Any other
// answer that is not one may have been cut in the middle of the copy, or names it not whole.
const AS_IT_WAS: ReadonlySet<unknown> = new Set<HistoryCode>(["not_a_request", "conflict", "landing_unsettled", "not_on_base", "name_not_utf8", "history_refused"]);
// The agent's own "value" is a request it would not take, and ran nothing of.
const asItWas = (outcome: Outcome): boolean =>
  "error" in outcome && (outcome.error.type === "value" || (outcome.error.type === "history" && AS_IT_WAS.has(outcome.error.code)));

// The requests that read the folder itself not at all, or only to refuse: a checkpoint's, in the copy and the history
// alone; and a landing's forgetting, which refuses where a file is not what was there before, as one moved aside is
// not, and which is asked from inside its landing's last step, where waiting for that landing would wait for itself.
const NOT_READING: ReadonlySet<string> = new Set(["snapshot", "restore", "forget"]);

const unavailable = (why: string): Outcome => ({
  error: { type: "unavailable", message: `This computer could not make this thread's copy of its folder: ${why}` },
});
const describe = (error: unknown) => (error instanceof Error ? error.message : String(error));
const NOT_OWN = "what its sandbox left at its path is not a folder of the app's own";
const CHANGED = "the folder's history was let go each time this thread's copy was asked for";

// Why the guest's answer to an open says the folder has no history, or null for any other answer.
function offOf(outcome: Outcome): Reason | null {
  if (!("ok" in outcome)) return null;
  const { history, reason } = (outcome.ok ?? {}) as { history?: unknown; reason?: unknown };
  return history === "off" && (reason === "cap" || reason === "names") ? reason : null;
}

interface Identity {
  dev: number;
  ino: number;
}
const same = (a: Identity, b: Identity) => a.dev === b.dev && a.ino === b.ino;

// What is at *path* in the app's data: a folder of its own, no link at its name or on its way, with its
// identity; nothing; or anything else. Throws for a look that did not answer in time.
async function look(path: string): Promise<Identity | "other" | null> {
  const looked = (async (): Promise<Identity | "other" | null> => {
    try {
      const found = await lstat(path);
      if (!found.isDirectory() || (await realpath(path)) !== path) return "other";
      return { dev: found.dev, ino: found.ino };
    } catch (error) {
      return (error as NodeJS.ErrnoException).code === "ENOENT" ? null : "other";
    }
  })();
  const answer = await Promise.race([looked, late(LOOK_MS)]);
  if (answer === "late") throw new Error(`the app's data did not answer within ${LOOK_MS / 1000} s`);
  return answer;
}

// What the app knows of a folder's place: the place, its folder in the app's data as it was found, each
// root's copy in it as the guest's last answered open left it, which is whole while it is there, and how
// often each root's open was cut by the history's bound.
interface Known {
  place: Place;
  store: Identity;
  copies: Map<string, Identity>;
  cuts: Map<string, number>;
}

// A place in the guest, as the first request since it was last let go gave it, and how much holds it.
interface Held {
  place: Place;
  count: number;
  timer?: NodeJS.Timeout;
}

// A root whose copy hosts work in: each host's hold, its copy as the last of them was given it, and the hold
// on its place.
interface Open {
  copy: Copy;
  handles: Set<Handle>;
  held: Held;
}

type Made = { copy: Copy; known: Known; handle?: Handle } | { failed: Outcome };
// A request not sent: the place it was for was let go before it could be.
const STALE = Symbol("stale");

export class Copies {
  // Each root's work on its copy, one after another: the end of the last one asked.
  private readonly turns = new Map<string, Promise<void>>();
  private readonly known = new Map<string, Known>();
  private readonly held = new Map<string, Held>();
  private readonly opens = new Map<string, Open>();
  // Each key's lettings go, one after another, until the last has ended: a request of the key waits for
  // them, so the guest is never asked to add a place it is taking away.
  private readonly leaving = new Map<string, Promise<void>>();
  // How often each key's place was let go for another: a place found before one is found again.
  private readonly lettings = new Map<string, number>();
  // Told at each take and each close of a copy: a letting go waits for the hosts on its place's copies.
  private readonly changes = new Set<() => void>();
  // Aborted at the app's stop: a copy's making is stopped by nothing else.
  private readonly halt = new AbortController();
  private readonly idleMs: number;
  private readonly cutMs: number;

  constructor(private readonly options: CopiesOptions) {
    this.idleMs = options.idleMs ?? PLACE_IDLE_MS;
    this.cutMs = options.cutMs ?? CUT_MS;
  }

  /**
   * The copy *root* works in, made if there is none, for a file host that starts on it, with that
   * host's hold on it, which holds it open until it is closed. A cancel is answered at once, holds
   * nothing, and the making goes on for the next to ask: git stopped mid-way would leave half a copy.
   * Never rejects.
   */
  async open(root: string, bound: BoundFolder, signal: AbortSignal): Promise<Opened> {
    const work = this.inTurn(root, async (): Promise<Opened> => {
      const made = await this.ensured(root, bound, true);
      return "failed" in made ? made : { copy: made.copy, handle: made.handle! };
    }).catch((error: unknown): Opened => ({ failed: unavailable(describe(error)) }));
    const opened = await this.until(signal, work);
    if (opened !== "cancelled") return opened;
    // Whoever asked has gone, and nothing would let its copy go: it is let go once it is made.
    void work.then((taken) => {
      if ("handle" in taken) this.close(taken.handle);
    });
    return { failed: CANCELLED };
  }

  /**
   * One request to the history of *root*'s folder, for its own copy, which is made first for every
   * action but a turn's own open; that one is asked as it came, and answered as the guest answered
   * it. A request the history refuses for want of a whole copy is asked again once, after an open
   * that does not move the copy; refused again, it is answered so in words. A cancel is answered at
   * once, and stops the request's git in the guest. Never rejects.
   */
  async ask(root: string, bound: BoundFolder, action: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome> {
    const outcome = await this.until(signal, this.inTurn(root, async (): Promise<Outcome> => {
      if (signal.aborted) return CANCELLED;
      return action === "open" ? this.turnOpen(root, bound, args, signal) : this.step(root, bound, action, args, signal);
    }).catch((error: unknown) => unavailable(describe(error))));
    return outcome === "cancelled" ? CANCELLED : outcome;
  }

  /**
   * The host given *handle* has let its root's copy go: once every host has, the folder's place is let
   * go when idle. A hold closed already lets go of nothing more.
   */
  close(handle: Handle): void {
    const open = this.opens.get(handle.root);
    if (!open?.handles.delete(handle)) return;
    if (open.handles.size > 0) return;
    this.opens.delete(handle.root);
    this.release(open.held);
    this.changed();
  }

  /** The app's stop: no place is let go later, and a copy's making under way is told to stop. */
  stop(): void {
    this.halt.abort();
    this.held.clear();
  }

  private async turnOpen(root: string, bound: BoundFolder, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome> {
    for (let turn = 0; turn < TURNS; turn += 1) {
      const found = await this.found(root, bound);
      if ("failed" in found) return found.failed;
      if (signal.aborted) return CANCELLED;
      const outcome = await this.opened(found.known, found.place, root, args, signal);
      if (outcome !== STALE) return outcome;
    }
    return unavailable(CHANGED);
  }

  private async step(root: string, bound: BoundFolder, action: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome> {
    let again = false;
    for (let turn = 0; turn < TURNS; turn += 1) {
      const made = await this.ensured(root, bound);
      if ("failed" in made) return made.failed;
      if (signal.aborted) return CANCELLED;
      const answer = await this.request(made.known, made.copy.place, root, action, args, signal);
      if (answer === STALE) continue;
      if (!notWhole(answer) || !("error" in answer)) return answer;
      if (again) {
        const { message } = answer.error;
        return {
          error: {
            ...answer.error,
            message: `This computer made this thread's copy of its folder again, and its history still found it not whole, so this was not done: ${message}`,
          },
        };
      }
      // As the history says: its next open makes the copy whole, here without moving it.
      again = true;
    }
    return unavailable(CHANGED);
  }

  // *root*'s copy: the folder the guest left at its last open, if that is still there, else opened by the
  // guest, which makes one that is missing or was cut short, and does not move it. *take*: held open for a
  // host, as it is found to be the copy, before anything can let its place go.
  private async ensured(root: string, bound: BoundFolder, take = false): Promise<Made> {
    for (let turn = 0; turn < TURNS; turn += 1) {
      const found = await this.found(root, bound);
      if ("failed" in found) return found;
      const { known, place } = found;
      const whole = known.copies.get(root);
      if (whole) {
        const there = await look(copyOf(place, root));
        if (this.known.get(place.key) !== known) continue;
        if (there !== null && there !== "other" && same(there, whole)) return this.made(root, copyIn(place, root, whole, bound), known, take);
        known.copies.delete(root);
      }
      // The app's own open, which whoever asked does not cut short.
      const outcome = await this.opened(known, place, root, { moves: false }, this.halt.signal);
      if (outcome === STALE) continue;
      if (!("ok" in outcome)) return { failed: outcome };
      const reason = offOf(outcome);
      if (reason) return { failed: historyOff(reason, bound.folder) };
      const made = known.copies.get(root);
      if (!made || this.known.get(place.key) !== known) continue;
      return this.made(root, copyIn(place, root, made, bound), known, take);
    }
    return { failed: unavailable(CHANGED) };
  }

  private made(root: string, copy: Copy, known: Known, take: boolean): Made {
    return take ? { copy, known, handle: this.take(root, copy) } : { copy, known };
  }

  // The place of *root*'s folder, and what the app knows of it. A place found before a letting go of its
  // key had ended is found again; one whose folder is not the one the app knew under its key is let go,
  // and found again. A letting go under way is not waited for here: a request waits for it.
  private async found(root: string, bound: BoundFolder): Promise<{ known: Known; place: Place } | { failed: Outcome }> {
    // A binding names its thread's copy, which is the root's own (binding/binder.ts).
    if (bound.history !== root) return { failed: unavailable("this chat is bound to another thread's copy") };
    const key = keyOf(bound.folder);
    for (let turn = 0; turn < TURNS; turn += 1) {
      const lettings = this.lettings.get(key) ?? 0;
      try {
        const { place } = await placeOf(this.options.dataDir, bound, { letGo: (was, store) => this.letGo(was, store) });
        const store = await look(place.history);
        if (store === null || store === "other") return { failed: unavailable("its place in the app's data is not a folder of the app's own") };
        if ((this.lettings.get(key) ?? 0) !== lettings) continue;
        const known = this.known.get(key);
        if (known && same(known.store, store)) return (known.cuts.get(root) ?? 0) >= CUTS ? { failed: tooLarge(bound.folder) } : { known, place };
        if (known) {
          // Not the place the app knew, though nothing of the app's let that one go: it goes now.
          await this.letGo(known.place, known.store.ino);
          continue;
        }
        const fresh: Known = { place, store, copies: new Map(), cuts: new Map() };
        this.known.set(key, fresh);
        return { known: fresh, place };
      } catch (error) {
        return { failed: error instanceof FolderReplaced ? FOLDER_UNAVAILABLE : unavailable(describe(error)) };
      }
    }
    return { failed: unavailable(CHANGED) };
  }

  // The guest's answer to an open of *root*'s copy, taken with what it left: the folder now at the copy's
  // path is the whole copy, another one where the guest made it again, and anything else there is none.
  // STALE where the place was let go before the open could be asked.
  private async opened(known: Known, place: Place, root: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome | typeof STALE> {
    // Refused before it reads the folder: the copy is as it was.
    const refused = await this.readable(place, signal);
    if (refused) return refused;
    // Not known as whole: the guest may make it, which takes as long as the folder is large.
    const making = !known.copies.has(root);
    known.copies.delete(root);
    const sent = { at: Number.NaN };
    const outcome = await this.request(known, place, root, "open", args, signal, () => {
      sent.at = performance.now();
      if (making) this.options.making?.({ root, folder: place.real.path, state: "begun" });
    });
    if (!Number.isNaN(sent.at)) {
      if (making) this.options.making?.({ root, folder: place.real.path, state: "ended" });
      const cut = outcome !== STALE && "error" in outcome && outcome.error.type !== "cancelled" && performance.now() - sent.at >= this.cutMs;
      if (cut) known.cuts.set(root, (known.cuts.get(root) ?? 0) + 1);
    }
    if (outcome === STALE || !("ok" in outcome)) return outcome;
    if (offOf(outcome)) {
      this.unvouched(known, root);
      return outcome;
    }
    const found = await look(copyOf(place, root)).catch((error: unknown) => describe(error));
    // A folder of the app's own, and no other thread's copy: a guest that is not ours could move one to this name.
    const copy = typeof found === "object" && found !== null && ![...known.copies.values()].some((other) => same(other, found)) ? found : null;
    const worked = this.opens.get(root)?.copy.folder;
    if (worked && (copy === null || !same(worked, copy))) this.options.replaced?.(root);
    if (copy === null) return unavailable(typeof found === "string" && found !== "other" ? found : NOT_OWN);
    known.copies.set(root, copy);
    return outcome;
  }

  // Null where *place*'s folder may be read now; otherwise what a request that reads it is answered instead.
  private async readable(place: Place, signal: AbortSignal): Promise<Outcome | null> {
    return (await this.options.readable?.(place.real.path, signal)) ?? null;
  }

  // One request to *place*'s history for *root*'s copy, the place held in the guest while it runs: once
  // every letting go of the place asked before it has ended, or STALE where one let it go for another.
  private async request(
    known: Known, place: Place, root: string, action: string, args: Record<string, unknown>, signal: AbortSignal, sent?: () => void,
  ): Promise<Outcome | typeof STALE> {
    // Every request but those reads the folder only once what a landing cut short there is back at its name; an open
    // was asked so already, before anything of the copy was let go for it (opened).
    const refused = NOT_READING.has(action) || action === "open" ? null : await this.readable(place, signal);
    if (refused) return refused;
    await this.settled(place.key);
    if (this.known.get(place.key) !== known) return STALE;
    const held = this.hold(place);
    let outcome: Outcome;
    try {
      sent?.();
      outcome = await this.options.vm.history({ place, thread: root, user: this.options.user, action, args }, signal);
    } catch (error) {
      outcome = unavailable(describe(error));
    } finally {
      this.release(held);
    }
    // An open answered with no copy, or a step stopped, cut or refused as not whole: what it may have left of the
    // copy is the guest's next open's to make whole, and no host works in it meanwhile.
    if (!("ok" in outcome) && (action === "open" || !asItWas(outcome))) this.unvouched(known, root);
    return outcome;
  }

  // The app vouches for *root*'s copy no more, before its root's next operation: the guest's next open makes it
  // whole, and each host on it is told, as for a copy made again.
  private unvouched(known: Known, root: string): void {
    known.copies.delete(root);
    if (this.opens.has(root)) this.options.replaced?.(root);
  }

  // The place *was*, whose folder in the app's data has the inode *store*, leaves this computer's hands, for
  // another: each host on one of its copies is told to go, and lets its copy go, then the guest lets go of the
  // place it was given. Only that place: one made at its path since, which the app may know and the guest
  // hold under the same key, is another, and is left alone. False where the app holds nothing of it. Rejects,
  // and lets nothing go, where a host has not let go within closeMs.
  private letGo(was: Place, store: number): Promise<boolean> {
    const { key } = was;
    const going = (this.leaving.get(key) ?? Promise.resolve()).then(async () => {
      const known = this.known.get(key);
      if (!known || known.place.history !== was.history || known.store.ino !== store) return false;
      const ms = this.options.closeMs ?? CLOSE_MS;
      const until = performance.now() + ms;
      const told = new Set<Handle>();
      for (let opens = this.holding(key); opens.length > 0; opens = this.holding(key)) {
        // Told again where a host took the copy since: it holds the place as well.
        for (const [root, open] of opens.filter(([, open]) => [...open.handles].some((handle) => !told.has(handle)))) {
          for (const handle of open.handles) told.add(handle);
          this.options.replaced?.(root);
        }
        // Let go as they were told.
        if (this.holding(key).length === 0) break;
        const left = until - performance.now();
        if (left <= 0) throw new Error(`a thread still works in the history of the folder that was at this path, and did not let it go within ${ms / 1000} s`);
        await this.change(left);
      }
      // In the moment the last host has let go: from here on no copy of the place is taken, nor any request sent.
      this.lettings.set(key, (this.lettings.get(key) ?? 0) + 1);
      this.known.delete(key);
      const held = this.held.get(key);
      if (!held) return true;
      this.held.delete(key);
      clearTimeout(held.timer);
      await this.options.vm.unplace(held.place);
      return true;
    });
    this.chain(key, going);
    return going;
  }

  // Once a host has taken a copy or let one go, or *ms* have passed.
  private change(ms: number): Promise<void> {
    return new Promise<void>((resolve) => {
      const done = () => {
        clearTimeout(timer);
        this.changes.delete(done);
        resolve();
      };
      const timer = setTimeout(done, ms);
      timer.unref();
      this.changes.add(done);
    });
  }

  private changed(): void {
    for (const done of [...this.changes]) done();
  }

  // The roots whose hosts work in a copy in the place of *key*.
  private holding(key: string): Array<[string, Open]> {
    return [...this.opens].filter(([, open]) => open.copy.place.key === key);
  }

  private take(root: string, copy: Copy): Handle {
    const handle: Handle = Object.freeze({ root });
    const open = this.opens.get(root);
    if (open) {
      open.handles.add(handle);
      open.copy = copy;
    } else {
      this.opens.set(root, { copy, handles: new Set([handle]), held: this.hold(copy.place) });
    }
    this.changed();
    return handle;
  }

  private hold(place: Place): Held {
    let held = this.held.get(place.key);
    if (!held) {
      held = { place, count: 0 };
      this.held.set(place.key, held);
    }
    clearTimeout(held.timer);
    held.timer = undefined;
    held.count += 1;
    return held;
  }

  private release(held: Held): void {
    held.count -= 1;
    // Not after the app's stop, which lets go of every hold.
    if (held.count > 0 || this.held.get(held.place.key) !== held) return;
    held.timer = setTimeout(() => this.idle(held), this.idleMs);
    held.timer.unref();
  }

  // Nothing has held *held* for idleMs: the guest lets it go, after any letting go of its key asked before.
  private idle(held: Held): void {
    const { key } = held.place;
    this.chain(key, (this.leaving.get(key) ?? Promise.resolve()).then(async () => {
      // Held again meanwhile, or let go already.
      if (held.count > 0 || this.held.get(key) !== held) return;
      this.held.delete(key);
      await this.options.vm.unplace(held.place);
    }));
  }

  // *going* is the last letting go of *key*'s place, until it has ended, whichever way.
  private chain(key: string, going: Promise<unknown>): void {
    const settled = going.then(() => {}, () => {});
    this.leaving.set(key, settled);
    void settled.then(() => {
      if (this.leaving.get(key) === settled) this.leaving.delete(key);
    });
  }

  // Once every letting go of *key*'s place asked so far has ended.
  private async settled(key: string): Promise<void> {
    for (let left = this.leaving.get(key); left; left = this.leaving.get(key)) await left;
  }

  // *work* once everything asked of *root*'s copy before it has ended.
  private inTurn<T>(root: string, work: () => Promise<T>): Promise<T> {
    const ran = (this.turns.get(root) ?? Promise.resolve()).then(work);
    const turn = ran.then(() => {}, () => {});
    this.turns.set(root, turn);
    void turn.then(() => {
      if (this.turns.get(root) === turn) this.turns.delete(root);
    });
    return ran;
  }

  // *work*'s answer, which never fails, or "cancelled" at once when *signal* aborts first.
  private until<T>(signal: AbortSignal, work: Promise<T>): Promise<T | "cancelled"> {
    return new Promise((resolve) => {
      const cancel = () => resolve("cancelled");
      if (signal.aborted) return cancel();
      signal.addEventListener("abort", cancel, { once: true });
      void work.then(resolve).finally(() => signal.removeEventListener("abort", cancel));
    });
  }
}

// *root*'s copy in *place*, as it was found: a folder of the app's own with that identity.
function copyIn(place: Place, root: string, { dev, ino }: Identity, bound: BoundFolder): Copy {
  return { place, folder: { path: copyOf(place, root), dev, ino, boot: BOOT_ID }, at: bound.folder };
}
