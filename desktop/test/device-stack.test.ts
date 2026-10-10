import { existsSync, mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApprovalRequest } from "../src/binding/approvals.js";
import { BOOT_ID, type FolderGuards } from "../src/binding/folder.js";
import type { StagedDownload } from "../src/browser/downloads.js";
import { Browsing } from "../src/browser/executor.js";
import type { NetworkApprovals } from "../src/hosts/tool-hosts.js";
import type { Bindings } from "../src/journal/bindings.js";
import { OperationJournal } from "../src/journal/journal.js";
import type { LinkStatus } from "../src/link/client.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import { ACCESS_ENDED, APP_CLOSED } from "../src/operations/runner.js";
import { type DeviceStack, type DeviceStackOptions, startDevice, stopDevice, type ToolLayer } from "../src/shell/device-stack.js";
import { FakeLinkServer } from "./fake-server.js";

const ROOT = "66666666-6666-4666-8666-666666666666";
// A sub-agent of the chat: its operations run in the chat's folder, as sessions of their own.
const CHILD = "77777777-7777-4777-8777-777777777777";
// Another chat of the agent's on this computer.
const OTHER = "88888888-8888-4888-8888-888888888888";
const IDENTITY = { deviceId: "d", orgId: "o", agentId: "a", userId: "u" };
const TAKEN: Outcome = { error: { type: "paused_by_user", message: "taken over" } };

// The tool layer under the binder, as far as the stack sees it.
class Tools implements ToolLayer {
  readonly ran: Operation[] = [];
  hold: "no" | "until-aborted" | "forever" = "no";
  bindings: Bindings | null = null;
  network: NetworkApprovals | null = null;
  // The sessions it says have a background process alive, and how it tells the stack they changed.
  liveRoots: string[] = [];
  changed: () => void = () => {};
  // The page each session's next browser operation acts in, as a browser here would say it.
  address?: (session: string) => Promise<string>;
  // The chat whose user holds the agent's browser: every chat's browser operations are refused meanwhile.
  taken: string | null = null;
  // What an operation answers, where a test says; and what the stack gave it to save its downloads with.
  answer: ((operation: Operation) => Outcome) | null = null;
  save: ((download: StagedDownload, stop: AbortSignal) => Promise<string>) | null = null;
  // The ports the journal held each time the stack had the tools tell the browser and the sandbox of them.
  readonly forwards: Array<Array<{ port: number; root: string }>> = [];
  // The ports each chat's sandbox listens on, and each question the stack's approvals asked of it.
  listens: number[] = [];
  readonly probed: Array<[string, number]> = [];
  // Whether a project's thread is given a copy of its folder to work in here.
  copies = false;

  constructor(private readonly base: string, private readonly order: string[]) {}

  keepsCopies(): boolean {
    return this.copies;
  }

  guards(): FolderGuards {
    return { home: join(this.base, "home"), dataDir: join(this.base, "data"), cacheDir: join(this.base, "cache"), appDirs: [] };
  }

  live(): string[] {
    return this.liveRoots;
  }

  refusal(operation: Operation): Outcome | null {
    return operation.kind.startsWith("browser.") && this.taken !== null ? TAKEN : null;
  }

  takeOver(root: string): boolean {
    this.taken ??= root;
    return this.taken === root;
  }

  handBack(root: string): boolean {
    if (this.taken !== root) return false;
    this.taken = null;
    return true;
  }

  saveDownloadsWith(save: (download: StagedDownload, stop: AbortSignal) => Promise<string>): void {
    this.save = save;
  }

  forwarded(): void {
    this.forwards.push(this.bindings?.forwards() ?? []);
  }

  listening(root: string, port: number): Promise<boolean> {
    this.probed.push([root, port]);
    return Promise.resolve(this.listens.includes(port));
  }

  run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    this.ran.push(operation);
    if (this.answer) return Promise.resolve(this.answer(operation));
    if (this.hold === "no") return Promise.resolve({ ok: `ran ${operation.kind}` });
    return new Promise((resolve) => {
      if (this.hold === "until-aborted") {
        signal.addEventListener("abort", () => {
          this.order.push("aborted");
          resolve({ error: { type: "cancelled", message: "stopped" } });
        });
      }
    });
  }

  stop(): Promise<void> {
    this.order.push("tools");
    return Promise.resolve();
  }
}

