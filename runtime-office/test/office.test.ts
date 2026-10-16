import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { EditorError, type Format } from "../src/formats/format.js";
import { word } from "../src/formats/word.js";
import { ContentError } from "../src/markdown.js";
import { FLUSH_WAIT_MS, Office, UNDO_WAIT_MS } from "../src/office.js";
import { PageGone, type PagePool } from "../src/pages.js";

type Reply = { sentAt: number; value: unknown };
type UndoState = { ready: boolean; locked: boolean; index: number; points: number; queued: boolean };
type UndoAttempt = { before: UndoState; after?: UndoState };

// An Undo moves the history's index and keeps its points.
const ready = (index: number, points = 2): UndoState => ({ ready: true, locked: false, index, points, queued: false });
const locked: UndoState = { ready: false, locked: true, index: 1, points: 2, queued: false };
const queued: UndoState = { ready: false, locked: true, index: -1, points: 0, queued: true };
const ran = (before: UndoState, after: UndoState): UndoAttempt => ({ before, after });

// An office over a fake pool whose one page answers *values* to its commands in turn, and *undoAttempts* (then
// *undoStates*) to its tries at an Undo, the last answer repeating.
function officeAnswering(values: unknown[], options: { kind?: string; formats?: Record<string, Format>; flushed?: boolean[]; modified?: boolean; poisoned?: boolean; undoAttempts?: UndoAttempt[]; undoStates?: UndoState[] } = {}) {
  const command = vi.fn(async (): Promise<Reply> => ({ sentAt: 0, value: values.shift() }));
  const method = vi.fn(async (_name: string, _args: unknown[] = []): Promise<Reply> => ({ sentAt: 0, value: undefined }));
  const flushed = [...(options.flushed ?? [true])];
  const waitFlushed = vi.fn(async (_since: number, _ms: number) => flushed.shift() ?? false);
  const modified = vi.fn(async () => options.modified ?? false);
  const poisoned = vi.fn(async () => options.poisoned ?? false);
  const next = <T>(list: T[]) => (list.length > 1 ? list.shift()! : list[0] ?? null);
  // By default the Undo runs from the editor's history at once.
  const attempts = [...(options.undoAttempts ?? [ran(ready(1), ready(0))])];
  const undoNow = vi.fn(async () => next(attempts));
  const states = [...(options.undoStates ?? [ready(0)])];
  const undoState = vi.fn(async () => next(states));
  const page = { kind: options.kind ?? "word", command, method, waitFlushed, modified, poisoned, undoNow, undoState, user: { id: "a", name: "Agent" } } as unknown as EditorPage;
  const pool = { get: vi.fn(async () => page), close: vi.fn(async () => undefined) };
  const office = new Office(pool as unknown as PagePool, options.formats ?? { word });
  return { office, command, method, pool, waitFlushed, modified, undoNow, undoState };
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

  const STALLED = "the editor stopped answering; the edit may have applied: read the document again before editing";

  it("sends no group, and undoes a command that failed straight after it", async () => {
    const { office, method, undoNow } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose } });
    await expect(office.edit("p", ops)).rejects.toEqual(new EditorError("the stamp failed; what it changed was undone"));
    expect(undoNow).toHaveBeenCalledTimes(1);
    expect(method).not.toHaveBeenCalled();
  });

  it("sends no Undo for a refusal, decided before anything changed", async () => {
    const refused = { op: 0, reason: "no such field" };
    const { office, undoNow } = officeAnswering([{ refused }], { kind: "loose", formats: { loose } });
    expect(await office.edit("p", ops)).toEqual({ applied: false, refused });
    expect(undoNow).not.toHaveBeenCalled();
  });

  it("sends no Undo for a command the editor did not run", async () => {
    const { office, undoNow } = officeAnswering([null], { kind: "loose", formats: { loose } });
    await expect(office.edit("p", ops)).rejects.toThrow(/^the editor could not run the edit; nothing of it applied$/);
    expect(undoNow).not.toHaveBeenCalled();
  });

  it("waits until the editor would run the Undo, and sees it run", async () => {
    const { office, pool, undoNow } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose }, undoAttempts: [{ before: locked }, { before: locked }, ran(ready(1), ready(0))] });
    await expect(office.edit("p", ops)).rejects.toEqual(new EditorError("the stamp failed; what it changed was undone"));
    expect(undoNow).toHaveBeenCalledTimes(3);
    expect(pool.close).not.toHaveBeenCalled();
  });

  it("waits for an Undo the editor queued behind its save to run", async () => {
    const { office, pool, undoState } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose }, undoAttempts: [ran(ready(-1, 0), queued)], undoStates: [queued, ready(-1, 0)] });
    await expect(office.edit("p", ops)).rejects.toEqual(new EditorError("the stamp failed; what it changed was undone"));
    expect(undoState).toHaveBeenCalledTimes(2);
    expect(pool.close).not.toHaveBeenCalled();
  });

  it("closes a page whose Undo did nothing, and says the edit may have applied", async () => {
    const { office, pool } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose }, undoAttempts: [ran(ready(-1, 0), ready(-1, 0))] });
    await expect(office.edit("p", ops)).rejects.toEqual(new EditorError(STALLED));
    expect(pool.close).toHaveBeenCalledWith("p");
  });

  it("closes a page whose history was cleared instead of undone", async () => {
    const { office, pool } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose }, undoAttempts: [ran(ready(0, 1), ready(-1, 0))] });
    await expect(office.edit("p", ops)).rejects.toEqual(new EditorError(STALLED));
    expect(pool.close).toHaveBeenCalledWith("p");
  });

  describe("with time running out", () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    async function settle(edit: Promise<unknown>): Promise<unknown> {
      const settled = edit.catch((e: unknown) => e);
      await vi.advanceTimersByTimeAsync(UNDO_WAIT_MS + 1_000);
      return settled;
    }

    it("closes a page the editor never got ready to undo in", async () => {
      const { office, pool, undoNow } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose }, undoAttempts: [{ before: locked }] });
      expect(await settle(office.edit("p", ops))).toEqual(new EditorError(STALLED));
      expect(undoNow.mock.calls.length).toBeGreaterThan(1);
      expect(pool.close).toHaveBeenCalledWith("p");
    });

    it("closes a page whose Undo went unanswered, trying no second one", async () => {
      const { office, pool, undoNow } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose } });
      undoNow.mockImplementation(() => new Promise(() => undefined));
      expect(await settle(office.edit("p", ops))).toEqual(new EditorError(STALLED));
      expect(undoNow).toHaveBeenCalledTimes(1);
      expect(pool.close).toHaveBeenCalledWith("p");
    });

    it("closes a page whose frame stops answering while a queued Undo waits", async () => {
      const { office, pool, undoState } = officeAnswering([{ __error: "the stamp failed" }], { kind: "loose", formats: { loose }, undoAttempts: [ran(ready(-1, 0), queued)] });
      undoState.mockImplementation(() => new Promise(() => undefined));
      expect(await settle(office.edit("p", ops))).toEqual(new EditorError(STALLED));
      expect(pool.close).toHaveBeenCalledWith("p");
    });
  });

  it("closes a page that stopped answering, sending nothing more", async () => {
    const { office, method, pool } = officeAnswering([{ __timeout: true }], { kind: "loose", formats: { loose } });
    await expect(office.edit("p", ops)).rejects.toBeInstanceOf(EditorError);
    expect(method).not.toHaveBeenCalled();
    expect(pool.close).toHaveBeenCalledWith("p");
  });
});

describe("one call at a time on a page", () => {
  it("runs a second call on the page only once the first has settled", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const { office, command } = officeAnswering([{ blocks: [] }]);
    command.mockImplementationOnce(async () => { await gate; return { sentAt: 0, value: { results: [{ op: 0 }] } }; });
    const edit = office.edit("p", ops);
    const read = office.read("p", { view: "outline" });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(command).toHaveBeenCalledTimes(1);
    release();
    expect(await edit).toMatchObject({ applied: true });
    expect(await read).toEqual({ blocks: [] });
    expect(command).toHaveBeenCalledTimes(2);
  });

  it("runs the next call after one that failed", async () => {
    const { office } = officeAnswering([{ __error: "x" }, { blocks: [] }]);
    const failed = office.read("p", { view: "outline" });
    const next = office.read("p", { view: "outline" });
    await expect(failed).rejects.toEqual(new EditorError("x"));
    expect(await next).toEqual({ blocks: [] });
  });
});
