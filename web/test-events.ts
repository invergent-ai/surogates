// Standalone test script for use-session-runtime event handling.
// Parses events.txt (SSE format) and simulates the state machine,
// printing the resulting messages to verify no duplication.
//
// Usage: npx tsx test-events.ts

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ── Types (copied from use-session-runtime.ts) ─────────────────────

interface ToolCallInfo {
  id: string;
  toolName: string;
  args: string;
  result?: string;
  status: "running" | "complete" | "error";
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: Date;
  status: "complete" | "streaming" | "error";
  toolCalls?: ToolCallInfo[];
  reasoning?: string;
}

// ── Parse SSE events from file ─────────────────────────────────────

interface SSEEvent {
  id: number;
  type: string;
  data: Record<string, unknown>;
}

function parseSSE(text: string): SSEEvent[] {
  const events: SSEEvent[] = [];
  const blocks = text.split("\n\n");

  for (const block of blocks) {
    const lines = block.trim().split("\n");
    let id = 0;
    let type = "";
    let dataStr = "";

    for (const line of lines) {
      if (line.startsWith("id: ")) id = parseInt(line.slice(4), 10);
      else if (line.startsWith("event: ")) type = line.slice(7);
      else if (line.startsWith("data: ")) dataStr = line.slice(6);
      else if (line.startsWith(": ")) continue; // comment
    }

    if (type && dataStr) {
      try {
        events.push({ id, type, data: JSON.parse(dataStr) });
      } catch {
        // skip malformed
      }
    }
  }

  return events;
}

// ── Event handler (mirrors use-session-runtime.ts) ─────────────────

function findLastAssistant(msgs: ChatMessage[]): ChatMessage | undefined {
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === "assistant") return msgs[i];
  }
  return undefined;
}

function findLastAssistantIndex(msgs: ChatMessage[]): number {
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === "assistant") return i;
  }
  return -1;
}

function simulate(events: SSEEvent[]): ChatMessage[] {
  const messages: ChatMessage[] = [];
  let hadDeltas = false;

  for (const evt of events) {
    const { type, id: eventId, data } = evt;

    switch (type) {
      case "user.message": {
        messages.push({
          id: `evt-${eventId}`,
          role: "user",
          content: (data.content as string) ?? "",
          createdAt: new Date(),
          status: "complete",
        });
        break;
      }

      case "llm.delta": {
        hadDeltas = true;
        const lastIdx = findLastAssistantIndex(messages);
        const lastMsg = lastIdx >= 0 ? messages[lastIdx] : null;
        const canAppend = !!(
          lastMsg &&
          lastMsg.status === "streaming" &&
          !(lastMsg.toolCalls && lastMsg.toolCalls.length > 0)
        );
        if (canAppend) {
          messages[lastIdx] = {
            ...lastMsg!,
            content: lastMsg!.content + ((data.content as string) ?? ""),
          };
        } else {
          messages.push({
            id: `evt-${eventId}`,
            role: "assistant",
            content: (data.content as string) ?? "",
            createdAt: new Date(),
            status: "streaming",
          });
        }
        break;
      }

      case "llm.response": {
        const msg = data.message as Record<string, unknown> | undefined;
        const responseContent =
          (msg?.content as string) ?? (data.content as string) ?? "";
        const hasToolCalls = !!(
          msg?.tool_calls &&
          Array.isArray(msg.tool_calls) &&
          (msg.tool_calls as unknown[]).length > 0
        );

        const useDeltaContent = hadDeltas;
        hadDeltas = false;

        const prevAssistant = findLastAssistant(messages);
        const prevHasTools = !!(
          prevAssistant?.toolCalls && prevAssistant.toolCalls.length > 0
        );
        const idx = findLastAssistantIndex(messages);

        if (useDeltaContent && idx >= 0) {
          if (hasToolCalls) {
            messages[idx] = {
              ...messages[idx],
              reasoning:
                (messages[idx].reasoning ?? "") + messages[idx].content,
              content: "",
              status: "streaming",
            };
          } else {
            messages[idx] = {
              ...messages[idx],
              status: "complete",
            };
          }
        } else if (prevHasTools || !prevAssistant) {
          messages.push({
            id: `evt-${eventId}`,
            role: "assistant",
            content: hasToolCalls ? "" : responseContent,
            reasoning:
              hasToolCalls && responseContent ? responseContent : undefined,
            createdAt: new Date(),
            status: hasToolCalls ? "streaming" : "complete",
          });
        } else if (idx >= 0) {
          if (hasToolCalls && responseContent) {
            messages[idx] = {
              ...messages[idx],
              reasoning:
                (messages[idx].reasoning ?? "") + responseContent,
              status: "streaming",
            };
          } else {
            messages[idx] = {
              ...messages[idx],
              content: responseContent || messages[idx].content,
              status: hasToolCalls ? "streaming" : "complete",
            };
          }
        }
        break;
      }

      case "tool.call": {
        let assistant = findLastAssistant(messages);
        if (!assistant || assistant.status === "complete") {
          assistant = {
            id: `evt-${eventId}-tc`,
            role: "assistant",
            content: "",
            createdAt: new Date(),
            status: "streaming",
          };
          messages.push(assistant);
        }
        assistant.toolCalls = assistant.toolCalls ?? [];
        const tcId = (data.tool_call_id as string) ?? `tc-${eventId}`;
        if (!assistant.toolCalls.some((t) => t.id === tcId)) {
          assistant.toolCalls.push({
            id: tcId,
            toolName: (data.name as string) ?? "unknown",
            args:
              typeof data.arguments === "string"
                ? data.arguments
                : JSON.stringify(data.arguments ?? {}),
            status: "running",
          });
        }
        break;
      }

      case "tool.result": {
        const assistant = findLastAssistant(messages);
        const tc = assistant?.toolCalls?.find(
          (t) => t.id === (data.tool_call_id as string),
        );
        if (tc) {
          tc.result =
            typeof data.content === "string"
              ? data.content
              : typeof data.result === "string"
                ? data.result
                : JSON.stringify(data.content ?? data.result ?? null);
          tc.status = "complete";
        }
        break;
      }

      case "llm.thinking": {
        const thinkIdx = findLastAssistantIndex(messages);
        if (thinkIdx >= 0) {
          const prev = messages[thinkIdx];
          messages[thinkIdx] = {
            ...prev,
            reasoning:
              (prev.reasoning ?? "") +
              ((data.reasoning as string) ?? (data.content as string) ?? ""),
          };
        }
        break;
      }

      case "session.complete":
      case "session.done":
      case "session.fail":
      case "harness.crash": {
        const doneIdx = findLastAssistantIndex(messages);
        if (doneIdx >= 0 && messages[doneIdx].status === "streaming") {
          messages[doneIdx] = {
            ...messages[doneIdx],
            status: type === "session.fail" ? "error" : "complete",
          };
        }
        break;
      }
    }
  }

  return messages;
}

