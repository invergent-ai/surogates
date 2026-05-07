import { describe, expect, it, vi } from "vitest";
import {
  parseChatCompletionSse,
  resolveOpenAiConfig,
  streamChatCompletions,
} from "../src/server/openai";

describe("parseChatCompletionSse", () => {
  it("parses content deltas and final usage chunks", () => {
    const chunks = parseChatCompletionSse([
      'data: {"choices":[{"delta":{"content":"Hel"}}]}',
      'data: {"choices":[{"delta":{"content":"lo"}}]}',
      'data: {"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6},"model":"demo-model","choices":[{"delta":{}}]}',
      "data: [DONE]",
      "",
    ].join("\n\n"));

    expect(chunks).toEqual([
      { type: "delta", content: "Hel" },
      { type: "delta", content: "lo" },
      {
        type: "usage",
        model: "demo-model",
        inputTokens: 4,
        outputTokens: 2,
      },
      { type: "done" },
    ]);
  });

  it("ignores malformed JSON chunks", () => {
    const chunks = parseChatCompletionSse([
      "data: not-json",
      'data: {"choices":[{"delta":{"content":"ok"}}]}',
    ].join("\n\n"));

    expect(chunks).toEqual([{ type: "delta", content: "ok" }]);
  });
});

describe("resolveOpenAiConfig", () => {
  it("uses chat-completions defaults and trims trailing slashes", () => {
    const config = resolveOpenAiConfig({
      OPENAI_API_KEY: "key",
      OPENAI_BASE_URL: "https://provider.example/v1/",
      OPENAI_MODEL: "model-a",
    });

    expect(config).toEqual({
      apiKey: "key",
      baseUrl: "https://provider.example/v1",
      model: "model-a",
    });
  });
});

describe("streamChatCompletions", () => {
  it("posts chat-completions stream requests and yields parsed chunks", async () => {
    const body = [
      'data: {"choices":[{"delta":{"content":"Hi"}}]}',
      "data: [DONE]",
      "",
    ].join("\n\n");
    const fetchMock = vi.fn(async () =>
      new Response(body, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    );

    const chunks = [];
    for await (const chunk of streamChatCompletions({
      fetchImpl: fetchMock,
      config: {
        apiKey: "key",
        baseUrl: "https://provider.example/v1",
        model: "model-a",
      },
      messages: [{ role: "user", content: "hello" }],
    })) {
      chunks.push(chunk);
    }

    expect(fetchMock).toHaveBeenCalledWith(
      "https://provider.example/v1/chat/completions",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          authorization: "Bearer key",
          "content-type": "application/json",
        }),
      }),
    );
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(call[1].body))).toEqual({
      model: "model-a",
      messages: [{ role: "user", content: "hello" }],
      stream: true,
      stream_options: { include_usage: true },
    });
    expect(chunks).toEqual([
      { type: "delta", content: "Hi" },
      { type: "done" },
    ]);
  });

  it("throws provider error details", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ error: { message: "bad key" } }), {
        status: 401,
      }),
    );

    await expect(async () => {
      for await (const _chunk of streamChatCompletions({
        fetchImpl: fetchMock,
        config: {
          apiKey: "key",
          baseUrl: "https://provider.example/v1",
          model: "model-a",
        },
        messages: [{ role: "user", content: "hello" }],
      })) {
        // consume generator
      }
    }).rejects.toThrow("OpenAI-compatible provider returned 401: bad key");
  });
});
