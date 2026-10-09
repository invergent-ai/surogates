// The browser host's process (spec, Section 1): an Electron utility process in the app, which
// hears its parent through process.parentPort, and a Node child process with an IPC channel in
// the tests. The browser it launches dies with it: its pipe closes.

import type { Outcome } from "../link/protocol.js";
import { type FromBrowser, NEW_TAB, type ToBrowser } from "./client.js";
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

// Each download a page finished goes to the parent, which saves it under the chat's folder.
const host = new BrowserHost({ downloaded: (download) => void send({ type: "download", ...download }) });
const running = new Map<string, AbortController>();
const answering = new Set<Promise<void>>();

function answer(id: string, work: Promise<Outcome>, type: "result" | "tried" = "result"): void {
  const sent = work.then((outcome) => send({ type, id, outcome }));
  answering.add(sent);
  void sent.finally(() => answering.delete(sent));
}

function received(message: ToBrowser): void {
  if (message.type === "op") {
    // A second under the id of one still running is never run: the client refuses it, and its answer would be the first's.
    if (running.has(message.id)) return;
    const controller = new AbortController();
    running.set(message.id, controller);
    answer(message.id, host.perform(message.launch, message.root, message.session, message.kind, message.args, controller.signal, message.id)
      .finally(() => running.delete(message.id)));
  } else if (message.type === "try") {
    answer(message.id, host.tryBrowser(message.executable), "tried");
  } else if (message.type === "address") {
    void host.address(message.session, message.upload === true, typeof message.of === "string" ? message.of : undefined, message.root)
      .then((said) => send(typeof said === "string" ? { type: "address", id: message.id, url: said } : { type: "address", id: message.id, url: NEW_TAB, ...said }));
  } else if (message.type === "not_coming") {
    host.notComing(message.of);
  } else if (message.type === "pause") {
    host.pause(message.root, message.paused);
  } else if (message.type === "show") {
    void host.show(message.root).then((shown) => send({ type: "shown", id: message.id, shown }));
  } else if (message.type === "cancel") {
    // Still running: it ends cancelled. Answered already: the answer crossed this on the way, and whoever
    // waited for it took the cancel's, so what the answer carried of what the page did is kept for the next.
    const controller = running.get(message.id);
    if (controller) controller.abort();
    else host.unanswered(message.id);
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
