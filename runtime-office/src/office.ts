import type { EditorPage } from "./connector.js";
import { Stalled, type EditContext, type Format, type Outcome, type Scope } from "./formats/format.js";
import { formatsFor } from "./formats/index.js";
import { PageGone, type PagePool, type OpenRequest } from "./pages.js";

// Right after a co-editor's change arrives, the editor skips commands for a moment and answers undefined, the
// document unchanged (common/plugins.js): a command is tried again before it counts as failed.
const RETRIES = 8;
const RETRY_MS = 300;
// An edit's changes reach the document server within about 0.1 s, a PDF's edited alone within about 1 s; an edit that
// changed nothing fires no state change, so the editor's flag decides after this long without one.
const QUICK_FLUSH_MS = 1_500;
export const FLUSH_WAIT_MS = 10_000;
const COMMIT = ["GroupActions", ""];
const CANCEL = ["GroupActions", "", "cancel"];
const STALLED_EDIT = "the editor stopped answering; the edit may have applied: read the document again before editing";

/** The editor could not do what was asked (not the agent's input, not a dead page). */
export class EditorError extends Error {}

type Reply = { sentAt: number; value: unknown };

const stalled = (reply: Reply): boolean => (reply.value as { __timeout?: true } | undefined)?.__timeout === true;

export class Office {
  private readonly formats: Record<string, Format>;

  constructor(private readonly pool: PagePool, formats?: Record<string, Format>) {
    this.formats = formats ?? formatsFor(pool);
  }

  open(request: OpenRequest): Promise<string> {
    return this.pool.open(request);
  }

  async read(id: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
    const page = await this.pool.get(id);
    const scope = this.formatOf(page).readScope(body);
    return (await this.readOnly(id, () => this.context(page).run(scope))) as Record<string, unknown>;
  }

  /** Applies the batch, all of it or none; then waits until the editor has sent it to the document server. */
  async edit(id: string, raw: unknown): Promise<Record<string, unknown>> {
    const page = await this.pool.get(id);
    const format = this.formatOf(page);
    const context = this.context(page);
    const ops = await this.readOnly(id, () => format.compile(raw, context));
    const work = () => format.edit(context, ops);
    const { outcome, sentAt } = format.grouped ? await this.inGroup(id, page, work) : await this.undoing(id, page, work);
    if ("refused" in outcome) return { applied: false, refused: outcome.refused };
    return { applied: true, results: outcome.results, flushed: await this.flushed(page, sentAt) };
  }

  /** Whether everything the agent's editor changed has reached the document server. */
  async flush(id: string): Promise<boolean> {
    const page = await this.pool.get(id);
    return !(await page.modified()) || page.waitFlushed(Date.now(), FLUSH_WAIT_MS);
  }

  close(id: string): Promise<void> {
    return this.pool.close(id);
  }

  health(): { pages: number; cap: number } {
    return { pages: this.pool.size(), cap: this.pool.cap() };
  }

  protected async run(page: EditorPage, scope: Scope): Promise<Reply> {
    const script = this.formatOf(page).script;
    let reply = await page.command(script, scope);
    for (let attempt = 0; attempt < RETRIES && reply.value === undefined; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
      reply = await page.command(script, scope);
    }
    return reply;
  }

  private formatOf(page: EditorPage): Format {
    const format = this.formats[page.kind];
    if (!format) throw new EditorError(`runtime-office does not edit ${page.kind} documents`);
    return format;
  }

  private context(page: EditorPage): EditContext {
    return { page, run: async (scope) => unwrap((await this.run(page, scope)).value) };
  }

  // A read changes nothing: a page that stopped answering is closed, and a fresh one may answer.
  private async readOnly<T>(id: string, work: () => Promise<T>): Promise<T> {
    try {
      return await work();
    } catch (error) {
      if (!(error instanceof Stalled)) throw error;
      await this.pool.close(id);
      throw new PageGone("the editor stopped answering");
    }
  }

  // Runs *work* in a group of actions: the editor holds its changes, sending none, until the group ends, and a cancel
  // takes them all back before any co-editor saw them. (An Undo after the command cannot: the change is sent within
  // 40 ms, and an Undo while the server has not acknowledged it does nothing.) A page that stopped answering is closed
  // with its group never ended; so is one that stopped answering the commit.
  private async inGroup(id: string, page: EditorPage, work: () => Promise<Outcome>): Promise<{ outcome: Outcome; sentAt: number }> {
    if (stalled(await page.method("StartAction", ["GroupActions"]))) {
      // Nothing ran: a fresh page may run the edit.
      await this.pool.close(id);
      throw new PageGone("the editor stopped answering");
    }
    let outcome: Outcome;
    try {
      outcome = await work();
    } catch (error) {
      if (error instanceof Stalled) {
        // Not PageGone: the caller would open a fresh page and apply the edit a second time.
        await this.pool.close(id);
        throw new EditorError(STALLED_EDIT);
      }
      await page.method("EndAction", CANCEL);
      await this.recycleIfPoisoned(id, page);
      throw error;
    }
    const commit = !("refused" in outcome);
    const sentAt = Date.now();
    const ended = await page.method("EndAction", commit ? COMMIT : CANCEL);
    if (commit && stalled(ended)) {
      await this.pool.close(id);
      throw new EditorError("the editor stopped answering as the edit was committed; read the document again before editing");
    }
    return { outcome, sentAt };
  }

  // PDF's editor cannot start a group: a batch is one command, its refusals decided before it changes anything, and
  // a command that failed partway is undone at once. In the PDF editor every command, a read too, is an undo point, so
  // the Undo must be the very next thing sent.
  private async undoing(id: string, page: EditorPage, work: () => Promise<Outcome>): Promise<{ outcome: Outcome; sentAt: number }> {
    const sentAt = Date.now();
    try {
      return { outcome: await work(), sentAt };
    } catch (error) {
      if (error instanceof Stalled) {
        await this.pool.close(id);
        throw new EditorError(STALLED_EDIT);
      }
      if (!(error instanceof EditorError)) throw error;
      await page.method("Undo", []);
      await this.recycleIfPoisoned(id, page);
      throw new EditorError(`${error.message}; what it changed was undone`);
    }
  }

  // A page whose registry a failed command left off must never send another change: it is closed, and the next call
  // opens a fresh one.
  private async recycleIfPoisoned(id: string, page: EditorPage): Promise<void> {
    if (await page.poisoned()) await this.pool.close(id);
  }

  // The editor says its changes reached the document server with a state change after the last send. Its modified
  // flag decides only when none came: an edit that changed nothing fires none. (Right after a commit the slide
  // editor's flag already reads false, and inside a command the spreadsheet editor's always reads true: it is read
  // from the editor's frame, after the wait.)
  private async flushed(page: EditorPage, since: number): Promise<boolean> {
    if (await page.waitFlushed(since, QUICK_FLUSH_MS)) return true;
    if (!(await page.modified())) return true;
    return page.waitFlushed(since, FLUSH_WAIT_MS);
  }
}

// A script's answer: an error or a stall said as one, an answer too deep for a command sent as JSON.
function unwrap(value: unknown): unknown {
  if (value === undefined || value === null) throw new EditorError("the editor could not run the request");
  const answer = value as { __timeout?: true; __error?: unknown; __json?: unknown };
  if (answer.__timeout) throw new Stalled("the editor stopped answering");
  if (typeof answer.__error === "string") throw new EditorError(answer.__error);
  if (typeof answer.__json === "string") return JSON.parse(answer.__json) as unknown;
  return value;
}
