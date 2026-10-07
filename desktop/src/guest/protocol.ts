// What the guest agent says: to the host over the control port, and to each
// root runner over the runner's stdin and stdout. Both are one JSON object a line.

import type { Refusal } from "../files/answers.js";
import type { Outcome } from "../link/protocol.js";
import type { ProcessHandle } from "./processes.js";

// The host user the agent's roots run for, as the host's answer to hello names it.
export interface HostUser {
  uid: number;
  gid: number;
  name: string;
  home: string;
}

// A root's session id, as the agent and the host proxy take one: its folder's name on the
// sessions disk, and its socket's.
export const ROOT_ID = /^[A-Za-z0-9_-]{1,64}$/;

// The most folders a guest holds at once, one for each root set up in it: each OS's
// backend has as many places for them (Linux's, its PCIe root ports).
export const MAX_SHARES = 8;

// How the guest mounts a root's folder, as the host's VM backend shared it. Each
// kind names who maps the folder's owner to the root's guest uid. virtiofs: its
// server on the host (Linux's virtiofsd), so the guest mounts it by its tag as it is.
// A backend whose share maps no owner, or maps them at the mount, adds its own kind.
export type Share = { kind: "virtiofs"; tag: string };

// The control port, ai.surogate.control (spec, Section 11, Transport). The agent
// says hello first, and the host answers it with its user; from then on the host
// asks. Every request carries an id of its sender's, and its answer the same id.
export type ToAgent =
  | { type: "done"; id: number; user: HostUser } // the answer to hello
  | { type: "ping"; id: number }
  // A root's guest uid, asked before its share is made, so its virtiofsd maps the host user to it.
  | { type: "uid"; id: number; root: string }
  // A root's namespaces and runner, with its folder at its own path from *share*,
  // and the handles of its background processes the host keeps, which it answers for.
  | { type: "setup"; id: number; root: string; folder: string; share: Share; ended: ProcessHandle[] }
  | { type: "op"; id: number; root: string; kind: string; args: Record<string, unknown> }
  // Everything of a root ends, and its share's mount goes: the host is letting its folder
  // go, and removes the share from the guest next.
  | { type: "teardown"; id: number; root: string; share: Share }
  | { type: "cancel"; id: number }; // the op of that id

export type FromAgent =
  | { type: "hello"; id: number }
  // Unasked: a root that was set up lost its runner, and everything of it ended. The host sets it up again.
  | { type: "lost"; root: string }
  // Unasked, each time they change: a root's process handles for the host to keep, and how many of its processes live.
  | { type: "handles"; root: string; handles: ProcessHandle[]; live: number }
  | { type: "pong"; id: number }
  | { type: "done"; id: number; uid?: number }
  | { type: "failed"; id: number; message: string }
  | { type: "result"; id: number; outcome: Outcome };

// Each root runner (runner.ts), after its {"ready":true}. The agent ends the
// runner by ending its stdin.

export type ToRunner =
  // stdin: a pipe the host can write to (a background process); else /dev/null.
  | { type: "spawn"; id: string; command: string; cwd: string; env: Record<string, string>; pty: boolean; stdin: boolean }
  // To everything of the command: its cgroup in the guest, its process group without one.
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
  // oom: the kernel ended one of its processes for memory.
  | { type: "exit"; id: string; code: number | null; signal: NodeJS.Signals | null; oom?: true }
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
