// A folder's place in the guest (spec, Section 13, "The user's computer"): its history from the
// app's data, mounted for writing, and the folder itself, read-only, for the agent's own git.
// Each is mounted at a path of the folder's key, in the agent's mount namespace alone: a
// root's namespaces leave /run behind (vm/enter-root), so no command of a thread's reaches
// the history, another thread's copy or the real files.

import { execFile } from "node:child_process";
import { chmod, mkdir, rmdir } from "node:fs/promises";
import { join } from "node:path";
import { promisify } from "node:util";

import { PLACE_KEY, type Share } from "./protocol.js";
import { BOUNDS, TAG } from "./root.js";

export const PLACES = "/run/surogate/places";
const MOUNT_RETRY_MS = 25;
// Nothing in either is a program to run: git's hooks never are, and a file of the user's is data here.
const OPTIONS = "nosuid,nodev,noexec";

const execute = promisify(execFile);

export interface PlacesOptions {
  folder?: string; // where the places are mounted: PLACES
  mount?(args: string[]): Promise<unknown>; // mount(8), by its arguments
  unmount?(args: string[]): Promise<unknown>; // umount(8)
  // A share the host has just added is there once the guest's kernel has found its device: until then its mount is tried again.
  mountMs?: number;
}

interface Mounted {
  history: Share;
  real: Share;
}

export class Places {
  private readonly mounted = new Map<string, Mounted>();
  // Each place's mounting and letting go, one after another: the end of the last one asked.
  private readonly changes = new Map<string, Promise<void>>();
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
  }

  /** Where the place of *key* is mounted: its history, and the folder. Throws for one that is not. */
  paths(key: string): { store: string; folder: string } {
    if (!this.mounted.has(key)) throw new Error("This folder's history is not in the sandbox");
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

  /** Both mounts of *key* go, lazily, the folder's first: the host removes its two shares next. */
  unmount(key: string): Promise<void> {
    return this.after(key, async () => {
      if (!this.mounted.delete(key)) return;
      const at = join(this.folder, key);
      for (const name of ["real", "history"]) {
        await this.run.unmount(["-l", join(at, name)]).catch(() => {});
        await rmdir(join(at, name)).catch(() => {});
      }
      await rmdir(at).catch(() => {});
    });
  }
}
