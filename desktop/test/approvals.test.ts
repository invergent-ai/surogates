import { getEventListeners } from "node:events";
import { mkdirSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  type ApprovalAnswer, type ApprovalPrompts, type ApprovalRequest, Approvals, type ChatLabel, PREVIEW_BYTES,
} from "../src/binding/approvals.js";
import { Binder, type FolderPrompts } from "../src/binding/binder.js";
import { BOOT_ID } from "../src/binding/folder.js";
import { connectDevice } from "../src/device.js";
import { FOLDER_UNAVAILABLE } from "../src/hosts/messages.js";
import type { Mode } from "../src/journal/bindings.js";
import { OperationJournal } from "../src/journal/journal.js";
import type { DeviceLink } from "../src/link/client.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import { APP_CLOSED, type Executor, type OperationRunner } from "../src/operations/runner.js";
import { FakeLinkServer } from "./fake-server.js";

const ROOT = "44444444-4444-4444-8444-444444444444";
const OTHER = "55555555-5555-4555-8555-555555555555";
const CHILD = "66666666-6666-4666-8666-666666666666";
const FOLDER = "/home/me/notes";

// The user at the desktop's prompts. With *auto*, each prompt is answered at once;
// without, it stays open until the test answers it or its signal aborts it.
// Confirmations get the next of *confirms*; past the end, they decline.
class User implements ApprovalPrompts {
  readonly asked: ApprovalRequest[] = [];
  readonly open: Array<{ request: ApprovalRequest; resolve: (answer: ApprovalAnswer) => void }> = [];
  readonly confirmations: ChatLabel[] = [];
  dismissed = 0;

  constructor(public auto: ApprovalAnswer | null = null, readonly confirms: boolean[] = []) {}

  approve(request: ApprovalRequest, signal: AbortSignal): Promise<ApprovalAnswer> {
    this.asked.push(request);
    if (this.auto) return Promise.resolve(this.auto);
    return new Promise((resolve) => {
      const prompt = { request, resolve };
      this.open.push(prompt);
      // Dismissed: the window closes. What it settles with is not the user's answer,
      // so it settles with the answer that would do most.
      signal.addEventListener("abort", () => {
        const at = this.open.indexOf(prompt);
        if (at < 0) return;
        this.open.splice(at, 1);
        this.dismissed += 1;
        resolve("stop_asking");
      }, { once: true });
    });
  }

  // Answer the prompt that opened first.
  answer(answer: ApprovalAnswer): void {
    this.open.shift()?.resolve(answer);
  }

  confirmFreeMode(chat: ChatLabel, signal: AbortSignal): Promise<boolean> {
    this.confirmations.push(chat);
    const confirmed = this.confirms.shift() ?? false;
    return signal.aborted ? Promise.resolve(true) : Promise.resolve(confirmed);
  }
}

let base: string;
let journal: OperationJournal;
let user: User;
let approvals: Approvals;

const bind = (root: string, mode: Mode) =>
  journal.bindings.add({ root, nonce: `nonce-${root}`, folder: FOLDER, dev: 1, ino: 1, boot: BOOT_ID, mode, boundAt: 1 });

let next = 0;
const op = (kind: string, args: Record<string, unknown>, root = ROOT, calling = root): Operation => {
  next += 1;
  return { id: `${kind}-${next}`, sessionId: root, callingSessionId: calling, invocationId: "1:call", ordinal: next, kind, args, digest: "d" };
};

const never = () => new AbortController().signal;
const chat = (calling = ROOT): ChatLabel => ({ agent: "Research assistant", root: ROOT, calling, folder: FOLDER });

const RUN = { command: "npm test", workdir: `${FOLDER}/web`, timeout: 120 };
const COMMAND_DENIED = { error: { type: "sandbox", message: "The user denied this command on this computer" } };
const CHANGE_DENIED = { error: { type: "os", code: "EACCES", message: "The user denied this change on this computer" } };
const INPUT_DENIED = { ok: { status: "error", error: "The user denied this input on this computer" } };

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "approvals-")));
  journal = new OperationJournal(join(base, "journal.sqlite"));
  user = new User();
  approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant" });
});

afterEach(() => {
  journal.close();
  rmSync(base, { recursive: true, force: true });
});

describe("a chat that works freely", () => {
  it("asks nothing, and lets every operation run", async () => {
    bind(ROOT, "free");
    for (const operation of [op("run", RUN), op("write", { key: `${FOLDER}/a.txt`, data: "" }), op("write_stdin", { session_id: "p", data: "y" })]) {
      expect(await approvals.admit(operation, never())).toBeNull();
    }
    expect(user.asked).toEqual([]);
  });
});

