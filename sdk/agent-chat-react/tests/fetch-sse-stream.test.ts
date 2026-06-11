// Silent-drop watchdog for the fetch-based SSE event stream.
//
// The harness emits an SSE ``: ping`` comment every 15s, so a healthy
// stream is never byte-silent for long. A silent TCP drop (laptop
// sleep, Wi-Fi roam, NAT mapping expiry) leaves ``reader.read()``
// pending forever with no error event — without a watchdog the live
// feed freezes until a full page reload. These tests pin the recovery
// behavior: sustained byte silence must fire ``onerror`` (exactly
// once) so the consumer's reconnect path takes over.
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FetchSseEventStream } from "@/runtime/fetch-sse-stream";

const STALL_MS = 45_000;

function sseResponse(): {
  response: Response;
  push: (text: string) => void;
  end: () => void;
} {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  const encoder = new TextEncoder();
  return {
    response: new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
    push: (text: string) => controller.enqueue(encoder.encode(text)),
    end: () => controller.close(),
  };
}

function openStream(response: Response) {
  const fetchFn = vi.fn().mockResolvedValue(response);
  const stream = new FetchSseEventStream("/api/v1/sessions/s1/events?after=0", {
    fetchFn,
  });
  const onerror = vi.fn();
  stream.onerror = onerror;
  return { stream, onerror, fetchFn };
}

beforeEach(() => {
  vi.useFakeTimers({
    toFake: [
      "setTimeout",
      "clearTimeout",
      "setInterval",
      "clearInterval",
      "Date",
    ],
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("FetchSseEventStream event parsing", () => {
  it("dispatches named events with id and data to listeners", async () => {
    const upstream = sseResponse();
    const { stream } = openStream(upstream.response);
    const events: Array<{ data: string; lastEventId: string }> = [];
    stream.addEventListener("llm.delta", (event) => events.push(event));

    await vi.advanceTimersByTimeAsync(0);
    upstream.push(': connected\n\nid: 7\nevent: llm.delta\ndata: {"a":1}\n\n');
    await vi.advanceTimersByTimeAsync(0);

    expect(events).toEqual([{ data: '{"a":1}', lastEventId: "7" }]);
    stream.close();
  });

  it("requests the stream with text/event-stream accept header", async () => {
    const upstream = sseResponse();
    const { stream, fetchFn } = openStream(upstream.response);
    await vi.advanceTimersByTimeAsync(0);

    const [url, init] = fetchFn.mock.calls[0] ?? [];
    expect(url).toBe("/api/v1/sessions/s1/events?after=0");
    expect(new Headers(init?.headers).get("Accept")).toBe("text/event-stream");
    stream.close();
  });
});

describe("FetchSseEventStream stall watchdog", () => {
  it("fires onerror exactly once after sustained byte silence", async () => {
    const upstream = sseResponse();
    const { stream, onerror } = openStream(upstream.response);
    await vi.advanceTimersByTimeAsync(0);
    upstream.push(": connected\n\n");

    await vi.advanceTimersByTimeAsync(STALL_MS * 3);

    expect(onerror).toHaveBeenCalledTimes(1);
    stream.close();
  });

  it("does not fire while harness pings keep arriving", async () => {
    const upstream = sseResponse();
    const { stream, onerror } = openStream(upstream.response);
    await vi.advanceTimersByTimeAsync(0);
    upstream.push(": connected\n\n");

    for (let i = 0; i < 8; i++) {
      await vi.advanceTimersByTimeAsync(15_000);
      upstream.push(`: ping ${i}\n\n`);
    }

    expect(onerror).not.toHaveBeenCalled();
    stream.close();
  });

  it("re-checks staleness on tab refocus without waiting for the timer", async () => {
    const upstream = sseResponse();
    const { stream, onerror } = openStream(upstream.response);
    await vi.advanceTimersByTimeAsync(0);
    upstream.push(": connected\n\n");

    // Jump the clock without running timer callbacks — exactly what a
    // sleeping laptop / throttled background tab looks like.
    vi.setSystemTime(Date.now() + STALL_MS * 2);
    document.dispatchEvent(new Event("visibilitychange"));

    expect(onerror).toHaveBeenCalledTimes(1);
    stream.close();
  });

  it("re-checks staleness when the browser reports network restored", async () => {
    const upstream = sseResponse();
    const { stream, onerror } = openStream(upstream.response);
    await vi.advanceTimersByTimeAsync(0);
    upstream.push(": connected\n\n");

    vi.setSystemTime(Date.now() + STALL_MS * 2);
    window.dispatchEvent(new Event("online"));

    expect(onerror).toHaveBeenCalledTimes(1);
    stream.close();
  });

  it("close() disarms the watchdog and its listeners", async () => {
    const upstream = sseResponse();
    const { stream, onerror } = openStream(upstream.response);
    await vi.advanceTimersByTimeAsync(0);
    upstream.push(": connected\n\n");

    stream.close();
    await vi.advanceTimersByTimeAsync(STALL_MS * 3);
    vi.setSystemTime(Date.now() + STALL_MS);
    document.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("online"));

    expect(onerror).not.toHaveBeenCalled();
  });
});

describe("FetchSseEventStream error funnel", () => {
  it("fires onerror exactly once on clean server close", async () => {
    const upstream = sseResponse();
    const { stream, onerror } = openStream(upstream.response);
    await vi.advanceTimersByTimeAsync(0);
    upstream.push(": connected\n\n");
    upstream.end();
    await vi.advanceTimersByTimeAsync(0);

    expect(onerror).toHaveBeenCalledTimes(1);

    // The watchdog must not re-fire after the stream already ended.
    await vi.advanceTimersByTimeAsync(STALL_MS * 3);
    expect(onerror).toHaveBeenCalledTimes(1);
    stream.close();
  });

  it("fires onerror once on a non-ok response", async () => {
    const fetchFn = vi
      .fn()
      .mockResolvedValue(new Response("denied", { status: 401 }));
    const stream = new FetchSseEventStream("/api/v1/inbox/stream", {
      fetchFn,
    });
    const onerror = vi.fn();
    stream.onerror = onerror;

    await vi.advanceTimersByTimeAsync(0);

    expect(onerror).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(STALL_MS * 3);
    expect(onerror).toHaveBeenCalledTimes(1);
    stream.close();
  });

  it("does not fire onerror when closed by the consumer", async () => {
    const upstream = sseResponse();
    const { stream, onerror } = openStream(upstream.response);
    await vi.advanceTimersByTimeAsync(0);
    upstream.push(": connected\n\n");

    stream.close();
    await vi.advanceTimersByTimeAsync(0);

    expect(onerror).not.toHaveBeenCalled();
  });
});
