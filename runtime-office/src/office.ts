import { readFileSync } from "node:fs";
import type { EditorPage } from "./connector.js";
import { PageGone, PagePool, type OpenRequest } from "./pages.js";

// Read once: the same script for every request, with the request as its scope.
const WORD = readFileSync(new URL("../editor/word.js", import.meta.url), "utf8");
const MAX_OUTLINE = 200;
// Right after a co-editor's change arrives, the editor skips commands for a moment and answers undefined, the
// document unchanged (common/plugins.js): a command is tried again before it counts as failed.
const RETRIES = 8;
const RETRY_MS = 300;

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
}
