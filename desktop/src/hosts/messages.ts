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
  | { type: "failed"; message: string }
  | { type: "result"; id: string; outcome: Outcome };

export const FOLDER_UNAVAILABLE: Outcome = {
  error: { type: "folder_unavailable", message: "The folder for this chat is no longer available on this computer" },
};
