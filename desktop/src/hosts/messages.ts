// What the main process and a tool host say to each other, over the host's IPC channel.

import type { Outcome } from "../link/protocol.js";

// A destination a command asked srt's proxy for: its host as srt compares it, and its port (policy.ts destination).
export interface Destination {
  host: string;
  port: number;
}

// What the app is asked about: a destination, and whether it is on a private network
// (RFC 1918, link-local, unique-local), which the prompt shows. This computer's own is never asked about.
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
  bwrapPath?: string;
}

export type ToHost =
  | HostStart
  | { type: "op"; id: string; kind: string; args: Record<string, unknown> }
  | { type: "cancel"; id: string }
  // A filesystem grant changed: the session runner, if one is up, is wrapped again.
  | { type: "restart"; reason: "grant" }
  | { type: "stop" };

export type FromHost =
  | { type: "ready" }
  // folder: the bound folder is not there, is not a folder, or was replaced; the app answers folder_unavailable.
  | { type: "failed"; message: string; folder?: true }
  | { type: "result"; id: string; outcome: Outcome }
  // How many background processes are alive: a host with any is never idle.
  | { type: "processes"; live: number };

export const FOLDER_UNAVAILABLE: Outcome = {
  error: { type: "folder_unavailable", message: "The folder for this chat is no longer available on this computer" },
};

// What a tool host and its session runner (runner.ts) say to each other: one JSON
// object a line, over the runner's stdin and stdout, after its {"ready":true}.
// The host ends the runner by ending its stdin.
export type ToRunner =
  // stdin: a pipe the host can write to (a background process); else /dev/null.
  | { type: "spawn"; id: string; command: string; cwd: string; env: Record<string, string>; pty: boolean; stdin: boolean }
  // To the command's process group and to everything that carries its marker.
  | { type: "signal"; id: string; signal: NodeJS.Signals }
  | { type: "stdin"; id: string; data: string }; // base64; answered written, or error with stdin

export type FromRunner =
  | { type: "started"; id: string; pid: number }
  | { type: "data"; id: string; data: string; err?: true } // base64; err: from its stderr
  // A stdin message was taken.
  | { type: "written"; id: string }
  | { type: "exit"; id: string; code: number | null; signal: NodeJS.Signals | null }
  // It could not be started; with stdin, a stdin message was refused and it goes on.
  | { type: "error"; id: string; message: string; stdin?: true };

export type SpawnRequest = Omit<Extract<ToRunner, { type: "spawn" }>, "type">;
