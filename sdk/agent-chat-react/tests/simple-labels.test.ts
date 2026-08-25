/**
 * The Simple-mode label vocabulary.
 *
 * These are the words a user actually reads for a tool call, and they
 * are derived from the call's arguments alone — no model involved. The
 * vocabulary covers tools that SIMPLE_MODE_HIDDEN_TOOLS currently
 * filters out of the view as well, because that policy moves
 * (``skill_view`` crossed it) and an entry nobody exercised is an entry
 * nobody checked.
 */
import { describe, expect, it } from "vitest";

import {
  extractToolDetail,
  toolRowLabel,
} from "../src/components/chat/simple-labels";
import type { ToolCallInfo } from "../src/types";

function call(toolName: string, args: unknown): ToolCallInfo {
  return { id: "c1", toolName, args: JSON.stringify(args), status: "complete" };
}

describe("search_files", () => {
  // ``pattern`` is the query; ``path`` is the optional root to search
  // under. Reading ``path`` labelled the search *directory* as the
  // query, and dropped the label entirely for the whole-workspace
  // search that omits it — which is the common case.
  it("takes the query from pattern, not path", () => {
    expect(
      extractToolDetail(call("search_files", { pattern: "product-marketing" })),
    ).toBe("product-marketing");
  });

  it("keeps the query when a search root is also given", () => {
    expect(
      extractToolDetail(
        call("search_files", { pattern: "invoice", path: "src/billing" }),
      ),
    ).toBe("invoice");
  });

  it("names a content search", () => {
    expect(toolRowLabel(call("search_files", { pattern: "invoice" }))).toBe(
      'Searched files for "invoice"',
    );
  });

  it("names a filename search differently", () => {
    // target=files matches names, not contents; calling that "searched
    // files for X" misreports what the agent looked at.
    expect(
      toolRowLabel(call("search_files", { pattern: "*.py", target: "files" })),
    ).toBe('Looked for files named "*.py"');
  });

  it("falls back when the pattern has not streamed in yet", () => {
    expect(toolRowLabel(call("search_files", {}))).toBe("Searched files");
  });
});

describe("the rest of the vocabulary still reads correctly", () => {
  it("labels path-based tools by their basename", () => {
    expect(toolRowLabel(call("patch", { path: "src/app/landing.html" }))).toBe(
      "Edited landing.html",
    );
    expect(toolRowLabel(call("list_files", { path: "src/lib" }))).toBe(
      "Listed lib",
    );
  });

  it("labels a skill load by its skill", () => {
    expect(toolRowLabel(call("skill_view", { name: "copywriting" }))).toBe(
      "Reading skill copywriting",
    );
  });

  it("drops structurally noisy detail from shell and code tools", () => {
    expect(toolRowLabel(call("terminal", { command: "rm -rf ./tmp" }))).toBe(
      "Ran a command",
    );
  });
});
