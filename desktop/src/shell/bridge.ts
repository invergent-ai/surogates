// The bridge's calls in the main process (spec, Section 8). Each is checked before it
// does anything, as Claude Desktop checks claude.ai's (its main bundle validates every
// call's sender frame, then each argument's type): the sender is the top frame of a
// view of the agent's web client, on the agent's exact origin, and each argument has
// its shape. What the page sends is copied field by field. No call answers an approval.

import type {
  DesktopAccount, DesktopAppearance, DesktopDeviceState, DesktopPreparedFolder,
} from "../../../web/src/lib/desktop-bridge-contract.js";
import { sameOrigin } from "./window-policy.js";

// Electron's event.senderFrame: null once the frame has navigated or gone.
export interface SenderFrame {
  readonly url: string;
  readonly parent: unknown;
}

export interface BridgeCalls {
  getDevice(): DesktopDeviceState;
  webSignIn(): Promise<{ code: string } | null>;
  signOut(): Promise<void>;
  prepareFolder(choice: "last" | "pick", window: string): Promise<DesktopPreparedFolder | null>;
  bindSession(sessionId: string, token: string, window: string): Promise<void>;
  // The chat asks every time from now on: the page can make a chat only safer.
  setMode(sessionId: string, mode: "ask"): Promise<void>;
  // The desktop's own confirmation: true once the chat works freely.
  requestFreeMode(sessionId: string, window: string): Promise<boolean>;
  // A folder confirmed in this window whose chat was never created: its bind is refused from now on.
  cancelPrepared(token: string, window: string): Promise<void>;
  getAppearance(): DesktopAppearance;
  setAccount(account: DesktopAccount | null): void;
  // The page registered its projects source (true), or withdrew it; the source stays in the page's preload.
  registerProjects(registered: boolean): void;
}

export type Handler = (frame: SenderFrame | null, window: string, ...args: unknown[]) => Promise<unknown>;

// The binder's confirmation token: 32 random bytes, base64url.
const PREPARED = /^[A-Za-z0-9_-]{43}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const text = (value: unknown, max: number): value is string => typeof value === "string" && value.length <= max;
const named = (value: unknown): value is string => text(value, 200) && value !== "";

function accountOf(value: unknown): DesktopAccount | null {
  if (value === null) return null;
  const { name, email, userId, orgId } = (typeof value === "object" && value !== null ? value : {}) as Record<string, unknown>;
  if (!text(name, 200) || !text(email, 320) || !named(userId) || !named(orgId)) throw new Error("Not an account");
  return { name, email, userId, orgId };
}

export function bridgeHandlers(origin: string, calls: BridgeCalls): Record<string, Handler> {
  // One question of each kind at a time for a window: a page cannot pile prompts up behind the one it has open.
  const asking = new Set<string>();
  const alone = <T>(kind: string, window: string, ask: () => Promise<T>): Promise<T> => {
    const key = `${kind}\0${window}`;
    if (asking.has(key)) return Promise.reject(new Error("Surogate is already asking"));
    asking.add(key);
    return Promise.resolve().then(ask).finally(() => asking.delete(key));
  };
  const checked = (run: (window: string, ...args: unknown[]) => unknown): Handler => async (frame, window, ...args) => {
    if (frame === null || frame.parent !== null || !sameOrigin(origin, frame.url)) throw new Error("Not the agent's web client");
    return run(window, ...args);
  };
  return {
    getDevice: checked(() => calls.getDevice()),
    webSignIn: checked(() => calls.webSignIn()),
    signOut: checked(() => calls.signOut()),
    prepareFolder: checked((window, choice) => {
      if (choice !== "last" && choice !== "pick") throw new Error("Not a folder choice");
      return alone("folder", window, () => calls.prepareFolder(choice, window));
    }),
    bindSession: checked((window, sessionId, token) => {
      if (typeof sessionId !== "string" || !UUID.test(sessionId)) throw new Error("Not a chat");
      if (typeof token !== "string" || !PREPARED.test(token)) throw new Error("Not a folder confirmation");
      return calls.bindSession(sessionId, token, window);
    }),
    setMode: checked((_window, sessionId, mode) => {
      if (typeof sessionId !== "string" || !UUID.test(sessionId)) throw new Error("Not a chat");
      if (mode !== "ask") throw new Error("Only the desktop can let a chat work freely");
      return calls.setMode(sessionId, mode);
    }),
    requestFreeMode: checked((window, sessionId) => {
      if (typeof sessionId !== "string" || !UUID.test(sessionId)) throw new Error("Not a chat");
      return alone("free mode", window, () => calls.requestFreeMode(sessionId, window));
    }),
    cancelPrepared: checked((window, token) => {
      if (typeof token !== "string" || !PREPARED.test(token)) throw new Error("Not a folder confirmation");
      return calls.cancelPrepared(token, window);
    }),
    getAppearance: checked(() => calls.getAppearance()),
    setAccount: checked((_window, account) => calls.setAccount(accountOf(account))),
    registerProjects: checked((_window, registered) => {
      if (typeof registered !== "boolean") throw new Error("Not a registration");
      calls.registerProjects(registered);
    }),
  };
}
