// What the MSI leaves an installed app on Windows (desktop/windows/msi/Surogate.wxs), and how an update is installed
// there: its record of where it was installed from, of its channel and of the version installed, in
// %ProgramData%\Surogate, SYSTEM's, which only administrators may change, as the app checks before it takes it
// (files/admins-own-win32.ts); the release keys the build wrote beside the app's code, in Program Files; and
// surogate-helper.exe's update, which asks Windows's UAC for an administrator to run surogate-admin.exe on the files
// the app downloaded, says once one has approved, and starts the app again after the MSI (native/windows/helper).

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

/**
 * What runs an update with *helper*, surogate-helper.exe: `update` on the release's folder, for this app's process
 * *pid*. It answers once Windows's prompt has an administrator's approval, with restarts: the app is to quit, for the MSI
 * to replace it, and the helper starts it again after. Or, where none approved or the helper could not ask, as it ended:
 * 1223 where the prompt was dismissed. The helper is never elevated, and outlives the app.
 */
export function updateRun(helper: string, pid: number = process.pid): (files: Staged) => Promise<Applied> {
  return (files) => new Promise((resolve) => {
    const folder = dirname(files.tarball);
    let child: ChildProcess;
    try {
      child = spawn(helper, ["update", "--app-pid", String(pid), "--staged", folder, "--version", basename(folder)], {
        detached: true, stdio: ["ignore", "pipe", "pipe"], windowsHide: true,
      });
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
      // Approved: the app lets the helper go, which waits for the install and starts the app again.
      child.stdout?.destroy();
      child.stderr?.destroy();
      child.unref();
      resolve({ code: 0, said: "", restarts: true });
    });
    child.once("exit", (code) => resolve({ code, said: said.trim() }));
  });
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
