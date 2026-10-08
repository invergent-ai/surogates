// The tool layer under a device's binder once the agent has a browser here (spec, Section 5):
// the browser's kinds go to the identity's browser host, every other kind to the tools beneath.

import type { FolderGuards } from "../binding/folder.js";
import { FOLDER_UNAVAILABLE } from "../hosts/messages.js";
import type { Operation, Outcome } from "../link/protocol.js";
import type { ToolLayer } from "../shell/device-stack.js";
import type { BrowserClient } from "./client.js";
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
  browser: Pick<BrowserClient, "perform" | "forget" | "stop" | "end">;
  // A browser operation runs only for a chat this computer bound: its binding, or undefined.
  bindingOf(root: string): unknown;
  // The browser Settings chose and the identity's profile for it, read at each operation; null: none here.
  launch(): Launch | null;
}

export class Browsing implements ToolLayer {
  constructor(private readonly options: BrowsingOptions) {}

  // No browser here: refused before the chat's user is asked to let the agent use it.
  refusal(operation: Operation): Outcome | null {
    if (isBrowserKind(operation.kind)) return this.options.launch() === null ? NO_BROWSER : null;
    return this.options.tools.refusal?.(operation) ?? null;
  }

  run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    if (!isBrowserKind(operation.kind)) return this.options.tools.run(operation, signal);
    if (!this.options.bindingOf(operation.sessionId)) return Promise.resolve(FOLDER_UNAVAILABLE);
    const launch = this.options.launch();
    return launch ? this.options.browser.perform(launch, operation, signal) : Promise.resolve(NO_BROWSER);
  }

  // A deleted chat: its tabs close, with every popup its sessions opened.
  retired(root: string): void {
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
