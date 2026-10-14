// How an update this app checked is put in place, by this OS (installer-linux.ts): where the
// installed app's record of its install is, and the updates of an installed app, with how each one
// is applied. Electron-free.

import type { Fetch } from "../download.js";
import { forThisOs, notBuilt } from "../platform.js";
import * as linux from "./installer-linux.js";
import { machineRecord } from "./installer-win32.js";
import type { Applied, Staged, UpdatesOptions } from "./updates.js";

export interface Installer {
  /** The installed app's record of where it was installed from, and of its channel: an administrator's alone to write. */
  record(): string;
  /** An installed app's updates of *version*, downloaded into *cache*, each applied as this OS installs one. */
  updates(version: string, cache: string, fetch: Fetch, signal: AbortSignal): UpdatesOptions;
  /** What applies a downloaded release with the helper *command* names, as a development build's test gives one. */
  helper(command: string[]): (files: Staged) => Promise<Applied>;
}

export const INSTALLER: Installer = forThisOs<Installer>({
  linux: { record: () => linux.ROOT_RECORD, updates: linux.installedUpdates, helper: linux.helperRun },
  win32: { record: () => machineRecord(), updates: () => notBuilt("Surogate Desktop's updates"), helper: () => notBuilt("Surogate Desktop's updates") },
}) ?? { record: () => notBuilt("Surogate Desktop's install"), updates: () => notBuilt("Surogate Desktop's updates"), helper: () => notBuilt("Surogate Desktop's updates") };
