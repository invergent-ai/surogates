// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { describe, expect, it } from "vitest";

import {
  ORB_STATE_LABELS,
  deriveOrbActivity,
  messageOrbState,
  toolOrbState,
} from "../src/runtime/orb-state";
import type {
  AgentChatMessage,
  AgentChatToolCallInfo,
} from "../src/types";

function toolCall(
  overrides: Partial<AgentChatToolCallInfo> = {},
): AgentChatToolCallInfo {
  return {
    id: "tc-1",
    toolName: "terminal",
    args: "{}",
    status: "running",
    ...overrides,
  };
}

function assistant(
  overrides: Partial<AgentChatMessage> = {},
): AgentChatMessage {
  return {
    id: "m-1",
    role: "assistant",
    content: "",
    createdAt: new Date("2026-07-23T00:00:00Z"),
    status: "streaming",
    ...overrides,
  };
}

function user(overrides: Partial<AgentChatMessage> = {}): AgentChatMessage {
  return {
    id: "u-1",
    role: "user",
    content: "hi",
    createdAt: new Date("2026-07-23T00:00:00Z"),
    status: "complete",
    ...overrides,
  };
}

describe("toolOrbState", () => {
  it("maps retrieval tools to searching", () => {
    for (const name of [
      "web_search",
      "web_extract",
      "web_crawl",
      "session_search",
      "kb_list_pages",
      "kb_read_page",
      "search_files",
      "list_files",
      "research_memory",
    ]) {
      expect(toolOrbState(name)).toBe("searching");
    }
  });

  it("maps authoring tools to shaping", () => {
    for (const name of [
      "write_file",
      "patch",
      "create_artifact",
      "generate_image",
      "generate_video",
      "skill_manage",
    ]) {
      expect(toolOrbState(name)).toBe("shaping");
    }
  });

  it("maps heavy-analysis tools to solving", () => {
    for (const name of ["run_coding_agent", "consult_expert", "vision_analyze"]) {
      expect(toolOrbState(name)).toBe("solving");
    }
  });

  it("maps ask_user_question to listening", () => {
    expect(toolOrbState("ask_user_question")).toBe("listening");
  });

  it("defaults execution and unknown tools to working", () => {
    for (const name of ["terminal", "browser_click", "memory", "todo", "no_such_tool"]) {
      expect(toolOrbState(name)).toBe("working");
    }
  });

  it("classifies MCP tools by their leaf verb", () => {
    expect(toolOrbState("mcp__linear__search_issues")).toBe("searching");
    expect(toolOrbState("mcp__linear__list_projects")).toBe("searching");
    expect(toolOrbState("mcp__notion__create_page")).toBe("shaping");
    expect(toolOrbState("mcp__notion__update_block")).toBe("shaping");
    expect(toolOrbState("mcp__slack__send_message")).toBe("working");
  });

  it("does not verb-classify non-MCP tool names", () => {
    expect(toolOrbState("search_everything_custom")).toBe("working");
  });
});

describe("messageOrbState", () => {
  it("prefers a pending ask_user_question over other running tools", () => {
    const msg = assistant({
      toolCalls: [
        toolCall({ id: "a", toolName: "ask_user_question" }),
        toolCall({ id: "b", toolName: "web_search" }),
      ],
    });
    expect(messageOrbState(msg)).toBe("listening");
  });

  it("uses the most recently started running tool", () => {
    const msg = assistant({
      toolCalls: [
        toolCall({ id: "a", toolName: "web_search", status: "complete" }),
        toolCall({ id: "b", toolName: "write_file" }),
      ],
    });
    expect(messageOrbState(msg)).toBe("shaping");
  });

  it("ignores completed tools and falls back to the stream phase", () => {
    const msg = assistant({
      content: "Here is the answer",
      toolCalls: [toolCall({ status: "complete" })],
    });
    expect(messageOrbState(msg)).toBe("composing");
  });

  it("treats a reasoning-only stream as solving", () => {
    const msg = assistant({ reasoning: "Let me think about this" });
    expect(messageOrbState(msg)).toBe("solving");
  });

  it("treats visible text streaming as composing even with reasoning", () => {
    const msg = assistant({ content: "The answer", reasoning: "hmm" });
    expect(messageOrbState(msg)).toBe("composing");
  });

  it("defaults a not-yet-streaming message to working", () => {
    expect(messageOrbState(assistant({ status: "complete" }))).toBe("working");
    expect(messageOrbState(assistant())).toBe("working");
  });

  it("lets a toolFilter hide plumbing tools from the orb", () => {
    const hidden = new Set(["list_files"]);
    const msg = assistant({
      content: "Let me check the workspace.",
      toolCalls: [toolCall({ toolName: "list_files" })],
    });
    // Unfiltered the orb would leak "searching"; Simple mode's filter
    // drops the hidden tool and the orb stays on the quiet generic
    // state — NOT "composing", the preamble text is finished, and NOT
    // the hidden tool's real activity.
    expect(messageOrbState(msg)).toBe("searching");
    expect(
      messageOrbState(msg, {
        toolFilter: (tc) => !hidden.has(tc.toolName),
      }),
    ).toBe("working");
  });

  it("keeps listening even when a toolFilter excludes ask_user_question", () => {
    const msg = assistant({
      toolCalls: [toolCall({ toolName: "ask_user_question" })],
    });
    expect(messageOrbState(msg, { toolFilter: () => false })).toBe("listening");
  });
});

describe("deriveOrbActivity", () => {
  it("derives from the latest assistant message", () => {
    const activity = deriveOrbActivity([
      user(),
      assistant({ toolCalls: [toolCall({ toolName: "web_search" })] }),
    ]);
    expect(activity).toEqual({
      state: "searching",
      label: ORB_STATE_LABELS.searching,
    });
  });

  it("skips trailing system markers appended after the assistant turn", () => {
    const activity = deriveOrbActivity([
      user(),
      assistant({ reasoning: "thinking hard" }),
      {
        ...assistant({ id: "sys-1", status: "complete" }),
        role: "system",
      },
    ]);
    expect(activity.state).toBe("solving");
  });

  it("returns working when the turn has not produced a message yet", () => {
    const activity = deriveOrbActivity([assistant({ status: "complete" }), user()]);
    expect(activity).toEqual({
      state: "working",
      label: ORB_STATE_LABELS.working,
    });
  });

  it("returns working for an empty thread", () => {
    expect(deriveOrbActivity([]).state).toBe("working");
  });
});
