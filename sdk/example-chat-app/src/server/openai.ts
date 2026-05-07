import type { ChatTurn } from "./session-store";

export interface OpenAiConfig {
  apiKey: string;
  baseUrl: string;
  model: string;
}

export type ChatCompletionChunk =
  | { type: "delta"; content: string }
  | { type: "usage"; model: string; inputTokens: number; outputTokens: number }
  | { type: "done" };

export interface StreamChatCompletionsInput {
  config: OpenAiConfig;
  messages: ChatTurn[];
  fetchImpl?: typeof fetch;
}

export function resolveOpenAiConfig(
  env: Record<string, string | undefined> = process.env,
): OpenAiConfig {
  return {
    apiKey: env.OPENAI_API_KEY ?? "",
    baseUrl: (env.OPENAI_BASE_URL ?? "https://api.openai.com/v1").replace(/\/+$/, ""),
    model: env.OPENAI_MODEL ?? "gpt-4o-mini",
  };
}

export async function* streamChatCompletions({
  config,
  messages,
  fetchImpl = fetch,
}: StreamChatCompletionsInput): AsyncGenerator<ChatCompletionChunk> {
  if (!config.apiKey) {
    throw new Error("OPENAI_API_KEY is required to send real chat messages.");
  }
  const response = await fetchImpl(`${config.baseUrl}/chat/completions`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${config.apiKey}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      model: config.model,
      messages,
      stream: true,
      stream_options: { include_usage: true },
    }),
  });

  if (!response.ok) {
    throw new Error(
      `OpenAI-compatible provider returned ${response.status}: ${await readErrorMessage(response)}`,
    );
  }
  if (!response.body) throw new Error("Provider response did not include a stream body.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const split = buffer.split(/\n\n/);
    buffer = split.pop() ?? "";
    for (const frame of split) {
      for (const chunk of parseChatCompletionSse(frame)) yield chunk;
    }
  }
  buffer += decoder.decode();
  for (const chunk of parseChatCompletionSse(buffer)) yield chunk;
}

export function parseChatCompletionSse(payload: string): ChatCompletionChunk[] {
  const chunks: ChatCompletionChunk[] = [];
  for (const frame of payload.split(/\n\n/)) {
    const data = frame
      .split(/\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim())
      .join("\n");
    if (!data) continue;
    if (data === "[DONE]") {
      chunks.push({ type: "done" });
      continue;
    }
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(data) as Record<string, unknown>;
    } catch {
      continue;
    }
    const content = extractContentDelta(parsed);
    if (content) chunks.push({ type: "delta", content });
    const usage = extractUsage(parsed);
    if (usage) chunks.push(usage);
  }
  return chunks;
}

function extractContentDelta(parsed: Record<string, unknown>) {
  const choices = Array.isArray(parsed.choices) ? parsed.choices : [];
  const first = choices[0] as Record<string, unknown> | undefined;
  const delta = first?.delta as Record<string, unknown> | undefined;
  return typeof delta?.content === "string" ? delta.content : "";
}

function extractUsage(parsed: Record<string, unknown>): ChatCompletionChunk | null {
  const usage = parsed.usage as Record<string, unknown> | undefined;
  if (!usage) return null;
  return {
    type: "usage",
    model: typeof parsed.model === "string" ? parsed.model : "",
    inputTokens: numberValue(usage.prompt_tokens),
    outputTokens: numberValue(usage.completion_tokens),
  };
}

async function readErrorMessage(response: Response) {
  const text = await response.text();
  try {
    const parsed = JSON.parse(text) as { error?: { message?: string }; message?: string };
    return parsed.error?.message ?? parsed.message ?? text;
  } catch {
    return text || response.statusText;
  }
}

function numberValue(value: unknown) {
  return typeof value === "number" ? value : 0;
}
