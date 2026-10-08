// One agent's device on this computer (spec, Sections 1 and 2): its journal, the
// tools under the binder, the binder, and the link. The binder runs every
// operation; the tools below it come from the caller, so they can be replaced
// without touching the shell.

import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

import type { ApprovalPrompts } from "../binding/approvals.js";
import { Binder, type FolderPrompts } from "../binding/binder.js";
import type { FolderGuards } from "../binding/folder.js";
import { connectDevice } from "../device.js";
import type { NetworkApprovals } from "../hosts/tool-hosts.js";
import type { Bindings } from "../journal/bindings.js";
import { OperationJournal } from "../journal/journal.js";
import type { LinkStatus } from "../link/client.js";
import type { Operation, Outcome, Welcome } from "../link/protocol.js";
import { APP_CLOSED, type Executor } from "../operations/runner.js";

// How long a quit waits for what runs to be recorded "closed by the app". An operation
// still held after it stays "started", and the next launch answers it interrupted.
export const QUIT_TIMEOUT_MS = 5_000;
// How long a sign-out waits for the agent to confirm this device is revoked.
export const REVOKE_TIMEOUT_MS = 5_000;

export type Identity = Pick<Welcome, "deviceId" | "orgId" | "agentId" | "userId">;

// What runs operations under the binder. Its guards are the folders no chat's folder
// may hold: the binder's sheet checks the same ones, or it could accept a folder the tools refuse.
export interface ToolLayer extends Executor {
  guards(): FolderGuards;
  // The sessions with a background process alive on the tools: a quit would end their work.
  live(): string[];
  stop(): Promise<void>;
  // What the tools refuse anyway, before the chat's user is asked about it: null to ask as usual.
  refusal?(operation: Operation): Outcome | null;
  // A deleted chat's root: what the tools keep for it goes, such as its browser tabs.
  retired?(root: string): void;
  // The address of the page a calling session's next browser operation acts in, for its prompt.
  address?(session: string): Promise<string>;
  // A chat's user takes the agent's browser over, for every chat, until that chat hands it back: whether the
  // chat holds it now, which it does not while another chat's take-over stands. Where it is held, as a chat
  // is told: true from that chat, false by nobody, "elsewhere" from another chat that is here, "orphaned"
  // from a chat that is gone, which any chat may hand back. A chat's newest page shown.
  takeOver?(root: string): boolean;
  handBack?(root: string): void;
  takenOver?(root: string): boolean | "orphaned" | "elsewhere";
  show?(root: string): Promise<boolean>;
}

export interface DeviceStackOptions {
  journalPath: string;
  url: string;
  token: string;
  agent: string; // the agent's name, as the prompts show it
  identity: Identity; // who every welcome must name
  // *changed*: what the tools' live() names may have changed.
  tools(bindings: Bindings, network: NetworkApprovals, changed: () => void): ToolLayer;
  prompts: FolderPrompts;
  approvalPrompts: ApprovalPrompts;
  onStatus(status: LinkStatus): void;
  // Each chat bound here, each change of a chat's mode, and each chat's folder forgotten, by the chat's id, once it is written.
  onBindingChanged?: (root: string) => void;
  onError(error: unknown): void;
  // How many sessions (a chat, and each of its sub-agents) have an operation running on the tools,
  // or a background process alive there, each time that changes: the quit asks first.
  onWorking?: (count: number) => void;
  quitTimeoutMs?: number;
  delay?: (attempt: number) => number;
}

const ENDED: readonly LinkStatus[] = ["revoked", "unauthenticated"];

export interface DeviceStack {
  readonly binder: Binder;
  readonly bindings: Bindings; // the journal's: each chat's folder, mode and grants
  readonly tools: ToolLayer;
  /**
   * Its user takes the agent's browser over, from the chat: its tools refuse every chat's browser operations,
   * and every chat's browser prompts go. Whether the chat holds the browser now: false while another chat's
   * take-over stands, which this one does not end.
   */
  takeOver(root: string): boolean;
  /**
   * Its user hands the browser back: the agent's browser operations run again. From the chat that holds it,
   * or from any chat once the one it was held from is gone; from another chat while its holder is here, nothing.
   */
  handBack(root: string): void;
  working(): number;
  stop(): Promise<void>;
  /** Revoke this device on its own link, then stop: true once the agent confirmed, false when it could not hear it in time. */
  revoke(timeoutMs?: number): Promise<boolean>;
  /** The agent ended this device's token: stop, and leave the journal with nothing to send or run, its bindings kept. */
  retire(): Promise<void>;
}

