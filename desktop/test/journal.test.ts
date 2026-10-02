import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { INTERRUPTED, MAX_OPEN_REPORTED, OperationJournal, RETAIN_MS } from "../src/journal/journal.js";
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

  it("never runs another request that comes under a known id", () => {
    const journal = new OperationJournal(path);
    journal.receive(operation("a"));
    journal.start("a");
    journal.finish("a", { ok: 1 });
    expect(journal.receive(operation("a", "another-digest"))).toEqual({ action: "ignore" });
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
    expect(journal.openIds()).toHaveLength(MAX_OPEN_REPORTED);
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
