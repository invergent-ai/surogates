import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { DatabaseSync } from "node:sqlite";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { INTERRUPTED, MAX_OPEN_REPORTED, OperationJournal, RETAIN_MS } from "../src/journal/journal.js";
import { type Binding, Bindings } from "../src/journal/bindings.js";
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

describe("a transfer's chunks", () => {
  const chunks = [Buffer.from("first"), Buffer.from("second")];
  const outcome = { ok: { transfer: { size: 11, sha256: "f".repeat(64) } } };

  it("are kept with the outcome across a restart, and dropped once the server acknowledged it", () => {
    const before = new OperationJournal(path);
    before.receive(operation("a"));
    before.start("a");
    expect(before.finish("a", outcome, chunks)).toBe(true);
    before.close();
    const after = new OperationJournal(path);
    expect(after.unsent()).toEqual([{ id: "a", digest: "digest-a", outcome }]);
    expect([after.chunk("a", 0), after.chunk("a", 1), after.chunk("a", 2)]).toEqual([...chunks, null]);
    after.acknowledge("a");
    expect(after.chunk("a", 0)).toBeNull();
    // The record stays: a repeat is answered, never run.
    expect(after.receive(operation("a"))).toEqual({ action: "reply", outcome });
    after.close();
  });

  it("are not kept when one of them cannot be written, and the operation can still finish", () => {
    const journal = new OperationJournal(path);
    journal.receive(operation("a"));
    journal.start("a");
    expect(() => journal.finish("a", outcome, [Buffer.from("x"), undefined as unknown as Buffer])).toThrow(
      "cannot be bound",
    );
    expect(journal.unsent()).toEqual([]);
    expect(journal.chunk("a", 0)).toBeNull();
    expect(journal.finish("a", { ok: 1 })).toBe(true);
    journal.close();
  });

  it("that fill the disk fail with SQLite's own error, and the operation can still finish", () => {
    const journal = new OperationJournal(path);
    journal.receive(operation("a"));
    journal.start("a");
    // A full disk: SQLite fails the write and rolls the transaction back by itself.
    const db = (journal as unknown as { db: DatabaseSync }).db;
    const { page_count: pages } = db.prepare("PRAGMA page_count").get() as { page_count: number };
    db.exec(`PRAGMA max_page_count = ${pages + 2}`);
    expect(() => journal.finish("a", outcome, [Buffer.alloc(1024 * 1024)])).toThrow("database or disk is full");
    expect(journal.chunk("a", 0)).toBeNull();
    expect(journal.finish("a", { ok: 1 })).toBe(true);
    journal.close();
  });

  it("are not kept for an outcome the journal does not record", () => {
    const journal = new OperationJournal(path);
    journal.receive(operation("a"));
    journal.start("a");
    journal.cancel("a");
    expect(journal.finish("a", outcome, chunks)).toBe(false);
    expect(journal.chunk("a", 0)).toBeNull();
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
    root, nonce: `nonce-${root}`, folder: `/home/me/${root}`, dev: 2049, ino: 7_340_033, boot: "boot-1", mode: "free", boundAt,
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

  it("change a root's mode, and keep it across a restart", () => {
    const before = new OperationJournal(path);
    before.bindings.add(binding("r1", 1));
    before.bindings.add(binding("r2", 2));
    before.bindings.setMode("r1", "ask");
    before.bindings.setMode("r3", "ask");
    before.close();
    const after = new OperationJournal(path);
    expect(after.bindings.get("r1")).toEqual({ ...binding("r1", 1), mode: "ask" });
    expect(after.bindings.get("r2")?.mode).toBe("free");
    expect(after.bindings.get("r3")).toBeUndefined();
    after.close();
  });

  it("keep the hosts a root's user allowed for it, once each, for a bound root only", () => {
    const before = new OperationJournal(path);
    before.bindings.add(binding("r1", 1));
    before.bindings.add(binding("r2", 2));
    before.bindings.allowDomain("r1", "example.com");
    before.bindings.allowDomain("r1", "[::1]");
    before.bindings.allowDomain("r1", "example.com");
    before.bindings.allowDomain("r3", "example.com");
    before.close();
    const after = new OperationJournal(path);
    expect(after.bindings.domains("r1")).toEqual(["example.com", "[::1]"]);
    expect([after.bindings.domains("r2"), after.bindings.domains("r3")]).toEqual([[], []]);
    expect(after.bindings.get("r1")).toEqual(binding("r1", 1));
    after.close();
  });

  it("tell whoever watches of each host allowed for a bound root, once it is kept", () => {
    const journal = new OperationJournal(path);
    journal.bindings.add(binding("r1", 1));
    const told: string[] = [];
    journal.bindings.watch((root) => told.push(root));
    journal.bindings.allowDomain("r1", "example.com");
    // Allowed already, or for a root with no binding: nothing is kept, and nothing told.
    journal.bindings.allowDomain("r1", "example.com");
    journal.bindings.allowDomain("r2", "example.com");
    expect(told).toEqual(["r1"]);
    journal.close();
  });

  it("forget a deleted root's binding and its allowed hosts both or neither, and tell of it once forgotten", () => {
    const journal = new OperationJournal(path);
    journal.bindings.add(binding("r1", 1));
    journal.bindings.allowDomain("r1", "example.com");
    // The binding's DELETE fails after the hosts' ran, as on a full disk.
    const db = (journal as unknown as { db: DatabaseSync }).db;
    const failing = new Proxy(db, {
      get(target, name) {
        if (name === "prepare") {
          return (sql: string) => {
            if (sql.startsWith("DELETE FROM bindings")) return { run: () => { throw new Error("disk I/O error"); } };
            return target.prepare(sql);
          };
        }
        const value = Reflect.get(target, name) as unknown;
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const told: string[] = [];
    const unwritten = new Bindings(failing);
    unwritten.watch((root) => told.push(root));
    expect(() => unwritten.retire("r1")).toThrow("disk I/O error");
    expect([journal.bindings.get("r1"), journal.bindings.domains("r1")]).toEqual([binding("r1", 1), ["example.com"]]);
    journal.bindings.watch((root) => told.push(root));
    journal.bindings.retire("r1");
    expect([journal.bindings.get("r1"), journal.bindings.domains("r1")]).toEqual([undefined, []]);
    // A root with no binding left changes nothing, and tells nothing.
    journal.bindings.retire("r1");
    expect(told).toEqual(["r1"]);
    journal.close();
  });

  it("keep the ports of a root's own servers its user let the browser open, one root a port, and tell of each change", () => {
    const before = new OperationJournal(path);
    before.bindings.add(binding("r1", 1));
    before.bindings.add(binding("r2", 2));
    const told: string[] = [];
    before.bindings.watch((root) => told.push(root));
    before.bindings.allowPort("r1", 3000);
    before.bindings.allowPort("r1", 8000);
    // Allowed already, or for a root with no binding: nothing is kept, and nothing told.
    before.bindings.allowPort("r1", 3000);
    before.bindings.allowPort("r3", 5000);
    expect(told).toEqual(["r1", "r1"]);
    // Another chat's user allowed the same port: it is that chat's now, and both hear of it.
    before.bindings.allowPort("r2", 3000);
    expect(told).toEqual(["r1", "r1", "r1", "r2"]);
    before.close();
    const after = new OperationJournal(path);
    expect([after.bindings.ports("r1"), after.bindings.ports("r2"), after.bindings.ports("r3")]).toEqual([[8000], [3000], []]);
    expect([after.bindings.portOwner(3000), after.bindings.portOwner(8000), after.bindings.portOwner(5000)]).toEqual(["r2", "r1", undefined]);
    expect(after.bindings.forwards()).toEqual([{ port: 3000, root: "r2" }, { port: 8000, root: "r1" }]);
    after.close();
  });

  it("forget a root's ports one at a time, with its browser, and with the root", () => {
    const journal = new OperationJournal(path);
    for (const [root, at] of [["r1", 1], ["r2", 2], ["r3", 3]] as const) {
      journal.bindings.add(binding(root, at));
      journal.bindings.allowBrowser(root);
    }
    journal.bindings.allowPort("r1", 3000);
    journal.bindings.allowPort("r1", 8000);
    journal.bindings.allowPort("r2", 5173);
    journal.bindings.allowPort("r3", 9000);
    const told: string[] = [];
    journal.bindings.watch((root) => told.push(root));
    // One taken back: another chat's port, or one never allowed, changes nothing.
    journal.bindings.disallowPort("r2", 3000);
    journal.bindings.disallowPort("r1", 4000);
    journal.bindings.disallowPort("r1", 3000);
    expect([journal.bindings.ports("r1"), told]).toEqual([[8000], ["r1"]]);
    // The browser taken back: its ports go with it.
    journal.bindings.disallowBrowser("r1");
    expect([journal.bindings.ports("r1"), journal.bindings.browsing("r1"), told]).toEqual([[], false, ["r1", "r1"]]);
    // A deleted chat's go with its binding.
    journal.bindings.retire("r2");
    expect([journal.bindings.ports("r2"), journal.bindings.portOwner(5173), told]).toEqual([[], undefined, ["r1", "r1", "r2"]]);
    expect(journal.bindings.forwards()).toEqual([{ port: 9000, root: "r3" }]);
    // The sandbox's own proxies' ports are no chat's servers: never kept, whoever asks.
    for (const proxy of [3128, 1080]) expect(() => journal.bindings.allowPort("r3", proxy)).toThrow(`Port ${proxy} is the sandbox's own proxy`);
    expect([journal.bindings.forwards(), told]).toEqual([[{ port: 9000, root: "r3" }], ["r1", "r1", "r2"]]);
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

describe("a device the agent revoked", () => {
  it("keeps nothing to send or run: its chunks go, what was unfinished is cancelled, and its bindings stay", () => {
    const journal = new OperationJournal(path);
    const outcome = { ok: { transfer: { size: 11, sha256: "f".repeat(64) } } };
    for (const id of ["sent", "finished", "started", "received"]) journal.receive(operation(id));
    for (const id of ["sent", "finished", "started"]) journal.start(id);
    journal.finish("sent", { ok: "done" });
    journal.acknowledge("sent");
    journal.finish("finished", outcome, [Buffer.from("first")]);
    journal.bindings.add({ root: "r1", nonce: "n", folder: "/home/me/Report", dev: 1, ino: 2, boot: "b", mode: "free", boundAt: 1 });
    journal.retire();
    expect(journal.unsent()).toEqual([]);
    expect(journal.openIds()).toEqual([]);
    expect(journal.chunk("finished", 0)).toBeNull();
    for (const id of ["finished", "started", "received"]) expect(journal.receive(operation(id))).toEqual({ action: "ignore" });
    expect(journal.receive(operation("sent"))).toEqual({ action: "reply", outcome: { ok: "done" } });
    expect(journal.bindings.folders()).toEqual(["/home/me/Report"]);
    journal.close();
  });

  it("names each bound folder once, the first bound first", () => {
    const journal = new OperationJournal(path);
    const bind = (root: string, folder: string, boundAt: number) =>
      journal.bindings.add({ root, nonce: `n-${root}`, folder, dev: 1, ino: 2, boot: "b", mode: "free", boundAt });
    bind("r1", "/home/me/Budget", 3);
    bind("r2", "/home/me/Report", 1);
    bind("r3", "/home/me/Budget", 2);
    expect(journal.bindings.folders()).toEqual(["/home/me/Report", "/home/me/Budget"]);
    journal.close();
  });
});