describe("a chat that asks every time", () => {
  it.each([
    ["a command", op("run", RUN), { kind: "command", command: "npm test", workdir: `${FOLDER}/web`, background: false }],
    [
      "a background command",
      op("start", { command: "npm run dev", workdir: null, task_id: "t", pty: false, notify_on_complete: false, watcher_interval: null }),
      { kind: "command", command: "npm run dev", workdir: null, background: true },
    ],
    [
      "a write",
      op("write", { key: `${FOLDER}/a.txt`, data: "aGVsbG8=" }),
      { kind: "change", action: "write", path: `${FOLDER}/a.txt`, bytes: 5, preview: { text: "hello", cut: false } },
    ],
    [
      "a write longer than its preview",
      op("write", { key: `${FOLDER}/long.txt`, data: Buffer.from("a".repeat(PREVIEW_BYTES + 10)).toString("base64") }),
      { kind: "change", action: "write", path: `${FOLDER}/long.txt`, bytes: PREVIEW_BYTES + 10, preview: { text: "a".repeat(PREVIEW_BYTES), cut: true } },
    ],
    [
      "a write of data that is not text",
      op("write", { key: `${FOLDER}/a.bin`, data: Buffer.from([0x89, 0x50, 0x00, 0x01]).toString("base64") }),
      { kind: "change", action: "write", path: `${FOLDER}/a.bin`, bytes: 4, preview: null },
    ],
    [
      "a write of UTF-8 with a NUL in it",
      op("write", { key: `${FOLDER}/a.bin`, data: Buffer.from("a\u0000b").toString("base64") }),
      { kind: "change", action: "write", path: `${FOLDER}/a.bin`, bytes: 3, preview: null },
    ],
    [
      "a write that ends part-way through a character",
      op("write", { key: `${FOLDER}/a.txt`, data: Buffer.from([0x68, 0x69, 0xc3]).toString("base64") }),
      { kind: "change", action: "write", path: `${FOLDER}/a.txt`, bytes: 3, preview: null },
    ],
    [
      "a write that opens with a byte order mark",
      op("write", { key: `${FOLDER}/run.sh`, data: Buffer.from("﻿#!/bin/sh\n").toString("base64") }),
      { kind: "change", action: "write", path: `${FOLDER}/run.sh`, bytes: 13, preview: { text: "﻿#!/bin/sh\n", cut: false } },
    ],
    [
      "a delete",
      op("delete", { key: `${FOLDER}/a.txt` }),
      { kind: "change", action: "delete", path: `${FOLDER}/a.txt`, bytes: null, preview: null },
    ],
    [
      "input to a process",
      op("write_stdin", { session_id: "proc_1", data: "y\n" }),
      { kind: "input", process: "proc_1", command: null, data: "y\n" },
    ],
  ])("asks before %s, naming the chat and exactly what it does, and lets it run once allowed", async (_name, operation, shown) => {
    bind(ROOT, "ask");
    const admitted = approvals.admit(operation, never());
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    expect(user.asked).toEqual([{ ...shown, chat: chat() }]);
    user.answer("allow");
    expect(await admitted).toBeNull();
  });

  it.each([
    ["a command", op("run", RUN), COMMAND_DENIED],
    ["a background command", op("start", { command: "npm run dev", workdir: null }), COMMAND_DENIED],
    ["a write", op("write", { key: `${FOLDER}/a.txt`, data: "" }), CHANGE_DENIED],
    ["a delete", op("delete", { key: `${FOLDER}/a.txt` }), CHANGE_DENIED],
    ["input to a process", op("write_stdin", { session_id: "proc_1", data: "y\n" }), INPUT_DENIED],
  ])("answers %s its user denied in the shape its tool reads as not done", async (_name, operation, outcome) => {
    bind(ROOT, "ask");
    user.auto = "deny";
    expect(await approvals.admit(operation, never())).toEqual(outcome);
  });

  it("never asks about what only reads, or stops a process", async () => {
    bind(ROOT, "ask");
    user.auto = "deny";
    for (const kind of [
      "resolve", "check_write", "stat", "read", "read_lines", "list_dir", "walk", "ripgrep", "which", "poll",
      "read_output", "wait", "kill", "list_processes",
    ]) {
      expect(await approvals.admit(op(kind, { key: `${FOLDER}/a.txt` }), never())).toBeNull();
    }
    expect(user.asked).toEqual([]);
  });

  it("never asks about the harness's own spilled output, and asks about anything else there", async () => {
    bind(ROOT, "ask");
    user.auto = "deny";
    const spill = `${FOLDER}/.surogates-results/terminal-output-0f3a.log`;
    expect(await approvals.admit(op("write", { key: spill, data: "" }), never())).toBeNull();
    expect(user.asked).toEqual([]);
    for (const operation of [
      op("delete", { key: spill }),
      op("write", { key: `${FOLDER}/sub/.surogates-results/x.log`, data: "" }),
      op("write", { key: `${FOLDER}/.surogates-results-old/x.log`, data: "" }),
      op("write", { key: [spill], data: "" }),
      op("write", { key: `${FOLDER}/.surogates-results/../a.txt`, data: "" }),
      op("write", { key: `${FOLDER}/.surogates-results/./x.log`, data: "" }),
    ]) {
      expect(await approvals.admit(operation, never())).toEqual(CHANGE_DENIED);
    }
    expect(user.asked).toHaveLength(6);
  });

  it("never asks about the whiteboard's canvas the chat's page saves, and asks about anything else there", async () => {
    bind(ROOT, "ask");
    user.auto = "deny";
    const canvas = `${FOLDER}/_whiteboard/canvas.json`;
    // The page's save is the user's own request.
    const saved = { ...op("write", { key: canvas, data: "" }), invocationId: "request:0f3a9c2e7b1d4a6f" };
    expect(await approvals.admit(saved, never())).toBeNull();
    expect(user.asked).toEqual([]);
    for (const operation of [
      // An agent's tool call writing the canvas would replace the user's board: asked, as anywhere else.
      op("write", { key: canvas, data: "" }),
      op("delete", { key: canvas }),
      op("write", { key: `${FOLDER}/sub/_whiteboard/canvas.json`, data: "" }),
      op("write", { key: `${FOLDER}/_whiteboard/../a.txt`, data: "" }),
      // Only the canvas: an agent's write beside it is asked about, as anywhere else.
      op("write", { key: `${FOLDER}/_whiteboard/other.json`, data: "" }),
      op("write", { key: `${FOLDER}/_whiteboard/sub/x`, data: "" }),
    ]) {
      expect(await approvals.admit(operation, never())).toEqual(CHANGE_DENIED);
    }
    expect(user.asked).toHaveLength(6);
  });

  it("asks about a kind it does not know, and in a chat whose mode it does not know", async () => {
    bind(ROOT, "ask");
    bind(OTHER, "maybe" as Mode);
    user.auto = "deny";
    expect(await approvals.admit(op("click", { selector: "#buy" }), never())).toEqual(COMMAND_DENIED);
    expect(await approvals.admit(op("run", RUN, OTHER), never())).toEqual(COMMAND_DENIED);
    expect(user.asked.map(({ chat: { root } }) => root)).toEqual([ROOT, OTHER]);
  });

  it("asks about a sub-agent's operation as the chat's own, and says it is a sub-agent's", async () => {
    bind(ROOT, "ask");
    user.auto = "deny";
    expect(await approvals.admit(op("run", RUN, ROOT, CHILD), never())).toEqual(COMMAND_DENIED);
    expect(user.asked[0]?.chat).toEqual(chat(CHILD));
  });

  it("opens one prompt per chat at a time, in the order its operations came, and one for each chat", async () => {
    bind(ROOT, "ask");
    bind(OTHER, "ask");
    const first = approvals.admit(op("run", RUN), never());
    // A sub-agent's is in its chat's line.
    const second = approvals.admit(op("write", { key: `${FOLDER}/a.txt`, data: "" }, ROOT, CHILD), never());
    const elsewhere = approvals.admit(op("run", { ...RUN, command: "ls" }, OTHER), never());
    await vi.waitFor(() => expect(user.open).toHaveLength(2));
    expect(user.open.map(({ request }) => [request.chat.root, request.kind])).toEqual([[ROOT, "command"], [OTHER, "command"]]);
    user.answer("allow");
    expect(await first).toBeNull();
    await vi.waitFor(() => expect(user.open.map(({ request }) => request.kind)).toEqual(["command", "change"]));
    user.answer("deny");
    expect(await elsewhere).toEqual(COMMAND_DENIED);
    user.answer("allow");
    expect(await second).toBeNull();
  });

  it("lets the rest of the chat's line through unasked once its user stops asking, and keeps it working freely", async () => {
    bind(ROOT, "ask");
    const line = [op("run", RUN), op("write", { key: `${FOLDER}/a.txt`, data: "" }), op("run", RUN)].map(
      (operation) => approvals.admit(operation, never()),
    );
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    user.answer("stop_asking");
    expect(await Promise.all(line)).toEqual([null, null, null]);
    expect(await approvals.admit(op("delete", { key: `${FOLDER}/a.txt` }), never())).toBeNull();
    expect(user.asked).toHaveLength(1);
    expect(journal.bindings.get(ROOT)?.mode).toBe("free");
  });

  it("lets the whole chat work freely when its user stops asking at a sub-agent's prompt", async () => {
    bind(ROOT, "ask");
    user.auto = "stop_asking";
    expect(await approvals.admit(op("run", RUN, ROOT, CHILD), never())).toBeNull();
    expect(journal.bindings.get(ROOT)?.mode).toBe("free");
    expect(await approvals.admit(op("run", RUN), never())).toBeNull();
    expect(user.asked).toHaveLength(1);
  });

  it("keeps an operation that comes late waiting behind the one still in line", async () => {
    bind(ROOT, "ask");
    const first = approvals.admit(op("run", RUN), never());
    const second = approvals.admit(op("run", RUN), never());
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    user.answer("allow");
    expect(await first).toBeNull();
    await vi.waitFor(() => expect(user.asked).toHaveLength(2));
    const late = approvals.admit(op("write", { key: `${FOLDER}/a.txt`, data: "" }), never());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(user.open.map(({ request }) => request.kind)).toEqual(["command"]);
    user.answer("allow");
    expect(await second).toBeNull();
    await vi.waitFor(() => expect(user.open.map(({ request }) => request.kind)).toEqual(["change"]));
    user.answer("deny");
    expect(await late).toEqual(CHANGE_DENIED);
  });

  it("dismisses an open prompt when its operation is stopped, and lets one waiting its turn leave the line", async () => {
    bind(ROOT, "ask");
    const [stopped, leaving] = [new AbortController(), new AbortController()];
    const first = approvals.admit(op("run", RUN), stopped.signal);
    const waiting = approvals.admit(op("run", RUN), leaving.signal);
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    leaving.abort();
    // The runner drops what a stopped operation's admit answers; Approvals itself never lets it run.
    expect(await waiting).toEqual(COMMAND_DENIED);
    // The one that left was last in line, and the first is still open: the next still waits for it.
    const after = approvals.admit(op("run", RUN), never());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(user.asked).toHaveLength(1);
    stopped.abort();
    // The dismissed prompt settled with "stop_asking", which is not its user's answer.
    expect(await first).toEqual(COMMAND_DENIED);
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    expect([user.asked.length, user.dismissed]).toEqual([2, 1]);
    // What a dismissed prompt settles with is not its user's answer: the chat still asks.
    expect(journal.bindings.get(ROOT)?.mode).toBe("ask");
    user.answer("allow");
    expect(await after).toBeNull();
  });

  it("settles once its signal aborts, even when its prompt never does, and the next in line is asked", async () => {
    bind(ROOT, "ask");
    const ignoring: ApprovalPrompts = {
      approve: (request) => {
        user.asked.push(request);
        return user.asked.length === 1 ? new Promise(() => {}) : Promise.resolve("allow");
      },
      confirmFreeMode: () => Promise.resolve(false),
    };
    const asking = new Approvals({ bindings: journal.bindings, prompts: ignoring, agent: "Research assistant" });
    const stopped = new AbortController();
    const first = asking.admit(op("run", RUN), stopped.signal);
    const next = asking.admit(op("write", { key: `${FOLDER}/a.txt`, data: "" }), never());
    await vi.waitFor(() => expect(user.asked).toHaveLength(1));
    stopped.abort();
    expect(await first).toEqual(COMMAND_DENIED);
    expect(await next).toBeNull();
    expect(user.asked).toHaveLength(2);
  });

  it("denies when the prompt cannot be shown, and on an answer it does not know", async () => {
    bind(ROOT, "ask");
    user.approve = () => Promise.reject(new Error("no display"));
    expect(await approvals.admit(op("run", RUN), never())).toEqual({
      error: { type: "sandbox", message: "This computer could not ask its user about this: no display" },
    });
    const unsure = new User("maybe" as ApprovalAnswer);
    const asking = new Approvals({ bindings: journal.bindings, prompts: unsure, agent: "Research assistant" });
    expect(await asking.admit(op("write_stdin", { session_id: "p", data: "y" }), never())).toEqual(INPUT_DENIED);
  });

  it("lets the operation through when Stop asking cannot be recorded, saying why, and denies when the journal cannot be read", async () => {
    bind(ROOT, "ask");
    const failures: unknown[] = [];
    const asking = new Approvals({
      bindings: journal.bindings, prompts: user, agent: "Research assistant", onError: (error) => failures.push(error),
    });
    const full = new Error("database or disk is full");
    vi.spyOn(journal.bindings, "setMode").mockImplementation(() => {
      throw full;
    });
    user.auto = "stop_asking";
    expect(await asking.admit(op("run", RUN), never())).toBeNull();
    expect(failures).toEqual([full]);
    vi.spyOn(journal.bindings, "get").mockImplementation(() => {
      throw new Error("disk I/O error");
    });
    const why = "This computer could not ask its user about this: disk I/O error";
    expect(await asking.admit(op("run", RUN), never())).toEqual({ error: { type: "sandbox", message: why } });
    expect(await asking.admit(op("delete", { key: `${FOLDER}/a.txt` }), never())).toEqual({
      error: { type: "os", code: "EACCES", message: why },
    });
    expect(await asking.admit(op("write_stdin", { session_id: "p", data: "y" }), never())).toEqual({
      ok: { status: "error", error: why },
    });
    expect(user.asked).toHaveLength(1);
  });

  it("leaves out of a write's preview a character its head cuts in two", async () => {
    bind(ROOT, "ask");
    user.auto = "deny";
    const data = `a${"é".repeat(PREVIEW_BYTES)}`;
    await approvals.admit(op("write", { key: `${FOLDER}/a.txt`, data: Buffer.from(data).toString("base64") }), never());
    expect(user.asked[0]).toMatchObject({ preview: { text: data.slice(0, PREVIEW_BYTES / 2), cut: true } });
  });

  it("names the command a background process of the chat runs in a prompt before input to it", async () => {
    bind(ROOT, "ask");
    bind(OTHER, "ask");
    user.auto = "deny";
    approvals.started(op("start", { command: "python3 manage.py shell", workdir: null }), { ok: { session_id: "proc_1", pid: 41 } });
    // Not a background command's start, or not started: nothing to name.
    approvals.started(op("run", { command: "ls" }), { ok: { session_id: "proc_2" } });
    approvals.started(op("start", { command: "npm run dev" }), { error: { type: "sandbox", message: "blocked" } });
    // Nor one whose outcome is not an object: the start still ran.
    for (const outcome of [null, undefined, "ran"]) approvals.started(op("start", { command: "npm run dev" }), outcome as unknown as Outcome);
    for (const [process, root] of [["proc_1", ROOT], ["proc_1", OTHER], ["proc_2", ROOT]] as const) {
      await approvals.admit(op("write_stdin", { session_id: process, data: "y\n" }, root), never());
    }
    expect(user.asked.map((request) => request.kind === "input" && request.command)).toEqual(["python3 manage.py shell", null, null]);
  });

  it.each([
    ["a command", op("run", RUN), { error: { type: "sandbox", message: "Nobody answered on this computer in time, so the command did not run" } }],
    [
      "a change", op("delete", { key: `${FOLDER}/a.txt` }),
      { error: { type: "os", code: "EACCES", message: "Nobody answered on this computer in time, so the change was not made" } },
    ],
    [
      "input to a process", op("write_stdin", { session_id: "p", data: "y" }),
      { ok: { status: "error", error: "Nobody answered on this computer in time, so the input was not sent" } },
    ],
  ])("answers %s nobody answered in time as not done, saying so", async (_name, operation, outcome) => {
    bind(ROOT, "ask");
    user.auto = "timeout";
    expect(await approvals.admit(operation, never())).toEqual(outcome);
    expect(journal.bindings.get(ROOT)?.mode).toBe("ask");
  });

  it("answers what would ask, for a chat this computer did not bind, as its host would, and asks nothing", async () => {
    user.auto = "deny";
    expect(await approvals.admit(op("run", RUN, OTHER), never())).toEqual(FOLDER_UNAVAILABLE);
    expect(await approvals.admit(op("read", { key: `${FOLDER}/a.txt` }, OTHER), never())).toBeNull();
    expect(user.asked).toEqual([]);
  });
});

