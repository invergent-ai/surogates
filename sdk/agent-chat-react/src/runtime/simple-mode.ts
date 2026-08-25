// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Simple mode's hidden-tool vocabulary, shared between the chat
// thread's labels and the orb-state derivation (and any external
// render layer, e.g. the website widget) so a quiet "Thinking…" label
// and its orb can never disagree about what stays hidden.

import type { AgentChatToolCallInfo } from "../types";
import type { OrbDerivationOptions } from "./orb-state";

// ``skill_view`` is deliberately absent: a skill load is the agent
// reaching for its own instructions, and naming the skill tells the
// user more about the turn than any prose summary of the same call
// could. It renders as a deterministic "Reading skill <name>" row.
export const SIMPLE_MODE_HIDDEN_TOOLS: ReadonlySet<string> = new Set([
  "list_files",
  "search_files",
  "session_search",
  "skills_list",
  "skill_manage",
  "process",
  "memory",
  // Shell commands and code execution are infrastructure plumbing the
  // user doesn't need to see when the goal is to know *what* the
  // agent accomplished, not *how*. Expert mode still has the full
  // command + output block.
  "terminal",
  "execute_code",
]);

export function isHiddenSimpleTool(tc: AgentChatToolCallInfo): boolean {
  if (SIMPLE_MODE_HIDDEN_TOOLS.has(tc.toolName)) return true;
  // Browser tools are an internal sub-grouped activity in Expert
  // mode; in Simple mode we hide them outright.
  if (tc.toolName.startsWith("browser_")) return true;
  return false;
}

/** Orb derivation options implementing Simple mode's hidden-tool policy. */
export const SIMPLE_MODE_ORB_OPTIONS: OrbDerivationOptions = {
  toolFilter: (tc) => !isHiddenSimpleTool(tc),
};