let base: string;
let folder: string;
let server: FakeLinkServer;
let order: string[];
let tools: Tools;
let statuses: LinkStatus[];
let errors: unknown[];
let stops: Array<() => Promise<void>>;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "stack-")));
  folder = join(base, "home", "work");
  mkdirSync(folder, { recursive: true });
  server = new FakeLinkServer();
  order = [];
  tools = new Tools(base, order);
  statuses = [];
  errors = [];
  stops = [];
});

afterEach(async () => {
  for (const stop of stops) await stop();
  await server.stop();
  rmSync(base, { recursive: true, force: true });
});

async function start(overrides: Partial<DeviceStackOptions> = {}) {
  const url = await server.start();
  const device = startDevice({
    journalPath: join(base, "data", "devices", "d", "journal.sqlite"),
    url,
    token: "surg_dev_test",
    agent: "agent.example.com",
    identity: IDENTITY,
    tools: (bindings, network, changed) => {
      tools.bindings = bindings;
      tools.network = network;
      tools.changed = changed;
      return tools;
    },
    prompts: { pickFolder: () => Promise.resolve(folder), confirmFolder: (sheet) => Promise.resolve({ mode: sheet.mode }) },
    approvalPrompts: { approve: () => Promise.resolve("deny"), confirmFreeMode: () => Promise.resolve(false) },
    onStatus: (status) => {
      statuses.push(status);
      if (status === "stopped") order.push("link");
    },
    onError: (error) => errors.push(error),
    delay: () => 20,
    ...overrides,
  });
  stops.push(() => device.stop());
  return device;
}

function op(id: string, kind: string, args: Record<string, unknown>, own = false, calling = ROOT, root = ROOT): Record<string, unknown> {
  return {
    type: "op", id, session_id: root, calling_session_id: calling, invocation_id: own ? "bind" : "1:c",
    ordinal: own ? 0 : 1, kind, args, digest: `digest-${id}`,
  };
}

const results = (id: string) => server.received.filter((frame) => frame.type === "op_result" && frame.id === id);

