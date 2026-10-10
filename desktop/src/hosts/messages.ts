// What the main process and a tool host say to each other, over the host's IPC channel.

import type { ProcessHandle } from "../guest/processes.js";
import type { Outcome } from "../link/protocol.js";

// A destination a command in the guest asked the host proxy for: its host as a grant names it, and its port (vm/egress.ts destination).
export interface Destination {
  host: string;
  port: number;
}

// What the app is asked about: a destination, and whether it is on a private network
// (RFC 1918, shared address space, link-local, unique-local), which the prompt shows.
// This computer's own is never asked about.
export interface NetworkAsk extends Destination {
  privateNetwork: boolean;
}

// What the app decides about a destination: let through the connections asking now,
// let its host through on every port for the rest of the chat as well, or refuse them.
export type NetworkAnswer = "allow" | "allow_session" | "deny";

export interface HostStart {
  type: "start";
  // What the host holds and its helper works in: the bound folder, which the host resolves; or a
  // project thread's copy of it, in the app's data (spec, Section 13).
  folder: string;
  // That folder's identity: a chat's when it was bound, with that boot's id; a copy's when the app
  // last looked at it. One replaced since is not the chat's.
  expect: { dev: number; ino: number; boot: string };
  // A thread's copy alone: the path of the folder it is a copy of, by which its helper's requests
  // and answers name its files.
  at?: string;
  // A landing's host alone, on the folder itself: the thread's copy its files come from, which
  // its helper reads, and the folder in the app's data where it keeps the files it replaces.
  landing?: { copy: string; kept: string };
  // How long it waits for another host to let its folder go; LOCK_WAIT_MS unless said.
  lockWaitMs?: number;
  tmp: string; // the file helper's working folder, which srt wraps it from
  dataDir: string; // the app's own data, never inside the folder
  cacheDir: string; // the app's own cache, <cache home>/surogate: the folder neither holds it nor lies in it
  env: Record<string, string>; // the app's: HOME, and LANG for the helper
  appDirs: string[]; // read-only folders the sandbox needs: the runtime and the app's files
  bwrapPath?: string;
}

export type ToHost =
  | HostStart
  | { type: "op"; id: string; kind: string; args: Record<string, unknown> }
  | { type: "cancel"; id: string }
  // The hook guard around a command that runs in the VM: why it may not run, answered
  // {ok: null} when it may; then the look after it, answered with its outcome and the look's notice.
  // run: a run, which an after ends, not a start or input to a process.
  | { type: "refusal"; id: string; run: boolean }
  | { type: "after"; id: string; outcome: Outcome }
  // The root's background processes in the VM, as the guest says: the folder's record keeps their handles.
  // live: how many of them are alive.
  | { type: "handles"; handles: ProcessHandle[]; live: number }
  | { type: "stop" };

export type FromHost =
  // With the handles of the processes its folder's record keeps, as a registry answers for them.
  | { type: "ready"; processes: ProcessHandle[] }
  // folder: the bound folder is not there, is not a folder, or was replaced; the app answers folder_unavailable.
  // busy: another host held the folder for as long as this one waited.
  | { type: "failed"; message: string; folder?: true; busy?: true }
  | { type: "result"; id: string; outcome: Outcome };

export const FOLDER_UNAVAILABLE: Outcome = {
  error: { type: "folder_unavailable", message: "The folder for this chat is no longer available on this computer" },
};
