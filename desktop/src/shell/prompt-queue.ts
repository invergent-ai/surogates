// The desktop's prompts take turns (spec, Section 4): one at a time over the app's
// window, in the order asked, as Claude Desktop queues its local consent (one at a
// time, index.chunk-CHp2HdS0.js). Each has 10 minutes from the moment it is asked,
// whether it waits its turn or is open: one still waiting then is never shown, and
// one open then is closed. A prompt whose asker goes leaves the line, or is closed.

// Claude Desktop's own deadline for its local consent.
export const PROMPT_MS = 10 * 60_000;

// What a prompt settles with when nobody answered it within its time.
export const TIMEOUT = "timeout";

// Opens its prompt, or settles it unshown when its time has run out: true once it opened.
type Turn = () => boolean;

export class PromptQueue {
  private readonly line: Turn[] = [];
  private open = false;
  private readonly listeners = new Set<() => void>();

  constructor(private readonly ms = PROMPT_MS) {}

  /** How many prompts wait behind the one open. */
  waiting(): number {
    return this.line.length;
  }

  /** Hear each change of how many wait, once the line has moved. A listener's throw is reported, and the line goes on. */
  onChange(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /**
   * Show a prompt at its turn: *show* opens it, closes it once its signal aborts, and
   * settles once it has closed. Settles with *show*'s answer; with TIMEOUT once its
   * time is up; with null once *signal* aborts. The next prompt opens only once this
   * one has closed. A *show* that throws or rejects is this prompt's rejection.
   */
  ask<T>(show: (signal: AbortSignal) => Promise<T>, signal: AbortSignal): Promise<T | typeof TIMEOUT | null> {
    const { promise, resolve, reject } = Promise.withResolvers<T | typeof TIMEOUT | null>();
    if (signal.aborted) {
      resolve(null);
      return promise;
    }
    const shown = new AbortController();
    let state: "waiting" | "open" | "done" = "waiting";
    const settle = (finish: () => void) => {
      if (state === "done") return;
      clearTimeout(timer);
      signal.removeEventListener("abort", aborted);
      state = "done";
      finish();
    };
    const end = (answer: typeof TIMEOUT | null) => {
      const at = state === "waiting" ? this.line.indexOf(turn) : -1;
      if (at !== -1) this.line.splice(at, 1);
      // An open prompt closes; the next opens once it has.
      shown.abort();
      settle(() => resolve(answer));
      if (at !== -1) this.changed();
    };
    const aborted = () => end(null);
    // On the clock the timers keep: a step of the system's clock ends no prompt.
    const deadline = performance.now() + this.ms;
    const timer = setTimeout(() => end(TIMEOUT), this.ms);
    signal.addEventListener("abort", aborted, { once: true });
    const turn: Turn = () => {
      // Its time ran out as the one before it closed: it is never shown.
      if (performance.now() >= deadline) {
        settle(() => resolve(TIMEOUT));
        return false;
      }
      state = "open";
      // One that ended before it could open is not opened.
      Promise.resolve().then(() => (shown.signal.aborted ? null : show(shown.signal))).then(
        (answer) => settle(() => resolve(answer)),
        (error: unknown) => settle(() => reject(error)),
      ).finally(() => {
        this.open = false;
        this.next();
      });
      return true;
    };
    this.line.push(turn);
    this.changed();
    this.next();
    return promise;
  }

  private next(): void {
    while (!this.open) {
      const turn = this.line.shift();
      if (!turn) return;
      this.open = turn();
      this.changed();
    }
  }

  private changed(): void {
    for (const listener of this.listeners) {
      try {
        listener();
      } catch (error) {
        console.error(error);
      }
    }
  }
}