describe("one agent's device", () => {
  it.each([[true, { ok: { history: { thread: ROOT } } }], [false, null]] as const)(
    "binds a project's thread to its copy only where its tools keep copies (%s), and says so to the server",
    async (copies, answer) => {
      tools.copies = copies;
      const device = await start();
      await server.until(() => statuses.includes("connected"));
      const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
      server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce, history: { thread: ROOT } }, true));
      await server.until(() => results("bind-1").length === 1);
      if (answer) expect(results("bind-1")[0]?.outcome).toEqual(answer);
      else expect(results("bind-1")[0]?.outcome).toMatchObject({ error: { type: "binding" } });
      expect(tools.bindings?.get(ROOT)?.history).toBe(copies ? ROOT : undefined);
    },
  );

  it("runs a bound chat's operations on its tools, through the binder", async () => {
    const device = await start();
    await server.until(() => statuses.includes("connected"));
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    expect(prepared?.folder).toBe(folder);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    expect(results("bind-1")[0]?.outcome).toEqual({ ok: null });
    expect(tools.bindings?.get(ROOT)?.folder).toBe(folder);
    server.send(op("run-1", "run", { command: "echo hi", workdir: null, timeout: 10 }));
    await server.until(() => results("run-1").length === 1);
    expect(results("run-1")[0]?.outcome).toEqual({ ok: "ran run" });
    expect(tools.ran.map((ran) => ran.id)).toEqual(["run-1"]);
    // The tools' network questions go to the binder's approvals: a chat this computer did not bind is denied.
    expect(await tools.network?.askNetwork("77777777-7777-4777-8777-777777777777",
      { host: "example.com", port: 443, privateNetwork: false }, new AbortController().signal)).toBe("deny");
  });

  it("names in an act's prompt the page its tools say the calling session acts in", async () => {
    const asked: ApprovalRequest[] = [];
    tools.address = (session) => Promise.resolve(`https://bank.example/${session}`);
    const device = await start({
      prompts: { pickFolder: () => Promise.resolve(folder), confirmFolder: () => Promise.resolve({ mode: "ask" }) },
      approvalPrompts: { approve: (request) => (asked.push(request), Promise.resolve("allow")), confirmFreeMode: () => Promise.resolve(false) },
    });
    await server.until(() => statuses.includes("connected"));
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    tools.bindings?.allowBrowser(ROOT);
    server.send(op("script-1", "browser.evaluate", { code: "return 1;" }, false, CHILD));
    await server.until(() => results("script-1").length === 1);
    expect(asked).toMatchObject([{ kind: "browser", action: "script", page: `https://bank.example/${CHILD}` }]);
  });

  it("asks its tools whether a chat's sandbox listens, and then its user, before its browser is sent to a port of the chat's own servers: to none nothing listens on, and none its user did not allow", async () => {
    const asked: ApprovalRequest[] = [];
    let answer: "deny" | "allow_session" = "deny";
    const device = await start({
      approvalPrompts: { approve: (request) => (asked.push(request), Promise.resolve(answer)), confirmFreeMode: () => Promise.resolve(false) },
    });
    await server.until(() => statuses.includes("connected"));
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    tools.bindings?.allowBrowser(ROOT);
    // Nothing listens there: the agent is told so, the tools run nothing, and the user is asked nothing.
    server.send(op("nav-1", "browser.navigate", { url: "http://localhost:3000/", wait_until: "load" }));
    await server.until(() => results("nav-1").length === 1);
    expect(results("nav-1")[0]?.outcome).toEqual({
      error: {
        type: "browser",
        message: "Nothing listens on port 3000 in this chat's sandbox. Start the server there as a background command, then open http://localhost:3000/ again.",
      },
    });
    expect(asked).toEqual([]);
    // Something does: its user is asked, and denied, the tools still run nothing.
    tools.listens = [3000];
    server.send(op("nav-2", "browser.navigate", { url: "http://127.0.0.1:3000/", wait_until: "load" }, false, CHILD));
    await server.until(() => results("nav-2").length === 1);
    expect(results("nav-2")[0]?.outcome).toEqual({ error: { type: "denied", message: "The user did not let the agent's browser open port 3000 of this chat's servers" } });
    expect(tools.ran.filter((ran) => ran.kind === "browser.navigate")).toEqual([]);
    // Allowed for the chat, it is kept with the chat's binding, and its next navigation there asks nobody.
    answer = "allow_session";
    server.send(op("nav-3", "browser.navigate", { url: "http://127.0.0.1:3000/", wait_until: "load" }, false, CHILD));
    await server.until(() => results("nav-3").length === 1);
    server.send(op("nav-4", "browser.navigate", { url: "http://localhost:3000/x", wait_until: "load" }));
    await server.until(() => results("nav-4").length === 1);
    expect([results("nav-3")[0]?.outcome, results("nav-4")[0]?.outcome]).toEqual([{ ok: "ran browser.navigate" }, { ok: "ran browser.navigate" }]);
    expect(tools.bindings?.ports(ROOT)).toEqual([3000]);
    // The sandbox is asked by the chat's root, whichever of its sessions navigates, and its user about that chat.
    expect(tools.probed).toEqual([[ROOT, 3000], [ROOT, 3000], [ROOT, 3000]]);
    expect(asked).toMatchObject([1, 2].map(() => ({ kind: "browser", action: "port", detail: "3000", chat: { root: ROOT, calling: CHILD } })));
    expect(asked).toHaveLength(2);
  });

  it("has its tools tell of the ports the browser may open at its start, what the run before kept, and at each change: allowed, moved, taken back, and gone with a deleted chat", async () => {
    // A port the run before kept.
    mkdirSync(join(base, "data", "devices", "d"), { recursive: true });
    const before = new OperationJournal(join(base, "data", "devices", "d", "journal.sqlite"));
    before.bindings.add({ root: OTHER, nonce: "nonce-other", folder, dev: 1, ino: 1, boot: BOOT_ID, mode: "free", boundAt: 1 });
    before.bindings.allowPort(OTHER, 5173);
    before.close();
    const device = await start({
      approvalPrompts: { approve: () => Promise.resolve("allow_session"), confirmFreeMode: () => Promise.resolve(false) },
    });
    await server.until(() => statuses.includes("connected"));
    expect(tools.forwards).toEqual([[{ port: 5173, root: OTHER }]]);
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    tools.bindings?.allowBrowser(ROOT);
    // Allowed by the chat's user at its agent's navigation: told before the navigation runs.
    tools.listens = [3000];
    tools.answer = (operation) => ({ ok: [operation.kind, tools.forwards.at(-1)] });
    server.send(op("nav-1", "browser.navigate", { url: "http://localhost:3000/", wait_until: "load" }));
    await server.until(() => results("nav-1").length === 1);
    expect(results("nav-1")[0]?.outcome).toEqual({ ok: ["browser.navigate", [{ port: 3000, root: ROOT }, { port: 5173, root: OTHER }]] });
    // Given to another chat, and taken back in Settings: each told at once.
    device.bindings.allowPort(ROOT, 5173);
    expect(tools.forwards.at(-1)).toEqual([{ port: 3000, root: ROOT }, { port: 5173, root: ROOT }]);
    device.bindings.disallowPort(ROOT, 3000);
    expect(tools.forwards.at(-1)).toEqual([{ port: 5173, root: ROOT }]);
    // The chat's browser taken back, its ports with it; and a chat deleted, its ports with its binding.
    device.bindings.allowPort(OTHER, 8000);
    device.bindings.disallowBrowser(ROOT);
    expect(tools.forwards.at(-1)).toEqual([{ port: 8000, root: OTHER }]);
    device.bindings.allowPort(ROOT, 3000);
    server.send({ ...op("retire-1", "retire", {}, true), invocation_id: "retire" });
    await server.until(() => results("retire-1").length === 1);
    expect([device.bindings.get(ROOT), tools.forwards.at(-1)]).toEqual([undefined, [{ port: 8000, root: OTHER }]]);
  });

  it("takes the agent's browser over through its tools, from the chat that asks first: every chat's open browser prompt dismissed and answered as its tools answer now", async () => {
    const asked: ApprovalRequest[] = [];
    const device = await start({
      approvalPrompts: {
        // Open until dismissed, when it settles with the answer that would let it through.
        approve: (request, signal) => (asked.push(request), new Promise((resolve) => signal.addEventListener("abort", () => resolve("allow_session")))),
        confirmFreeMode: () => Promise.resolve(false),
      },
    });
    await server.until(() => statuses.includes("connected"));
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    // Another chat bound here, whose browser prompt is the one open: the browser is the same one.
    tools.bindings?.add({ root: OTHER, nonce: "nonce-other", folder, dev: 1, ino: 1, boot: BOOT_ID, mode: "free", boundAt: 1 });
    server.send(op("nav-1", "browser.navigate", { url: "https://example.com/", wait_until: "load" }, false, OTHER, OTHER));
    await vi.waitFor(() => expect(asked).toMatchObject([{ kind: "browser", action: "use", chat: { root: OTHER } }]));
    expect(device.takeOver(ROOT)).toBe(true);
    await server.until(() => results("nav-1").length === 1);
    expect(results("nav-1")[0]?.outcome).toEqual(TAKEN);
    expect(tools.ran).toEqual([]);
    expect(tools.bindings?.browsing(OTHER)).toBe(false);
    // Another chat's take-over does not steal it, and its hand back ends nothing.
    expect(device.takeOver(OTHER)).toBe(false);
    expect(device.handBack(OTHER)).toBe(false);
    expect(tools.taken).toBe(ROOT);
    // Handed back by the chat that holds it, and the stack says whether its tools released anything.
    expect(device.handBack(ROOT)).toBe(true);
    expect(tools.taken).toBeNull();
    expect(device.handBack(ROOT)).toBe(false);
  });

  it("stops its link when the welcome names another identity, and says why", async () => {
    await start({ identity: { ...IDENTITY, agentId: "another-agent" } });
    await server.until(() => statuses.includes("stopped"));
    expect(errors.map(String)).toEqual([
      "Error: This computer's token now connects to agent a, not another-agent, so its link stopped",
    ]);
  });

  it("quits in order: the link, what runs recorded closed by the app, the tools, the journal", async () => {
    const device = await start();
    await server.until(() => statuses.includes("connected"));
    tools.hold = "until-aborted";
    // A chat bound earlier, as the journal keeps it.
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    server.send(op("run-1", "run", { command: "sleep 9", workdir: null, timeout: 10 }));
    await server.until(() => tools.ran.length === 1);
    await device.stop();
    expect(order).toEqual(["link", "aborted", "tools"]);
    // A second stop, as a second quit would ask, changes nothing.
    await device.stop();
    expect(order).toEqual(["link", "aborted", "tools"]);
    // Closed: the file opens again, and the operation is recorded closed by the app.
    const journal = new OperationJournal(join(base, "data", "devices", "d", "journal.sqlite"));
    try {
      expect(journal.unsent().find((result) => result.id === "run-1")?.outcome).toEqual(APP_CLOSED);
    } finally {
      journal.close();
    }
  });

  it("revokes itself on its own link: what runs ends as access ended, then it stops", async () => {
    const device = await start();
    await server.until(() => statuses.includes("connected"));
    tools.hold = "until-aborted";
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    server.send(op("run-1", "run", { command: "sleep 9", workdir: null, timeout: 10 }));
    await server.until(() => tools.ran.length === 1);
    expect(await device.revoke()).toBe(true);
    expect(server.received.filter((frame) => frame.type === "revoke")).toHaveLength(1);
    expect(statuses.at(-1)).toBe("revoked");
    expect(order).toEqual(["aborted", "tools"]);
    const journal = new OperationJournal(join(base, "data", "devices", "d", "journal.sqlite"));
    try {
      expect(journal.unsent().find((result) => result.id === "run-1")?.outcome).toEqual(ACCESS_ENDED);
    } finally {
      journal.close();
    }
  });

  it("retires once the agent ended its token: it stops, and its journal keeps its bindings and nothing to send", async () => {
    const device = await start();
    await server.until(() => statuses.includes("connected"));
    tools.hold = "until-aborted";
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    server.send(op("run-1", "run", { command: "sleep 9", workdir: null, timeout: 10 }));
    await server.until(() => tools.ran.length === 1);
    server.close(4403);
    await server.until(() => statuses.includes("revoked"));
    await device.retire();
    const journal = new OperationJournal(join(base, "data", "devices", "d", "journal.sqlite"));
    try {
      expect(journal.unsent()).toEqual([]);
      expect(journal.bindings.folders()).toEqual([folder]);
    } finally {
      journal.close();
    }
  });

  it("says when the agent cannot hear the revocation, and stops all the same", async () => {
    const device = await start();
    await server.until(() => statuses.includes("connected"));
    server.drop();
    await server.until(() => statuses.at(-1) === "offline" || statuses.at(-1) === "connecting");
    await server.stop();
    expect(await device.revoke(200)).toBe(false);
    expect(order).toEqual(["link", "tools"]);
  });

  it("counts the sessions whose operations its tools run, and says each time the count changes", async () => {
    const counts: number[] = [];
    const device = await start({ onWorking: (count) => counts.push(count) });
    await server.until(() => statuses.includes("connected"));
    tools.hold = "until-aborted";
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    expect(device.working()).toBe(0);
    server.send(op("run-1", "run", { command: "sleep 9", workdir: null, timeout: 10 }));
    server.send(op("run-2", "run", { command: "sleep 9", workdir: null, timeout: 10 }));
    await server.until(() => tools.ran.length === 2);
    expect(device.working()).toBe(1);
    server.send({ type: "cancel", id: "run-1" });
    server.send({ type: "cancel", id: "run-2" });
    await server.until(() => device.working() === 0);
    expect(counts).toEqual([1, 0]);
  });

  it("counts a chat whose background process lives on the tools as working, the chat once, and says when that changes", async () => {
    const counts: number[] = [];
    const device = await start({ onWorking: (count) => counts.push(count) });
    await server.until(() => statuses.includes("connected"));
    tools.hold = "until-aborted";
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    tools.liveRoots = [ROOT];
    tools.changed();
    expect(device.working()).toBe(1);
    // A command of the same chat while its server runs: still one chat at work.
    server.send(op("run-1", "run", { command: "sleep 9", workdir: null, timeout: 10 }));
    await server.until(() => tools.ran.length === 1);
    expect(device.working()).toBe(1);
    server.send({ type: "cancel", id: "run-1" });
    await server.until(() => order.includes("aborted"));
    expect(device.working()).toBe(1);
    tools.liveRoots = [];
    tools.changed();
    expect(device.working()).toBe(0);
    expect(counts).toEqual([1, 0]);
  });

  it("gives its tools what saves a download: through its binder, asked as a write of the chat's that a page downloaded, and made create-only on its tools", async () => {
    const asked: ApprovalRequest[] = [];
    const device = await start({
      prompts: { pickFolder: () => Promise.resolve(folder), confirmFolder: () => Promise.resolve({ mode: "ask" }) },
      approvalPrompts: { approve: (request) => (asked.push(request), Promise.resolve("allow")), confirmFreeMode: () => Promise.resolve(false) },
    });
    await server.until(() => statuses.includes("connected"));
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    // The chat's file host, as far as a save asks it: each path its own, nothing at any, and a write that lands.
    tools.answer = (operation) => ({ ok: operation.kind === "resolve" ? operation.args.path : null });
    const staged = join(base, "staged");
    writeFileSync(staged, "report");
    const stop = new AbortController();
    expect(await tools.save?.({ root: ROOT, session: CHILD, name: "report.txt", path: staged, user: false }, stop.signal)).toBe(
      'The page downloaded "report.txt". It is saved in the chat\'s folder as Downloads/report.txt.',
    );
    const key = join(folder, "Downloads", "report.txt");
    expect(asked).toMatchObject([{ kind: "change", action: "write", path: key, bytes: 6, download: "page", chat: { root: ROOT, calling: CHILD } }]);
    expect(tools.ran.map(({ kind, args }) => [kind, args.path ?? args.key, args.create])).toEqual([
      ["resolve", join(folder, "Downloads"), undefined], ["stat", join(folder, "Downloads"), undefined],
      ["resolve", key, undefined], ["stat", key, undefined], ["write", key, true],
    ]);
    expect(tools.ran.at(-1)?.args.data).toBe(Buffer.from("report").toString("base64"));
    // Counted as the chat's sub-agent's work while it ran, and gone since; the staged file went too.
    expect([device.working(), existsSync(staged)]).toEqual([0, false]);
    // Its user's own, made while they held the browser: asked as theirs.
    writeFileSync(staged, "theirs");
    await tools.save?.({ root: ROOT, session: CHILD, name: "statement.pdf", path: staged, user: true }, stop.signal);
    expect(asked.at(-1)).toMatchObject({ kind: "change", path: join(folder, "Downloads", "statement.pdf"), download: "user" });
    // Told to stop, as when its chat is deleted: what it asks of its tools is stopped by the same, and nobody is asked.
    stop.abort();
    writeFileSync(staged, "late");
    tools.ran.length = 0;
    expect(await tools.save?.({ root: ROOT, session: CHILD, name: "late.txt", path: staged, user: false }, stop.signal)).toContain("but it was not saved");
    expect([asked.length, tools.ran.some((ran) => ran.kind === "write"), existsSync(staged)]).toEqual([2, false, false]);
  });

  it("counts a chat whose background process lives beneath the browser's layer, as the app's own stack wires it", async () => {
    const device = await start({
      tools: (bindings, network, changed) => {
        tools.changed = changed;
        return new Browsing({
          tools,
          browser: {
            perform: () => Promise.resolve({ ok: null }), forget: () => {}, stop: () => Promise.resolve(), end: () => Promise.resolve(), address: () => Promise.resolve("about:blank"),
            notComing: () => {}, pause: () => {}, show: () => Promise.resolve(false), onDownload: () => {}, forwards: () => {},
          },
          bindingOf: (root) => bindings.get(root),
          launch: () => null,
          staging: join(base, "data", "browser-profiles", "tmp"),
          ports: () => bindings.forwards(),
          vm: { door: join(base, "browser.sock"), forwards: () => {}, listening: () => Promise.resolve(false) },
        });
      },
    });
    await server.until(() => statuses.includes("connected"));
    tools.liveRoots = [ROOT];
    tools.changed();
    expect(device.working()).toBe(1);
    tools.liveRoots = [];
    tools.changed();
    expect(device.working()).toBe(0);
  });

  it("counts a chat's sub-agent apart from the chat, as a session of its own", async () => {
    const counts: number[] = [];
    const device = await start({ onWorking: (count) => counts.push(count) });
    await server.until(() => statuses.includes("connected"));
    tools.hold = "until-aborted";
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    server.send(op("run-1", "run", { command: "sleep 9", workdir: null, timeout: 10 }));
    server.send(op("run-2", "run", { command: "sleep 9", workdir: null, timeout: 10 }, false, CHILD));
    await server.until(() => tools.ran.length === 2);
    expect(device.working()).toBe(2);
    server.send({ type: "cancel", id: "run-1" });
    server.send({ type: "cancel", id: "run-2" });
    await server.until(() => device.working() === 0);
    expect(counts).toEqual([1, 2, 1, 0]);
  });

  it("clears its deadline once what runs has stopped, so nothing is left waiting on it", async () => {
    const device = await start({ quitTimeoutMs: 54_321 });
    await server.until(() => statuses.includes("connected"));
    const armed = vi.spyOn(globalThis, "setTimeout");
    const cleared = vi.spyOn(globalThis, "clearTimeout");
    try {
      await device.stop();
      const deadlines = armed.mock.calls.flatMap((call, index) => (call[1] === 54_321 ? [armed.mock.results[index]?.value] : []));
      expect(deadlines).toHaveLength(1);
      expect(cleared.mock.calls.map(([timer]) => timer)).toContain(deadlines[0]);
    } finally {
      armed.mockRestore();
      cleared.mockRestore();
    }
  });

  it("waits no longer than its deadline for an operation that ignores the quit", async () => {
    const device = await start({ quitTimeoutMs: 200 });
    await server.until(() => statuses.includes("connected"));
    tools.hold = "forever";
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    server.send(op("run-1", "run", { command: "sleep 9", workdir: null, timeout: 10 }));
    await server.until(() => tools.ran.length === 1);
    const started = performance.now();
    await device.stop();
    expect(performance.now() - started).toBeLessThan(2_000);
    expect(order).toEqual(["link", "tools"]);
    // The journal closed with it still started: the next launch answers it interrupted.
    const journal = new OperationJournal(join(base, "data", "devices", "d", "journal.sqlite"));
    try {
      expect(journal.recovered).toBe(1);
    } finally {
      journal.close();
    }
  });
});

