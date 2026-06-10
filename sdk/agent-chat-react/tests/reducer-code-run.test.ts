// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// code.run_* events synthesize a ``code_run`` tool frame on a streaming
// assistant message and accumulate streamed progress into its result
// buffer, finalizing on code.run_result (or marking it errored).

import { describe, expect, it } from "vitest";
import {
  applyAgentChatEvent,
  createInitialAgentChatState,
} from "../src/runtime/reducer";
import type { AgentChatRuntimeEvent } from "../src/types";

function started(runId: string, eventId = 1): AgentChatRuntimeEvent {
  return {
    type: "code.run_started",
    eventId,
    data: {
      run_id: runId,
      agent: "claude",
      provider: "anthropic",
      prompt: "fix the bug",
      source_event_id: 0,
    },
  };
}

function progress(runId: string, chunk: string, eventId = 2): AgentChatRuntimeEvent {
  return {
    type: "code.run_progress",
    eventId,
    data: { run_id: runId, agent: "claude", chunk },
  };
}

function result(
  runId: string,
  data: Record<string, unknown>,
  eventId = 5,
): AgentChatRuntimeEvent {
  return {
    type: "code.run_result",
    eventId,
    data: { run_id: runId, agent: "claude", ...data },
  };
}

function frameFor(
  state: ReturnType<typeof createInitialAgentChatState>,
  runId: string,
) {
  for (const msg of state.messages) {
    const tc = msg.toolCalls?.find((c) => c.id === `code-run-${runId}`);
    if (tc) return tc;
  }
  return undefined;
}

describe("code.run_* events", () => {
  it("creates a running code_run frame on a streaming assistant message", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, started("r1"));

    const frame = frameFor(state, "r1");
    expect(frame).toBeDefined();
    expect(frame?.toolName).toBe("code_run");
    expect(frame?.status).toBe("running");
    expect(JSON.parse(frame!.args)).toMatchObject({
      agent: "claude",
      provider: "anthropic",
      prompt: "fix the bug",
    });
    expect(state.isRunning).toBe(true);
    expect(state.terminal).toBe(false);
  });

  it("appends progress chunks to the frame output buffer", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, started("r1"));
    state = applyAgentChatEvent(state, progress("r1", "hello "));
    state = applyAgentChatEvent(state, progress("r1", "world", 3));

    const frame = frameFor(state, "r1");
    expect(JSON.parse(frame!.result!).output).toBe("hello world");
    expect(frame?.status).toBe("running");
  });

  it("finalizes the frame on code.run_result with final message + tokens", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, started("r1"));
    state = applyAgentChatEvent(state, progress("r1", "working..."));
    state = applyAgentChatEvent(
      state,
      result("r1", {
        final_message: "done",
        error: null,
        input_tokens: 12,
        output_tokens: 34,
      }),
    );

    const frame = frameFor(state, "r1");
    expect(frame?.status).toBe("complete");
    const parsed = JSON.parse(frame!.result!);
    expect(parsed.finalMessage).toBe("done");
    expect(parsed.output).toBe("working...");
    expect(parsed.inputTokens).toBe(12);
    expect(parsed.outputTokens).toBe(34);
    expect(parsed.error).toBeNull();
  });

  it("marks the frame errored when code.run_result carries an error", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, started("r1"));
    state = applyAgentChatEvent(
      state,
      result("r1", { final_message: "", error: "boom" }),
    );

    const frame = frameFor(state, "r1");
    expect(frame?.status).toBe("error");
    expect(JSON.parse(frame!.result!).error).toBe("boom");
  });

  it("is idempotent: a replayed run_started does not stack frames", () => {
    let state = createInitialAgentChatState({ isLoadingHistory: false });
    state = applyAgentChatEvent(state, started("r1"));
    state = applyAgentChatEvent(state, started("r1", 9));

    const frames = state.messages.flatMap(
      (m) => m.toolCalls?.filter((c) => c.id === "code-run-r1") ?? [],
    );
    expect(frames).toHaveLength(1);
  });
});
