import { describe, expect, it } from "vitest";

import { CANCELLED } from "../src/guest/command.js";
import { Control, type ControlRoots, NO_HELLO } from "../src/guest/control.js";
import type { FromAgent, HostUser } from "../src/guest/protocol.js";
import type { Outcome } from "../src/link/protocol.js";

const USER: HostUser = { uid: 1000, gid: 1000, name: "someone", home: "/home/someone" };

// Roots that record what the control asked of them.
function fakeRoots() {
  const calls: unknown[] = [];
  const roots: ControlRoots = {
    uid: (root) => {
      if (root === "bad") throw new Error("not a root session id: bad");
      return 10_000;
    },
    setup: async (root, folder, share, user) => {
      calls.push(["setup", root, folder, share, user]);
      if (root === "broken") throw new Error("the session runner exited: no namespaces");
    },
    teardown: async (root) => {
      calls.push(["teardown", root]);
    },
    perform: (root, kind, args, signal, id) => {
      calls.push(["perform", root, kind, args, id]);
      return new Promise<Outcome>((resolve) => {
        if (kind === "run") signal.addEventListener("abort", () => resolve(CANCELLED), { once: true });
        else resolve({ ok: true });
      });
    },
  };
  return { roots, calls };
}

function control() {
  const sent: FromAgent[] = [];
  const { roots, calls } = fakeRoots();
  const agent = new Control((message) => sent.push(message), roots);
  const tell = (message: unknown) => agent.receive(JSON.stringify(message));
  const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
  return { agent, sent, calls, tell, settle };
}

