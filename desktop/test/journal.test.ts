import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { INTERRUPTED, MAX_OPEN_REPORTED, OperationJournal, RETAIN_MS } from "../src/journal/journal.js";
import type { Binding } from "../src/journal/bindings.js";
import type { Operation } from "../src/link/protocol.js";

function operation(id: string, digest = `digest-${id}`): Operation {
  return {
    id, sessionId: "root", callingSessionId: "root", invocationId: "12:call_1",
    ordinal: 1, kind: "run", args: { command: "echo hi" }, digest,
  };
}

let dir: string;
let path: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "journal-"));
  path = join(dir, "journal.sqlite");
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("an operation's life", () => {
  it("runs once; a repeat while it runs is ignored; a repeat after it finished gets its outcome", () => {
    const journal = new OperationJournal(path);
    expect(journal.receive(operation("a"))).toEqual({ action: "run" });
    journal.start("a");
    expect(journal.receive(operation("a"))).toEqual({ action: "ignore" });
    expect(journal.finish("a", { ok: "done" })).toBe(true);
    expect(journal.receive(operation("a"))).toEqual({ action: "reply", outcome: { ok: "done" } });
    journal.close();
  });

  it("keeps a result of undefined as null, so a replay or a resend is a frame the server accepts", () => {
    const journal = new OperationJournal(path);
    journal.receive(operation("a"));
    journal.start("a");
    expect(journal.finish("a", { ok: undefined })).toBe(true);
    expect(journal.unsent()).toEqual([{ id: "a", digest: "digest-a", outcome: { ok: null } }]);
    expect(journal.receive(operation("a"))).toEqual({ action: "reply", outcome: { ok: null } });
    journal.close();
  });

  it("never runs another request that comes under a known id", () => {
    const journal = new OperationJournal(path);
    journal.receive(operation("a"));
    journal.start("a");
    journal.finish("a", { ok: 1 });
    expect(journal.receive(operation("a", "another-digest"))).toEqual({ action: "ignore" });
    journal.close();
  });
});

describe("starting an operation", () => {
  it("is true only for the call that moved it from received to started", () => {
    const journal = new OperationJournal(path);
    journal.receive(operation("a"));
    expect(journal.start("a")).toBe(true);
    expect(journal.start("a")).toBe(false);
    journal.close();
  });

  it("is false for a cancelled or an unknown id", () => {
    const journal = new OperationJournal(path);
    journal.receive(operation("cancelled"));
    journal.cancel("cancelled");
    expect(journal.start("cancelled")).toBe(false);
    expect(journal.start("unknown")).toBe(false);
    journal.close();
  });
});

describe("a crash", () => {
  it("leaves an operation that started answered as interrupted, never run again", () => {
    const before = new OperationJournal(path);
    before.receive(operation("a"));
    before.start("a");
    before.close();

    const after = new OperationJournal(path);
    expect(after.recovered).toBe(1);
    expect(after.receive(operation("a"))).toEqual({ action: "reply", outcome: INTERRUPTED });
    expect(after.unsent()).toEqual([{ id: "a", digest: "digest-a", outcome: INTERRUPTED }]);
    after.close();
  });

  it("runs an operation that was received but never started", () => {
    const before = new OperationJournal(path);
    before.receive(operation("a"));
    before.close();

    const after = new OperationJournal(path);
    expect(after.recovered).toBe(0);
    expect(after.receive(operation("a"))).toEqual({ action: "run" });
    after.close();
  });
});

describe("a cancel", () => {
  it("that arrives first means the operation is never run", () => {
    const journal = new OperationJournal(path);
    journal.cancel("a");
    expect(journal.receive(operation("a"))).toEqual({ action: "ignore" });
    journal.close();
  });

  it("during the run drops the result", () => {
    const journal = new OperationJournal(path);
    journal.receive(operation("a"));
    journal.start("a");
    journal.cancel("a");
    expect(journal.finish("a", { ok: "late" })).toBe(false);
    expect(journal.unsent()).toEqual([]);
    expect(journal.receive(operation("a"))).toEqual({ action: "ignore" });
    journal.close();
  });

  it("after the result keeps it", () => {
    const journal = new OperationJournal(path);
    journal.receive(operation("a"));
    journal.start("a");
    journal.finish("a", { ok: "kept" });
    journal.cancel("a");
    expect(journal.receive(operation("a"))).toEqual({ action: "reply", outcome: { ok: "kept" } });
    journal.close();
  });

  it("twice is the same as once", () => {
    const journal = new OperationJournal(path);
    journal.cancel("a");
    journal.cancel("a");
    expect(journal.receive(operation("a"))).toEqual({ action: "ignore" });
    journal.close();
  });
});

describe("a second journal on the same file", () => {
  it("fails to open while the first is open, and leaves its running operations alone", () => {
    const first = new OperationJournal(path);
    first.receive(operation("a"));
    first.start("a");
    expect(() => new OperationJournal(path)).toThrow();
    expect(first.finish("a", { ok: 1 })).toBe(true);
    first.close();
    const after = new OperationJournal(path);
    expect(after.recovered).toBe(0);
    after.close();
  });
});

describe("the device it belongs to", () => {
  it("is the first one that claims it", () => {
    const journal = new OperationJournal(path);
    expect(journal.claim("device-1")).toBe(true);
    expect(journal.claim("device-1")).toBe(true);
    expect(journal.claim("device-2")).toBe(false);
    journal.close();
  });
});

