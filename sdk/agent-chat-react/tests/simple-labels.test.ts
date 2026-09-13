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
  deriveSingleToolLabel,
  extractToolDetail,
  sameToolGroupLabel,
  skillViewLabel,
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

describe("arguments are model output, not a contract", () => {
  // parseArgs is a JSON.parse behind an unchecked generic, so any field
  // can arrive as any type. A label helper must never throw: there is
  // no ErrorBoundary in the SDK, so one bad row unmounts the thread.
  it("survives a non-string file_path", () => {
    expect(
      skillViewLabel([call("skill_view", { name: "x", file_path: ["a"] })]),
    ).toBe("Reading skill x");
  });

  it("survives a non-string pattern", () => {
    expect(extractToolDetail(call("search_files", { pattern: 42 }))).toBe(null);
  });

  it("survives a non-string path", () => {
    expect(toolRowLabel(call("patch", { path: { nested: true } }))).toBe(
      "Edited a file",
    );
  });
});

describe("knowledge base tools", () => {
  it("names the search query", () => {
    const tc = call("kb_search_pages", { query: "franciza incendiu" });
    expect(toolRowLabel(tc)).toBe('Searched the knowledge base for "franciza incendiu"');
    expect(deriveSingleToolLabel(tc)).toBe("Knowledge base search · franciza incendiu");
  });

  it("names a document lookup differently", () => {
    expect(
      toolRowLabel(call("kb_search_pages", { query: "CG-LOC-2024", mode: "documents" })),
    ).toBe('Looked up document "CG-LOC-2024"');
  });

  it("names the page read and its page span", () => {
    expect(
      toolRowLabel(call("kb_read_page", { path: "sources/conditii-generale.json", pages: "7-11" })),
    ).toBe("Read conditii-generale, pages 7-11");
    expect(toolRowLabel(call("kb_read_page", { path: "concepts/franciza.md" }))).toBe(
      "Read franciza",
    );
  });

  it("counts a run of the same tool in words", () => {
    expect(sameToolGroupLabel("kb_read_page", 2)).toBe("Read 2 knowledge base pages");
    expect(sameToolGroupLabel("kb_search_pages", 3)).toBe("Ran 3 knowledge base searches");
    expect(sameToolGroupLabel("merge_experiment", 2)).toBe("Merge experiment × 2");
  });
});

describe("other one-liner tools", () => {
  it("labels web_crawl by the url it takes", () => {
    expect(toolRowLabel(call("web_crawl", { url: "https://www.example.com/docs" }))).toBe(
      "Crawled example.com",
    );
  });

  it("labels session_search by its query", () => {
    expect(toolRowLabel(call("session_search", { query: "invoice" }))).toBe(
      'Searched session for "invoice"',
    );
  });

  it("labels vision_analyze by the image, never a data URL", () => {
    expect(toolRowLabel(call("vision_analyze", { image: "shots/home.png" }))).toBe(
      "Looked at home.png",
    );
    expect(toolRowLabel(call("vision_analyze", { image: "data:image/png;base64,AAAA" }))).toBe(
      "Looked at an image",
    );
  });
});

describe("legacy target aliases", () => {
  // The server maps {grep: content, find: files} before running the
  // search, so the raw argument the model emitted can be either name.
  it("treats target=find as a filename search", () => {
    expect(
      toolRowLabel(call("search_files", { pattern: "*.py", target: "find" })),
    ).toBe('Looked for files named "*.py"');
  });

  it("treats target=grep as a content search", () => {
    expect(
      toolRowLabel(call("search_files", { pattern: "invoice", target: "grep" })),
    ).toBe('Searched files for "invoice"');
  });
});
