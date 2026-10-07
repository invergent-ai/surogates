import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PROMPT_MS, PromptQueue, TIMEOUT } from "../src/shell/prompt-queue.js";

// A prompt as the window shows it: open until the test answers it, or closed once its signal aborts.
class Prompt {
  readonly opened: string[] = [];
  readonly closed: string[] = [];
  private readonly answers = new Map<string, (answer: string) => void>();

  show(name: string) {
    return (signal: AbortSignal): Promise<string | null> => {
      this.opened.push(name);
      return new Promise((resolve) => {
        this.answers.set(name, resolve);
        signal.addEventListener("abort", () => {
          this.closed.push(name);
          resolve(null);
        }, { once: true });
      });
    };
  }

  answer(name: string, answer: string): void {
    this.answers.get(name)?.(answer);
  }
}

const never = () => new AbortController().signal;
let queue: PromptQueue;
let prompt: Prompt;

beforeEach(() => {
  vi.useFakeTimers();
  queue = new PromptQueue();
  prompt = new Prompt();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the desktop's prompts", () => {
  it("open one at a time, in the order asked, each once the one before has closed", async () => {
    const first = queue.ask(prompt.show("first"), never());
    const second = queue.ask(prompt.show("second"), never());
    await vi.advanceTimersByTimeAsync(0);
    expect(prompt.opened).toEqual(["first"]);
    expect(queue.waiting()).toBe(1);
    prompt.answer("first", "allow");
    expect(await first).toBe("allow");
    await vi.advanceTimersByTimeAsync(0);
    expect(prompt.opened).toEqual(["first", "second"]);
    expect(queue.waiting()).toBe(0);
    prompt.answer("second", "deny");
    expect(await second).toBe("deny");
  });

  it("tell each change of how many wait", async () => {
    const counts: number[] = [];
    queue.onChange(() => counts.push(queue.waiting()));
    const first = queue.ask(prompt.show("first"), never());
    void queue.ask(prompt.show("second"), never());
    await vi.advanceTimersByTimeAsync(0);
    prompt.answer("first", "allow");
    await first;
    await vi.advanceTimersByTimeAsync(0);
    expect(counts).toEqual([1, 0, 1, 0]);
  });

  it("time out after 10 minutes: one waiting then is never shown, and one open then is closed", async () => {
    const open = queue.ask(prompt.show("open"), never());
    await vi.advanceTimersByTimeAsync(PROMPT_MS / 2);
    const waiting = queue.ask(prompt.show("waiting"), never());
    await vi.advanceTimersByTimeAsync(PROMPT_MS / 2);
    expect(await open).toBe(TIMEOUT);
    expect(prompt.closed).toEqual(["open"]);
    // Its turn came: it is shown, with the rest of its own time.
    await vi.advanceTimersByTimeAsync(0);
    expect(prompt.opened).toEqual(["open", "waiting"]);
    const late = queue.ask(prompt.show("late"), never());
    await vi.advanceTimersByTimeAsync(PROMPT_MS / 2);
    expect(await waiting).toBe(TIMEOUT);
    await vi.advanceTimersByTimeAsync(0);
    prompt.answer("late", "allow");
    expect(await late).toBe("allow");
  });

  it("never show one whose time ran out while it waited its turn", async () => {
    const asked = ["open", "second", "third"].map((name) => queue.ask(prompt.show(name), never()));
    await vi.advanceTimersByTimeAsync(PROMPT_MS);
    expect(await Promise.all(asked)).toEqual([TIMEOUT, TIMEOUT, TIMEOUT]);
    await vi.advanceTimersByTimeAsync(0);
    expect(prompt.opened).toEqual(["open"]);
    expect(queue.waiting()).toBe(0);
  });

  it("close an open prompt whose asker went, and let one waiting its turn leave the line", async () => {
    const [stopped, leaving] = [new AbortController(), new AbortController()];
    const open = queue.ask(prompt.show("open"), stopped.signal);
    const waiting = queue.ask(prompt.show("waiting"), leaving.signal);
    const after = queue.ask(prompt.show("after"), never());
    await vi.advanceTimersByTimeAsync(0);
    leaving.abort();
    expect(await waiting).toBeNull();
    expect(queue.waiting()).toBe(1);
    stopped.abort();
    expect(await open).toBeNull();
    expect(prompt.closed).toEqual(["open"]);
    await vi.advanceTimersByTimeAsync(0);
    expect(prompt.opened).toEqual(["open", "after"]);
    prompt.answer("after", "allow");
    expect(await after).toBe("allow");
  });

  it("show nothing for an asker already gone", async () => {
    const gone = new AbortController();
    gone.abort();
    expect(await queue.ask(prompt.show("gone"), gone.signal)).toBeNull();
    expect(prompt.opened).toEqual([]);
  });

  it("open the next once one that could not be shown has failed, and tell the asker why", async () => {
    const failing = queue.ask(() => Promise.reject(new Error("no display")), never());
    const next = queue.ask(prompt.show("next"), never());
    await expect(failing).rejects.toThrow("no display");
    await vi.advanceTimersByTimeAsync(0);
    prompt.answer("next", "allow");
    expect(await next).toBe("allow");
  });

  it("keep the next waiting until the one closed has gone, however late it closes", async () => {
    let closed = (): void => {};
    const slow = queue.ask((signal) => new Promise<string>((resolve) => {
      signal.addEventListener("abort", () => {
        closed = () => resolve("late");
      }, { once: true });
    }), never());
    await vi.advanceTimersByTimeAsync(PROMPT_MS / 2);
    void queue.ask(prompt.show("next"), never());
    await vi.advanceTimersByTimeAsync(PROMPT_MS / 2);
    expect(await slow).toBe(TIMEOUT);
    await vi.advanceTimersByTimeAsync(0);
    expect(prompt.opened).toEqual([]);
    closed();
    await vi.advanceTimersByTimeAsync(0);
    expect(prompt.opened).toEqual(["next"]);
  });

  it("show nothing for an asker gone before its prompt could open", async () => {
    const going = new AbortController();
    const asked = queue.ask(prompt.show("gone"), going.signal);
    going.abort();
    expect(await asked).toBeNull();
    await vi.advanceTimersByTimeAsync(0);
    expect(prompt.opened).toEqual([]);
  });

  it("tell the asker why one could not be shown when its show throws, and open the next", async () => {
    const failing = queue.ask(() => {
      throw new Error("no window");
    }, never());
    const next = queue.ask(prompt.show("next"), never());
    await expect(failing).rejects.toThrow("no window");
    await vi.advanceTimersByTimeAsync(0);
    prompt.answer("next", "allow");
    expect(await next).toBe("allow");
  });

  it("show one waiting its turn whatever the system clock does", async () => {
    const open = queue.ask(prompt.show("open"), never());
    const waiting = queue.ask(prompt.show("waiting"), never());
    await vi.advanceTimersByTimeAsync(1_000);
    vi.setSystemTime(Date.now() + 60 * 60_000);
    prompt.answer("open", "allow");
    expect(await open).toBe("allow");
    await vi.advanceTimersByTimeAsync(0);
    expect(prompt.opened).toEqual(["open", "waiting"]);
    prompt.answer("waiting", "allow");
    expect(await waiting).toBe("allow");
  });
});

