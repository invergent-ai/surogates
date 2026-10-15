import { chromium, type Browser, type Page } from "playwright-core";
import { EditorPage } from "./connector.js";

/** No room for another document: the caller tries again later. */
export class Busy extends Error {}
/** The page is not open (never was, closed idle, or its editor died): open it again. */
export class PageGone extends Error {}

export interface PoolOptions {
  hostUrl: string;
  maxPages: number;
  idleMs: number;
  openTimeoutMs: number;
  commandTimeoutMs: number;
  ignoreTls: boolean;
  executablePath?: string;
}

export interface OpenRequest {
  docKey: string;
  sessionId: string;
  config: Record<string, unknown>;
  documentServerUrl: string;
}

/** A page's id: one page per document key per chat. The worker computes the same id. */
export function pageId(docKey: string, sessionId: string): string {
  return `${docKey}.${sessionId}`;
}

// The document server sometimes answers an editor's fetch of the file "Download failed" (-4) and serves the same
// file on the next try.
const OPEN_ATTEMPTS = 3;

function downloadFailed(error: unknown): boolean {
  return error instanceof Error && error.message.includes('"errorCode":-4');
}

export class PagePool {
  private browser: Promise<Browser> | null = null;
  private readonly pages = new Map<string, EditorPage>();
  private readonly opening = new Map<string, Promise<string>>();
  private measuring: Promise<Page> | null = null;

  constructor(private readonly options: PoolOptions) {}

  size(): number {
    return this.pages.size + this.opening.size;
  }

  cap(): number {
    return this.options.maxPages;
  }

  /** The page for the document and chat, opened when there is none; the config is used only to open it. */
  async open(request: OpenRequest): Promise<string> {
    const id = pageId(request.docKey, request.sessionId);
    const existing = this.pages.get(id);
    if (existing) {
      if ((await existing.dead()) === null) {
        existing.lastUsed = Date.now();
        return id;
      }
      await this.close(id);
    }
    const pending = this.opening.get(id);
    if (pending) return pending;
    if (this.size() >= this.options.maxPages) {
      throw new Busy(`runtime-office has ${this.options.maxPages} documents open, its limit`);
    }
    const opened = (async () => {
      for (let attempt = 1; ; attempt++) {
        try {
          const page = await EditorPage.open(await this.launch(), {
            ...this.options,
            config: request.config,
            documentServerUrl: request.documentServerUrl,
          });
          this.pages.set(id, page);
          return id;
        } catch (error) {
          if (attempt >= OPEN_ATTEMPTS || !downloadFailed(error)) throw error;
        }
      }
    })().finally(() => this.opening.delete(id));
    this.opening.set(id, opened);
    return opened;
  }

  async get(id: string): Promise<EditorPage> {
    const page = this.pages.get(id);
    if (!page) throw new PageGone(`no page ${id} is open`);
    const dead = await page.dead();
    if (dead !== null) {
      await this.close(id);
      throw new PageGone(dead);
    }
    return page;
  }

  async close(id: string): Promise<void> {
    const page = this.pages.get(id);
    this.pages.delete(id);
    await page?.close();
  }

  /** Closes the pages no call used for the idle time, so the agent leaves the co-author list. */
  async sweep(now = Date.now()): Promise<number> {
    let closed = 0;
    for (const [id, page] of [...this.pages]) {
      if (now - page.lastUsed > this.options.idleMs) {
        await this.close(id);
        closed += 1;
      }
    }
    return closed;
  }

  /** A page of its own for measuring slides: no script of their content runs (their CSP says so) and nothing they
   * name is fetched (every request is aborted). Device scale 2, so a drawn svg stays sharp. */
  async measuringPage(): Promise<Page> {
    const current = this.measuring && (await this.measuring.catch(() => null));
    if (!current || current.isClosed()) {
      this.measuring = (async () => {
        const context = await (await this.launch()).newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 2 });
        await context.route("**/*", (route) => route.abort());
        return context.newPage();
      })();
    }
    return this.measuring!;
  }

  async shutdown(): Promise<void> {
    for (const id of [...this.pages.keys()]) await this.close(id);
    await (await this.measuring?.catch(() => null))?.context().close().catch(() => undefined);
    this.measuring = null;
    const browser = await this.browser?.catch(() => null);
    this.browser = null;
    await browser?.close();
  }

  private launch(): Promise<Browser> {
    if (!this.browser) {
      // ponytail: Chromium's sandbox is off (pods have no user namespaces); the pages load only the document server's editor.
      const launching = chromium.launch({ headless: true, executablePath: this.options.executablePath, chromiumSandbox: false });
      this.browser = launching;
      launching.then(
        (browser) => browser.on("disconnected", () => {
          this.browser = null;
          this.pages.clear();
          this.measuring = null;
        }),
        () => { this.browser = null; },
      );
    }
    return this.browser;
  }
}