describe("a device that cannot start or stop", () => {
  const journalPath = () => join(base, "data", "devices", "d", "journal.sqlite");
  // The journal's lock is free: another can open it.
  const free = () => {
    const journal = new OperationJournal(journalPath());
    journal.close();
  };

  it("lets its journal go and stops its tools when its start throws, and says why", async () => {
    tools.guards = () => {
      throw new Error("the app's environment has no HOME");
    };
    await expect(start()).rejects.toThrow("the app's environment has no HOME");
    expect(order).toEqual(["tools"]);
    free();
  });

  it("closes its journal when its tools' stop throws, and says why", async () => {
    tools.stop = () => {
      order.push("tools");
      return Promise.reject(new Error("a host would not stop"));
    };
    const device = await start();
    stops.pop();
    await server.until(() => statuses.includes("connected"));
    await expect(device.stop()).rejects.toThrow("a host would not stop");
    free();
  });
});

describe("the app's stop", () => {
  const vm = () => {
    const stopped: string[] = [];
    return { stopped, shared: { stop: async () => void stopped.push("vm") } };
  };

  it("stops what every device shares after the device, when the device's stop throws too", async () => {
    const { stopped, shared } = vm();
    const failing = { stop: () => Promise.reject(new Error("a host would not stop")) } as unknown as DeviceStack;
    await expect(stopDevice(Promise.resolve(failing), shared)).rejects.toThrow("a host would not stop");
    expect(stopped).toEqual(["vm"]);
  });

  it("stops what every device shares when the device never started, or there is none", async () => {
    const { stopped, shared } = vm();
    await stopDevice(Promise.reject(new Error("no start")), shared);
    await stopDevice(undefined, shared);
    await stopDevice(undefined, null);
    expect(stopped).toEqual(["vm", "vm"]);
  });
});