describe("the desktop's prompts, told to their listeners", () => {
  it("open one at a time, in order, for a listener that asks as it hears", async () => {
    const first = queue.ask(prompt.show("first"), never());
    const second = queue.ask(prompt.show("second"), never());
    let asked = false;
    queue.onChange(() => {
      if (asked) return;
      asked = true;
      void queue.ask(prompt.show("third"), never());
    });
    await vi.advanceTimersByTimeAsync(0);
    prompt.answer("first", "allow");
    await first;
    await vi.advanceTimersByTimeAsync(0);
    expect(prompt.opened).toEqual(["first", "second"]);
    expect(queue.waiting()).toBe(1);
    prompt.answer("second", "allow");
    await second;
    await vi.advanceTimersByTimeAsync(0);
    expect(prompt.opened).toEqual(["first", "second", "third"]);
  });

  it("go on past a listener that throws, and report its throw", async () => {
    const thrown = new Error("listener");
    const reported = vi.spyOn(console, "error").mockImplementation(() => {});
    const first = queue.ask(prompt.show("first"), never());
    const second = queue.ask(prompt.show("second"), never());
    queue.onChange(() => {
      throw thrown;
    });
    await vi.advanceTimersByTimeAsync(0);
    prompt.answer("first", "allow");
    expect(await first).toBe("allow");
    await vi.advanceTimersByTimeAsync(0);
    expect(prompt.opened).toEqual(["first", "second"]);
    const third = queue.ask(prompt.show("third"), never());
    expect(queue.waiting()).toBe(1);
    prompt.answer("second", "deny");
    expect(await second).toBe("deny");
    await vi.advanceTimersByTimeAsync(0);
    prompt.answer("third", "allow");
    expect(await third).toBe("allow");
    expect(reported).toHaveBeenCalledWith(thrown);
  });

  it("drop no other prompt from the line, once a listener has thrown", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const first = queue.ask(prompt.show("first"), never());
    const second = queue.ask(prompt.show("second"), never());
    let thrown = false;
    queue.onChange(() => {
      if (thrown) return;
      thrown = true;
      throw new Error("listener");
    });
    await vi.advanceTimersByTimeAsync(0);
    prompt.answer("first", "allow");
    await first;
    await vi.advanceTimersByTimeAsync(PROMPT_MS / 2);
    const third = queue.ask(prompt.show("third"), never());
    void queue.ask(prompt.show("fourth"), never());
    await vi.advanceTimersByTimeAsync(PROMPT_MS / 2);
    expect(await second).toBe(TIMEOUT);
    await vi.advanceTimersByTimeAsync(0);
    prompt.answer("third", "allow");
    expect(await third).toBe("allow");
    await vi.advanceTimersByTimeAsync(0);
    expect(prompt.opened).toEqual(["first", "second", "third", "fourth"]);
  });
});
