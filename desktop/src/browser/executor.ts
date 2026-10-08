// The tool layer under a device's binder once the agent has a browser here (spec, Section 5):
// the browser's kinds go to the identity's browser host, every other kind to the tools beneath.

import type { FolderGuards } from "../binding/folder.js";
import { FOLDER_UNAVAILABLE } from "../hosts/messages.js";
import type { Operation, Outcome } from "../link/protocol.js";
import type { ToolLayer } from "../shell/device-stack.js";
import { type BrowserClient, PAUSED } from "./client.js";
import type { Launch } from "./host.js";

export const BROWSER_KINDS = "browser.";

// As surogates/devices/browser.py's NO_BROWSER says it, which the tools' result is.
export const NO_BROWSER: Outcome = {
  error: {
    type: "no_browser",
    message: "No supported browser on this computer. Install Google Chrome, Microsoft Edge, Brave or Vivaldi, or pick one in Settings → Browser. The Snap build of Chromium is not supported.",
  },
};

export const isBrowserKind = (kind: string): boolean => kind.startsWith(BROWSER_KINDS);

export interface BrowsingOptions {
  tools: ToolLayer;
  browser: Pick<BrowserClient, "perform" | "forget" | "stop" | "end" | "address" | "pause" | "show">;
  // A browser operation runs only for a chat this computer bound: its binding, or undefined.
  bindingOf(root: string): unknown;
  // The browser Settings chose and the identity's profile for it, read at each operation; null: none here.
  launch(): Launch | null;
}

export class Browsing implements ToolLayer {
  // ponytail: the chats whose user took the browser over, until handed back, in this run of the app only: the browser
  // ends with the app too, and its next launch is a new one.
  private readonly paused = new Set<string>();

  constructor(private readonly options: BrowsingOptions) {}

  // A chat its user took the browser over, or no browser here: refused before the chat's user is asked anything.
  refusal(operation: Operation): Outcome | null {
    if (!isBrowserKind(operation.kind)) return this.options.tools.refusal?.(operation) ?? null;
    if (this.paused.has(operation.sessionId)) return PAUSED;
    return this.options.launch() === null ? NO_BROWSER : null;
  }

  run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    if (!isBrowserKind(operation.kind)) return this.options.tools.run(operation, signal);
    if (!this.options.bindingOf(operation.sessionId)) return Promise.resolve(FOLDER_UNAVAILABLE);
    // Let through before its user took the browser over, it never reaches the browser after.
    if (this.paused.has(operation.sessionId)) return Promise.resolve(PAUSED);
    const launch = this.options.launch();
    return launch ? this.options.browser.perform(launch, operation, signal) : Promise.resolve(NO_BROWSER);
  }

  address(session: string): Promise<string> {
    return this.options.browser.address(session);
  }

  /** The chat's user takes its browser over: its agent's browser operations are answered paused_by_user until handed back. */
  takeOver(root: string): void {
    this.paused.add(root);
    this.options.browser.pause(root, true);
  }

  /** The chat's user handed its browser back, through the desktop's own confirmation. */
  handBack(root: string): void {
    this.paused.delete(root);
    this.options.browser.pause(root, false);
  }

  takenOver(root: string): boolean {
    return this.paused.has(root);
  }

  /** The chat's newest page brought to the front: whether there was one. */
  show(root: string): Promise<boolean> {
    return this.options.browser.show(root);
  }

  // A deleted chat: its tabs close, with every popup its sessions opened.
  retired(root: string): void {
    this.paused.delete(root);
    this.options.browser.forget(root);
    this.options.tools.retired?.(root);
  }

  guards(): FolderGuards {
    return this.options.tools.guards();
  }

  // A tab runs nothing of a chat's once its operation answers: what is alive is the tools'.
  live(): string[] {
    return this.options.tools.live();
  }

  // The app's quit, a log out: the browser closes, and the tools stop.
  async stop(): Promise<void> {
    await Promise.all([this.options.browser.stop(), this.options.tools.stop()]);
  }

  // The computer's access ended: the browser closes with every tab, and what runs in the tools ends.
  async end(): Promise<void> {
    await Promise.all([this.options.browser.end(), this.options.tools.end?.()]);
  }
}
