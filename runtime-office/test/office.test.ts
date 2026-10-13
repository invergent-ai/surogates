import { describe, expect, it, vi } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { EditorError, Office } from "../src/office.js";
import { PageGone, type PagePool } from "../src/pages.js";

// An office over a fake pool whose one page answers *values* in turn.
function officeAnswering(...values: unknown[]) {
  const command = vi.fn(async () => ({ sentAt: 0, value: values.shift() }));
  const page = { command } as unknown as EditorPage;
  const pool = { get: vi.fn(async () => page), close: vi.fn(async () => undefined) };
  return { office: new Office(pool as unknown as PagePool), command, pool };
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
