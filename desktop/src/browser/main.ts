// The browser host's process (spec, Section 1): an Electron utility process in the app, which
// hears its parent through process.parentPort, and a Node child process with an IPC channel in
// the tests. The browser it launches dies with it: its pipe closes.

import type { Outcome } from "../link/protocol.js";
import type { FromBrowser, ToBrowser } from "./client.js";
import { BrowserHost } from "./host.js";

// Electron's, in a utility process.
interface ParentPort {
  on(event: "message", listener: (event: { data: unknown }) => void): void;
  postMessage(message: unknown): void;
}

const parent = (process as { parentPort?: ParentPort }).parentPort;
// Settles once the message is on its way: a stop's exit waits for the answers before it.
const send = (message: FromBrowser): Promise<void> => new Promise((resolve) => {
  if (parent) {
    parent.postMessage(message);
    resolve();
  } else if (process.send) {
    process.send(message, undefined, undefined, () => resolve());
  } else {
    resolve();
  }
});

const host = new BrowserHost();
const running = new Map<string, AbortController>();
const answering = new Set<Promise<void>>();

function answer(id: string, work: Promise<Outcome>, type: "result" | "tried" = "result"): void {
  const sent = work.then((outcome) => send({ type, id, outcome }));
  answering.add(sent);
  void sent.finally(() => answering.delete(sent));
}

function received(message: ToBrowser): void {
  if (message.type === "op") {
    const controller = new AbortController();
    running.set(message.id, controller);
    answer(message.id, host.perform(message.launch, message.root, message.session, message.kind, message.args, controller.signal)
      .finally(() => running.delete(message.id)));
  } else if (message.type === "try") {
    answer(message.id, host.tryBrowser(message.executable), "tried");
  } else if (message.type === "cancel") {
    running.get(message.id)?.abort();
  } else if (message.type === "forget") {
    void host.forget(message.root);
  } else if (message.type === "stop") {
    void (async () => {
      for (const controller of running.values()) controller.abort();
      await host.close();
      await Promise.all(answering);
      // Messages arrive in order: the parent, once it hears this, has every answer, and ends this process.
      if (parent) void send({ type: "stopped" });
      else process.exit(0);
    })();
  }
}

if (parent) {
  parent.on("message", (event) => received(event.data as ToBrowser));
} else {
  process.on("message", (message) => received(message as ToBrowser));
  // The app went without a word: the browser goes with this process.
  process.on("disconnect", () => process.exit(0));
}
