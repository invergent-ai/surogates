// The system's notifications, raised by the main process as Claude Desktop raises its own
// (index.chunk-CV5ohobO.js): one per tag, a newer one replacing it, each held until it is
// clicked, closed or fails to show, so that its click still reaches the app. A click opens
// what it tells of. Its title may be the agent's words, as a chat's or a question's; its body
// is the app's own, since some notification services read markup in a body.

import { report } from "../report.js";

// Electron's Notification, as far as the service uses it. On Linux, failed is emitted too.
export interface Shown {
  on(event: "click" | "close", listener: () => void): unknown;
  on(event: "failed", listener: (event: unknown, error: string) => void): unknown;
  show(): void;
  close(): void;
}

export interface Notice {
  tag: string; // one notice per tag: a newer one replaces it
  title: string;
  body: string;
  open(): void; // what a click does
}

export class Notifications {
  private readonly shown = new Map<string, Shown>();

  /** *make* is null where the system has no notification service. */
  constructor(
    private readonly make: ((content: { title: string; body: string }) => Shown) | null,
    private readonly onError: (error: unknown) => void,
  ) {}

  show(notice: Notice): void {
    if (!this.make) return;
    this.close(notice.tag);
    const shown = this.make({ title: notice.title, body: notice.body });
    this.shown.set(notice.tag, shown);
    const done = () => {
      if (this.shown.get(notice.tag) === shown) this.shown.delete(notice.tag);
    };
    shown.on("click", () => {
      // One closed, replaced or signed out of opens nothing.
      if (this.shown.get(notice.tag) !== shown) return;
      this.shown.delete(notice.tag);
      notice.open();
    });
    shown.on("close", done);
    shown.on("failed", (_event, error) => {
      done();
      report(this.onError, new Error(`Surogate could not show a notification: ${error || "the system has no notification service"}`));
    });
    shown.show();
  }

  close(tag: string): void {
    const shown = this.shown.get(tag);
    if (!shown) return;
    this.shown.delete(tag);
    shown.close();
  }

  /** Close every notice: what the user signed out of, or quit, opens nothing more. */
  closeAll(): void {
    for (const tag of [...this.shown.keys()]) this.close(tag);
  }
}
