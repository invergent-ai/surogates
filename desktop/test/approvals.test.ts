import { mkdirSync, mkdtempSync, realpathSync, rmSync, statSync } from "node:fs";
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
import { ToolHosts } from "../src/hosts/tool-hosts.js";
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
      "resolve", "check_write", "stat", "read", "read_lines", "list_dir", "ripgrep", "which", "poll", "read_output",
      "wait", "kill", "list_processes",
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

  it("gives a command its whole timeout once it is allowed, however long its prompt was open", { timeout: 30_000 }, async () => {
    const folder = join(base, "notes");
    const home = join(base, "home");
    mkdirSync(folder);
    mkdirSync(home);
    const { dev, ino } = statSync(folder);
    journal.bindings.add({ root: ROOT, nonce: "n".repeat(16), folder, dev, ino, boot: BOOT_ID, mode: "ask", boundAt: 1 });
    const tools = new ToolHosts({
      bindingOf: (root) => journal.bindings.get(root),
      dataDir: join(base, "data"),
      env: { HOME: home, LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
    });
    try {
      await connect(user, tools);
      const run = op("run", { command: "sleep 0.45; echo done", workdir: null, timeout: 1 });
      server.send(frame(run));
      await vi.waitFor(() => expect(user.open).toHaveLength(1));
      // Longer than the command's whole timeout.
      await new Promise((resolve) => setTimeout(resolve, 1_500));
      user.answer("allow");
      await server.until(() => results(run.id).length === 1, 20_000);
      expect(results(run.id)[0]?.outcome).toEqual({ ok: { output: "done\n", returncode: 0, timed_out: false } });
    } finally {
      await tools.stop();
    }
  });
});
