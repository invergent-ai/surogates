import { describe, expect, it, vi } from "vitest";
import type { EditorPage } from "../src/connector.js";
import type { Format } from "../src/formats/format.js";
import { word } from "../src/formats/word.js";
import { ContentError } from "../src/markdown.js";
import { EditorError, FLUSH_WAIT_MS, Office, UNDO_WAIT_MS } from "../src/office.js";
import { PageGone, type PagePool } from "../src/pages.js";

type Reply = { sentAt: number; value: unknown };
type UndoState = { ready: boolean; locked: boolean; index: number; queued: boolean };

const ready = (index: number): UndoState => ({ ready: true, locked: false, index, queued: false });

// An office over a fake pool whose one page answers *values* to its commands in turn.
function officeAnswering(values: unknown[], options: { kind?: string; formats?: Record<string, Format>; flushed?: boolean[]; modified?: boolean; poisoned?: boolean; undoStates?: (UndoState | null)[] } = {}) {
  const command = vi.fn(async (): Promise<Reply> => ({ sentAt: 0, value: values.shift() }));
  const method = vi.fn(async (_name: string, _args: unknown[] = []): Promise<Reply> => ({ sentAt: 0, value: undefined }));
  const flushed = [...(options.flushed ?? [true])];
  const waitFlushed = vi.fn(async (_since: number, _ms: number) => flushed.shift() ?? false);
  const modified = vi.fn(async () => options.modified ?? false);
  const poisoned = vi.fn(async () => options.poisoned ?? false);
  // By default the Undo runs from the editor's history at once.
  const undoStates = [...(options.undoStates ?? [ready(1), ready(0)])];
  const undoState = vi.fn(async () => (undoStates.length > 1 ? undoStates.shift()! : undoStates[0] ?? null));
  const page = { kind: options.kind ?? "word", command, method, waitFlushed, modified, poisoned, undoState, user: { id: "a", name: "Agent" } } as unknown as EditorPage;
  const pool = { get: vi.fn(async () => page), close: vi.fn(async () => undefined) };
  const office = new Office(pool as unknown as PagePool, options.formats ?? { word });
  return { office, command, method, pool, waitFlushed, modified, undoState };
}

const ops = [{ op: "insert", target: { end: true }, content: { markdown: "Hi." } }];

describe("Office reads", () => {
  it("checks the request against the page's format", async () => {
    const { office, command } = officeAnswering([{ blocks: [] }]);
    await expect(office.read("p", { view: "everything" })).rejects.toBeInstanceOf(ContentError);
    expect(command).not.toHaveBeenCalled();
  });

  it("tries a command answered undefined again", async () => {
    const { office, command } = officeAnswering([undefined, { blocks: [] }]);
    expect(await office.read("p", { view: "outline" })).toEqual({ blocks: [] });
    expect(command).toHaveBeenCalledTimes(2);
  });

  it("unwraps an answer sent as JSON, deeper than a command may nest", async () => {
    const deep = { a: { b: { c: { d: { e: { f: { g: { h: { i: { j: { k: 1 } } } } } } } } } } };
    const { office } = officeAnswering([{ __json: JSON.stringify(deep) }]);
    expect(await office.read("p", { view: "outline" })).toEqual(deep);
  });

  it("closes a page that stopped answering", async () => {
    const { office, pool } = officeAnswering([{ __timeout: true }]);
    await expect(office.read("p", { view: "outline" })).rejects.toBeInstanceOf(PageGone);
    expect(pool.close).toHaveBeenCalledWith("p");
  });

  it("throws the editor's error, and when it answers nothing", async () => {
    await expect(officeAnswering([{ __error: "x" }]).office.read("p", { view: "search", query: "q" })).rejects.toEqual(new EditorError("x"));
    await expect(officeAnswering([null]).office.read("p", { view: "block", target: "1" })).rejects.toBeInstanceOf(EditorError);
  });

  it("refuses a document type it has no format for", async () => {
    const { office } = officeAnswering([], { kind: "diagram" });
    await expect(office.read("p", { view: "outline" })).rejects.toEqual(new EditorError("runtime-office does not edit diagram documents"));
  });
});