describe("what the journal reports", () => {
  it("holds open what was received or started, newest first, at most the cap", () => {
    let clock = 0;
    const journal = new OperationJournal(path, () => ++clock);
    journal.receive(operation("received"));
    journal.receive(operation("started"));
    journal.start("started");
    journal.receive(operation("finished"));
    journal.start("finished");
    journal.finish("finished", { ok: 1 });
    journal.cancel("cancelled");
    expect(journal.openIds()).toEqual(["started", "received"]);
    for (let i = 0; i < MAX_OPEN_REPORTED + 5; i++) journal.receive(operation(`many-${i}`));
    const open = journal.openIds();
    expect(open).toHaveLength(MAX_OPEN_REPORTED);
    expect(open[0]).toBe(`many-${MAX_OPEN_REPORTED + 4}`);
    expect(open).not.toContain("received");
    journal.close();
  });

  it("orders operations that share a timestamp newest first", () => {
    const journal = new OperationJournal(path, () => 5);
    journal.receive(operation("a"));
    journal.receive(operation("b"));
    journal.receive(operation("c"));
    expect(journal.openIds()).toEqual(["c", "b", "a"]);
    journal.close();
  });

  it("resends a result until the server acknowledges it", () => {
    const journal = new OperationJournal(path);
    journal.receive(operation("a"));
    journal.start("a");
    journal.finish("a", { ok: 1 });
    expect(journal.unsent().map((r) => r.id)).toEqual(["a"]);
    journal.acknowledge("a");
    expect(journal.unsent()).toEqual([]);
    expect(journal.receive(operation("a"))).toEqual({ action: "reply", outcome: { ok: 1 } });
    journal.close();
  });

  it("keeps an acknowledged result's payload until the day is up", () => {
    let now = 1_000;
    const journal = new OperationJournal(path, () => now);
    journal.receive(operation("a"));
    journal.start("a");
    journal.finish("a", { ok: "big" });
    journal.acknowledge("a");
    now += RETAIN_MS;
    expect(journal.prune()).toBe(0);
    expect(journal.receive(operation("a"))).toEqual({ action: "reply", outcome: { ok: "big" } });
    journal.close();
  });

  it("drops an acknowledged result's payload after a day, and never runs it again", () => {
    let now = 1_000;
    const journal = new OperationJournal(path, () => now);
    journal.receive(operation("a"));
    journal.start("a");
    journal.finish("a", { ok: "big" });
    journal.acknowledge("a");
    now += RETAIN_MS + 1;
    expect(journal.prune()).toBe(1);
    expect(journal.receive(operation("a"))).toEqual({ action: "ignore" });
    journal.close();
  });
});

describe("an operation answered before it started", () => {
  it("is finished with its outcome, sent until acknowledged, and never runs", () => {
    const journal = new OperationJournal(path);
    journal.receive(operation("a"));
    expect(journal.answer("a", { ok: undefined })).toBe(true);
    expect(journal.unsent()).toEqual([{ id: "a", digest: "digest-a", outcome: { ok: null } }]);
    expect(journal.start("a")).toBe(false);
    expect(journal.receive(operation("a"))).toEqual({ action: "reply", outcome: { ok: null } });
    journal.close();
    const after = new OperationJournal(path);
    expect(after.recovered).toBe(0);
    expect(after.unsent()).toEqual([{ id: "a", digest: "digest-a", outcome: { ok: null } }]);
    after.acknowledge("a");
    expect(after.unsent()).toEqual([]);
    expect(after.answer("a", { ok: 2 })).toBe(false);
    expect(after.receive(operation("a"))).toEqual({ action: "reply", outcome: { ok: null } });
    after.close();
  });

  it("is not answered once it was cancelled, started or finished, nor when unknown", () => {
    const journal = new OperationJournal(path);
    journal.receive(operation("cancelled"));
    journal.cancel("cancelled");
    journal.receive(operation("started"));
    journal.start("started");
    journal.receive(operation("finished"));
    journal.answer("finished", { ok: 1 });
    for (const id of ["cancelled", "started", "finished", "unknown"]) expect(journal.answer(id, { ok: 2 })).toBe(false);
    expect(journal.receive(operation("finished"))).toEqual({ action: "reply", outcome: { ok: 1 } });
    journal.close();
  });
});

describe("the bindings", () => {
  const binding = (root: string, boundAt: number): Binding => ({
    root, nonce: `nonce-${root}`, folder: `/home/me/${root}`, dev: 2049, ino: 7_340_033, mode: "free", boundAt,
  });

  it("keep each root's folder across a restart, and give the latest", () => {
    const before = new OperationJournal(path);
    expect(before.bindings.last()).toBeUndefined();
    before.bindings.add(binding("r1", 2));
    before.bindings.add(binding("r2", 1));
    before.close();
    const after = new OperationJournal(path);
    expect(after.bindings.get("r1")).toEqual(binding("r1", 2));
    expect(after.bindings.get("r3")).toBeUndefined();
    expect(after.bindings.last()).toEqual(binding("r1", 2));
    after.close();
  });

  it("bind a root once", () => {
    const journal = new OperationJournal(path);
    journal.bindings.add(binding("r1", 1));
    expect(() => journal.bindings.add({ ...binding("r1", 2), folder: "/elsewhere" })).toThrow();
    expect(journal.bindings.get("r1")?.folder).toBe("/home/me/r1");
    journal.close();
  });

  it("read back a folder whose device and inode numbers are past 2^53", () => {
    const journal = new OperationJournal(path);
    const large = { ...binding("r1", 1), dev: 2 ** 53 + 4, ino: 2 ** 53 + 2 };
    journal.bindings.add(large);
    expect(journal.bindings.get("r1")).toEqual(large);
    expect(journal.bindings.last()).toEqual(large);
    journal.close();
  });
});
