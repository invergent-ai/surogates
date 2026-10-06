// The VM manager's process (spec, Section 11): an Electron utility process in the
// app, which hears its parent through process.parentPort, and a Node child process
// with an IPC channel in the tests and the cross-check. QEMU and virtiofsd die with it.

import type { Outcome } from "../link/protocol.js";
import type { FromManager, ToManager } from "./client.js";
import { unavailable, VmManager } from "./manager.js";

// Electron's, in a utility process.
interface ParentPort {
  on(event: "message", listener: (event: { data: unknown }) => void): void;
  postMessage(message: unknown): void;
}

const parent = (process as { parentPort?: ParentPort }).parentPort;
// Settles once the message is on its way: a stop's exit waits for the answers before it.
const send = (message: FromManager): Promise<void> => new Promise((resolve) => {
  if (parent) {
    parent.postMessage(message);
    resolve();
  } else if (process.send) {
    process.send(message, undefined, undefined, () => resolve());
  } else {
    resolve();
  }
});

let manager: VmManager | null = null;
const running = new Map<string, AbortController>();
// Every answer still to send.
const answering = new Set<Promise<void>>();

// *work*'s outcome, sent as *id*'s result; a stop waits for it.
function answer(id: string, work: Promise<Outcome>): void {
  const sent = work.then((outcome) => send({ type: "result", id, outcome }));
  answering.add(sent);
  void sent.finally(() => answering.delete(sent));
}

function received(message: ToManager): void {
  if (message.type === "start") {
    manager ??= new VmManager(message.options);
    void send({ type: "ready" });
  } else if (message.type === "op") {
    const { id } = message.operation;
    if (!manager) return void send({ type: "result", id, outcome: unavailable("was not started") });
    const controller = new AbortController();
    running.set(id, controller);
    answer(id, manager.perform(message.operation, controller.signal).finally(() => running.delete(id)));
  } else if (message.type === "teardown") {
    answer(message.id, (manager?.teardown(message.root) ?? Promise.resolve()).then(() => ({ ok: null })));
  } else if (message.type === "cancel") {
    running.get(message.id)?.abort();
  } else if (message.type === "stop") {
    void (async () => {
      await manager?.stop();
      // What ran when the stop came is answered first: "is stopping", or its end.
      await Promise.all(answering);
      // Messages arrive in order: the parent, once it hears this, has every answer, and ends this process.
      if (parent) void send({ type: "stopped" });
      else process.exit(0);
    })();
  }
}

if (parent) {
  parent.on("message", (event) => received(event.data as ToManager));
} else {
  process.on("message", (message) => received(message as ToManager));
  // The app went without a word: the guest goes with this process.
  process.on("disconnect", () => process.exit(0));
}
