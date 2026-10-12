// How an update this app checked is put in place on Linux (spec, Section 9, "In-app updates"): by
// the install script's root helper, which pkexec runs under polkit, after an administrator's approval,
// on the files the app downloaded; the helper checks all of them again before it applies anything.

import type { ChildProcess } from "node:child_process";
import { StringDecoder } from "node:string_decoder";

import { spawnClean } from "../clean-child.js";
import type { Fetch } from "../download.js";
import type { Applied, Staged, UpdatesOptions } from "./updates.js";

// What the install script leaves an installed app, each root's alone: its record of where it was
// installed from; the helper pkexec runs, whose list of release keys is the one this computer
// trusts; and the mark of the version installed for every user now.
export const ROOT_RECORD = "/etc/surogate/install.json";
export const ROOT_HELPER = "/opt/surogate/bin/surogate-apply-update";
const INSTALLED = "/opt/surogate/current/release.json";

// The most that is kept of what a helper said, its end: its last lines are why it failed.
const SAID_MAX = 4000;
// How long what a helper says is waited for once the helper itself has ended.
const OUTPUT_MS = 2_000;

// How an installed app runs the root helper: pkexec, by its whole path, on the helper at the path
// with no link in it that the install script's polkit action names. Never with pkexec's own agent:
// started from a terminal in a session with no polkit agent, pkexec would ask for a password on
// that terminal and wait there, with the app's line at "Installing". Without it, pkexec ends at
// once with 127, and the line says an administrator is needed.
export const AS_ROOT = ["/usr/bin/pkexec", "--disable-internal-agent", ROOT_HELPER];

/**
 * What runs the root helper on a downloaded release: *command*, then --apply and the files'
 * paths, with no more of the app's environment than a PATH. In an installed app, pkexec and
 * the helper's path; polkit asks an administrator, and the helper checks the files again as root.
 */
export function helperRun(command: string[]): (files: Staged) => Promise<Applied> {
  return (files) => new Promise((resolve) => {
    const [program, ...args] = command;
    let child: ChildProcess;
    try {
      // With its three standard descriptors and nothing else the app has open: pkexec is root's
      // process for as long as its prompt is.
      child = spawnClean(program!, [...args, "--apply", files.manifest, files.signature, files.tarball], {
        stdio: ["ignore", "ignore", "pipe"], env: { PATH: "/usr/bin:/bin" },
      });
    } catch (error) {
      return resolve({ code: null, said: error instanceof Error ? error.message : String(error) });
    }
    // Before anything else is asked of it: one that could not be started, as for want of file
    // descriptors, tells its error later, and has no output to read.
    child.once("error", (error) => resolve({ code: null, said: error.message }));
    // What it says is one stream, read as it comes: a letter may come in two reads. Its end alone
    // is kept, cut at each read by the one rule, so that no more than a read of it is ever held.
    const decoder = new StringDecoder("utf8");
    const kept = (all: string) => all.slice(-SAID_MAX);
    let said = "";
    child.stderr?.on("data", (chunk: Buffer) => {
      said = kept(said + decoder.write(chunk));
    });
    // One a signal ended has no exit code: how it ended is the last of what is said of it.
    const answer = (code: number | null, signal: NodeJS.Signals | null): void => {
      clearTimeout(waited);
      said = kept(said + decoder.end());
      resolve({ code, said: (code === null ? `${said.trim()}\nits helper was stopped by ${signal}` : said).trim() });
    };
    // Its exit is its answer. What it says ends with it, or a moment after; a program it left
    // running holds that open for as long as it runs, and is not waited for beyond OUTPUT_MS.
    let waited: NodeJS.Timeout | undefined;
    child.once("exit", (code, signal) => {
      waited = setTimeout(() => answer(code, signal), OUTPUT_MS);
    });
    child.once("close", answer);
  });
}

/**
 * An installed app's updates: what the install script left it, where the user's downloads go, and
 * the root helper run under polkit.
 */
export function installedUpdates(version: string, cache: string, fetch: Fetch, signal: AbortSignal): UpdatesOptions {
  return { version, record: ROOT_RECORD, rootOwned: true, helper: ROOT_HELPER, installed: INSTALLED, cache, fetch, signal, apply: helperRun(AS_ROOT) };
}
