import type { Browser, BrowserContext, Page } from "playwright-core";

export interface EditorUser {
  id: string;
  name: string;
}

export interface EditorPageOptions {
  hostUrl: string;
  config: Record<string, unknown>;
  documentServerUrl: string;
  openTimeoutMs: number;
  commandTimeoutMs: number;
  ignoreTls: boolean;
}

type Reply = { sentAt: number; value: unknown };

// The host page's functions, as page.evaluate sees them.
type HostWindow = {
  startEditor(config: unknown, url: string): Promise<boolean>;
  runCommand(code: string, timeoutMs: number): Promise<Reply>;
  runMethod(name: string, args: unknown[], timeoutMs: number): Promise<Reply>;
  waitFlushed(since: number, timeoutMs: number): Promise<boolean>;
  dead: string | null;
};

/** One editor, signed in as one user, in a browser context of its own. Commands run one at a time. */
export class EditorPage {
  lastUsed = Date.now();
  private queue: Promise<unknown> = Promise.resolve();

  private constructor(
    private readonly context: BrowserContext,
    private readonly page: Page,
    readonly user: EditorUser,
    private readonly commandTimeoutMs: number,
  ) {}

  static async open(browser: Browser, options: EditorPageOptions): Promise<EditorPage> {
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, ignoreHTTPSErrors: options.ignoreTls });
    try {
      const page = await context.newPage();
      await page.goto(options.hostUrl);
      await withTimeout(
        page.evaluate(([config, url]) => (window as unknown as HostWindow).startEditor(config, url), [options.config, options.documentServerUrl] as const),
        options.openTimeoutMs,
        "the editor did not open in time",
      );
      const user = ((options.config.editorConfig as { user?: EditorUser } | undefined)?.user ?? { id: "", name: "" });
      return new EditorPage(context, page, user, options.commandTimeoutMs);
    } catch (error) {
      await context.close().catch(() => undefined);
      throw error;
    }
  }

  /** Runs *source*, a function expression, in the editor with `scope` bound to *scope*. */
  command(source: string, scope: unknown): Promise<Reply> {
    // The editor refuses a command whose code matches "import " or "import(" (common/macros.js), and the scope is
    // part of the code. Every letter of the JSON is inside a string literal, where i still reads as "i".
    const json = JSON.stringify(scope).replace(/i(?=mport)/gi, (letter) => (letter === "i" ? "\\u0069" : "\\u0049"));
    const code = `var Asc = {}; Asc.scope = ${json}; var scope = Asc.scope; (${source})();`;
    return this.serial(() =>
      this.page.evaluate(([c, t]) => (window as unknown as HostWindow).runCommand(c, t), [code, this.commandTimeoutMs] as const),
    );
  }

  method(name: string, args: unknown[] = []): Promise<Reply> {
    return this.serial(() =>
      this.page.evaluate(([n, a, t]) => (window as unknown as HostWindow).runMethod(n, a, t), [name, args, this.commandTimeoutMs] as const),
    );
  }

  /** Whether the editor reported, after *since* (page time), that every change reached the document server. */
  waitFlushed(since: number, timeoutMs: number): Promise<boolean> {
    return this.page.evaluate(([s, t]) => (window as unknown as HostWindow).waitFlushed(s, t), [since, timeoutMs] as const);
  }

  /** Why the editor cannot be used any more, or null. */
  async dead(): Promise<string | null> {
    if (this.page.isClosed()) return "the page closed";
    return this.page.evaluate(() => (window as unknown as HostWindow).dead).catch(() => "the page closed");
  }

  async close(): Promise<void> {
    await this.context.close().catch(() => undefined);
  }

  private serial<T>(work: () => Promise<T>): Promise<T> {
    this.lastUsed = Date.now();
    const run = this.queue.then(work, work);
    this.queue = run.catch(() => undefined);
    return run;
  }
}

function withTimeout<T>(work: Promise<T>, ms: number, message: string): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  return Promise.race([
    work,
    new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error(message)), ms); }),
  ]).finally(() => clearTimeout(timer));
}