describe("the agent's control port", () => {
  it("says hello first, and answers a ping at any time", () => {
    const { agent, sent, tell } = control();
    agent.hello();
    tell({ type: "ping", id: 1 });
    expect(sent).toEqual([{ type: "hello", id: 0 }, { type: "pong", id: 1 }]);
  });

  it("ignores lines that are not its messages", () => {
    const { agent, sent } = control();
    for (const line of ["", "{", "null", "42", '"ping"', '{"type":"ping"}', '{"type":"ping","id":"1"}']) agent.receive(line);
    expect(sent).toEqual([]);
  });

  it("answers a root's guest uid, or why it cannot", () => {
    const { sent, tell } = control();
    tell({ type: "uid", id: 1, root: "root-1" });
    tell({ type: "uid", id: 2, root: "bad" });
    expect(sent).toEqual([
      { type: "done", id: 1, uid: 10_000 },
      { type: "failed", id: 2, message: "not a root session id: bad" },
    ]);
  });

  it("sets up a root only once the host has answered hello, with the host's user", async () => {
    const { agent, sent, calls, tell, settle } = control();
    agent.hello();
    tell({ type: "setup", id: 1, root: "root-1", folder: "/home/someone/project", share: { kind: "virtiofs", tag: "r1" } });
    // An answer to another request of the agent's is not hello's.
    tell({ type: "done", id: 4, user: USER });
    tell({ type: "setup", id: 5, root: "root-1", folder: "/home/someone/project", share: { kind: "virtiofs", tag: "r1" } });
    tell({ type: "done", id: 0, user: USER });
    tell({ type: "setup", id: 2, root: "root-1", folder: "/home/someone/project", share: { kind: "virtiofs", tag: "r1" } });
    tell({ type: "setup", id: 3, root: "broken", folder: "/home/someone/other", share: { kind: "virtiofs", tag: "r2" } });
    await settle();
    expect(sent.slice(1)).toEqual([
      { type: "failed", id: 1, message: NO_HELLO },
      { type: "failed", id: 5, message: NO_HELLO },
      { type: "done", id: 2 },
      { type: "failed", id: 3, message: "the session runner exited: no namespaces" },
    ]);
    expect(calls).toEqual([
      ["setup", "root-1", "/home/someone/project", { kind: "virtiofs", tag: "r1" }, USER],
      ["setup", "broken", "/home/someone/other", { kind: "virtiofs", tag: "r2" }, USER],
    ]);
  });

  it("answers an operation with its outcome, and cancels one the host cancels", async () => {
    const { sent, calls, tell, settle } = control();
    tell({ type: "op", id: 1, root: "root-1", kind: "which", args: { name: "pandoc" } });
    tell({ type: "op", id: 2, root: "root-1", kind: "run", args: { command: "sleep 30" } });
    await settle();
    expect(sent).toEqual([{ type: "result", id: 1, outcome: { ok: true } }]);
    tell({ type: "cancel", id: 2 });
    tell({ type: "cancel", id: 3 });
    await settle();
    expect(sent).toEqual([{ type: "result", id: 1, outcome: { ok: true } }, { type: "result", id: 2, outcome: CANCELLED }]);
    expect(calls).toEqual([
      ["perform", "root-1", "which", { name: "pandoc" }, "op-1"],
      ["perform", "root-1", "run", { command: "sleep 30" }, "op-2"],
    ]);
  });

  it("answers a request whose fields are not what its type names, and one of a type it does not know", async () => {
    const { agent, sent, calls, tell, settle } = control();
    tell({ type: "uid", id: 1 });
    tell({ type: "setup", id: 2, root: "root-1", folder: 7, share: { kind: "virtiofs", tag: "r1" } });
    tell({ type: "op", id: 3, root: "root-1", args: {} });
    tell({ type: "bogus", id: 4 });
    // A share of a kind this agent does not mount.
    tell({ type: "setup", id: 5, root: "root-1", folder: "/home/someone/project", share: { kind: "9p", tag: "r1" } });
    tell({ type: "setup", id: 6, root: "root-1", folder: "/home/someone/project", tag: "r1" });
    await settle();
    expect(sent).toEqual([
      { type: "failed", id: 1, message: "The agent cannot take this uid request" },
      { type: "failed", id: 2, message: "The agent cannot take this setup request" },
      { type: "result", id: 3, outcome: { error: { type: "value", message: "The agent cannot take this op request" } } },
      { type: "failed", id: 4, message: "The agent does not know the request bogus" },
      { type: "failed", id: 5, message: "The agent cannot take this setup request" },
      { type: "failed", id: 6, message: "The agent cannot take this setup request" },
    ]);
    expect(calls).toEqual([]);
    agent.hello();
  });

  it("takes the host's user from hello's answer only, once, and in its shape", async () => {
    const { agent, sent, calls, tell, settle } = control();
    agent.hello();
    tell({ type: "done", id: 0, user: { uid: "1000", gid: 1000, name: "someone", home: "/home/someone" } });
    tell({ type: "setup", id: 1, root: "root-1", folder: "/home/someone/project", share: { kind: "virtiofs", tag: "r1" } });
    tell({ type: "done", id: 0, user: USER });
    tell({ type: "done", id: 0, user: { ...USER, name: "other" } });
    tell({ type: "setup", id: 2, root: "root-1", folder: "/home/someone/project", share: { kind: "virtiofs", tag: "r1" } });
    await settle();
    expect(sent.slice(1)).toEqual([{ type: "failed", id: 1, message: NO_HELLO }, { type: "done", id: 2 }]);
    expect(calls).toEqual([["setup", "root-1", "/home/someone/project", { kind: "virtiofs", tag: "r1" }, USER]]);
  });

  it("answers an operation whose id is still running, and keeps the first one cancellable", async () => {
    const { sent, tell, settle } = control();
    tell({ type: "op", id: 1, root: "root-1", kind: "run", args: { command: "sleep 30" } });
    tell({ type: "op", id: 1, root: "root-1", kind: "which", args: { name: "sh" } });
    await settle();
    expect(sent).toEqual([
      { type: "result", id: 1, outcome: { error: { type: "other", message: "An operation with this id is already running" } } },
    ]);
    tell({ type: "cancel", id: 1 });
    await settle();
    expect(sent.at(-1)).toEqual({ type: "result", id: 1, outcome: CANCELLED });
  });

  it("answers an operation the roots could not, rather than end the agent", async () => {
    const sent: FromAgent[] = [];
    const roots: ControlRoots = {
      uid: () => 10_000,
      setup: async () => {},
      teardown: async () => {},
      perform: () => Promise.reject(new Error("broken")),
    };
    new Control((message) => sent.push(message), roots).receive(JSON.stringify({ type: "op", id: 1, root: "r", kind: "run", args: {} }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(sent).toEqual([{ type: "result", id: 1, outcome: { error: { type: "other", message: "Error: broken" } } }]);
  });

  it("tears a root down when the host asks, and answers once it has", async () => {
    const { sent, calls, tell, settle } = control();
    tell({ type: "teardown", id: 1, root: "root-1" });
    tell({ type: "teardown", id: 2 });
    await settle();
    expect(sent).toEqual([
      { type: "failed", id: 2, message: "The agent cannot take this teardown request" },
      { type: "done", id: 1 },
    ]);
    expect(calls).toEqual([["teardown", "root-1"]]);
  });
});
