import { EventEmitter } from "node:events";

import { describe, expect, it } from "vitest";

import { type Notice, Notifications, type Shown } from "../src/shell/notifications.js";

// Notifications as the system shows them: each one made, shown and closed is recorded.
class FakeShown extends EventEmitter implements Shown {
  shown = false;
  closed = false;
  constructor(readonly content: { title: string; body: string }) {
    super();
  }
  show(): void {
    this.shown = true;
  }
  close(): void {
    this.closed = true;
    this.emit("close");
  }
}

function service() {
  const made: FakeShown[] = [];
  const errors: string[] = [];
  const opened: string[] = [];
  const notifications = new Notifications((content) => {
    const shown = new FakeShown(content);
    made.push(shown);
    return shown;
  }, (error) => errors.push(String(error)));
  const notice = (tag: string, title = tag): Notice => ({ tag, title, body: "Finished.", open: () => opened.push(tag) });
  return { notifications, made, errors, opened, notice };
}

describe("the notification service", () => {
  it("shows one notice per tag, a newer one closing the one it replaces", () => {
    const { notifications, made, notice } = service();
    notifications.show(notice("chat:a", "First"));
    notifications.show(notice("chat:b"));
    notifications.show(notice("chat:a", "Second"));
    expect(made.map((shown) => [shown.content.title, shown.shown, shown.closed])).toEqual([
      ["First", true, true], ["chat:b", true, false], ["Second", true, false],
    ]);
  });

  it("opens what a click tells of, once, and forgets a notice clicked, closed or failed", () => {
    const { notifications, made, opened, errors, notice } = service();
    notifications.show(notice("chat:a"));
    notifications.show(notice("chat:b"));
    notifications.show(notice("chat:c"));
    made[0]!.emit("click");
    made[1]!.emit("close");
    made[2]!.emit("failed", {}, "");
    notifications.closeAll();
    // None is held any more: closing them all closes none of them.
    expect(made.map((shown) => shown.closed)).toEqual([false, false, false]);
    expect(opened).toEqual(["chat:a"]);
    expect(errors).toEqual(["Error: Surogate could not show a notification: the system has no notification service"]);
  });

  it("closes every notice it holds, and a closed one opens nothing", () => {
    const { notifications, made, opened, notice } = service();
    notifications.show(notice("chat:a"));
    notifications.show(notice("asking"));
    notifications.closeAll();
    made[0]!.emit("click");
    expect(made.map((shown) => shown.closed)).toEqual([true, true]);
    expect(opened).toEqual([]);
  });

  it("shows nothing where the system has no notification service", () => {
    const notifications = new Notifications(null, () => {});
    expect(() => notifications.show({ tag: "asking", title: "t", body: "b", open: () => {} })).not.toThrow();
  });
});