// ── Main ────────────────────────────────────────────────────────────

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const eventsPath = path.resolve(__dirname, "../events.txt");
const raw = fs.readFileSync(eventsPath, "utf-8");
const events = parseSSE(raw);

console.log(`Parsed ${events.length} events\n`);

const messages = simulate(events);

console.log(`Resulting messages: ${messages.length}\n`);
console.log("=".repeat(72));

for (let i = 0; i < messages.length; i++) {
  const msg = messages[i];
  const toolCount = msg.toolCalls?.length ?? 0;

  console.log(`\n[${i}] ${msg.role.toUpperCase()} (status=${msg.status})`);

  if (msg.reasoning) {
    console.log(`  reasoning: "${msg.reasoning.slice(0, 100)}${msg.reasoning.length > 100 ? "..." : ""}"`);
  }

  if (toolCount > 0) {
    console.log(`  tools (${toolCount}):`);
    for (const tc of msg.toolCalls!) {
      console.log(`    - ${tc.toolName} [${tc.status}]`);
    }
  }

  if (msg.content) {
    console.log(`  content: "${msg.content.slice(0, 100)}${msg.content.length > 100 ? "..." : ""}"`);
  }

  if (!msg.reasoning && !msg.content && toolCount === 0) {
    console.log(`  (empty)`);
  }
}

console.log("\n" + "=".repeat(72));

// Check for duplicates — flag if the same text appears in multiple messages.
const allTexts: string[] = [];
for (const msg of messages) {
  if (msg.reasoning) allTexts.push(msg.reasoning.trim());
  if (msg.content) allTexts.push(msg.content.trim());
}

const seen = new Set<string>();
let dupes = 0;
for (const t of allTexts) {
  if (t.length < 10) continue; // skip short fragments
  if (seen.has(t)) {
    console.log(`\n** DUPLICATE TEXT: "${t.slice(0, 80)}..."`);
    dupes++;
  }
  seen.add(t);
}

if (dupes === 0) {
  console.log("\nNo duplicate text detected.");
} else {
  console.log(`\n** ${dupes} duplicate(s) found!`);
}
