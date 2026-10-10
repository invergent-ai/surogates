import { describe, expect, it } from "vitest";

import { CANCELLED } from "../src/guest/command.js";
import { Control, type ControlRoots, NO_HELLO } from "../src/guest/control.js";
import type { ProcessHandle } from "../src/guest/processes.js";
import type { FromAgent, HostUser } from "../src/guest/protocol.js";
import type { Outcome } from "../src/link/protocol.js";

const USER: HostUser = { uid: 1000, gid: 1000, name: "someone", home: "/home/someone" };
const HANDLE: ProcessHandle = {
  id: "proc_000000000001", command: "make", cwd: "/home/someone/project", task_id: null, started_at: 1,
  ended: { exit_code: 0, output: "done\n", note: null },
};

// Roots that record what the control asked of them.
function fakeRoots() {
  const calls: unknown[] = [];
  const roots: ControlRoots = {
    uid: (root) => {
      if (root === "bad") throw new Error("not a root session id: bad");
      return 10_000;
    },
    setup: async (root, folder, share, user, ended) => {
      calls.push(["setup", root, folder, share, user, ended]);
      if (root === "broken") throw new Error("the session runner exited: no namespaces");
    },
    teardown: async (root, share) => {
      calls.push(["teardown", root, share]);
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
    tell({ type: "setup", id: 1, root: "root-1", folder: "/home/someone/project", share: { kind: "virtiofs", tag: "r1" }, ended: [] });
    // An answer to another request of the agent's is not hello's.
    tell({ type: "done", id: 4, user: USER });
    tell({ type: "setup", id: 5, root: "root-1", folder: "/home/someone/project", share: { kind: "virtiofs", tag: "r1" }, ended: [] });
    tell({ type: "done", id: 0, user: USER });
    tell({ type: "setup", id: 2, root: "root-1", folder: "/home/someone/project", share: { kind: "virtiofs", tag: "r1" }, ended: [HANDLE] });
    tell({ type: "setup", id: 3, root: "broken", folder: "/home/someone/other", share: { kind: "virtiofs", tag: "r2" }, ended: [] });
    await settle();
    expect(sent.slice(1)).toEqual([
      { type: "failed", id: 1, message: NO_HELLO },
      { type: "failed", id: 5, message: NO_HELLO },
      { type: "done", id: 2 },
      { type: "failed", id: 3, message: "the session runner exited: no namespaces" },
    ]);
    expect(calls).toEqual([
      ["setup", "root-1", "/home/someone/project", { kind: "virtiofs", tag: "r1" }, USER, [HANDLE]],
      ["setup", "broken", "/home/someone/other", { kind: "virtiofs", tag: "r2" }, USER, []],
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
    tell({ type: "setup", id: 2, root: "root-1", folder: 7, share: { kind: "virtiofs", tag: "r1" }, ended: [] });
    tell({ type: "op", id: 3, root: "root-1", args: {} });
    tell({ type: "bogus", id: 4 });
    // A share of a kind this agent does not mount.
    tell({ type: "setup", id: 5, root: "root-1", folder: "/home/someone/project", share: { kind: "9p", tag: "r1" }, ended: [] });
    tell({ type: "setup", id: 6, root: "root-1", folder: "/home/someone/project", tag: "r1", ended: [] });
    // Handles that are not a list of them.
    tell({ type: "setup", id: 7, root: "root-1", folder: "/home/someone/project", share: { kind: "virtiofs", tag: "r1" }, ended: {} });
    await settle();
    expect(sent).toEqual([
      { type: "failed", id: 1, message: "The agent cannot take this uid request" },
      { type: "failed", id: 2, message: "The agent cannot take this setup request" },
      { type: "result", id: 3, outcome: { error: { type: "value", message: "The agent cannot take this op request" } } },
      { type: "failed", id: 4, message: "The agent does not know the request bogus" },
      { type: "failed", id: 5, message: "The agent cannot take this setup request" },
      { type: "failed", id: 6, message: "The agent cannot take this setup request" },
      { type: "failed", id: 7, message: "The agent cannot take this setup request" },
    ]);
    expect(calls).toEqual([]);
    agent.hello();
  });

  it("takes the host's user from hello's answer only, once, and in its shape", async () => {
    const { agent, sent, calls, tell, settle } = control();
    agent.hello();
    tell({ type: "done", id: 0, user: { uid: "1000", gid: 1000, name: "someone", home: "/home/someone" } });
    tell({ type: "setup", id: 1, root: "root-1", folder: "/home/someone/project", share: { kind: "virtiofs", tag: "r1" }, ended: [] });
    tell({ type: "done", id: 0, user: USER });
    tell({ type: "done", id: 0, user: { ...USER, name: "other" } });
    tell({ type: "setup", id: 2, root: "root-1", folder: "/home/someone/project", share: { kind: "virtiofs", tag: "r1" }, ended: [] });
    await settle();
    expect(sent.slice(1)).toEqual([{ type: "failed", id: 1, message: NO_HELLO }, { type: "done", id: 2 }]);
    expect(calls).toEqual([["setup", "root-1", "/home/someone/project", { kind: "virtiofs", tag: "r1" }, USER, []]]);
  });

  it("puts the company's CA of hello's answer in the guest's trust store before it sets up any root, and takes a CA not in its shape as none", async () => {
    const PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----";
    const machine = (told: unknown[], done: Promise<void>) => ({
      setClock: async () => {}, woke: () => {}, heard: () => {}, powerOff: async () => {},
      trust: (certificates: string[]) => {
        told.push(certificates);
        return done;
      },
    });
    const setup = { type: "setup", id: 1, root: "root-1", folder: "/home/someone/project", share: { kind: "virtiofs", tag: "r1" }, ended: [] };
    const told: unknown[] = [];
    let trusted = () => {};
    const sent: FromAgent[] = [];
    const { roots, calls } = fakeRoots();
    const agent = new Control((message) => sent.push(message), roots, machine(told, new Promise((resolve) => {
      trusted = resolve;
    })));
    agent.receive(JSON.stringify({ type: "done", id: 0, user: USER, ca: [PEM] }));
    agent.receive(JSON.stringify(setup));
    // Told once: a second answer to hello is not taken.
    agent.receive(JSON.stringify({ type: "done", id: 0, user: USER, ca: [] }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect([told, calls, sent]).toEqual([[[PEM]], [], []]);
    trusted();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls).toEqual([["setup", "root-1", "/home/someone/project", { kind: "virtiofs", tag: "r1" }, USER, []]]);
    expect(sent).toEqual([{ type: "done", id: 1 }]);
    for (const ca of [undefined, PEM, [PEM, 7], { 0: PEM }]) {
      const each: unknown[] = [];
      new Control(() => {}, roots, machine(each, Promise.resolve())).receive(JSON.stringify({ type: "done", id: 0, user: USER, ca }));
      expect(each).toEqual([[]]);
    }
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

  it("tears a root down and lets its share's mount go when the host asks, and answers once it has", async () => {
    const { sent, calls, tell, settle } = control();
    const share = { kind: "virtiofs", tag: "r12" };
    tell({ type: "teardown", id: 1, root: "root-1", share });
    tell({ type: "teardown", id: 2, root: "root-1" });
    tell({ type: "teardown", id: 3, root: "root-1", share: { kind: "plan9", tag: "r12" } });
    await settle();
    expect(sent).toEqual([
      { type: "failed", id: 2, message: "The agent cannot take this teardown request" },
      { type: "failed", id: 3, message: "The agent cannot take this teardown request" },
      { type: "done", id: 1 },
    ]);
    expect(calls).toEqual([["teardown", "root-1", share]]);
  });

  it("powers the guest off at the host's shutdown, and answers nothing: the VM's exit is the answer", async () => {
    const sent: FromAgent[] = [];
    let powered = 0;
    const agent = new Control((message) => sent.push(message), fakeRoots().roots, { setClock: async () => {}, woke: () => {}, heard: () => {}, powerOff: async () => void (powered += 1), trust: async () => {} });
    agent.receive(JSON.stringify({ type: "shutdown", id: 1 }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect([powered, sent]).toEqual([1, []]);
  });

  it("sets the guest's clock to the host's time, moves its runs' backstops on by the time it slept, and refuses a time that is not one", async () => {
    const sent: FromAgent[] = [];
    const set: number[] = [];
    const slept: number[] = [];
    let heard = 0;
    const machine = { setClock: async (now: number) => void set.push(now), woke: (ms: number) => void slept.push(ms), heard: () => void (heard += 1), powerOff: async () => {}, trust: async () => {} };
    const agent = new Control((message) => sent.push(message), fakeRoots().roots, machine);
    const times = [[1, 1_791_000_000_000, 90_000], [2, "soon", 0], [3, -1, 0], [4, 1_791_000_000_000, -5], [5, 1_791_000_000_000, "long"]] as const;
    for (const [id, now, asleep] of times) agent.receive(JSON.stringify({ type: "time", id, now, slept: asleep }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(sent).toEqual([
      ...[2, 3, 4, 5].map((id) => ({ type: "failed", id, message: "The agent cannot take this time request" })),
      { type: "done", id: 1 },
    ]);
    // Each run's backstop is told how long the computer slept; every line it hears is the host's word.
    expect([set, slept, heard]).toEqual([[1_791_000_000_000], [90_000], 5]);
  });
});