describe("Office edits in a group", () => {
  it("checks the ops before the editor sees them", async () => {
    const { office, method } = officeAnswering([]);
    await expect(office.edit("p", [{ op: "paint" }])).rejects.toBeInstanceOf(ContentError);
    expect(method).not.toHaveBeenCalled();
  });

  it("commits the group of an applied edit, and waits for its changes to be sent", async () => {
    const { office, method, waitFlushed } = officeAnswering([{ results: [{ op: 0 }] }]);
    expect(await office.edit("p", ops)).toEqual({ applied: true, results: [{ op: 0 }], flushed: true });
    expect(method.mock.calls).toEqual([["StartAction", ["GroupActions"]], ["EndAction", ["GroupActions", ""]]]);
    expect(waitFlushed).toHaveBeenCalledTimes(1);
  });

  it("cancels the group of a refused edit", async () => {
    const refused = { op: 0, reason: "gone", block: null };
    const { office, method } = officeAnswering([{ refused }]);
    expect(await office.edit("p", ops)).toEqual({ applied: false, refused });
    expect(method.mock.calls).toEqual([["StartAction", ["GroupActions"]], ["EndAction", ["GroupActions", "", "cancel"]]]);
  });

  it("cancels the group of an edit that failed", async () => {
    const { office, method } = officeAnswering([{ __error: "x" }]);
    await expect(office.edit("p", ops)).rejects.toEqual(new EditorError("x"));
    expect(method.mock.calls.at(-1)).toEqual(["EndAction", ["GroupActions", "", "cancel"]]);
  });

  it("closes a page a failed edit left poisoned, after cancelling its group", async () => {
    const { office, method, pool } = officeAnswering([{ __error: "TypeError: Cannot read properties of null" }], { poisoned: true });
    await expect(office.edit("p", ops)).rejects.toBeInstanceOf(EditorError);
    expect(method.mock.calls.at(-1)).toEqual(["EndAction", ["GroupActions", "", "cancel"]]);
    expect(pool.close).toHaveBeenCalledWith("p");
  });

  it("closes a page that stopped answering an edit, without ending its group", async () => {
    const { office, method, pool } = officeAnswering([{ __timeout: true }]);
    const error = await office.edit("p", ops).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(EditorError);
    expect(error).not.toBeInstanceOf(PageGone);
    expect(pool.close).toHaveBeenCalledWith("p");
    expect(method.mock.calls).toEqual([["StartAction", ["GroupActions"]]]);
  });

  it("closes a page that stopped answering as the edit was committed, and says to read again", async () => {
    const { office, method, pool } = officeAnswering([{ results: [{ op: 0 }] }]);
    method.mockResolvedValueOnce({ sentAt: 0, value: undefined }).mockResolvedValueOnce({ sentAt: 0, value: { __timeout: true } });
    const error = await office.edit("p", ops).catch((e: unknown) => e);
    expect(error).toEqual(new EditorError("the editor stopped answering as the edit was committed; read the document again before editing"));
    expect(pool.close).toHaveBeenCalledWith("p");
  });

  it("keeps the refusal of an edit whose cancel went unanswered", async () => {
    const refused = { op: 0, reason: "gone", block: null };
    const { office, method, pool } = officeAnswering([{ refused }]);
    method.mockResolvedValueOnce({ sentAt: 0, value: undefined }).mockResolvedValueOnce({ sentAt: 0, value: { __timeout: true } });
    expect(await office.edit("p", ops)).toEqual({ applied: false, refused });
    expect(pool.close).not.toHaveBeenCalled();
  });

  it("closes a page that stopped answering before the edit ran, so a fresh page may run it", async () => {
    const { office, command, method, pool } = officeAnswering([]);
    method.mockResolvedValueOnce({ sentAt: 0, value: { __timeout: true } });
    await expect(office.edit("p", ops)).rejects.toBeInstanceOf(PageGone);
    expect(command).not.toHaveBeenCalled();
    expect(pool.close).toHaveBeenCalledWith("p");
  });
});

describe("the flush rule", () => {
  it("reads the editor's flag only when no state change came, and trusts it when nothing is pending", async () => {
    const { office, waitFlushed, modified } = officeAnswering([{ results: [] }], { flushed: [false], modified: false });
    expect((await office.edit("p", ops)).flushed).toBe(true);
    expect(waitFlushed).toHaveBeenCalledTimes(1);
    expect(modified).toHaveBeenCalledTimes(1);
  });

  it("waits the full time when the flag says changes are pending", async () => {
    const { office, waitFlushed } = officeAnswering([{ results: [] }], { flushed: [false, true], modified: true });
    expect((await office.edit("p", ops)).flushed).toBe(true);
    expect(waitFlushed.mock.calls[1]![1]).toBe(FLUSH_WAIT_MS);
  });

  it("says unflushed when nothing came in time", async () => {
    const { office } = officeAnswering([{ results: [] }], { flushed: [false, false], modified: true });
    expect((await office.edit("p", ops)).flushed).toBe(false);
  });

  it("flushes at once when the frame says nothing is pending", async () => {
    const { office, waitFlushed } = officeAnswering([], { modified: false });
    expect(await office.flush("p")).toBe(true);
    expect(waitFlushed).not.toHaveBeenCalled();
  });
});

