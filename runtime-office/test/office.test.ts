import { describe, expect, it, vi } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { EditorError, Office } from "../src/office.js";
import { PageGone, type PagePool } from "../src/pages.js";

// An office over a fake pool whose one page answers *values* to its commands in turn.
function officeAnswering(...values: unknown[]) {
  const command = vi.fn(async () => ({ sentAt: 0, value: values.shift() }));
  const method = vi.fn(async (_name: string, _args: unknown[] = []): Promise<{ sentAt: number; value: unknown }> => ({ sentAt: 0, value: undefined }));
  const page = { command, method, user: { id: "a", name: "Agent" }, waitFlushed: vi.fn(async () => true) } as unknown as EditorPage;
  const pool = { get: vi.fn(async () => page), close: vi.fn(async () => undefined) };
  return { office: new Office(pool as unknown as PagePool), command, method, pool };
}

describe("Office answers", () => {
  it("tries a command answered undefined again", async () => {
    const { office, command } = officeAnswering(undefined, { blocks: [] });
    expect(await office.read("p", { view: "outline" })).toEqual({ blocks: [] });
    expect(command).toHaveBeenCalledTimes(2);
  });

  it("closes a page that stopped answering", async () => {
    const { office, pool } = officeAnswering({ __timeout: true });
    await expect(office.read("p", { view: "outline" })).rejects.toBeInstanceOf(PageGone);
    expect(pool.close).toHaveBeenCalledWith("p");
  });

  it("throws the editor's error", async () => {
    const { office } = officeAnswering({ __error: "x" });
    await expect(office.read("p", { view: "search", query: "q" })).rejects.toEqual(new EditorError("x"));
  });

  it("throws when the editor answers nothing", async () => {
    const { office } = officeAnswering(null);
    await expect(office.read("p", { view: "block", target: "1" })).rejects.toBeInstanceOf(EditorError);
  });
});

describe("Office edits", () => {
  const ops = [{ op: "delete" as const, target: { end: true as const } }];

  it("commits the group of an applied edit", async () => {
    const { office, method } = officeAnswering({ results: [{ op: 0 }] }, false);
    expect(await office.edit("p", ops)).toEqual({ applied: true, results: [{ op: 0 }], flushed: true });
    expect(method.mock.calls).toEqual([["StartAction", ["GroupActions"]], ["EndAction", ["GroupActions", ""]]]);
  });

  it("cancels the group of a refused edit", async () => {
    const refused = { op: 0, reason: "gone", block: null };
    const { office, method } = officeAnswering({ refused });
    expect(await office.edit("p", ops)).toEqual({ applied: false, refused });
    expect(method.mock.calls).toEqual([["StartAction", ["GroupActions"]], ["EndAction", ["GroupActions", "", "cancel"]]]);
  });

  it("cancels the group of an edit that failed", async () => {
    const { office, method } = officeAnswering({ __error: "x" });
    await expect(office.edit("p", ops)).rejects.toEqual(new EditorError("x"));
    expect(method.mock.calls.at(-1)).toEqual(["EndAction", ["GroupActions", "", "cancel"]]);
  });

  it("closes a page that stopped answering an edit, without ending its group", async () => {
    const { office, method, pool } = officeAnswering({ __timeout: true });
    const error = await office.edit("p", ops).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(EditorError);
    expect(error).not.toBeInstanceOf(PageGone);
    expect(pool.close).toHaveBeenCalledWith("p");
    expect(method.mock.calls).toEqual([["StartAction", ["GroupActions"]]]);
  });

  it("closes a page that stopped answering before the edit ran, so a fresh page may run it", async () => {
    const { office, command, method, pool } = officeAnswering();
    method.mockResolvedValueOnce({ sentAt: 0, value: { __timeout: true } });
    await expect(office.edit("p", ops)).rejects.toBeInstanceOf(PageGone);
    expect(command).not.toHaveBeenCalled();
    expect(pool.close).toHaveBeenCalledWith("p");
  });
});
