// A toast on Windows: one that times out leaves the screen for the Action Center, where Electron still emits its click
// (Electron, Notification, close's reason "timedOut"): it is held for that click, which opens what it tells of. One
// the person dismissed, or the app closed, is not. On Linux, a notification closed in any way opens nothing, as before.

import { EventEmitter } from "node:events";

import { describe, expect, it } from "vitest";

import type { Notice, Shown } from "../src/shell/notifications.js";
import { importedOn } from "./platforms.js";

class Toast extends EventEmitter implements Shown {
  show(): void {}
  close(): void {
    this.emit("close", { reason: "applicationHidden" });
  }
}

async function service(os: NodeJS.Platform) {
  const { Notifications } = await importedOn(os, () => import("../src/shell/notifications.js"));
  const made: Toast[] = [];
  const opened: string[] = [];
  const notifications = new Notifications(() => {
    const toast = new Toast();
    made.push(toast);
    return toast;
  }, () => {});
  const notice = (tag: string): Notice => ({ tag, title: tag, body: "Finished.", open: () => opened.push(tag) });
  return { notifications, made, opened, notice };
}

describe("a toast on Windows", () => {
  it("opens its chat from the Action Center once it has timed out off the screen", async () => {
    const { notifications, made, opened, notice } = await service("win32");
    notifications.show(notice("chat:a"));
    made[0]!.emit("close", { reason: "timedOut" });
    made[0]!.emit("click");
    expect(opened).toEqual(["chat:a"]);
  });

  it("opens nothing once the person dismissed it, the app closed it, or a newer one replaced it", async () => {
    const { notifications, made, opened, notice } = await service("win32");
    notifications.show(notice("chat:a"));
    made[0]!.emit("close", { reason: "userCanceled" });
    made[0]!.emit("click");
    notifications.show(notice("chat:b"));
    notifications.close("chat:b");
    made[1]!.emit("click");
    notifications.show(notice("chat:c"));
    made[2]!.emit("close", { reason: "timedOut" });
    notifications.show(notice("chat:c"));
    made[2]!.emit("click");
    expect(opened).toEqual([]);
  });

  it("is closed in the Action Center too, once timed out, when the app closes its tag", async () => {
    const { notifications, made, notice } = await service("win32");
    notifications.show(notice("chat:a"));
    made[0]!.emit("close", { reason: "timedOut" });
    let closed = false;
    made[0]!.close = () => void (closed = true);
    notifications.close("chat:a");
    expect(closed).toBe(true);
  });
});

it("opens nothing on Linux once a notification closed, however it closed", async () => {
  const { notifications, made, opened, notice } = await service("linux");
  notifications.show(notice("chat:a"));
  made[0]!.emit("close", { reason: "timedOut" });
  made[0]!.emit("click");
  expect(opened).toEqual([]);
});
