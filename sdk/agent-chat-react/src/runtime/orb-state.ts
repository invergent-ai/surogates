// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Maps live chat activity onto the six `thinking-orbs` animation
// states so every "the agent is busy" indicator can show *what kind*
// of busy — searching, solving, composing… — instead of a generic
// shimmer. Pure data + functions: no React, so the same derivation can
// back other render layers (e.g. the Preact website widget).

import type { OrbState } from "thinking-orbs";
import type { AgentChatMessage, AgentChatToolCallInfo } from "../types";

export type { OrbState };

/** A resolved activity: which orb animation to show and its label. */
export interface OrbActivity {
  state: OrbState;
  /** Human label for the activity, e.g. "Searching…". */
  label: string;
}

/**
 * Default per-state labels. `working` deliberately keeps the historical
 * "Working on it..." copy so the bottom-of-thread indicator reads the
 * same as before when nothing more specific is known.
 */
export const ORB_STATE_LABELS: Readonly<Record<OrbState, string>> = {
  working: "Working on it...",
  searching: "Searching…",
  solving: "Solving…",
  listening: "Waiting for your answer…",
  composing: "Writing…",
  shaping: "Shaping…",
};

/**
 * Builtin tool name → orb state. Tools not listed here (terminal,
 * browser_*, delegation, cron, github, …) fall through to `working` —
 * the generic "hands busy" animation.
 */
const TOOL_ORB_STATES: Readonly<Record<string, OrbState>> = {
  // Retrieval: the scan-meridian globe.
  web_search: "searching",
  web_extract: "searching",
  web_crawl: "searching",
  session_search: "searching",
  kb_list_pages: "searching",
  kb_read_page: "searching",
  search_files: "searching",
  list_files: "searching",
  research_memory: "searching",
  fetch_channel_messages: "searching",
  fetch_channel_file: "searching",
  // Creation/authoring: the circle→triangle→square morph.
  write_file: "shaping",
  patch: "shaping",
  create_artifact: "shaping",
  generate_image: "shaping",
  generate_video: "shaping",
  skill_manage: "shaping",
  research_outline: "shaping",
  // Heavy analysis/computation: bands scramble, then click back solved.
  run_coding_agent: "solving",
  consult_expert: "solving",
  vision_analyze: "solving",
  idea_tree: "solving",
  dispatch_experiments: "solving",
  merge_experiment: "solving",
  // Waiting on the user: the waveform.
  ask_user_question: "listening",
};

const MCP_TOOL_PREFIX = "mcp__";
const MCP_SEARCHING_RE =
  /(?:^|_)(?:search|query|find|lookup|fetch|list|read|get)(?:_|$)/;
const MCP_SHAPING_RE =
  /(?:^|_)(?:write|create|generate|edit|update|build|make)(?:_|$)/;

/**
 * Resolve a tool name to an orb state. MCP tools
 * (``mcp__{server}__{tool}``) have no fixed vocabulary, so their leaf
 * name is classified by verb: retrieval verbs → `searching`, creation
 * verbs → `shaping`, anything else → `working`.
 */
export function toolOrbState(toolName: string): OrbState {
  const mapped = TOOL_ORB_STATES[toolName];
  if (mapped) return mapped;
  if (toolName.startsWith(MCP_TOOL_PREFIX)) {
    const leaf = toolName.split("__").pop() ?? "";
    if (MCP_SEARCHING_RE.test(leaf)) return "searching";
    if (MCP_SHAPING_RE.test(leaf)) return "shaping";
  }
  return "working";
}

function lastRunningToolCall(
  message: AgentChatMessage,
): AgentChatToolCallInfo | undefined {
  const calls = message.toolCalls ?? [];
  for (let i = calls.length - 1; i >= 0; i--) {
    const tc = calls[i];
    if (tc && tc.status === "running") return tc;
  }
  return undefined;
}

/**
 * Orb state for a single assistant message (one iteration). Priority:
 * a pending ``ask_user_question`` wins (the agent is parked on the
 * user), then the most recently started running tool, then the
 * reasoning stream (`solving`), then visible text streaming
 * (`composing`). A message with none of those — e.g. the request is in
 * flight but nothing has streamed yet — is plain `working`.
 */
export function messageOrbState(message: AgentChatMessage): OrbState {
  const running = lastRunningToolCall(message);
  if (running) {
    if (
      message.toolCalls?.some(
        (tc) => tc.toolName === "ask_user_question" && tc.status === "running",
      )
    ) {
      return "listening";
    }
    return toolOrbState(running.toolName);
  }
  if (message.status !== "streaming") return "working";
  if (message.content.length > 0) return "composing";
  if (message.reasoning && message.reasoning.length > 0) return "solving";
  return "working";
}

/**
 * Orb activity for the thread as a whole — what the bottom-of-thread
 * running indicator should show. Scans backward to the most recent
 * assistant message (skipping trailing system markers the reducer
 * appends after a turn, mirroring ``isAwaitingUserInput`` in the chat
 * thread) and derives its state; a trailing user message means the
 * turn hasn't produced anything yet → `working`.
 */
export function deriveOrbActivity(messages: AgentChatMessage[]): OrbActivity {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (!m) continue;
    if (m.role === "user") break;
    if (m.role !== "assistant") continue;
    const state = messageOrbState(m);
    return { state, label: ORB_STATE_LABELS[state] };
  }
  return { state: "working", label: ORB_STATE_LABELS.working };
}
