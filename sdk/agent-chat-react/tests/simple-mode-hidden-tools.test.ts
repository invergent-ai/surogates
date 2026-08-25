/**
 * Simple mode's hidden-tool policy.
 *
 * The set is the single source of truth shared by the thread's labels,
 * the expanded body rows and the orb-state derivation, so a change here
 * silently changes three surfaces at once. These assertions pin the
 * membership decisions that carry a rationale.
 */
import { describe, expect, it } from "vitest";

import {
  SIMPLE_MODE_HIDDEN_TOOLS,
  isHiddenSimpleTool,
} from "../src/runtime/simple-mode";
import type { AgentChatToolCallInfo } from "../src/types";

function call(toolName: string): AgentChatToolCallInfo {
  return { id: "c1", toolName, args: "{}", status: "complete" };
}

describe("SIMPLE_MODE_HIDDEN_TOOLS", () => {
  it("keeps skill_view visible", () => {
    // A skill load is the agent reaching for its own instructions —
    // naming the skill tells the user more about the turn than any
    // prose summary of the same call. It renders as a deterministic
    // "Reading skill <name>" row instead of being suppressed.
    expect(SIMPLE_MODE_HIDDEN_TOOLS.has("skill_view")).toBe(false);
    expect(isHiddenSimpleTool(call("skill_view"))).toBe(false);
  });

  it("still hides pure exploration and infrastructure plumbing", () => {
    for (const name of [
      "list_files",
      "search_files",
      "session_search",
      "skills_list",
      "skill_manage",
      "process",
      "memory",
      "terminal",
      "execute_code",
    ]) {
      expect(isHiddenSimpleTool(call(name))).toBe(true);
    }
  });

  it("hides every browser_* tool by prefix", () => {
    expect(isHiddenSimpleTool(call("browser_click"))).toBe(true);
    expect(isHiddenSimpleTool(call("browser_navigate"))).toBe(true);
  });

  it("shows user-facing tools", () => {
    for (const name of [
      "web_search",
      "write_file",
      "patch",
      "todo",
      "create_artifact",
      "consult_expert",
      "ask_user_question",
    ]) {
      expect(isHiddenSimpleTool(call(name))).toBe(false);
    }
  });
});
