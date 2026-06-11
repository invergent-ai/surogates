import type { AgentChatSseMessageEvent } from "../types";

// The harness emits an SSE ``: ping`` comment every 15 seconds, so a
// healthy stream is never byte-silent for long. A silent TCP drop
// (laptop sleep/resume, Wi-Fi roam, NAT mapping expiry) leaves
// ``reader.read()`` pending forever with NO error event — without a
// watchdog the live feed freezes until a full page reload. Three
// missed pings means the connection is dead.
//
// Native ``EventSource`` cannot implement this: browsers do not expose
// SSE comment lines to JavaScript, so ping arrivals are invisible.
// Reading the bytes ourselves through ``fetch()`` is what makes the
// liveness signal observable at all.
const STREAM_STALL_THRESHOLD_MS = 45_000;
const STREAM_STALL_CHECK_INTERVAL_MS = 5_000;

class StreamStallWatchdog {
  private lastByteAt = Date.now();
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly onStall: () => void;
  private readonly check = () => {
    if (Date.now() - this.lastByteAt > STREAM_STALL_THRESHOLD_MS) {
      this.onStall();
    }
  };

  constructor(onStall: () => void) {
    this.onStall = onStall;
  }

  start() {
    this.lastByteAt = Date.now();
    this.timer = setInterval(this.check, STREAM_STALL_CHECK_INTERVAL_MS);
    // Instant re-check on tab refocus / network restore: browsers
    // throttle interval timers in background tabs, but these events
    // fire the moment the user is back in front of a possibly-dead
    // stream, so recovery starts immediately instead of on the next
    // (throttled) tick.
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", this.check);
    }
    if (typeof window !== "undefined") {
      window.addEventListener("online", this.check);
    }
  }

  touch() {
    this.lastByteAt = Date.now();
  }

  stop() {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", this.check);
    }
    if (typeof window !== "undefined") {
      window.removeEventListener("online", this.check);
    }
  }
}

export interface FetchSseEventStreamOptions {
  /** Fetch implementation — pass the app's authenticated fetch wrapper. */
  fetchFn?: (
    input: RequestInfo | URL,
    init?: RequestInit,
  ) => Promise<Response>;
}

/**
 * SSE-over-fetch event stream with silent-drop detection.
 *
 * Structurally compatible with ``AgentChatEventStream`` (and the inbox
 * stream contract): ``addEventListener``/``close``/``onerror``. Every
 * failure mode — bad response, mid-stream error, clean server close,
 * and watchdog-detected silent stall — funnels through a once-only
 * guard, so consumers can treat ``onerror`` as "reconnect now" without
 * risking a leaked second stream from double-firing.
 */
export class FetchSseEventStream {
  private readonly abort = new AbortController();
  private readonly decoder = new TextDecoder();
  private readonly listeners = new Map<
    string,
    Set<(event: AgentChatSseMessageEvent) => void>
  >();
  private buffer = "";
  private eventType = "";
  private eventId = "";
  private dataLines: string[] = [];
  private readonly url: string;
  private readonly fetchFn: (
    input: RequestInfo | URL,
    init?: RequestInit,
  ) => Promise<Response>;
  private readonly watchdog = new StreamStallWatchdog(() => this.fail());
  private errored = false;

  onerror: (() => void) | null = null;

  constructor(url: string, options: FetchSseEventStreamOptions = {}) {
    this.url = url;
    this.fetchFn = options.fetchFn ?? fetch;
    queueMicrotask(() => void this.start());
  }

  addEventListener(
    type: string,
    listener: (event: AgentChatSseMessageEvent) => void,
  ) {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  close() {
    this.watchdog.stop();
    this.abort.abort();
  }

  private fail() {
    if (this.errored) return;
    this.errored = true;
    this.watchdog.stop();
    this.onerror?.();
  }

  private async start() {
    try {
      const response = await this.fetchFn(this.url, {
        signal: this.abort.signal,
        headers: { Accept: "text/event-stream" },
      });
      if (!response.ok || !response.body) {
        this.fail();
        return;
      }
      this.watchdog.start();
      const reader = response.body.getReader();
      while (!this.abort.signal.aborted) {
        const { done, value } = await reader.read();
        if (done) break;
        this.watchdog.touch();
        this.feed(this.decoder.decode(value, { stream: true }));
      }
      this.feed(this.decoder.decode());
      this.flushEvent();
      // Server closed the response. Signal onerror so the consumer's
      // reconnect path runs — matches native EventSource semantics,
      // where any close is surfaced as an error.
      if (!this.abort.signal.aborted) {
        this.fail();
      }
    } catch (error) {
      if (!this.abort.signal.aborted) {
        console.error("SSE stream failed:", error);
        this.fail();
      }
    } finally {
      this.watchdog.stop();
    }
  }

  private feed(chunk: string) {
    this.buffer += chunk;
    for (;;) {
      const newlineIndex = this.buffer.indexOf("\n");
      if (newlineIndex < 0) return;
      const line = this.buffer.slice(0, newlineIndex).replace(/\r$/, "");
      this.buffer = this.buffer.slice(newlineIndex + 1);
      this.processLine(line);
    }
  }

  private processLine(line: string) {
    if (line === "") {
      this.flushEvent();
      return;
    }
    if (line.startsWith(":")) return;

    const separator = line.indexOf(":");
    const field = separator >= 0 ? line.slice(0, separator) : line;
    const rawValue = separator >= 0 ? line.slice(separator + 1) : "";
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;

    if (field === "event") {
      this.eventType = value;
    } else if (field === "id") {
      this.eventId = value;
    } else if (field === "data") {
      this.dataLines.push(value);
    }
  }

  private flushEvent() {
    if (!this.eventType || this.dataLines.length === 0) {
      this.dataLines = [];
      return;
    }
    const listeners = this.listeners.get(this.eventType);
    const event = {
      data: this.dataLines.join("\n"),
      lastEventId: this.eventId,
    };
    if (listeners) {
      for (const listener of listeners) {
        listener(event);
      }
    }
    this.eventType = "";
    this.dataLines = [];
  }
}