export function startDevice(options: DeviceStackOptions): DeviceStack {
  mkdirSync(dirname(options.journalPath), { recursive: true, mode: 0o700 });
  const journal = new OperationJournal(options.journalPath);
  const made: { tools?: ToolLayer } = {};
  try {
    return deviceOn(journal, options, made);
  } catch (error) {
    // A start that fails leaves nothing open: its tools stop, and the journal's lock goes for the next start.
    void made.tools?.stop().catch(() => {});
    journal.close();
    throw error;
  }
}

/**
 * The app's stop, after a device's: a device still starting is stopped once it has
 * started, then what every device shares (the VM), whether or not the device's stop
 * throws, which it then rethrows.
 */
export async function stopDevice(started: Promise<DeviceStack> | undefined, shared: { stop(): Promise<void> } | null): Promise<void> {
  try {
    await started?.then((stack) => stack.stop(), () => {});
  } finally {
    await shared?.stop();
  }
}

// The stack on *journal*. *made* holds its tools as soon as they are made, for a start that fails after.
function deviceOn(journal: OperationJournal, options: DeviceStackOptions, made: { tools?: ToolLayer }): DeviceStack {
  // The tools ask the binder's approvals about the network; the binder exists by the time any host asks.
  const network: NetworkApprovals = { askNetwork: (root, asked, signal) => binder.approvals.askNetwork(root, asked, signal) };
  // Each session's operations on the tools, counted as they run: the binder runs nothing else there.
  // A chat's sub-agents are sessions of their own: their operations carry the chat as sessionId.
  // A chat with a background process alive on the tools works too, as a quit would end it.
  const running = new Map<string, number>();
  let told = 0;
  const working = (): number => new Set([...running.keys(), ...(made.tools?.live() ?? [])]).size;
  const tell = (): void => {
    const now = working();
    if (now !== told) options.onWorking?.((told = now));
  };
  const tools = options.tools(journal.bindings, network, tell);
  made.tools = tools;
  if (options.onBindingChanged) journal.bindings.watch(options.onBindingChanged);
  const count = (session: string, change: number): void => {
    const left = (running.get(session) ?? 0) + change;
    if (left === 0) running.delete(session);
    else running.set(session, left);
    tell();
  };
  const counted: Executor = {
    run: async (operation, signal) => {
      count(operation.callingSessionId, 1);
      try {
        return await tools.run(operation, signal);
      } finally {
        count(operation.callingSessionId, -1);
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
    refusal: (operation) => tools.refusal?.(operation) ?? null,
    retired: (root) => tools.retired?.(root),
    address: tools.address?.bind(tools),
    approvalPrompts: options.approvalPrompts,
    onError: options.onError,
  });
  const { identity } = options;
  // Settled once the agent ends this device's token: what a revoke waits for.
  const ended = Promise.withResolvers<void>();
  const { link, runner } = connectDevice({
    url: options.url,
    token: options.token,
    journal,
    executor: binder,
    onStatus: (status) => {
      if (ENDED.includes(status)) ended.resolve();
      options.onStatus(status);
    },
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
  let retiring = false;
  // The quit order: the link, so nothing new comes; what runs, recorded closed by the
  // app, within the deadline; the tools; the journal.
  const quit = async (): Promise<void> => {
    await link.stop();
    let deadline: NodeJS.Timeout | undefined;
    await Promise.race([
      runner.suspend(APP_CLOSED),
      new Promise((resolve) => {
        deadline = setTimeout(resolve, options.quitTimeoutMs ?? QUIT_TIMEOUT_MS);
      }),
    ]);
    // Cleared once what runs has stopped: a plain Node process would otherwise live on until the deadline.
    clearTimeout(deadline);
    // The journal closes whatever the tools' stop does: its lock is the next launch's.
    try {
      await tools.stop();
    } finally {
      try {
        if (retiring) journal.retire();
      } finally {
        journal.close();
      }
    }
  };
  const stop = (): Promise<void> => {
    stopping ??= quit();
    return stopping;
  };
  return {
    binder,
    bindings: journal.bindings,
    tools,
    takeOver: (root) => {
      const held = tools.takeOver?.(root) === true;
      // Each prompt dismissed is answered as the tools answer by then: paused.
      if (held) binder.approvals.dismissBrowser();
      return held;
    },
    handBack: (root) => tools.handBack?.(root),
    working,
    stop,
    retire: () => {
      retiring = true;
      return stop();
    },
    revoke: async (timeoutMs = REVOKE_TIMEOUT_MS) => {
      let timer: NodeJS.Timeout | undefined;
      // The agent closes the link as revoked once it heard the frame, and suspends what runs here with it.
      const heard = link.revoke() && await Promise.race([
        ended.promise.then(() => true),
        new Promise<boolean>((resolve) => {
          timer = setTimeout(() => resolve(false), timeoutMs);
        }),
      ]);
      clearTimeout(timer);
      await stop();
      return heard;
    },
  };
}