describe("a command's connection to a destination off the package hosts", () => {
  const SITE = { host: "example.com", port: 443, privateNetwork: false };

  it.each([["works freely", "free"], ["asks every time", "ask"]] as const)(
    "asks in a chat that %s, naming the chat and the destination, and lets it through once allowed",
    async (_name, mode) => {
      bind(ROOT, mode);
      const asked = approvals.askNetwork(ROOT, SITE, never());
      await vi.waitFor(() => expect(user.open).toHaveLength(1));
      expect(user.asked).toEqual([{ kind: "network", chat: chat(), host: "example.com", port: 443, privateNetwork: false }]);
      user.answer("allow");
      expect(await asked).toBe("allow");
      // Allowed once: nothing is kept.
      expect(approvals.granted(ROOT)).toEqual([]);
    },
  );

  it("says when the destination is on a private network", async () => {
    bind(ROOT, "free");
    user.auto = "deny";
    expect(await approvals.askNetwork(ROOT, { host: "192.168.1.20", port: 8080, privateNetwork: true }, never())).toBe("deny");
    expect(user.asked).toEqual([{ kind: "network", chat: chat(), host: "192.168.1.20", port: 8080, privateNetwork: true }]);
  });

  it("keeps the host allowed for the session with the chat's binding, on every port, for that chat only", async () => {
    bind(ROOT, "free");
    bind(OTHER, "free");
    user.auto = "allow_session";
    expect(await approvals.askNetwork(ROOT, SITE, never())).toBe("allow_session");
    expect(await approvals.askNetwork(ROOT, { host: "[::1]", port: 3000, privateNetwork: false }, never())).toBe("allow_session");
    expect(approvals.granted(ROOT)).toEqual(["example.com", "[::1]"]);
    expect(approvals.granted(OTHER)).toEqual([]);
  });

  it("asks again about a host its user took back, and keeps the chat's other hosts and another chat's", async () => {
    bind(ROOT, "free");
    bind(OTHER, "free");
    journal.bindings.allowDomain(ROOT, "example.com");
    journal.bindings.allowDomain(ROOT, "[::1]");
    journal.bindings.allowDomain(OTHER, "example.com");
    journal.bindings.disallowDomain(ROOT, "example.com");
    expect([approvals.granted(ROOT), approvals.granted(OTHER)]).toEqual([["[::1]"], ["example.com"]]);
    user.auto = "deny";
    expect(await approvals.askNetwork(ROOT, SITE, never())).toBe("deny");
    expect(user.asked.map(({ kind }) => kind)).toEqual(["network"]);
    // A host never allowed, or a chat not bound here, changes nothing.
    journal.bindings.disallowDomain(ROOT, "pypi.example.org");
    journal.bindings.disallowDomain(CHILD, "example.com");
    expect(approvals.granted(OTHER)).toEqual(["example.com"]);
  });

  it("lets through unasked another port of a host allowed for the session while it waited its turn", async () => {
    bind(ROOT, "free");
    const first = approvals.askNetwork(ROOT, SITE, never());
    const second = approvals.askNetwork(ROOT, { ...SITE, port: 80 }, never());
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    user.answer("allow_session");
    expect([await first, await second]).toEqual(["allow_session", "allow"]);
    expect(user.asked).toHaveLength(1);
  });

  it("lets a host allowed for the session through at once, not behind the chat's open prompt", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowDomain(ROOT, "example.com");
    const command = approvals.admit(op("run", RUN), never());
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    expect(await approvals.askNetwork(ROOT, { ...SITE, port: 8443 }, never())).toBe("allow");
    expect(user.asked.map(({ kind }) => kind)).toEqual(["command"]);
    user.answer("allow");
    expect(await command).toBeNull();
  });

  it("denies on Deny, when nobody answers in time, on an answer a network prompt does not offer, and when the prompt fails, saying why", async () => {
    bind(ROOT, "ask");
    for (const answer of ["deny", "timeout", "stop_asking", "maybe"] as ApprovalAnswer[]) {
      user.auto = answer;
      expect(await approvals.askNetwork(ROOT, SITE, never())).toBe("deny");
    }
    // Stop asking is not a network answer: the chat still asks every time.
    expect(journal.bindings.get(ROOT)?.mode).toBe("ask");
    const errors: unknown[] = [];
    const failing = new Approvals({
      bindings: journal.bindings,
      prompts: { approve: () => Promise.reject(new Error("no display")), confirmFreeMode: () => Promise.resolve(false) },
      agent: "Research assistant",
      onError: (error) => errors.push(error),
    });
    expect(await failing.askNetwork(ROOT, SITE, never())).toBe("deny");
    expect(errors.map(String)).toEqual(["Error: no display"]);
    expect(approvals.granted(ROOT)).toEqual([]);
  });

  it("denies a chat this computer did not bind, and asks nothing", async () => {
    user.auto = "allow";
    expect(await approvals.askNetwork(OTHER, SITE, never())).toBe("deny");
    expect(user.asked).toEqual([]);
  });

  it("denies when the chat's binding or its grants cannot be read, saying why, and asks nothing", async () => {
    bind(ROOT, "free");
    user.auto = "allow";
    const errors: unknown[] = [];
    const reading = new Approvals({
      bindings: journal.bindings, prompts: user, agent: "Research assistant", onError: (error) => errors.push(error),
    });
    const get = vi.spyOn(journal.bindings, "get").mockImplementation(() => {
      throw new Error("locked");
    });
    expect(await reading.askNetwork(ROOT, SITE, never())).toBe("deny");
    get.mockRestore();
    vi.spyOn(journal.bindings, "domains").mockImplementation(() => {
      throw new Error("busy");
    });
    expect(await reading.askNetwork(ROOT, SITE, never())).toBe("deny");
    expect(errors.map(String)).toEqual(["Error: locked", "Error: busy"]);
    expect(user.asked).toEqual([]);
  });

  it("denies, saying why, an ask whose chat's grants cannot be read once its turn in the chat's line comes", async () => {
    bind(ROOT, "free");
    const errors: unknown[] = [];
    const reading = new Approvals({
      bindings: journal.bindings, prompts: user, agent: "Research assistant", onError: (error) => errors.push(error),
    });
    const first = reading.askNetwork(ROOT, SITE, never());
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    // Its grants read as it is asked, then not once the prompt before it is answered.
    const second = reading.askNetwork(ROOT, { ...SITE, port: 80 }, never());
    vi.spyOn(journal.bindings, "domains").mockImplementation(() => {
      throw new Error("busy");
    });
    user.answer("allow");
    expect(await Promise.allSettled([first, second])).toEqual([{ status: "fulfilled", value: "allow" }, { status: "fulfilled", value: "deny" }]);
    expect(errors.map(String)).toEqual(["Error: busy"]);
    expect(user.asked).toHaveLength(1);
  });

  it("waits its turn in the chat's line, and is dismissed when its host stops", async () => {
    bind(ROOT, "ask");
    const command = approvals.admit(op("run", RUN), never());
    const host = new AbortController();
    const asked = approvals.askNetwork(ROOT, SITE, host.signal);
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    expect(user.open[0]?.request.kind).toBe("command");
    user.answer("allow");
    expect(await command).toBeNull();
    await vi.waitFor(() => expect(user.open.map(({ request }) => request.kind)).toEqual(["network"]));
    host.abort();
    // What a dismissed prompt settles with is not its user's answer.
    expect(await asked).toBe("deny");
    expect([user.dismissed, approvals.granted(ROOT)]).toEqual([1, []]);
  });

  it("lets these connections through when the session's grant cannot be recorded, saying why", async () => {
    bind(ROOT, "free");
    const errors: unknown[] = [];
    const recording = new Approvals({
      bindings: journal.bindings, prompts: new User("allow_session"), agent: "Research assistant", onError: (error) => errors.push(error),
    });
    vi.spyOn(journal.bindings, "allowDomain").mockImplementation(() => {
      throw new Error("disk full");
    });
    expect(await recording.askNetwork(ROOT, SITE, never())).toBe("allow");
    expect(errors.map(String)).toEqual(["Error: disk full"]);
    expect(approvals.granted(ROOT)).toEqual([]);
  });

  it("denies an operation whose prompt answers Allow for this session: that answer is a network prompt's", async () => {
    bind(ROOT, "ask");
    user.auto = "allow_session";
    expect(await approvals.admit(op("run", RUN), never())).toEqual(COMMAND_DENIED);
  });
});