describe("Office edits without a group", () => {
  // A format like PDF's, whose editor cannot start a group.
  const loose: Format = {
    script: "function () {}", grouped: false, readScope: () => ({}),
    compile: async (raw) => raw as unknown[],
    edit: async (context, list) => (await context.run({ action: "edit", ops: list })) as { results: unknown[] },
  };

  it("sends no group, and undoes a command that failed straight after it", async () => {
    const { office, method } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose } });
    await expect(office.edit("p", ops)).rejects.toEqual(new EditorError("the stamp failed; what it changed was undone"));
    expect(method.mock.calls).toEqual([["Undo", []]]);
  });

  it("sends no Undo for a refusal, decided before anything changed", async () => {
    const refused = { op: 0, reason: "no such field" };
    const { office, method } = officeAnswering([{ refused }], { kind: "loose", formats: { loose } });
    expect(await office.edit("p", ops)).toEqual({ applied: false, refused });
    expect(method).not.toHaveBeenCalled();
  });

  it("sends no Undo for a command the editor did not run", async () => {
    const { office, method } = officeAnswering([null], { kind: "loose", formats: { loose } });
    await expect(office.edit("p", ops)).rejects.toThrow(/^the editor could not run the edit; nothing of it applied$/);
    expect(method).not.toHaveBeenCalled();
  });

  it("closes a page whose Undo went unanswered, and says the edit may have applied", async () => {
    const { office, method, pool } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose } });
    method.mockResolvedValueOnce({ sentAt: 0, value: { __timeout: true } });
    await expect(office.edit("p", ops)).rejects.toThrow(/^the editor stopped answering; the edit may have applied: read the document again before editing$/);
    expect(pool.close).toHaveBeenCalledWith("p");
  });

  it("waits for the editor's lock on the failed command to clear before the Undo, and sees the Undo run", async () => {
    const locked = { ready: false, locked: true, index: 1, queued: false };
    const { office, method, pool, undoState } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose }, undoStates: [locked, locked, ready(1), ready(0)] });
    await expect(office.edit("p", ops)).rejects.toEqual(new EditorError("the stamp failed; what it changed was undone"));
    expect(method.mock.calls).toEqual([["Undo", []]]);
    expect(undoState).toHaveBeenCalledTimes(4);
    expect(pool.close).not.toHaveBeenCalled();
  });

  it("waits for an Undo the editor queued behind its save to run", async () => {
    const queued = { ready: false, locked: true, index: -1, queued: true };
    const { office, pool, undoState } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose }, undoStates: [ready(-1), queued, queued, ready(-1)] });
    await expect(office.edit("p", ops)).rejects.toEqual(new EditorError("the stamp failed; what it changed was undone"));
    expect(undoState).toHaveBeenCalledTimes(4);
    expect(pool.close).not.toHaveBeenCalled();
  });

  it("closes a page whose Undo did nothing, and says the edit may have applied", async () => {
    const { office, method, pool } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose }, undoStates: [ready(-1), ready(-1)] });
    await expect(office.edit("p", ops)).rejects.toThrow(/^the editor stopped answering; the edit may have applied: read the document again before editing$/);
    expect(method.mock.calls).toEqual([["Undo", []]]);
    expect(pool.close).toHaveBeenCalledWith("p");
  });

  it("closes a page whose lock never cleared, sending no Undo", async () => {
    vi.useFakeTimers();
    try {
      const { office, method, pool } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose }, undoStates: [{ ready: false, locked: true, index: 1, queued: false }] });
      const edit = office.edit("p", ops).catch((e: unknown) => e);
      await vi.advanceTimersByTimeAsync(UNDO_WAIT_MS + 1_000);
      expect(await edit).toEqual(new EditorError("the editor stopped answering; the edit may have applied: read the document again before editing"));
      expect(method).not.toHaveBeenCalled();
      expect(pool.close).toHaveBeenCalledWith("p");
    } finally {
      vi.useRealTimers();
    }
  });

  it("closes a page that stopped answering, sending nothing more", async () => {
    const { office, method, pool } = officeAnswering([{ __timeout: true }], { kind: "loose", formats: { loose } });
    await expect(office.edit("p", ops)).rejects.toBeInstanceOf(EditorError);
    expect(method).not.toHaveBeenCalled();
    expect(pool.close).toHaveBeenCalledWith("p");
  });
});
