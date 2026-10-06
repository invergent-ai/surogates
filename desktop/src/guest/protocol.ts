// What the guest agent says to each root runner (runner.ts): one JSON object a
// line, over the runner's stdin and stdout, after its {"ready":true}. The agent
// ends the runner by ending its stdin. Until commands move into the VM, a tool
// host speaks the same to its session runner.

import type { Refusal } from "../files/answers.js";

// The host user the agent's roots run for, as the host's answer to hello names it.
export interface HostUser {
  uid: number;
  gid: number;
  name: string;
  home: string;
}

export type ToRunner =
  // stdin: a pipe the host can write to (a background process); else /dev/null.
  | { type: "spawn"; id: string; command: string; cwd: string; env: Record<string, string>; pty: boolean; stdin: boolean }
  // To the command's process group and to everything that carries its marker.
  | { type: "signal"; id: string; signal: NodeJS.Signals }
  | { type: "stdin"; id: string; data: string } // base64; answered written, or error with stdin
  // Where a command would run, looked up in the runner's own view and as its user: run's workdir checks.
  | { type: "place"; id: string; folder: string; home: string; workdir: string | null }
  // shutil.which, in the commands' environment.
  | { type: "which"; id: string; name: string; cwd: string };

export type FromRunner =
  | { type: "started"; id: string; pid: number }
  | { type: "data"; id: string; data: string; err?: true } // base64; err: from its stderr
  // A stdin message was taken.
  | { type: "written"; id: string }
  | { type: "exit"; id: string; code: number | null; signal: NodeJS.Signals | null }
  // It could not be started; with stdin, a stdin message was refused and it goes on.
  | { type: "error"; id: string; message: string; stdin?: true }
  // The answers to place: the folder a command would run in, and why it cannot be entered (an errno name), or the refusal.
  | { type: "placed"; id: string; cwd: string; unenterable: string | null }
  | { type: "refused"; id: string; refusal: Refusal }
  // The answer to which.
  | { type: "found"; id: string; found: boolean };

export type SpawnRequest = Omit<Extract<ToRunner, { type: "spawn" }>, "type">;

// The messages the runner answers once each, by their id.
export type Question = Extract<ToRunner, { type: "place" | "which" }>;
export type Answer = Extract<FromRunner, { type: "placed" | "refused" | "found" }>;
