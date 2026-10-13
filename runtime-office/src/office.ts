import { readFileSync } from "node:fs";
import type { EditorPage } from "./connector.js";
import type { EditorOp } from "./ops.js";
import { PageGone, PagePool, type OpenRequest } from "./pages.js";

// Read once: the same script for every request, with the request as its scope.
const WORD = readFileSync(new URL("../editor/word.js", import.meta.url), "utf8");
const MAX_OUTLINE = 200;
// Right after a co-editor's change arrives, the editor skips commands for a moment and answers undefined, the
// document unchanged (common/plugins.js): a command is tried again before it counts as failed.
const RETRIES = 8;
const RETRY_MS = 300;

export const FLUSH_WAIT_MS = 10_000;

/** The editor could not do what was asked (not the agent's input, not a dead page). */
export class EditorError extends Error {}

export type ReadRequest =
  | { view: "outline"; start?: number; limit?: number }
  | { view: "block"; target: string }
  | { view: "search"; query: string };

export class Office {
  constructor(private readonly pool: PagePool) {}

  open(request: OpenRequest): Promise<string> {
    return this.pool.open(request);
  }

  async read(id: string, request: ReadRequest): Promise<Record<string, unknown>> {
    const page = await this.pool.get(id);
    const scope =
      request.view === "outline" ? { action: "outline", start: request.start ?? 0, limit: Math.min(request.limit ?? MAX_OUTLINE, MAX_OUTLINE) }
      : request.view === "block" ? { action: "view", target: request.target }
      : { action: "search", query: request.query };
    return this.answer(id, (await this.run(page, scope)).value);
  }

  /** Applies the batch, all of it or none; then waits until the editor has sent it to the document server. */
  async edit(id: string, ops: EditorOp[]): Promise<Record<string, unknown>> {
    const page = await this.pool.get(id);
    const { sentAt, value } = await this.inGroup(id, page, { action: "edit", ops, agent: page.user });
    if (value === undefined || value === null) throw new EditorError("the editor could not run the edit; nothing of it applied");
    const result = value as Record<string, unknown>;
    if (result.__timeout) {
      // Not PageGone: the caller would open a fresh page and apply the edit a second time.
      await this.pool.close(id);
      throw new EditorError("the editor stopped answering; the edit may have applied: read the document again before editing");
    }
    if (typeof result.__error === "string") throw new EditorError(result.__error);
    if (result.refused) return { applied: false, refused: result.refused };
    return { applied: true, results: result.results, flushed: await this.flushed(page, sentAt) };
  }

  /** Whether everything the agent's editor changed has reached the document server. */
  async flush(id: string): Promise<boolean> {
    return this.flushed(await this.pool.get(id));
  }

  close(id: string): Promise<void> {
    return this.pool.close(id);
  }

  health(): { pages: number; cap: number } {
    return { pages: this.pool.size(), cap: this.pool.cap() };
  }

  protected async run(page: EditorPage, scope: unknown): Promise<{ sentAt: number; value: unknown }> {
    let reply = await page.command(WORD, scope);
    for (let attempt = 0; attempt < RETRIES && reply.value === undefined; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
      reply = await page.command(WORD, scope);
    }
    return reply;
  }

  protected async answer(id: string, value: unknown): Promise<Record<string, unknown>> {
    if (value === undefined || value === null) throw new EditorError("the editor could not run the request");
    const result = value as Record<string, unknown>;
    if (result.__timeout) {
      await this.pool.close(id);
      throw new PageGone("the editor stopped answering");
    }
    if (typeof result.__error === "string") throw new EditorError(result.__error);
    return result;
  }

  // Runs the command in a group of actions: the editor holds its changes, sending none, until the group ends, and a
  // cancel takes them all back before any co-editor saw them. (An Undo after the command cannot: the change is sent
  // within 40 ms, and an Undo while the server has not acknowledged it does nothing.) A page that stopped answering is
  // closed by the caller, its group never ended.
  private async inGroup(id: string, page: EditorPage, scope: unknown): Promise<{ sentAt: number; value: unknown }> {
    const started = await page.method("StartAction", ["GroupActions"]);
    if ((started.value as { __timeout?: true } | undefined)?.__timeout) {
      // Nothing ran: a fresh page may run the edit.
      await this.pool.close(id);
      throw new PageGone("the editor stopped answering");
    }
    let end: string[] | null = ["GroupActions", "", "cancel"];
    try {
      const reply = await this.run(page, scope);
      const result = reply.value as Record<string, unknown> | null | undefined;
      if (result?.__timeout) end = null;
      else if (result && !result.refused && result.__error === undefined) end = ["GroupActions", ""];
      return reply;
    } finally {
      if (end) await page.method("EndAction", end);
    }
  }

  // An edit that changed nothing fires no state change: the flag is read first.
  private async flushed(page: EditorPage, since?: number): Promise<boolean> {
    const { sentAt, value } = await this.run(page, { action: "modified" });
    return value === false ? true : page.waitFlushed(since ?? sentAt, FLUSH_WAIT_MS);
  }
}
