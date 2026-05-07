import { afterEach, describe, expect, it, vi } from "vitest";
import { createExampleChatAdapter } from "../src/client/adapter";

class FakeEventSource {
  static lastUrl = "";
  onerror: (() => void) | null = null;
  listeners = new Map<string, Array<(event: MessageEvent<string>) => void>>();

  constructor(url: string) {
    FakeEventSource.lastUrl = url;
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  close() {}
}

const originalFetch = globalThis.fetch;
const originalEventSource = globalThis.EventSource;

afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.EventSource = originalEventSource;
  vi.restoreAllMocks();
});

describe("createExampleChatAdapter", () => {
  it("maps sendMessage to the local backend", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ eventId: 7, status: "accepted" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    globalThis.fetch = fetchMock;
    const adapter = createExampleChatAdapter("/api");

    await expect(
      adapter.sendMessage({ sessionId: "s-1", content: "hello" }),
    ).resolves.toEqual({ eventId: 7, status: "accepted" });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sessions/s-1/messages",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ content: "hello" }),
      }),
    );
  });

  it("opens EventSource streams with the after cursor", () => {
    globalThis.EventSource = FakeEventSource as unknown as typeof EventSource;
    const adapter = createExampleChatAdapter("/api");

    const stream = adapter.openEventStream({ sessionId: "s-1", after: 12 });
    stream.addEventListener("llm.delta", () => undefined);

    expect(FakeEventSource.lastUrl).toBe("/api/sessions/s-1/events?after=12");
  });

  it("uploads workspace files using FormData", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ path: "demo.txt", size: 4 }), {
        status: 201,
        headers: { "content-type": "application/json" },
      }),
    );
    globalThis.fetch = fetchMock;
    const adapter = createExampleChatAdapter("/api");

    const upload = await adapter.uploadWorkspaceFile({
      sessionId: "s-1",
      directory: "notes",
      file: new File(["demo"], "demo.txt", { type: "text/plain" }),
    });

    expect(upload).toEqual({ path: "demo.txt", size: 4 });
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toBe("/api/sessions/s-1/workspace/upload");
    expect(call[1].body).toBeInstanceOf(FormData);
  });
});
