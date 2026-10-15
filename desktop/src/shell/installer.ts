// How an update this app checked is put in place, by this OS (installer-linux.ts, installer-win32.ts,
// installer-darwin.ts): where the installed app's record of its install is, the updates of an installed app, with how
// each one is applied, and how the app removes itself where it does. Electron-free.

import { dirname, win32 } from "node:path";

import type { Fetch } from "../download.js";
import { HELPER } from "../native-helper.js";
import { forThisOs, notBuilt } from "../platform.js";
import * as darwin from "./installer-darwin.js";
import * as linux from "./installer-linux.js";
import * as windows from "./installer-win32.js";
import type { Places } from "./places.js";
import type { Applied, Staged, UpdatesOptions } from "./updates.js";

/** How the installed app removes itself from this computer, from the menu under the user's name. */
export interface Removal {
  /** That menu's item. */
  item: string;
  /** What the removal asks first: whether to go on, what goes and what stays, and whether the user's own data goes too. */
  asked(): { message: string; detail: string; checkbox: string };
  /** The user's own folders of the app's that go where they ask it. */
  forget(places: Places): string[];
  /** Removes the app from this computer, then the folders it is given. */
  run(forget: string[]): Promise<Applied>;
}

export interface Installer {
  /** The installed app's record of where it was installed from, and of its channel: an administrator's alone to write. */
  record(): string;
  /** An installed app's updates of *version*, downloaded into *cache*, each applied as this OS installs one. */
  updates(version: string, cache: string, fetch: Fetch, signal: AbortSignal): UpdatesOptions;
  /** What applies a downloaded release with the helper *command* names, as a development build's test gives one. */
  helper(command: string[]): (files: Staged) => Promise<Applied>;
  /** How the installed app removes itself from this computer; null where it does not. */
  uninstall: Removal | null;
  /** What an installed app does once a start of it reaches its main window: on macOS, what an update left beside it goes. */
  reachedWindow(): Promise<void>;
}

const nothing = (): Promise<void> => Promise.resolve();

export const INSTALLER: Installer = forThisOs<Installer>({
  linux: { record: () => linux.ROOT_RECORD, updates: linux.installedUpdates, helper: linux.helperRun, uninstall: null, reachedWindow: nothing },
  win32: {
    record: () => windows.machineRecord(), updates: windows.installedUpdates, helper: ([helper]) => windows.updateRun(helper!),
    uninstall: {
      item: "Remove Surogate from This Computer…", asked: () => windows.REMOVAL, forget: (places) => [places.root],
      run: windows.removeRun(win32.join(dirname(process.execPath), "bin", "surogate-helper.exe")),
    },
    reachedWindow: nothing,
  },
  darwin: {
    record: () => darwin.MACHINE_RECORD, updates: darwin.installedUpdates, helper: ([helper]) => darwin.updateRun(helper!, darwin.bundleOf() ?? ""),
    uninstall: {
      item: "Remove Surogate from This Mac…", asked: () => darwin.removalAsked(darwin.bundleOf()), forget: darwin.forgotten,
      run: (forget) => darwin.removeRun(HELPER, darwin.bundleOf())(forget),
    },
    reachedWindow: () => darwin.reachedWindow(darwin.bundleOf()),
  },
}) ?? {
  record: () => notBuilt("Surogate Desktop's install"), updates: () => notBuilt("Surogate Desktop's updates"), helper: () => notBuilt("Surogate Desktop's updates"),
  uninstall: null, reachedWindow: nothing,
};