describe("a chat's mode", () => {
  it("may be switched to Ask every time by the page, never to Work freely", async () => {
    bind(ROOT, "free");
    approvals.setMode(ROOT, "ask");
    expect(journal.bindings.get(ROOT)?.mode).toBe("ask");
    expect(() => approvals.setMode(ROOT, "free" as "ask")).toThrow("Only the desktop can let a chat work freely");
    expect(journal.bindings.get(ROOT)?.mode).toBe("ask");
    expect(() => approvals.setMode(OTHER, "ask")).toThrow("This chat has no folder on this computer");
    user.auto = "deny";
    expect(await approvals.admit(op("run", RUN), never())).toEqual(COMMAND_DENIED);
  });

  it("works freely only once its user confirms it in the desktop's own window", async () => {
    bind(ROOT, "ask");
    // Only true confirms: not a window that answers something else.
    const confirming = new User(null, [false, "yes" as unknown as boolean, true]);
    const asking = new Approvals({ bindings: journal.bindings, prompts: confirming, agent: "Research assistant" });
    // Each from a page of its own: a page whose user kept the chat asking is not asked again.
    expect(await asking.requestFreeMode(ROOT, never(), "page-1")).toBe(false);
    expect(await asking.requestFreeMode(ROOT, never(), "page-2")).toBe(false);
    expect(journal.bindings.get(ROOT)?.mode).toBe("ask");
    expect(await asking.requestFreeMode(ROOT, never(), "page-3")).toBe(true);
    expect(journal.bindings.get(ROOT)?.mode).toBe("free");
    // Already working freely: nothing to confirm.
    expect(await asking.requestFreeMode(ROOT, never(), "page-3")).toBe(true);
    expect(confirming.confirmations).toEqual([chat(), chat(), chat()]);
    await expect(asking.requestFreeMode(OTHER, never(), "page-1")).rejects.toThrow("This chat has no folder on this computer");
  });

  it("keeps asking, and asks nothing, for a page already gone", async () => {
    bind(ROOT, "ask");
    const gone = new AbortController();
    gone.abort();
    expect(await approvals.requestFreeMode(ROOT, gone.signal, "page-1")).toBe(false);
    expect(journal.bindings.get(ROOT)?.mode).toBe("ask");
    expect(user.confirmations).toEqual([]);
  });

  it("opens one confirmation for requests that come while it is open, and each hears its answer", async () => {
    bind(ROOT, "ask");
    let confirm = (_yes: boolean): void => {};
    const asked: ChatLabel[] = [];
    const confirming = new Approvals({
      bindings: journal.bindings,
      prompts: {
        approve: () => Promise.resolve("deny"),
        confirmFreeMode: (chat) => {
          asked.push(chat);
          return new Promise((resolve) => {
            confirm = resolve;
          });
        },
      },
      agent: "Research assistant",
    });
    const both = [confirming.requestFreeMode(ROOT, never(), "page-1"), confirming.requestFreeMode(ROOT, never(), "page-2")];
    await vi.waitFor(() => expect(asked).toHaveLength(1));
    confirm(true);
    expect(await Promise.all(both)).toEqual([true, true]);
    expect(journal.bindings.get(ROOT)?.mode).toBe("free");
    expect(asked).toHaveLength(1);
  });

  it("settles once its page goes, even when the confirmation never does, and asks again for the next request", async () => {
    bind(ROOT, "ask");
    const asked: ChatLabel[] = [];
    const ignoring = new Approvals({
      bindings: journal.bindings,
      prompts: {
        approve: () => Promise.resolve("deny"),
        confirmFreeMode: (chat) => {
          asked.push(chat);
          return asked.length === 1 ? new Promise(() => {}) : Promise.resolve(false);
        },
      },
      agent: "Research assistant",
    });
    const gone = new AbortController();
    const first = ignoring.requestFreeMode(ROOT, gone.signal, "page-1");
    await vi.waitFor(() => expect(asked).toHaveLength(1));
    gone.abort();
    expect(await first).toBe(false);
    expect(await ignoring.requestFreeMode(ROOT, never(), "page-2")).toBe(false);
    expect(asked).toHaveLength(2);
    expect(journal.bindings.get(ROOT)?.mode).toBe("ask");
  });

  it("is true when the chat came to work freely while its confirmation was open, whatever that answers", async () => {
    bind(ROOT, "ask");
    const freeing = new Approvals({
      bindings: journal.bindings,
      prompts: {
        approve: () => Promise.resolve("deny"),
        // "Allow and stop asking" on the prompt it waited behind, then "Keep asking".
        confirmFreeMode: () => {
          journal.bindings.setMode(ROOT, "free");
          return Promise.resolve(false);
        },
      },
      agent: "Research assistant",
    });
    expect(await freeing.requestFreeMode(ROOT, never(), "page-1")).toBe(true);
  });
});

describe("a page that asks for Work freely again", () => {
  it("is refused without a prompt, for that chat, once its user kept the chat asking, until another page asks", async () => {
    bind(ROOT, "ask");
    bind(OTHER, "ask");
    const keeping = new User(null, [false, false, true]);
    const asking = new Approvals({ bindings: journal.bindings, prompts: keeping, agent: "Research assistant" });
    expect(await asking.requestFreeMode(ROOT, never(), "page-1")).toBe(false);
    await expect(asking.requestFreeMode(ROOT, never(), "page-1")).rejects.toThrow("The user chose to keep this chat asking");
    expect(keeping.confirmations).toHaveLength(1);
    // Another chat on that page is its own; so is the next page.
    expect(await asking.requestFreeMode(OTHER, never(), "page-1")).toBe(false);
    expect(await asking.requestFreeMode(ROOT, never(), "page-2")).toBe(true);
    expect(keeping.confirmations.map(({ root }) => root)).toEqual([ROOT, OTHER, ROOT]);
  });

  it("is asked again from the same page when the page went away while the confirmation was open", async () => {
    bind(ROOT, "ask");
    const gone = new AbortController();
    const asked: ChatLabel[] = [];
    const leaving = new Approvals({
      bindings: journal.bindings,
      prompts: {
        approve: () => Promise.resolve("deny"),
        confirmFreeMode: (chat) => {
          asked.push(chat);
          return asked.length === 1 ? new Promise(() => {}) : Promise.resolve(false);
        },
      },
      agent: "Research assistant",
    });
    const first = leaving.requestFreeMode(ROOT, gone.signal, "page-1");
    await vi.waitFor(() => expect(asked).toHaveLength(1));
    gone.abort();
    expect(await first).toBe(false);
    expect(await leaving.requestFreeMode(ROOT, never(), "page-1")).toBe(false);
    expect(asked).toHaveLength(2);
  });

  it("is asked again from another page that heard the confirmation dropped with the page that opened it", async () => {
    bind(ROOT, "ask");
    const gone = new AbortController();
    const asked: ChatLabel[] = [];
    const dropping = new Approvals({
      bindings: journal.bindings,
      prompts: {
        approve: () => Promise.resolve("deny"),
        confirmFreeMode: (chat) => {
          asked.push(chat);
          return asked.length === 1 ? new Promise(() => {}) : Promise.resolve(false);
        },
      },
      agent: "Research assistant",
    });
    const both = [dropping.requestFreeMode(ROOT, gone.signal, "page-1"), dropping.requestFreeMode(ROOT, never(), "page-2")];
    await vi.waitFor(() => expect(asked).toHaveLength(1));
    gone.abort();
    expect(await Promise.all(both)).toEqual([false, false]);
    // Nobody answered it: page 2 is asked, and only its own "Keep asking" holds it.
    expect(await dropping.requestFreeMode(ROOT, never(), "page-2")).toBe(false);
    expect(asked).toHaveLength(2);
    await expect(dropping.requestFreeMode(ROOT, never(), "page-2")).rejects.toThrow("The user chose to keep this chat asking");
  });
});

