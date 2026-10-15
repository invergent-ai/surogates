/**
 * IterationGroup — Simple-mode renderer for a single assistant
 * iteration. Behaviors covered:
 *
 * - Collapsed by default when an iterationSummary is attached; only the
 *   summary line + status dot show.
 * - Click expands the row to what its tools returned: their output, or
 *   a failure's code and message. A row with nothing returned does not
 *   expand at all.
 * - Live placeholders ("Thinking..." / "Working (N tools)") replace the
 *   summary line while the iteration is still streaming.
 * - Permanently expanded fallback when the iteration is complete but no
 *   summary ever arrived (replay of pre-feature history, summarizer
 *   timeout).
 */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { IterationGroup } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "@invergent/ui-sdk/tooltip";
import type { ChatMessage } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

function buildMessage(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "",
    createdAt: new Date(),
    status: "complete",
    ...over,
  };
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) {
    act(() => root?.unmount());
  }
  root = null;
  container?.remove();
  container = null;
});


function mount(node: ReactElement): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    // TooltipProvider is normally established by <AgentChat>; provide one
    // here so the per-tool blocks (Patch, Read, etc.) can mount.
    root?.render(<TooltipProvider>{node}</TooltipProvider>);
  });
  return container;
}


describe("IterationGroup", () => {
  it("renders the summary as a collapsed row when present", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      iterationSummary: {
        iterationIndex: 0,
        summary: "Rework hero paragraph",
        toolCallIds: ["c1"],
        startedAt: "",
        endedAt: "",
      },
      reasoning: "long internal reasoning text",
      toolCalls: [
        { id: "c1", toolName: "patch", args: "{}", status: "complete" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    expect(dom.textContent).toContain("Rework hero paragraph");
    // Reasoning content stays hidden in Simple mode.
    expect(dom.textContent).not.toContain("long internal reasoning text");
  });

  it("expands to what the command's tools returned, without reasoning or a Done line", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      iterationSummary: {
        iterationIndex: 0,
        summary: "Outline the plan",
        toolCallIds: [],
        startedAt: "",
        endedAt: "",
      },
      reasoning: "long internal reasoning text",
      reasoningTokens: 2500,
      toolCalls: [
        {
          id: "c1",
          toolName: "patch",
          args: "{\"path\":\"x.html\"}",
          status: "complete",
          result: JSON.stringify({ success: true, output: "Applied 1 hunk to x.html" }),
        },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    const trigger = dom.querySelector("button");
    expect(trigger).not.toBeNull();
    act(() => {
      trigger!.click();
    });
    expect(dom.textContent).toContain("Applied 1 hunk to x.html");
    expect(dom.textContent).not.toContain("Thought");
    expect(dom.textContent).not.toContain("Done");
    expect(dom.innerHTML).not.toContain("long internal reasoning text");
  });

  it("expands a failed command to its error code and message", () => {
    const message = buildMessage({
      iterationSummary: {
        iterationIndex: 0, summary: "Editor unreachable, document edit failed",
        toolCallIds: [], startedAt: "", endedAt: "",
      },
      toolCalls: [
        {
          id: "c1",
          toolName: "office_edit",
          args: "{}",
          status: "complete",
          result: JSON.stringify({ error: "office_unreachable", message: "The live editor did not respond." }),
        },
      ],
    });
    const dom = mount(<IterationGroup message={message} />);
    act(() => { dom.querySelector<HTMLButtonElement>("button[aria-expanded='false']")!.click(); });
    expect(dom.textContent).toContain("office_unreachable");
    expect(dom.textContent).toContain("The live editor did not respond.");
  });

  it("shows a Thinking placeholder while streaming and no tools have started", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      status: "streaming",
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    expect(dom.textContent).toMatch(/Thinking/i);
  });

  it("shows a derived live label collapsing same-tool runs while streaming", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      status: "streaming",
      toolCalls: [
        { id: "c1", toolName: "patch", args: "{}", status: "running" },
        { id: "c2", toolName: "patch", args: "{}", status: "running" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    expect(dom.textContent).toMatch(/Running.+Patch.+×.+2/);
  });

  it("shows a derived single-tool live label including the path detail", () => {
    // Even when tools have started and reasoning text exists, the
    // streaming row stays collapsed — users who want progress detail
    // switch to Expert mode.
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      status: "streaming",
      reasoning: "internal thought that should stay hidden",
      toolCalls: [
        { id: "c1", toolName: "patch", args: "{\"path\":\"x.html\"}", status: "running" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    expect(dom.textContent).toMatch(/Running.+Patch.+x\.html/);
    // Reasoning content stays hidden (no expanded view during stream).
    expect(dom.textContent).not.toContain("internal thought");
  });

  it("swaps shimmer for derived label once all tools complete, even with status=streaming", () => {
    // Regression for user-reported bug: tool-using assistant messages
    // never transition out of status="streaming" — the reducer leaves
    // them that way for the rest of the turn. The IterationGroup must
    // detect "no running tools" as the iteration-done signal.
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      status: "streaming",  // still tagged streaming!
      toolCalls: [
        { id: "c1", toolName: "read_file", args: "{\"path\":\"x.html\"}", status: "complete", result: "{\"content\": \"<p>hi</p>\"}" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    // Collapsed derived label, NOT the shimmer.
    expect(dom.textContent).not.toMatch(/Running/i);
    expect(dom.textContent).not.toMatch(/Thinking/i);
    expect(dom.textContent).toContain("Read");
    expect(dom.querySelector("button[aria-expanded='false']"))
      .not.toBeNull();
  });

  it("derives a tool-name label when no summary is present (complete)", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        { id: "c1", toolName: "read_file", args: "{\"path\":\"x.html\"}", status: "complete", result: "{\"content\": \"<p>hi</p>\"}" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    // Collapsed by default — derived label includes the human tool
    // name and the path detail.
    expect(dom.textContent).toContain("Read · x.html");
    // Collapsible trigger exists so users can check what it returned.
    const trigger = dom.querySelector("button[aria-expanded='false']");
    expect(trigger).not.toBeNull();
  });

  it("lists each call of a same-tool run as a command of its own", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        { id: "c1", toolName: "patch", args: "{\"path\":\"a.html\"}", status: "complete" },
        { id: "c2", toolName: "patch", args: "{\"path\":\"b.html\"}", status: "complete" },
        { id: "c3", toolName: "patch", args: "{\"path\":\"c.html\"}", status: "complete" },
      ],
    });
    const dom = mount(<IterationGroup message={message} />);
    expect(dom.textContent).toContain("Ran 3 commands");
    expect(dom.textContent).not.toContain("Edited 3 files");
    act(() => {
      [...dom.querySelectorAll("button")]
        .find((b) => b.textContent?.includes("Ran "))!
        .click();
    });
    expect(dom.textContent).toContain("Patch · a.html");
    expect(dom.textContent).toContain("Patch · c.html");
  });

  it("lists a mixed batch's calls one by one, never 'Used N tools'", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        { id: "c1", toolName: "patch", args: "{\"path\":\"x.html\"}", status: "complete" },
        { id: "c2", toolName: "read_file", args: "{\"path\":\"y.md\"}", status: "complete" },
      ],
    });
    const dom = mount(<IterationGroup message={message} />);
    expect(dom.textContent).toContain("Ran 2 commands");
    expect(dom.textContent).not.toContain("Used 2 tools");
    act(() => {
      [...dom.querySelectorAll("button")]
        .find((b) => b.textContent?.includes("Ran "))!
        .click();
    });
    expect(dom.textContent).toContain("Patch · x.html");
    expect(dom.textContent).toContain("Read · y.md");
  });

  it("keeps reasoning out of a row, which has nothing to open without tool output", () => {
    const reasoning = "Private reasoning that must stay out of Simple mode.";
    const message = buildMessage({
      iterationSummary: {
        iterationIndex: 0, summary: "Updated the page",
        toolCallIds: [], startedAt: "", endedAt: "",
      },
      reasoning,
      reasoningTokens: 2500,
    });
    const dom = mount(
      <IterationGroup message={message} />,
    );
    expect(dom.textContent).toContain("Updated the page");
    expect(dom.querySelector("button")).toBeNull();
    expect(dom.textContent).not.toContain("Thought");
    expect(dom.innerHTML).not.toContain(reasoning);
  });

  it("updates the live count from reported usage without exposing the trace", () => {
    const message = buildMessage({ status: "streaming", reasoning: "Private trace" });
    const dom = mount(
      <IterationGroup message={message} />,
    );
    // Text alone never produces an estimated token count.
    expect(dom.textContent).toBe("Thinking...");
    for (const [tokens, label] of [[100, "100"], [500, "500"], [1200, "1.2k"], [2500, "2.5k"]] as const) {
      act(() => {
        root?.render(
          <TooltipProvider>
            <IterationGroup
              message={{ ...message, reasoningTokens: tokens }}
            />
          </TooltipProvider>,
        );
      });
      expect(dom.textContent).toBe(`Thinking... ${label} tokens`);
      expect(dom.innerHTML).not.toContain(message.reasoning);
      expect(dom.querySelector("button")).toBeNull();
    }
  });

  it("hides internal tools (list_files, search_files, browser_*, etc.) in Simple mode", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        { id: "c1", toolName: "list_files", args: "{\"path\":\".\"}", status: "complete", result: "{\"output\": \"a.txt\\nb.txt\"}" },
        { id: "c2", toolName: "search_files", args: "{}", status: "complete" },
        { id: "c3", toolName: "browser_click", args: "{}", status: "complete" },
        { id: "c4", toolName: "session_search", args: "{}", status: "complete" },
        { id: "c5", toolName: "process", args: "{}", status: "complete" },
        { id: "c6", toolName: "memory", args: "{}", status: "complete" },
        // Plus one visible tool so the iteration still renders.
        { id: "c7", toolName: "patch", args: "{\"path\":\"x.html\"}", status: "complete", result: "{\"output\": \"Applied\"}" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    // Header counts only visible tools (1: the patch).
    expect(dom.textContent).toContain("Patch · x.html");
    expect(dom.textContent).not.toContain("List Files");
    expect(dom.textContent).not.toContain("Search Files");
    expect(dom.textContent).not.toContain("Browser");
    expect(dom.textContent).not.toContain("Process");
    expect(dom.textContent).not.toContain("Memory");

    // Expand — the row is the patch alone, so it opens onto the patch's
    // output; the hidden calls are no row of their own, and with no summary
    // speaking for the whole step, their output stays out too.
    const trigger = dom.querySelector("button[aria-expanded='false']");
    act(() => { (trigger as HTMLButtonElement).click(); });
    expect(dom.textContent).toContain("Applied");
    expect(dom.textContent).not.toContain("a.txt");
    expect(dom.textContent).not.toContain("Listed");
    expect(dom.textContent).not.toContain("Done");
  });

  it("keeps the todo tool visible in Simple mode (user-facing task management)", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        { id: "c1", toolName: "todo", args: "{\"action\":\"add\",\"item\":\"draft outline\"}", status: "complete" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    expect(dom.textContent).toContain("Todo");
    // The list is the todo tool's output, so the row always opens onto it.
    const trigger = dom.querySelector("button[aria-expanded='false']");
    act(() => { (trigger as HTMLButtonElement).click(); });
    expect(dom.textContent).toContain("Todo");
  });

  it("hides failed tool calls in Simple mode", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        // A successful retry alongside a failed first attempt.
        {
          id: "c1",
          toolName: "patch",
          args: "{\"path\":\"x.html\"}",
          status: "complete",
          result: JSON.stringify({ error: "patch did not apply" }),
        },
        { id: "c2", toolName: "patch", args: "{\"path\":\"x.html\"}", status: "complete" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    // Failed first attempt is hidden, only the successful retry counts.
    expect(dom.textContent).toContain("Patch · x.html");
    expect(dom.textContent).not.toContain("× 2");
  });

  it("renders nothing when all tools are internal and reasoning is empty", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        { id: "c1", toolName: "list_files", args: "{}", status: "complete" },
        { id: "c2", toolName: "browser_click", args: "{}", status: "complete" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    expect(dom.textContent ?? "").toBe("");
  });

  it("shows generic Thinking when only a hidden tool is running", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      status: "streaming",
      toolCalls: [
        { id: "c1", toolName: "list_files", args: "{}", status: "running" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    expect(dom.textContent).toMatch(/Thinking/);
    expect(dom.textContent).not.toContain("List Files");
  });

  it("shows a shell command's output when its row opens, never the command line", () => {
    // The label says what the agent did; the output is the detail a
    // reader opens the row to check. The raw command stays out.
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      iterationSummary: {
        iterationIndex: 0,
        summary: "Investigated the workspace",
        toolCallIds: [],
        startedAt: "",
        endedAt: "",
      },
      toolCalls: [
        {
          id: "c1",
          toolName: "terminal",
          args: JSON.stringify({
            command: "cd __WORKSPACE__ && python3 -c 'import pandas'",
          }),
          status: "complete",
          result: JSON.stringify({ output: "pandas 2.2.3", exit_code: 0 }),
        },
        {
          id: "c2",
          toolName: "execute_code",
          args: JSON.stringify({ code: "print(1 + 1)" }),
          status: "complete",
        },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    const trigger = dom.querySelector("button[aria-expanded='false']");
    act(() => { (trigger as HTMLButtonElement).click(); });
    expect(dom.textContent).toContain("Investigated the workspace");
    expect(dom.textContent).toContain("pandas 2.2.3");
    // One call with output: no per-call label over it.
    expect(dom.textContent).not.toContain("Ran a command");
    // execute_code returned nothing, so it leaves no trace.
    expect(dom.textContent).not.toContain("Executed code");
    expect(dom.textContent).not.toContain("python3");
    expect(dom.textContent).not.toContain("__WORKSPACE__");
  });

  // ── skill_view: a deterministic row, never a model caption ──────
  //
  // A skill load is the agent reaching for its own instructions, so
  // the skill's name is the whole story of the iteration. The harness
  // deliberately emits no summary for these iterations; the label is
  // derived from the args so it is instant, free and never wrong.

  it("labels a completed skill_view with the skill name", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        {
          id: "c1",
          toolName: "skill_view",
          args: '{"name":"copywriting"}',
          status: "complete",
        },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    expect(dom.textContent).toContain("Reading skill copywriting");
  });

  it("appends the file when skill_view opens a linked skill file", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        {
          id: "c1",
          toolName: "skill_view",
          args: '{"name":"copywriting","file_path":"reference/tone.md"}',
          status: "complete",
        },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    expect(dom.textContent).toContain("Reading skill copywriting · tone.md");
  });

  it("names every skill when one iteration loads several", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        { id: "c1", toolName: "skill_view", args: '{"name":"copywriting"}', status: "complete" },
        { id: "c2", toolName: "skill_view", args: '{"name":"social"}', status: "complete" },
      ],
    });
    const dom = mount(<IterationGroup message={message} />);
    expect(dom.textContent).toContain("Ran 2 commands");
    act(() => {
      [...dom.querySelectorAll("button")]
        .find((b) => b.textContent?.includes("Ran "))!
        .click();
    });
    expect(dom.textContent).toContain("Reading skill copywriting");
    expect(dom.textContent).toContain("Reading skill social");
  });

  it("falls back to a countable label when the skill name is unparseable", () => {
    // Args stream in a character at a time; a half-written name must
    // never reach the row.
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        { id: "c1", toolName: "skill_view", args: '{"na', status: "complete" },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    expect(dom.textContent).toContain("Reading a skill");
  });

  it("uses the same wording for the live shimmer as for the finished row", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      status: "streaming",
      toolCalls: [
        {
          id: "c1",
          toolName: "skill_view",
          args: '{"name":"copywriting"}',
          status: "running",
        },
      ],
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    // "Reading skill copywriting…" while live, "Reading skill
    // copywriting" once done — the row must not rename itself.
    expect(dom.textContent).toContain("Reading skill copywriting");
    expect(dom.textContent).not.toContain("Running Skill");
  });

  it("names a skill load beside the other calls of its batch", () => {
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
      toolCalls: [
        { id: "c1", toolName: "skill_view", args: '{"name":"copywriting"}', status: "complete" },
        { id: "c2", toolName: "web_search", args: '{"query":"dashboards"}', status: "complete" },
      ],
    });
    const dom = mount(<IterationGroup message={message} />);
    expect(dom.textContent).toContain("Ran 2 commands");
    act(() => {
      [...dom.querySelectorAll("button")]
        .find((b) => b.textContent?.includes("Ran "))!
        .click();
    });
    expect(dom.textContent).toContain("Reading skill copywriting");
    expect(dom.textContent).toContain("Web search · dashboards");
  });

  it("renders nothing when complete, no summary, no tools, no reasoning", () => {
    // Empty iteration — nothing to derive. The surrounding
    // SimpleAssistantGroup still renders any final text + the
    // TurnSummaryCard, so dropping this row is the right move.
    const message = buildMessage({
      turnId: "t-1",
      iterationIndex: 0,
    });
    const dom = mount(
      <IterationGroup
        message={message}
      />,
    );
    expect(dom.textContent ?? "").toBe("");
  });
});
