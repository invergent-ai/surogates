// What the main process and a tool host say to each other, over the host's IPC channel.

import type { ProcessHandle } from "../guest/processes.js";
import type { Outcome } from "../link/protocol.js";

// A destination a command asked srt's proxy for: its host as srt compares it, and its port (policy.ts destination).
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
  folder: string; // the bound folder; the host resolves it
  // The folder's identity when the chat was bound, and that boot's id: a folder replaced since is not the chat's.
  expect: { dev: number; ino: number; boot: string };
  tmp: string; // the root session's temp folder
  dataDir: string; // the app's own data, never inside the folder
  env: Record<string, string>; // the app-built environment: HOME, LANG, PATH
  appDirs: string[]; // read-only folders the sandbox needs: the runtime and the app's files
  // The hosts the chat's user allowed for the chat past the package hosts, on every port: srt's allowedDomains entries.
  domains: string[];
  bwrapPath?: string;
}

export type ToHost =
  | HostStart
  | { type: "op"; id: string; kind: string; args: Record<string, unknown> }
  | { type: "cancel"; id: string }
  // A filesystem grant changed: the session runner, if one is up, is wrapped again.
  | { type: "restart"; reason: "grant" }
  // The app's answer to a network ask. With remember, its host goes through from now on, on every port, without asking.
  | { type: "answer"; id: number; allow: boolean; remember: boolean }
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
  | { type: "failed"; message: string; folder?: true }
  | { type: "result"; id: string; outcome: Outcome }
  // How many background processes are alive: a host with any is never idle.
  | { type: "processes"; live: number }
  // A command asked for a destination off the list, and its connection waits for the app's answer.
  // One at a time per destination: the connections asking meanwhile wait for the same answer.
  | { type: "ask"; id: number; host: string; port: number; privateNetwork: boolean };

export const FOLDER_UNAVAILABLE: Outcome = {
  error: { type: "folder_unavailable", message: "The folder for this chat is no longer available on this computer" },
};