describe("approvals over the link", () => {
  let server: FakeLinkServer;
  let url: string;
  let devices: Array<{ link: DeviceLink; runner: OperationRunner }>;
  let ran: Operation[];

  // The tool hosts, as far as the binder sees them: as ToolHosts does, they look the
  // chat's folder up in the bindings when the operation runs.
  const hosts: Executor = {
    run: (operation) => {
      if (!journal.bindings.get(operation.sessionId)) return Promise.resolve(FOLDER_UNAVAILABLE);
      ran.push(operation);
      return Promise.resolve({ ok: `ran ${operation.kind}` });
    },
  };
  // No folder is prepared, unless a test says so: every chat is bound already.
  const noFolders: FolderPrompts = { confirmFolder: () => Promise.resolve(null), pickFolder: () => Promise.resolve(null) };

  // The device as the app wires it: the binder, asking the approvals, in front of the tool hosts.
  const binderFor = (prompts: User, tools: Executor = hosts, folders = noFolders) => new Binder({
    bindings: journal.bindings, prompts: folders, guards: { home: base, dataDir: join(base, "data"), appDirs: [] },
    agent: "Research assistant", hosts: tools, approvalPrompts: prompts,
  });

  async function connect(prompts: User | Binder, tools: Executor = hosts): Promise<{ link: DeviceLink; runner: OperationRunner }> {
    const binder = prompts instanceof Binder ? prompts : binderFor(prompts, tools);
    const device = connectDevice({ url, token: "surg_dev_test", journal, executor: binder, onError: () => {}, delay: () => 20 });
    devices.push(device);
    device.link.start();
    await server.until(() => device.link.status === "connected");
    return device;
  }

  const frame = (operation: Operation): Record<string, unknown> => ({
    type: "op", id: operation.id, session_id: operation.sessionId, calling_session_id: operation.callingSessionId,
    invocation_id: operation.invocationId, ordinal: operation.ordinal, kind: operation.kind, args: operation.args,
    digest: operation.digest,
  });
  const results = (id: string) => server.received.filter((f) => f.type === "op_result" && f.id === id);

  beforeEach(async () => {
    server = new FakeLinkServer();
    url = await server.start();
    devices = [];
    ran = [];
  });

  afterEach(async () => {
    for (const { link } of devices) await link.stop();
    await server.stop();
  });

  it("asks again at the next launch when the app quit with a prompt open, and the command never ran", async () => {
    bind(ROOT, "ask");
    const before = await connect(user);
    const run = op("run", RUN);
    server.send(frame(run));
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    // The quit order: the link, then the work that runs, then the journal.
    await before.link.stop();
    await before.runner.suspend(APP_CLOSED);
    expect(user.dismissed).toBe(1);
    journal.close();
    journal = new OperationJournal(join(base, "journal.sqlite"));
    expect(journal.openIds()).toEqual([run.id]);
    const again = new User();
    await connect(again);
    expect(server.hellos.at(-1)?.open).toEqual([run.id]);
    server.send(frame(run));
    await vi.waitFor(() => expect(again.open).toHaveLength(1));
    expect(ran).toEqual([]);
    again.answer("allow");
    await server.until(() => results(run.id).length === 1);
    expect(results(run.id)[0]?.outcome).toEqual({ ok: "ran run" });
    expect(ran.map((operation) => operation.id)).toEqual([run.id]);
  });

  it("dismisses an open prompt when the chat is stopped, records nothing for it, and asks the next", async () => {
    bind(ROOT, "ask");
    await connect(user);
    const [stopped, after] = [op("run", RUN), op("run", RUN)];
    server.send(frame(stopped));
    server.send(frame(after));
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    server.send({ type: "cancel", id: stopped.id });
    await vi.waitFor(() => expect([user.dismissed, user.asked.length]).toEqual([1, 2]));
    user.answer("deny");
    await server.until(() => results(after.id).length === 1);
    expect(results(after.id)[0]?.outcome).toEqual(COMMAND_DENIED);
    expect([results(stopped.id), ran, journal.openIds()]).toEqual([[], [], []]);
  });

  it("answers a command that comes just before its chat's bind folder_unavailable, and never runs it", async () => {
    const notes = join(base, "notes");
    mkdirSync(notes);
    const binder = binderFor(user, hosts, {
      pickFolder: () => Promise.resolve(notes),
      confirmFolder: () => Promise.resolve({ mode: "ask" }),
    });
    const ready = await binder.prepareFolder("pick", "window-1", never());
    if (!ready) throw new Error("the folder was not confirmed");
    const run = op("run", RUN);
    const bindChat: Operation = {
      id: "bind-1", sessionId: ROOT, callingSessionId: ROOT, invocationId: "bind", ordinal: 0, kind: "bind",
      args: { folder: ready.folder, nonce: ready.nonce }, digest: "b",
    };
    // One burst: ws hands the app both frames in one tick, the chat's bind behind the command.
    server.behindWelcome = [frame(run), frame(bindChat)];
    await connect(binder);
    await server.until(() => results(run.id).length === 1 && results(bindChat.id).length === 1);
    expect(results(run.id)[0]?.outcome).toEqual(FOLDER_UNAVAILABLE);
    expect(results(bindChat.id)[0]?.outcome).toEqual({ ok: null });
    expect([ran, user.asked]).toEqual([[], []]);
    expect(journal.bindings.get(ROOT)?.mode).toBe("ask");
  });

  it("answers a browser operation that comes just before its chat's bind folder_unavailable, asking no one", async () => {
    const notes = join(base, "notes");
    mkdirSync(notes);
    const binder = binderFor(user, hosts, {
      pickFolder: () => Promise.resolve(notes),
      confirmFolder: () => Promise.resolve({ mode: "free" }),
    });
    const ready = await binder.prepareFolder("pick", "window-1", never());
    if (!ready) throw new Error("the folder was not confirmed");
    const navigate = op("browser.navigate", { url: "https://example.com/", wait_until: "load" });
    const bindChat: Operation = {
      id: "bind-1", sessionId: ROOT, callingSessionId: ROOT, invocationId: "bind", ordinal: 0, kind: "bind",
      args: { folder: ready.folder, nonce: ready.nonce }, digest: "b",
    };
    // One burst, the browser's first use behind the chat's bind: its prompt would otherwise open for a chat bound after it.
    server.behindWelcome = [frame(navigate), frame(bindChat)];
    await connect(binder);
    await server.until(() => results(navigate.id).length === 1 && results(bindChat.id).length === 1);
    expect(results(navigate.id)[0]?.outcome).toEqual(FOLDER_UNAVAILABLE);
    expect([ran, user.asked]).toEqual([[], []]);
  });

  it("gives a command its whole timeout once it is allowed, however long its prompt was open: the tools have it only from then", { timeout: 30_000 }, async () => {
    bind(ROOT, "ask");
    let reached = 0;
    const tools: Executor = {
      run: (operation, signal) => {
        reached = performance.now();
        return hosts.run(operation, signal);
      },
    };
    await connect(user, tools);
    const run = op("run", { command: "sleep 0.45; echo done", workdir: null, timeout: 1 });
    server.send(frame(run));
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    // Longer than the command's whole timeout.
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    const allowed = performance.now();
    user.answer("allow");
    await server.until(() => results(run.id).length === 1, 20_000);
    // The guest's timeout runs from the command's start in the guest, so from here.
    expect(reached).toBeGreaterThanOrEqual(allowed);
    expect(results(run.id)[0]?.outcome).toEqual({ ok: "ran run" });
  });
});

