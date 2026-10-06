// One agent's device on this computer (spec, Sections 1 and 2): its journal, the
// tools under the binder, the binder, and the link. The binder runs every
// operation; the tools below it come from the caller, so they can be replaced
// without touching the shell.

import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

import type { ApprovalPrompts } from "../binding/approvals.js";
import { Binder, type FolderPrompts } from "../binding/binder.js";
import type { FolderGuards } from "../binding/folder.js";
import { connectDevice } from "../device.js";
import type { NetworkApprovals } from "../hosts/tool-hosts.js";
import type { Bindings } from "../journal/bindings.js";
import { OperationJournal } from "../journal/journal.js";
import type { LinkStatus } from "../link/client.js";
import type { Welcome } from "../link/protocol.js";
import { APP_CLOSED, type Executor } from "../operations/runner.js";

// How long a quit waits for what runs to be recorded "closed by the app". An operation
// still held after it stays "started", and the next launch answers it interrupted.
export const QUIT_TIMEOUT_MS = 5_000;

export type Identity = Pick<Welcome, "deviceId" | "orgId" | "agentId" | "userId">;

// What runs operations under the binder. Its guards are the folders no chat's folder
// may hold: the binder's sheet checks the same ones, or it could accept a folder the tools refuse.
export interface ToolLayer extends Executor {
  guards(): FolderGuards;
  stop(): Promise<void>;
}

export interface DeviceStackOptions {
  journalPath: string;
  url: string;
  token: string;
  agent: string; // the agent's name, as the prompts show it
  identity: Identity; // who every welcome must name
  tools(bindings: Bindings, network: NetworkApprovals): ToolLayer;
  prompts: FolderPrompts;
  approvalPrompts: ApprovalPrompts;
  onStatus(status: LinkStatus): void;
  onError(error: unknown): void;
  // How many chats have an operation running on the tools, each time that changes: the quit asks first.
  onWorking?: (count: number) => void;
  quitTimeoutMs?: number;
  delay?: (attempt: number) => number;
}

export interface DeviceStack {
  readonly binder: Binder;
  working(): number;
  stop(): Promise<void>;
}

export function startDevice(options: DeviceStackOptions): DeviceStack {
  mkdirSync(dirname(options.journalPath), { recursive: true, mode: 0o700 });
  const journal = new OperationJournal(options.journalPath);
  // The tools ask the binder's approvals about the network; the binder exists by the time any host asks.
  const network: NetworkApprovals = {
    granted: (root) => binder.approvals.granted(root),
    askNetwork: (root, asked, signal) => binder.approvals.askNetwork(root, asked, signal),
  };
  const tools = options.tools(journal.bindings, network);
  // Each chat's operations on the tools, counted as they run: the binder runs nothing else there.
  const running = new Map<string, number>();
  const count = (root: string, change: number): void => {
    const before = running.size;
    const left = (running.get(root) ?? 0) + change;
    if (left === 0) running.delete(root);
    else running.set(root, left);
    if (running.size !== before) options.onWorking?.(running.size);
  };
  const counted: Executor = {
    run: async (operation, signal) => {
      count(operation.sessionId, 1);
      try {
        return await tools.run(operation, signal);
      } finally {
        count(operation.sessionId, -1);
      }
    },
    end: () => tools.end?.() ?? Promise.resolve(),
  };
  const binder: Binder = new Binder({
    bindings: journal.bindings,
    prompts: options.prompts,
    guards: tools.guards(),
    agent: options.agent,
    hosts: counted,
    approvalPrompts: options.approvalPrompts,
    onError: options.onError,
  });
  const { identity } = options;
  const { link, runner } = connectDevice({
    url: options.url,
    token: options.token,
    journal,
    executor: binder,
    onStatus: options.onStatus,
    onError: options.onError,
    delay: options.delay,
    // The bindings are this identity's: a token that now names another stops before anything runs.
    onWelcome: (welcome) => {
      const named = (Object.keys(identity) as Array<keyof Identity>).find((key) => welcome[key] !== identity[key]);
      if (named !== undefined) {
        throw new Error(
          `This computer's token now connects to ${named.replace("Id", "")} ${welcome[named]}, not ${identity[named]}, so its link stopped`,
        );
      }
    },
  });
  link.start();

  let stopping: Promise<void> | undefined;
  // The quit order: the link, so nothing new comes; what runs, recorded closed by the
  // app, within the deadline; the tools; the journal.
  const quit = async (): Promise<void> => {
    await link.stop();
    await Promise.race([runner.suspend(APP_CLOSED), sleep(options.quitTimeoutMs ?? QUIT_TIMEOUT_MS)]);
    await tools.stop();
    journal.close();
  };
  return {
    binder,
    working: () => running.size,
    stop: () => {
      stopping ??= quit();
      return stopping;
    },
  };
}
