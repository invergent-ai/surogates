// The data root's state schema at the app's start: each start compares the app's own, package.json's stateSchema, with
// the highest one an app has recorded in the data root, and an older app refuses to start on newer data, in words that
// name the version that changed it. On macOS and Windows, where an older app can be put over a newer one (an older .dmg
// dragged over it, an older MSI an administrator runs); on Linux the root helper refuses a rollback itself
// (release/install.sh), so no app records anything there. Electron-free.

import { closeSync, constants, lstatSync, openSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { forThisOs } from "../platform.js";

/** Whether this OS's app records the state schema it leaves. */
export const RECORDS_STATE = forThisOs({ darwin: true, win32: true }) ?? false;

/** The record's name in the data root. */
export const STATE_RECORD = "state-schema.json";

/** Why the app does not start: its dialog's message and detail. */
export interface Refused {
  message: string;
  detail: string;
}

// The record, {"schema": n, "version": "x.y.z"}, of the highest schema recorded and the version that first left it;
// null for none, or for one that is not one, which is recorded afresh.
function recorded(path: string): { schema: number; version: string } | null {
  const found = lstatSync(path, { throwIfNoEntry: false });
  if (!found?.isFile()) return null;
  try {
    const { schema, version } = JSON.parse(readFileSync(path, "utf8")) as { schema?: unknown; version?: unknown };
    return Number.isSafeInteger(schema) && (schema as number) >= 1 && typeof version === "string" ? { schema: schema as number, version } : null;
  } catch {
    return null;
  }
}

/**
 * Whether the app of *version*, whose state schema is *schema*, may start on the data root *root*: null where it may,
 * having recorded its schema there where it is the highest yet; why not where an app of a newer schema left the data.
 * With *records* false, as on Linux, nothing is read or written.
 */
export function newerData(root: string, schema: number, version: string, records: boolean = RECORDS_STATE): Refused | null {
  if (!records) return null;
  const path = join(root, STATE_RECORD);
  const found = recorded(path);
  if (found && found.schema > schema) {
    return {
      message: "This Surogate is older than the data it would open",
      detail: `Surogate ${version} cannot open this computer's Surogate data, which Surogate ${found.version} changed. Install Surogate ${found.version} or later.`,
    };
  }
  if (found && found.schema === schema) return null;
  // Made afresh beside it and put in place by one rename: a start cut off leaves the old record or the new.
  const partial = `${path}.partial`;
  rmSync(partial, { force: true });
  const fd = openSync(partial, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
  try {
    writeFileSync(fd, `${JSON.stringify({ schema, version })}\n`);
  } finally {
    closeSync(fd);
  }
  renameSync(partial, path);
  return null;
}
