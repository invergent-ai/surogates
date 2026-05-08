import { describe, expect, it } from "vitest";
import { toolErrorSummary } from "../src/components/chat/tools/shared";
import { parseTerminalResult } from "../src/components/chat/tools/terminal-tool";

describe("tool result summaries", () => {
  it("summarizes structured tool errors", () => {
    expect(
      toolErrorSummary(JSON.stringify({
        error: "sandbox_unavailable",
        reason: "Sandbox pod sandbox-0df712402538 failed to become ready",
      })),
    ).toBe("Sandbox is unavailable. Workspace commands cannot run right now.");
  });

  it("renders sandbox-unavailable terminal results as command failures", () => {
    const parsed = parseTerminalResult(
      JSON.stringify({
        error: "sandbox_unavailable",
        reason: "Sandbox pod sandbox-0df712402538 failed to become ready",
      }),
      JSON.stringify({ command: "pip install python-pptx" }),
    );

    expect(parsed).toMatchObject({
      command: "pip install python-pptx",
      exit_code: 1,
      error: "Sandbox is unavailable. Workspace commands cannot run right now.",
      output: "Sandbox is unavailable. Workspace commands cannot run right now.",
    });
  });
});
