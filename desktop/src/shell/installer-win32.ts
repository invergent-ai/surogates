// What the MSI leaves an installed app on Windows (desktop/windows/msi/Surogate.wxs), and how an update is installed
// there: its record of where it was installed from, of its channel and of the version installed, in
// %ProgramData%\Surogate, SYSTEM's, which only administrators may change, as the app checks before it takes it
// (files/admins-own-win32.ts); the release keys the build wrote beside the app's code, in Program Files; and
// surogate-helper.exe's update, which asks Windows's UAC for an administrator to run surogate-admin.exe on the files
// the app downloaded, says once one has approved, and starts the app again after the MSI (native/windows/helper); and
// its remove, which has surogate-admin.exe uninstall the app the same way.

import { type ChildProcess, spawn } from "node:child_process";
import { basename, dirname, win32 } from "node:path";

import type { Fetch } from "../download.js";
import type { Applied, Staged, UpdatesOptions } from "./updates.js";

/** The install record's path: %ProgramData%\Surogate\install.json, %ProgramData% as Windows names it to this app. */
export function machineRecord(env: NodeJS.ProcessEnv = process.env): string {
  const data = env.ProgramData;
  if (!data || !/^[A-Za-z]:\\/.test(data)) throw new Error("Windows names no ProgramData folder for this app, so it cannot find its install record");
  return win32.join(data, "Surogate", "install.json");
}

// What the helper says once an administrator approved: the app quits on it (native/windows/helper, update.rs).
const APPROVED = "approved";
// The most that is kept of what the helper said, its end.
const SAID_MAX = 4000;

// The helper run with *args*: it answers once Windows's prompt has an administrator's approval, with restarts, as the
// app is to quit and the helper goes on; or, where none approved or it could not ask, as it ended, 1223 where the prompt
// was dismissed. The helper is never elevated, and outlives the app.
function helperRun(helper: string, args: string[]): Promise<Applied> {
  return new Promise((resolve) => {
    let child: ChildProcess;
    try {
      child = spawn(helper, args, { detached: true, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    } catch (error) {
      return resolve({ code: null, said: error instanceof Error ? error.message : String(error) });
    }
    let said = "";
    let heard = "";
    child.once("error", (error) => resolve({ code: null, said: error.message }));
    child.stderr?.setEncoding("utf8").on("data", (chunk: string) => {
      said = (said + chunk).slice(-SAID_MAX);
    });
    child.stdout?.setEncoding("utf8").on("data", (chunk: string) => {
      heard += chunk;
      if (!heard.split(/\r?\n/).includes(APPROVED)) return;
      // Approved: the app lets the helper go, which waits for the admin program.
      child.stdout?.destroy();
      child.stderr?.destroy();
      child.unref();
      resolve({ code: 0, said: "", restarts: true });
    });
    child.once("exit", (code) => resolve({ code, said: said.trim() }));
  });
}

/**
 * What runs an update with *helper*, surogate-helper.exe: `update` on the release's folder, for this app's process
 * *pid*. Approved, the app quits for the MSI to replace it, and the helper starts it again after.
 */
export function updateRun(helper: string, pid: number = process.pid): (files: Staged) => Promise<Applied> {
  return (files) => {
    const folder = dirname(files.tarball);
    return helperRun(helper, ["update", "--app-pid", String(pid), "--staged", folder, "--version", basename(folder)]);
  };
}

/** What the removal asks first on Windows: it is for every user, and an administrator's to approve. */
export const REMOVAL = {
  message: "Remove Surogate from this computer?",
  detail: "Surogate is uninstalled for every user of this computer, once an administrator approves it. The folders your chats work on are left as they are.",
  checkbox: "Also delete your Surogate data on this computer: your sign-in, your settings and the sandbox's disks",
};

/**
 * What removes the app with *helper*: `remove`, for this app's process *pid*, which uninstalls it for every user once an
 * administrator approved and the app has quit, and then removes the user's own folders it is given, *forget*.
 */
export function removeRun(helper: string, pid: number = process.pid): (forget: string[]) => Promise<Applied> {
  return (forget) => helperRun(helper, ["remove", "--app-pid", String(pid), ...forget.flatMap((folder) => ["--forget", folder])]);
}

/**
 * An installed app's updates on Windows: the MSI's record, which also marks the version installed; the release keys of
 * the install, *install*\resources\release-keys; and the helper beside them, *install*\bin\surogate-helper.exe. The
 * install is the app's own program's folder.
 */
export function installedUpdates(version: string, cache: string, fetch: Fetch, signal: AbortSignal, install: string = dirname(process.execPath)): UpdatesOptions {
  const record = machineRecord();
  return {
    version, record, rootOwned: true, helper: win32.join(install, "resources", "release-keys"), installed: record, cache, fetch, signal,
    apply: updateRun(win32.join(install, "bin", "surogate-helper.exe")),
  };
}