describe("the browser on this computer", () => {
  const BROWSER_DENIED = { error: { type: "denied", message: "The user did not let the agent use the browser on this computer in this chat" } };
  const ACT_DENIED = { error: { type: "denied", message: "The user denied this in the agent's browser on this computer" } };
  const navigate = (root = ROOT, calling = root) => op("browser.navigate", { url: "https://example.com/", wait_until: "load" }, root, calling);

  it("asks a chat's first use in either mode, once: Allow for this chat holds for the chat and its sub-agents, with its binding", async () => {
    for (const [root, mode] of [[ROOT, "free"], [OTHER, "ask"]] as const) {
      bind(root, mode);
      user = new User("allow_session");
      approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant" });
      expect(await approvals.admit(op("browser.observe", { script: "snapshot@1", params: {} }, root), never())).toBeNull();
      expect(user.asked).toEqual([{ kind: "browser", chat: { ...chat(), root, calling: root }, action: "use", detail: "" }]);
      // A sub-agent of the chat, and a read, ask nothing more.
      expect(await approvals.admit(op("browser.screenshot", { clip: null, labels: [] }, root, CHILD), never())).toBeNull();
      expect(user.asked).toHaveLength(1);
    }
    // Kept with the binding: the next launch asks nothing more.
    journal.close();
    journal = new OperationJournal(join(base, "journal.sqlite"));
    user = new User("deny");
    approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant" });
    expect(await approvals.admit(navigate(), never())).toBeNull();
    expect(user.asked).toEqual([]);
  });

  it("refuses a first use the user denies, or nobody answers, or a dismissed prompt, and asks again next time", async () => {
    bind(ROOT, "free");
    user = new User("deny");
    approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant" });
    expect(await approvals.admit(navigate(), never())).toEqual(BROWSER_DENIED);
    user.auto = "allow";
    expect(await approvals.admit(navigate(), never())).toEqual(BROWSER_DENIED);
    user.auto = "timeout";
    expect(await approvals.admit(navigate(), never())).toEqual({
      error: { type: "denied", message: "Nobody answered on this computer in time, so the agent's browser did nothing" },
    });
    user.auto = null;
    const dismissed = new AbortController();
    const asking = approvals.admit(navigate(), dismissed.signal);
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    dismissed.abort();
    expect(await asking).toEqual(ACT_DENIED);
    expect(user.asked).toHaveLength(4);
    expect(journal.bindings.browsing(ROOT)).toBe(false);
  });

  describe("a port of the chat's own servers", () => {
    const open = (url: string, root = ROOT, calling = root) => op("browser.navigate", { url, wait_until: "load" }, root, calling);
    const NOT_LISTENING = (number: number) => ({
      error: {
        type: "browser",
        message: `Nothing listens on port ${number} in this chat's sandbox. Start the server there as a background command, then open http://localhost:${number}/ again.`,
      },
    });
    // The ports each chat's sandbox listens on, and each one the approvals asked it about.
    let listens: Record<string, number[]>;
    let probed: Array<[string, number]>;
    const made = (prompts: User) => new Approvals({
      bindings: journal.bindings, prompts, agent: "Research assistant",
      listening: (root, number) => (probed.push([root, number]), Promise.resolve(listens[root]?.includes(number) === true)),
    });

    beforeEach(() => {
      listens = { [ROOT]: [3000, 80], [OTHER]: [3000] };
      probed = [];
    });

    it("asks the chat's sandbox whether it listens there, in either mode, however the address spells this computer: a port that does goes on to the browser", async () => {
      bind(ROOT, "free");
      journal.bindings.allowBrowser(ROOT);
      user = new User("deny");
      approvals = made(user);
      const urls = ["http://localhost:3000/app", "http://127.0.0.1:3000/", "http://[::1]:3000/x", "http://2130706433:3000/", "http://0x7f.1:3000/", "http://LOCALHOST.:3000/"];
      for (const url of urls) expect(await approvals.admit(open(url, ROOT, CHILD), never()), url).toBeNull();
      // Its user is asked nothing for it, and the chat's own sandbox each time, by the chat and never its sub-agent.
      expect([user.asked, probed]).toEqual([[], urls.map(() => [ROOT, 3000])]);
      // A chat that asks every time is asked for the navigation as for any, once the sandbox has answered; port 80 is an address without one.
      bind(OTHER, "ask");
      journal.bindings.allowBrowser(OTHER);
      listens[OTHER] = [80];
      probed = [];
      expect(await approvals.admit(open("http://localhost/", OTHER), never())).toEqual(ACT_DENIED);
      expect(user.asked).toEqual([{ kind: "browser", chat: { ...chat(OTHER), root: OTHER }, action: "open", detail: "http://localhost/" }]);
      expect(probed).toEqual([[OTHER, 80]]);
      // Another chat's server on the port is not this chat's.
      expect(await approvals.admit(open("http://localhost:3000/", OTHER), never())).toEqual(NOT_LISTENING(3000));
    });

    it("asks a chat that may not use the browser yet for the browser first, and its sandbox only then", async () => {
      bind(ROOT, "free");
      user = new User("deny");
      approvals = made(user);
      expect(await approvals.admit(open("http://localhost:3000/"), never())).toEqual(BROWSER_DENIED);
      expect([user.asked, probed]).toEqual([[{ kind: "browser", chat: chat(), action: "use", detail: "" }], []]);
      user.auto = "allow_session";
      expect(await approvals.admit(open("http://localhost:3000/"), never())).toBeNull();
      expect([user.asked.length, probed]).toEqual([2, [[ROOT, 3000]]]);
    });

    it("asks about nothing but a port of this computer's own names over plain http: any other address is the proxy's to judge", async () => {
      bind(ROOT, "free");
      journal.bindings.allowBrowser(ROOT);
      user = new User("deny");
      approvals = made(user);
      for (const url of [
        "https://localhost:3000/", "http://example.com:3000/", "http://app.localhost:3000/", "http://0.0.0.0:3000/", "http://127.0.0.2:3000/",
        "http://[::ffff:127.0.0.1]:3000/", "http://[::]:3000/", "http://localhost.example.com:3000/", "http://user@localhost:3000/", "ws://localhost:3000/",
        "not an address",
      ]) {
        expect(await approvals.admit(open(url), never()), url).toBeNull();
      }
      // Nor at any operation but a navigation, whatever it names.
      expect(await approvals.admit(op("browser.observe", { script: "snapshot@1", params: { url: "http://localhost:3000/" } }), never())).toBeNull();
      expect([user.asked, probed]).toEqual([[], []]);
    });

    it("asks nobody about a port nothing listens on in the chat's sandbox, or one of the sandbox's own proxies, and says why", async () => {
      bind(ROOT, "ask");
      journal.bindings.allowBrowser(ROOT);
      user = new User("allow_session");
      approvals = made(user);
      expect(await approvals.admit(open("http://localhost:8000/"), never())).toEqual(NOT_LISTENING(8000));
      for (const proxy of [3128, 1080]) {
        listens[ROOT] = [proxy];
        expect(await approvals.admit(open(`http://localhost:${proxy}/`), never())).toEqual({
          error: { type: "browser", message: `Port ${proxy} is the sandbox's own proxy for this chat's commands, which the agent's browser does not open` },
        });
      }
      // A sandbox that cannot be asked listens on nothing.
      approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant", listening: () => Promise.reject(new Error("no sandbox")) });
      expect(await approvals.admit(open("http://localhost:3000/"), never())).toEqual(NOT_LISTENING(3000));
      approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant" });
      expect(await approvals.admit(open("http://localhost:3000/"), never())).toEqual(NOT_LISTENING(3000));
      // Nor does one whose answer is no yes.
      approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant", listening: () => Promise.resolve("yes" as unknown as boolean) });
      expect(await approvals.admit(open("http://localhost:3000/"), never())).toEqual(NOT_LISTENING(3000));
      // Not even for the navigation, in a chat that asks every time.
      expect([user.asked, probed]).toEqual([[], [[ROOT, 8000]]]);
    });

    it("goes with a take-over or a cancel while the sandbox is asked, as any browser prompt does", async () => {
      bind(ROOT, "free");
      journal.bindings.allowBrowser(ROOT);
      let taken = false;
      const PAUSED = { error: { type: "paused_by_user", message: "The user took over the agent's browser on this computer" } };
      // A sandbox that answers only once its user has taken the browser over.
      const answering = Promise.withResolvers<boolean>();
      let asked = 0;
      approvals = new Approvals({
        bindings: journal.bindings, prompts: user, agent: "Research assistant", listening: () => ((asked += 1), answering.promise),
        refusal: (operation) => (taken && operation.kind.startsWith("browser.") ? PAUSED : null),
      });
      const cancelled = new AbortController();
      const leaving = approvals.admit(open("http://localhost:3000/"), cancelled.signal);
      await vi.waitFor(() => expect(asked).toBe(1));
      cancelled.abort();
      expect(await leaving).toEqual(ACT_DENIED);
      const waiting = approvals.admit(open("http://localhost:3000/"), never());
      await vi.waitFor(() => expect(asked).toBe(2));
      taken = true;
      approvals.dismissBrowser();
      answering.resolve(true);
      expect(await waiting).toEqual(PAUSED);
      expect(user.asked).toEqual([]);
    });
  });

  it("asks in Ask every time before each act on the page, after the first use, and never before a read", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    user = new User("allow");
    approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant" });
    for (const read of [
      op("browser.observe", { script: "snapshot@1", params: {} }), op("browser.screenshot", { clip: null, labels: [] }), op("browser.close", {}),
      op("browser.mouse", { action: "move", x: 1, y: 2 }), op("browser.mouse", { action: "wheel", x: 1, y: 2, delta_x: 0, delta_y: 300 }),
    ]) {
      expect(await approvals.admit(read, never())).toBeNull();
    }
    expect(user.asked).toEqual([]);
    for (const act of [
      navigate(), op("browser.evaluate", { code: "return 1;" }), op("browser.mouse", { action: "click", x: 5, y: 6, button: "right", clicks: 1 }),
      op("browser.keyboard", { action: "type", text: "secret", at: null, delay: 0 }), op("browser.keyboard", { action: "type", text: 'say "hi"', at: { x: 7, y: 8 }, delay: 0 }),
      op("browser.keyboard", { action: "press", keys: "Control+Enter", delay: 0 }),
      op("browser.mouse", { action: "drag", path: [[1, 2], [3, 4]], button: "left" }),
    ]) {
      expect(await approvals.admit(act, never())).toBeNull();
    }
    expect(user.asked.map((request) => request.kind === "browser" && [request.action, request.detail])).toEqual([
      ["open", "https://example.com/"], ["script", "return 1;"], ["click", "5, 6 (right button)"], ["type", "secret"],
      ["type", '"say \\"hi\\"" at 7, 8'], ["press", "Control+Enter"], ["drag", "[[1,2],[3,4]]"],
    ]);
    user.auto = "deny";
    expect(await approvals.admit(navigate(), never())).toEqual(ACT_DENIED);
  });

  it("asks in Ask every time before files of the chat's folder go to the page, each named whole, and names the file input's own frame", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    user = new User("allow");
    // The browser says the page a session acts in, and, for an upload, the frame of the file input that asked.
    const said: Array<[string, boolean]> = [];
    approvals = new Approvals({
      bindings: journal.bindings, prompts: user, agent: "Research assistant",
      address: (session, upload = false) => (said.push([session, upload]), Promise.resolve(upload ? "https://uploads.example/form" : "https://bank.example/account")),
    });
    const paths = [`${FOLDER}/report.pdf`, `${FOLDER}/scan.png`];
    expect(await approvals.admit(op("browser.set_input_files", { paths }, ROOT, CHILD), never())).toBeNull();
    expect(await approvals.admit(op("browser.evaluate", { code: "return 1;" }, ROOT, CHILD), never())).toBeNull();
    expect(user.asked.map((request) => request.kind === "browser" && [request.action, request.files ?? request.detail, request.page])).toEqual([
      // The site that gets the files is the input's own, whatever page it is framed in. Each file is an item of its own.
      ["upload", paths, "https://uploads.example/form"], ["script", "return 1;", "https://bank.example/account"],
    ]);
    // A name that holds a line break is one item, whatever follows the break: never two files.
    const broken = [`${FOLDER}/public.txt\n${FOLDER}/draft.txt`];
    expect(await approvals.admit(op("browser.set_input_files", { paths: broken }, ROOT, CHILD), never())).toBeNull();
    expect(user.asked.at(-1)).toMatchObject({ action: "upload", detail: "", files: broken });
    user.asked.pop();
    said.pop();
    expect(said).toEqual([[CHILD, true], [CHILD, false]]);
    // A chat that works freely gives them once the agent may use the browser.
    journal.bindings.setMode(ROOT, "free");
    expect(await approvals.admit(op("browser.set_input_files", { paths }), never())).toBeNull();
    expect(user.asked).toHaveLength(2);
  });

  it("tells the browser which upload its prompt is for, so that the input it names is that upload's alone", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    user = new User("allow");
    const said: unknown[][] = [];
    approvals = new Approvals({
      bindings: journal.bindings, prompts: user, agent: "Research assistant",
      address: (...asked) => (said.push(asked), Promise.resolve("https://uploads.example/form")),
    });
    const [first, second] = [op("browser.set_input_files", { paths: [`${FOLDER}/a.pdf`] }, ROOT, CHILD), op("browser.set_input_files", { paths: [`${FOLDER}/b.pdf`] })];
    expect(first.id).not.toBe(second.id);
    expect(await approvals.admit(first, never())).toBeNull();
    expect(await approvals.admit(second, never())).toBeNull();
    expect(await approvals.admit(op("browser.evaluate", { code: "return 1;" }), never())).toBeNull();
    // Each upload by its own operation; any other act names no upload. And each with the chat it is of: a
    // session is asked after only for its own chat.
    expect(said).toEqual([[CHILD, true, first.id, ROOT], [ROOT, true, second.id, ROOT], [ROOT, false, undefined, ROOT]]);
  });

  it("asks nobody about an upload whose site the browser does not say in time, and gives it no leave: its prompt would name no site", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    user = new User("allow");
    approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant", address: () => new Promise(() => {}) });
    const started = performance.now();
    expect(await approvals.admit(op("browser.set_input_files", { paths: [`${FOLDER}/report.pdf`] }), never())).toEqual({
      error: { type: "denied", message: "The agent's browser on this computer did not say in time which site would get the files, so nobody was asked and the page was given nothing" },
    });
    expect(performance.now() - started).toBeLessThan(2_000);
    expect(user.asked).toEqual([]);
    // Nor where this computer has no word of the browser's at all, or the browser fails to say.
    for (const address of [undefined, () => Promise.reject(new Error("gone"))]) {
      approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant", ...(address ? { address } : {}) });
      expect(await approvals.admit(op("browser.set_input_files", { paths: [`${FOLDER}/report.pdf`] }), never())).toMatchObject({ error: { type: "denied" } });
    }
    expect(user.asked).toEqual([]);
  });

  it("gives an upload no leave once the browser is taken over while its prompt is open, or while the browser was still saying where its input is: answered as the tools answer then, whatever the prompt settles with", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    const PAUSED = { error: { type: "paused_by_user", message: "The user took over the agent's browser on this computer" } };
    const paths = [`${FOLDER}/report.pdf`];
    for (const [address, prompts] of [[() => Promise.resolve("https://uploads.example/form"), 1], [() => new Promise<string>(() => {}), 0]] as const) {
      let taken = false;
      // Its prompt, dismissed, settles with "Allow and stop asking": the answer that would do most.
      user = new User();
      approvals = new Approvals({
        bindings: journal.bindings, prompts: user, agent: "Research assistant", address,
        refusal: (operation) => (taken && operation.kind.startsWith("browser.") ? PAUSED : null),
      });
      const upload = approvals.admit(op("browser.set_input_files", { paths }), never());
      // The first is asked about by now; the second still waits for the browser to say where its input is.
      await new Promise((done) => setTimeout(done, 50));
      taken = true;
      approvals.dismissBrowser();
      expect(await upload).toEqual(PAUSED);
      expect(journal.bindings.get(ROOT)?.mode).toBe("ask");
      expect([user.asked.length, user.dismissed]).toEqual([prompts, prompts]);
    }
  });

  it("asks nobody about an upload the browser says can be given to nothing, as one for an input in a frame that runs as no site: refused in the browser's words", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    user = new User("allow");
    const why = "The file input that asked is in a frame that runs as no site, so it is given no files";
    approvals = new Approvals({
      bindings: journal.bindings, prompts: user, agent: "Research assistant",
      address: (_session, upload) => Promise.resolve(upload ? { refused: why } : "https://bank.example/account"),
    });
    expect(await approvals.admit(op("browser.set_input_files", { paths: [`${FOLDER}/report.pdf`] }), never())).toEqual({ error: { type: "browser", message: why } });
    expect(user.asked).toEqual([]);
    // Any other act in that page is asked about as ever.
    expect(await approvals.admit(op("browser.evaluate", { code: "return 1;" }), never())).toBeNull();
    expect(user.asked).toMatchObject([{ action: "script", page: "https://bank.example/account" }]);
  });

  it("tells the browser when an upload it was asked about got no leave, however its prompt ended: it is not coming, and the browser keeps nothing for it", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    const PAUSED = { error: { type: "paused_by_user", message: "The user took over the agent's browser on this computer" } };
    const unasked: string[] = [];
    let taken = false;
    // The browser's word for an upload's input, as the test says: an address, none in time, or that nothing can be given.
    let said: () => Promise<string | { refused: string }> = () => Promise.resolve("https://uploads.example/form");
    const asks = (answer: ApprovalAnswer | null) => {
      user = new User(answer);
      approvals = new Approvals({
        bindings: journal.bindings, prompts: user, agent: "Research assistant", address: () => said(),
        refusal: (operation) => (taken && operation.kind.startsWith("browser.") ? PAUSED : null),
        notComing: (of) => void unasked.push(of),
      });
    };
    const upload = () => op("browser.set_input_files", { paths: [`${FOLDER}/report.pdf`] });
    // Allowed, once or for good: the upload is coming, and the browser knows it by its operation until it has.
    for (const answer of ["allow", "stop_asking"] as const) {
      journal.bindings.setMode(ROOT, "ask");
      asks(answer);
      expect(await approvals.admit(upload(), never())).toBeNull();
    }
    // In a chat that works freely nobody is asked, and the browser was told of no upload.
    expect(await approvals.admit(upload(), never())).toBeNull();
    expect(unasked).toEqual([]);
    journal.bindings.setMode(ROOT, "ask");
    // Denied, or run out.
    for (const answer of ["deny", "timeout"] as const) {
      asks(answer);
      const denied = upload();
      expect(await approvals.admit(denied, never())).toMatchObject({ error: { type: "denied" } });
      expect(unasked.splice(0)).toEqual([denied.id]);
    }
    // Dismissed by a take-over, and stopped by its session, each with its prompt open.
    for (const ends of ["taken over", "stopped"] as const) {
      asks(null);
      taken = false;
      const stopped = new AbortController();
      const open = upload();
      const asking = approvals.admit(open, stopped.signal);
      await vi.waitFor(() => expect(user.open).toHaveLength(1));
      if (ends === "stopped") stopped.abort();
      else {
        taken = true;
        approvals.dismissBrowser();
      }
      await asking;
      expect(unasked.splice(0), ends).toEqual([open.id]);
    }
    taken = false;
    // Never asked about at all: the browser said nothing can be given, or did not say where in time.
    for (const none of [() => Promise.resolve({ refused: "no site" }), () => new Promise<string>(() => {})]) {
      said = none;
      asks("allow");
      const refused = upload();
      expect(await approvals.admit(refused, never())).toMatchObject({ error: {} });
      expect([user.asked, unasked.splice(0)]).toEqual([[], [refused.id]]);
    }
    // No other act of the agent's is an upload the browser was asked about.
    said = () => Promise.resolve("https://bank.example/account");
    asks("deny");
    expect(await approvals.admit(op("browser.evaluate", { code: "return 1;" }), never())).toMatchObject({ error: { type: "denied" } });
    expect(unasked).toEqual([]);
  });

  it("names a mouse press and a mouse release for what they are, not a click", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    user = new User("allow");
    approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant" });
    for (const action of ["down", "up"]) {
      expect(await approvals.admit(op("browser.mouse", { action, x: 5, y: 6, button: "right" }), never())).toBeNull();
    }
    expect(user.asked.map((request) => request.kind === "browser" && [request.action, request.detail])).toEqual([
      ["down", "5, 6 (right button)"], ["up", "5, 6 (right button)"],
    ]);
  });

  it("names the page each act would act in, as the browser says it just before, wherever the page sent itself", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    user = new User("allow");
    let at = "https://evil.example/";
    const sessions: string[] = [];
    approvals = new Approvals({
      bindings: journal.bindings, prompts: user, agent: "Research assistant", address: (session) => (sessions.push(session), Promise.resolve(at)),
    });
    expect(await approvals.admit(navigate(ROOT, CHILD), never())).toBeNull();
    // The page it opened sent itself on to another site, one its user is signed in to.
    at = "https://bank.example/account";
    for (const act of [
      op("browser.evaluate", { code: "return 1;" }, ROOT, CHILD), op("browser.mouse", { action: "click", x: 5, y: 6, button: "left", clicks: 1 }, ROOT, CHILD),
      op("browser.keyboard", { action: "type", text: "hunter2", at: null, delay: 0 }, ROOT, CHILD), op("browser.keyboard", { action: "press", keys: "Enter", delay: 0 }, ROOT, CHILD),
      op("browser.mouse", { action: "drag", path: [[1, 2], [3, 4]], button: "left" }, ROOT, CHILD),
    ]) {
      expect(await approvals.admit(act, never())).toBeNull();
    }
    expect(user.asked.map((request) => request.kind === "browser" && [request.action, request.page])).toEqual([
      ["open", undefined], ...["script", "click", "type", "press", "drag"].map((action) => [action, "https://bank.example/account"]),
    ]);
    // The calling session's page: a sub-agent acts in its own tab.
    expect(sessions).toEqual([CHILD, CHILD, CHILD, CHILD, CHILD]);
  });

  it("says the page is not known when the browser does not say within a second, and asks all the same", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    user = new User("allow");
    approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant", address: () => new Promise(() => {}) });
    const started = performance.now();
    expect(await approvals.admit(op("browser.evaluate", { code: "return 1;" }), never())).toBeNull();
    expect(performance.now() - started).toBeLessThan(2_000);
    expect(user.asked).toMatchObject([{ action: "script", page: null }]);
  });

  it("asks a chat that asks every time its first use and then the act, in its one line, and Stop asking lets it work freely", async () => {
    bind(ROOT, "ask");
    user = new User();
    approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant" });
    const asking = approvals.admit(navigate(), never());
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    // A command of the chat waits behind the browser's prompts.
    const command = approvals.admit(op("run", RUN), never());
    user.answer("allow_session");
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    expect(user.open[0]?.request).toMatchObject({ kind: "browser", action: "open" });
    user.answer("stop_asking");
    expect(await asking).toBeNull();
    // The chat works freely now: the command waiting is let through unasked.
    expect(await command).toBeNull();
    expect(journal.bindings.get(ROOT)?.mode).toBe("free");
  });

  it("dismisses every chat's open and waiting browser prompts once the browser is taken over, answering them as the tools answer now", async () => {
    for (const root of [ROOT, OTHER]) {
      bind(root, "ask");
      journal.bindings.allowBrowser(root);
    }
    const PAUSED = { error: { type: "paused_by_user", message: "The user took over the agent's browser on this computer" } };
    let taken = false;
    user = new User();
    approvals = new Approvals({
      bindings: journal.bindings, prompts: user, agent: "Research assistant",
      // The browser is the agent's one browser here: held from one chat, it is refused to every chat.
      refusal: (operation) => (taken && operation.kind.startsWith("browser.") ? PAUSED : null),
    });
    const open = approvals.admit(navigate(), never());
    const waiting = approvals.admit(op("browser.evaluate", { code: "return 1;" }), never());
    const command = approvals.admit(op("run", RUN), never());
    const other = approvals.admit(navigate(OTHER), never());
    await vi.waitFor(() => expect(user.open.map(({ request }) => request.chat.root)).toEqual([ROOT, OTHER]));
    taken = true;
    approvals.dismissBrowser();
    // Dismissed, each prompt settles with the answer that would do most: it is not the user's.
    expect(await open).toEqual(PAUSED);
    expect(await waiting).toEqual(PAUSED);
    // Another chat's too, which never asked for the take-over.
    expect(await other).toEqual(PAUSED);
    expect(user.dismissed).toBe(2);
    // A chat's command asks as before.
    await vi.waitFor(() => expect(user.open.map(({ request }) => [request.kind, request.chat.root])).toEqual([["command", ROOT]]));
    user.answer("allow");
    expect(await command).toBeNull();
    // Handed back: a chat's next act asks again.
    taken = false;
    const again = approvals.admit(navigate(), never());
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    user.answer("allow");
    expect(await again).toBeNull();
  });

  it("keeps nothing of a chat's browser prompts once they settle, and leaves no listener on the signal they came with", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    user = new User("allow");
    approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant" });
    // One signal for many operations, as a link's that lives as long as the app.
    const lasting = never();
    for (let n = 0; n < 50; n += 1) expect(await approvals.admit(navigate(), lasting)).toBeNull();
    expect(user.asked).toHaveLength(50);
    expect(getEventListeners(lasting, "abort")).toHaveLength(0);
    expect((approvals as unknown as { browsing: Map<string, unknown> }).browsing.size).toBe(0);
  });

  it("asks the tools' refusal again when a browser operation's turn comes, and asks its user nothing about one refused meanwhile", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    const PAUSED = { error: { type: "paused_by_user", message: "The user took over the agent's browser on this computer" } };
    let taken = false;
    user = new User();
    approvals = new Approvals({
      bindings: journal.bindings, prompts: user, agent: "Research assistant",
      refusal: (operation) => (taken && operation.kind.startsWith("browser.") ? PAUSED : null),
    });
    const open = approvals.admit(navigate(), never());
    const waiting = approvals.admit(op("browser.evaluate", { code: "return 1;" }), never());
    await vi.waitFor(() => expect(user.open).toHaveLength(1));
    // Refused by the tools from now on, its prompts not dismissed: the one waiting is caught at its turn all the same.
    taken = true;
    // Whatever it would be asked, it would be let through.
    user.auto = "allow";
    user.answer("allow");
    expect(await open).toBeNull();
    expect(await waiting).toEqual(PAUSED);
    expect(user.asked).toHaveLength(1);
  });

  it("asks nothing about a browser operation that comes already stopped", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    // Whatever it would be asked, it would be let through.
    user = new User("allow");
    approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "Research assistant" });
    const stopped = new AbortController();
    stopped.abort();
    expect(await approvals.admit(navigate(), stopped.signal)).toEqual(ACT_DENIED);
    expect(user.asked).toEqual([]);
  });

  it("answers a browser operation waiting behind the chat's open command prompt at the take-over, not once the command is answered", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    const PAUSED = { error: { type: "paused_by_user", message: "The user took over the agent's browser on this computer" } };
    let taken = false;
    user = new User();
    approvals = new Approvals({
      bindings: journal.bindings, prompts: user, agent: "Research assistant",
      refusal: (operation) => (taken && operation.kind.startsWith("browser.") ? PAUSED : null),
    });
    const command = approvals.admit(op("run", RUN), never());
    const waiting = approvals.admit(navigate(), never());
    await vi.waitFor(() => expect(user.open.map(({ request }) => request.kind)).toEqual(["command"]));
    taken = true;
    approvals.dismissBrowser();
    // Answered now, the command's prompt still open before it in the chat's line.
    expect(await Promise.race([waiting, new Promise((done) => setTimeout(() => done("still waiting its turn"), 1_000))])).toEqual(PAUSED);
    expect(user.open.map(({ request }) => request.kind)).toEqual(["command"]);
    expect(user.dismissed).toBe(0);
    user.answer("allow");
    expect(await command).toBeNull();
  });

  it("answers a dismissed browser operation though its prompt never settles", async () => {
    bind(ROOT, "ask");
    journal.bindings.allowBrowser(ROOT);
    const PAUSED = { error: { type: "paused_by_user", message: "The user took over the agent's browser on this computer" } };
    let taken = false;
    const asked: ApprovalRequest[] = [];
    approvals = new Approvals({
      bindings: journal.bindings, agent: "Research assistant",
      // A prompt that takes no notice of its signal, and never answers.
      prompts: { approve: (request) => (asked.push(request), new Promise<ApprovalAnswer>(() => {})), confirmFreeMode: () => Promise.resolve(false) },
      refusal: (operation) => (taken && operation.kind.startsWith("browser.") ? PAUSED : null),
    });
    const open = approvals.admit(navigate(), never());
    await vi.waitFor(() => expect(asked).toHaveLength(1));
    taken = true;
    approvals.dismissBrowser();
    expect(await Promise.race([open, new Promise((done) => setTimeout(() => done("held by its prompt"), 1_000))])).toEqual(PAUSED);
  });

  it("tells whoever watches of a chat's first use allowed once, and of nothing for a chat this computer did not bind", () => {
    bind(ROOT, "free");
    const heard: string[] = [];
    journal.bindings.watch((root) => heard.push(root));
    journal.bindings.allowBrowser(ROOT);
    journal.bindings.allowBrowser(ROOT);
    journal.bindings.allowBrowser("77777777-7777-4777-8777-777777777777");
    expect(heard).toEqual([ROOT]);
  });

  it("lets a close through unasked in a chat whose agent may not use the browser: it closes nothing", async () => {
    for (const [root, mode] of [[ROOT, "free"], [OTHER, "ask"]] as const) {
      bind(root, mode);
      expect(await approvals.admit(op("browser.close", {}, root), never())).toBeNull();
    }
    expect(user.asked).toEqual([]);
    expect(journal.bindings.browsing(ROOT)).toBe(false);
  });

  it("forgets a deleted chat's first use with its binding", () => {
    bind(ROOT, "free");
    journal.bindings.allowBrowser(ROOT);
    journal.bindings.retire(ROOT);
    expect(journal.bindings.browsing(ROOT)).toBe(false);
  });

  it("refuses a browser operation for a chat this computer did not bind, asking no one", async () => {
    expect(await approvals.admit(navigate(), never())).toEqual(FOLDER_UNAVAILABLE);
    expect(user.asked).toEqual([]);
  });
});
