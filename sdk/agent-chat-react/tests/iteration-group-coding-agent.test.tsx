/**
 * Simple-mode rendering for run_coding_agent + code_run tool calls:
 * - a run_coding_agent call collapses to a clean "Coding agent · <task>"
 *   header, and the expanded row reads "Claude Code: <task>" / "Codex: …".
 * - a code_run frame renders the rich CodeRunToolBlock (streamed output),
 *   not a raw "code_run" tool name.
 */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { IterationGroup } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { ChatMessage } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function mount(node: ReactElement): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root?.render(<TooltipProvider>{node}</TooltipProvider>));
  return container;
}

function msg(over: Partial<ChatMessage>): ChatMessage {
  return {
    id: "m1", role: "assistant", content: "", createdAt: new Date(),
    status: "complete", ...over,
  };
}

describe("IterationGroup — coding agent rows", () => {
  it("renders run_coding_agent as 'Coding agent · <task>', not the raw tool name", () => {
    const dom = mount(
      <IterationGroup
        message={msg({
          toolCalls: [{
            id: "c1", toolName: "run_coding_agent",
            args: JSON.stringify({ agent: "claude", prompt: "fix the build" }),
            status: "complete",
          }],
        })}
        sessionId="s-1"
        artifactFallbacks={{}}
      />,
    );
    expect(dom.textContent).toContain("Coding agent");
    expect(dom.textContent).toContain("fix the build");
    expect(dom.textContent).not.toContain("run_coding_agent");

    // Expand → agent-specific prose.
    act(() => dom.querySelector("button")!.click());
    expect(dom.textContent).toContain("Claude Code: fix the build");
  });

  it("renders a code_run frame as the rich CodeRunToolBlock", () => {
    const dom = mount(
      <IterationGroup
        message={msg({
          toolCalls: [{
            id: "c2", toolName: "code_run",
            args: JSON.stringify({ agent: "codex", provider: "openai", prompt: "review it" }),
            result: JSON.stringify({
              agent: "codex", output: "› Bash\nall tests pass",
              finalMessage: "Reviewed. 4/4 tests pass.", error: null,
              inputTokens: 5, outputTokens: 2,
            }),
            status: "complete",
          }],
        })}
        sessionId="s-1"
        artifactFallbacks={{}}
      />,
    );
    // Collapsed header reads cleanly (not "code_run").
    expect(dom.textContent).not.toContain("code_run");
    // Expand → the CodeRunToolBlock surfaces the run + final message.
    act(() => dom.querySelector("button")!.click());
    expect(dom.textContent).toContain("codex");
    expect(dom.textContent).toContain("Reviewed. 4/4 tests pass.");
  });
});
