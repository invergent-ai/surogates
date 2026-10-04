// What the main process and a tool host say to each other, over the host's IPC channel.

import type { Outcome } from "../link/protocol.js";

export interface HostStart {
  type: "start";
  folder: string; // the bound folder; the host resolves it
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
  | { type: "stop" };

export type FromHost =
  | { type: "ready" }
  // folder: the bound folder is not there, or is not a folder; the app answers folder_unavailable.
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
  | { type: "stdin"; id: string; data: string }; // base64

export type FromRunner =
  | { type: "started"; id: string; pid: number }
  | { type: "data"; id: string; data: string; err?: true } // base64; err: from its stderr
  | { type: "exit"; id: string; code: number | null; signal: NodeJS.Signals | null }
  // It could not be started.
  | { type: "error"; id: string; message: string };

export type SpawnRequest = Omit<Extract<ToRunner, { type: "spawn" }>, "type">;
