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

// A folder's key, the app's own name for it, which its place in the guest is mounted under:
// 16 hex digits, and so a folder's name that leads nowhere else.
export const PLACE_KEY = /^[0-9a-f]{16}$/;

// A teardown's failure when something of the root waits on its share, which stalled: the
// share is still in use in the guest, so the host keeps it until the VM stops.
export const HELD = "What this chat ran is waiting on its folder, which does not answer";

// The guest's third port, ai.surogate.inbound (spec, Section 11, Network, "Later"): the host opens a
// stream on it for each connection the agent's browser makes to a server of a root's own (inbound.ts).
export const INBOUND_PORT = "ai.surogate.inbound";

// What a root's runner sends on the root's socket for a connection the agent asked it for (ToRunner's
// dial): "/in/<id>", then the connection's bytes, or "/in/<id> <errno>" when nothing took it. No
// destination a command names starts with '/' (network.ts).
export const INBOUND_LINE = /^\/in\/([0-9a-f]{32})(?: ([A-Z]{1,16}))?$/;
export const inboundLine = (id: string, reason?: string): string => `/in/${id}${reason ? ` ${reason}` : ""}\n`;
// How many connections of the browser's one root may have open at once, counted apart from its commands'
// own (network.ts, MAX_TUNNELS): neither can take the other's place. One browser's most, measured, is 146.
export const MAX_INBOUND = 160;

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
  // A folder's place, for the agent's own git and in no root's namespaces: its history from the
  // app's data, mounted for writing, and the folder itself, read-only (guest/places.ts).
  | { type: "place"; id: number; key: string; history: Share; real: Share }
  // Both mounts of a place go: the host removes its two shares next.
  | { type: "unplace"; id: number; key: string }
  // The computer woke: its clock now, in milliseconds since the epoch, and how long it slept.
  // Every run's backstop in the guest falls that much later, whatever the guest's clock did.
  | { type: "time"; id: number; now: number; slept: number }
  // The host's stop: every root ends, the sessions disk is written out, and the guest
  // powers off. Answered by the VM's exit.
  | { type: "shutdown"; id: number }
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
  | { type: "which"; id: string; name: string; cwd: string }
  // A connection into the root (spec, Section 5): the runner connects to *port* of its own loopback, the
  // family *first* names before the other, and brings the connection, or why it has none, to the agent on
  // the root's socket (INBOUND_LINE).
  | { type: "dial"; id: string; port: number; first: 4 | 6 };

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
