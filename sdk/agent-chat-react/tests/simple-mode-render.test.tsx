/**
 * Simple-mode ChatThread rendering: AssistantGroup composes
 * IterationGroup + final-answer text + TurnSummaryCard when
 * viewMode="simple"; falls back to the existing Expert timeline
 * when viewMode="expert".
 *
 * Also verifies system entries (skill_invoked, artifact) stay
 * visible in Simple mode (review correction).
 */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { ChatThread } from "../src/components/chat/chat-thread";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type { AgentChatAdapter, ChatMessage } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

function adapterStub(): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    listSessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
    createSession: vi.fn(),
    getSession: vi.fn(),
    sendMessage: vi.fn(),
    openEventStream: vi.fn(() => ({
      addEventListener: vi.fn(),
      close: vi.fn(),
      onerror: null,
    })),
    getWorkspaceDownloadUrl: vi.fn(
      ({ sessionId, path }: { sessionId: string; path: string }) =>
        `/api/v1/sessions/${sessionId}/workspace/download?path=${encodeURIComponent(path)}`,
    ),
    getWorkspaceFile: vi.fn().mockResolvedValue({
      path: "x",
      content: "",
      size: 0,
      encoding: "utf-8" as const,
      truncated: false,
    }),
  } as unknown as AgentChatAdapter;
}


function assistantMessage(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "asst-1",
    role: "assistant",
    content: "Here's what I did.",
    createdAt: new Date(),
    status: "complete",
    turnId: "t-1",
    iterationIndex: 0,
    iterationSummary: {
      iterationIndex: 0,
      summary: "Reworked the hero copy",
      toolCallIds: ["c1"],
      startedAt: "",
      endedAt: "",
    },
    toolCalls: [
      { id: "c1", toolName: "patch", args: "{}", status: "complete", result: "{}" },
    ],
    turnSummary: {
      turnId: "t-1",
      recap: "Reworked the hero around brain/hands.",
      artifacts: [{ kind: "file", label: "landing.html", ref: "landing.html" }],
    },
    ...over,
  };
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  vi.useRealTimers();
});

function mount(node: ReactElement): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <AgentChatAdapterProvider
        value={{ adapter: adapterStub(), sessionId: "s-1" }}
      >
        <TooltipProvider>{node}</TooltipProvider>
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

function rerender(node: ReactElement): HTMLDivElement {
  if (!root || !container) throw new Error("Call mount before rerender");
  act(() => {
    root?.render(
      <AgentChatAdapterProvider
        value={{ adapter: adapterStub(), sessionId: "s-1" }}
      >
        <TooltipProvider>{node}</TooltipProvider>
      </AgentChatAdapterProvider>,
    );
  });
  return container;
}

const noop = () => Promise.resolve();


describe("Simple mode ChatThread rendering", () => {
  it("shows the iteration summary line, hides per-tool entries by default, shows the recap", () => {
    const messages = [
      // Iteration 0 with tool calls; iteration summary attached.
      assistantMessage({
        id: "iter-0",
        content: "",
        reasoning: "thinking briefly",
      }),
      // Iteration 1: final text answer.
      {
        id: "final",
        role: "assistant" as const,
        content: "Here's what I did.",
        createdAt: new Date(),
        status: "complete" as const,
        turnId: "t-1",
        iterationIndex: 1,
        turnSummary: {
          turnId: "t-1",
          recap: "Reworked the hero around brain/hands.",
          artifacts: [
            { kind: "file" as const, label: "landing.html", ref: "landing.html" },
          ],
        },
      },
    ];

    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        terminal={true}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );

    expect(dom.textContent).toContain("Reworked the hero copy"); // iteration summary
    expect(dom.textContent).toContain("Here's what I did."); // final answer
    expect(dom.textContent).toContain("Reworked the hero around brain/hands."); // recap
    expect(dom.textContent).toContain("landing.html"); // artifact
    // Per-tool labels stay collapsed in Simple mode.
    expect(dom.textContent).not.toMatch(/^Patch$/m);
  });

  it("labels arbor research tools in Simple mode instead of raw tool names", () => {
    const messages: ChatMessage[] = [
      {
        id: "iter-0",
        role: "assistant",
        content: "",
        createdAt: new Date(),
        status: "complete",
        turnId: "t-arbor",
        iterationIndex: 0,
        toolCalls: [
          {
            id: "a1",
            toolName: "idea_tree",
            args: JSON.stringify({ action: "report" }),
            status: "complete",
            result: "{}",
          },
        ],
      },
    ];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        terminal={true}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    // Friendly label + action detail, not the raw snake_case tool name.
    expect(dom.textContent).toContain("Idea tree");
    expect(dom.textContent).not.toContain("idea_tree");
  });

  it("Expert mode renders the per-tool timeline and hides the TurnSummaryCard", () => {
    const messages = [assistantMessage()];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        terminal={true}
        onSend={noop}
        onStop={noop}
        viewMode="expert"
      />,
    );
    expect(dom.textContent).toContain("Patch");
    // TurnSummaryCard is hidden in Expert.
    expect(dom.textContent).not.toContain(
      "Reworked the hero around brain/hands.",
    );
  });

  it("Simple mode hides skill.invoked system markers", () => {
    const messages: ChatMessage[] = [
      {
        id: "sys-1",
        role: "system",
        content: "frontend-design",
        createdAt: new Date(),
        status: "complete",
        systemKind: "skill_invoked",
        systemMeta: { skill: "frontend-design", staged_at: null },
      },
      assistantMessage({ id: "iter-0" }),
    ];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        terminal={true}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expect(dom.textContent).not.toContain("frontend-design");
    // Skill markers render as the OrphanSystemMarker green dot — that
    // wrapper must not be present either.
    expect(dom.querySelector(".bg-emerald-500")).toBeNull();
  });

  it("renders synthetic file-artifact cards even when turn.summary is missing", () => {
    const messages: ChatMessage[] = [
      {
        id: "iter-0",
        role: "assistant",
        content: "Done.",
        createdAt: new Date(),
        status: "complete",
        turnId: "t-2",
        iterationIndex: 0,
        toolCalls: [
          {
            id: "c1",
            toolName: "write_file",
            args: JSON.stringify({ path: "reports/Summary.docx" }),
            status: "complete",
            result: "{}",
          },
        ],
      },
    ];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        terminal={true}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    // No turn.summary on the tail message, but the synthetic
    // derivation should still produce a download card.
    expect(dom.textContent).toContain("Summary.docx");
    expect(dom.textContent).toContain("Word document");
    const downloadAnchor = Array.from(dom.querySelectorAll("a"))
      .find((a) => a.textContent?.includes("Download"));
    expect(downloadAnchor).toBeDefined();
  });

  it("prefers turn.summary artifacts over the synthetic derivation when both exist", () => {
    const messages: ChatMessage[] = [
      {
        id: "iter-0",
        role: "assistant",
        content: "Done.",
        createdAt: new Date(),
        status: "complete",
        turnId: "t-3",
        iterationIndex: 0,
        toolCalls: [
          {
            id: "c1",
            toolName: "write_file",
            args: JSON.stringify({ path: "fallback.docx" }),
            status: "complete",
            result: "{}",
          },
        ],
        turnSummary: {
          turnId: "t-3",
          recap: "Crafted the deliverable.",
          artifacts: [
            { kind: "file", label: "Final.docx", ref: "final.docx" },
          ],
        },
      },
    ];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        terminal={true}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    // Only the harness-summary file gets a WorkspaceFileCard
    // (Download anchor). The synthetic candidate (fallback.docx) is
    // dropped because the harness summary already named its picks;
    // the IterationGroup header may still mention fallback.docx as
    // a tool-derived label, but no download card is built for it.
    const downloadNames = Array.from(dom.querySelectorAll("a"))
      .filter((a) => a.textContent?.includes("Download"))
      .map((a) => a.getAttribute("download"));
    expect(downloadNames).toEqual(["final.docx"]);
  });

  it("does not show a TurnSummaryPending skeleton while waiting for an optional summary", () => {
    // Reproduces the post-text-only-response window: the harness has
    // emitted ``llm.response`` (tail.status = complete, finalText set,
    // no tool calls → reducer flipped isRunning to false) but has not
    // yet emitted ``turn.summary`` or ``session.complete`` (terminal
    // still false). The summary event is optional, so showing an
    // inferred loading skeleton causes a flash when no summary lands.
    const messages: ChatMessage[] = [
      {
        id: "final",
        role: "assistant",
        content: "Here's the answer.",
        createdAt: new Date(),
        status: "complete",
        turnId: "t-1",
        iterationIndex: 0,
      },
    ];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expect(dom.textContent).not.toContain("Summarizing conversation");
  });

  it("hides the TurnSummaryPending skeleton once the session is terminal", () => {
    // Same shape as above but with terminal=true (session.complete /
    // done / fail arrived without a turn.summary). The skeleton must
    // disappear so historic sessions don't display a perpetual
    // placeholder.
    const messages: ChatMessage[] = [
      {
        id: "final",
        role: "assistant",
        content: "Here's the answer.",
        createdAt: new Date(),
        status: "complete",
        turnId: "t-1",
        iterationIndex: 0,
      },
    ];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        terminal={true}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expect(dom.textContent).not.toContain("Summarizing conversation");
  });

  it("Simple mode shows a tool-derived collapsed label when an iteration has no summary", () => {
    const messages = [
      assistantMessage({
        id: "iter-0",
        iterationSummary: undefined,
      }),
    ];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={false}
        terminal={true}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    // Shows a derived label (the human tool name) instead of the LLM
    // summary; the underlying per-tool detail stays collapsed until
    // the user clicks the row.
    expect(dom.textContent).toContain("Patch");
    expect(dom.textContent).not.toContain("Reworked the hero copy");
    // The summary row is collapsed by default.
    expect(dom.querySelector("button[aria-expanded='false']"))
      .not.toBeNull();
  });

  it("shows 'Working on it...' in the between-iterations gap", () => {
    // Previous tool-bearing iteration finished (all tool calls resolved)
    // but the next llm.response hasn't landed yet. Tool-using messages
    // stay tagged status="streaming" per the reducer, so the iteration
    // row is collapsed/complete. While isRunning is still true, the
    // group-level shimmer must surface so users see continued progress.
    const messages: ChatMessage[] = [
      {
        id: "user-1",
        role: "user",
        content: "Build me a thing",
        createdAt: new Date(),
        status: "complete",
      },
      {
        id: "iter-0",
        role: "assistant",
        content: "",
        createdAt: new Date(),
        status: "streaming",
        turnId: "t-1",
        iterationIndex: 0,
        iterationSummary: {
          iterationIndex: 0,
          summary: "Edited the hero copy",
          toolCallIds: ["c1"],
          startedAt: "",
          endedAt: "",
        },
        toolCalls: [
          { id: "c1", toolName: "patch", args: "{}", status: "complete", result: "{}" },
        ],
      },
    ];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={true}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expect(dom.textContent).toContain("Working on it");
  });

  it("shows one thread-level 'Working on it...' indicator while running", () => {
    const messages: ChatMessage[] = [
      {
        id: "user-1",
        role: "user",
        content: "First question",
        createdAt: new Date(),
        status: "complete",
      },
      {
        id: "assistant-1",
        role: "assistant",
        content: "First answer.",
        createdAt: new Date(),
        status: "complete",
      },
      {
        id: "user-2",
        role: "user",
        content: "Follow up",
        createdAt: new Date(),
        status: "complete",
      },
      {
        id: "assistant-2",
        role: "assistant",
        content: "Second answer.",
        createdAt: new Date(),
        status: "complete",
      },
    ];

    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={true}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );

    const indicators = dom.textContent?.match(/Working on it/g) ?? [];
    expect(indicators).toHaveLength(1);
  });

  it("shows 'Working on it...' after the last message when the tail is user", () => {
    const messages: ChatMessage[] = [
      {
        id: "user-1",
        role: "user",
        content: "First question",
        createdAt: new Date(),
        status: "complete",
      },
      {
        id: "assistant-1",
        role: "assistant",
        content: "First answer.",
        createdAt: new Date(),
        status: "complete",
      },
      {
        id: "user-2",
        role: "user",
        content: "Follow up",
        createdAt: new Date(),
        status: "complete",
      },
    ];

    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={true}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );

    const indicators = dom.textContent?.match(/Working on it/g) ?? [];
    expect(indicators).toHaveLength(1);
    expect(dom.textContent).toMatch(/Follow up.*Working on it/s);
  });

  it("does not flash 'Working on it...' for brief running transitions", () => {
    vi.useFakeTimers();
    const messages: ChatMessage[] = [
      {
        id: "user-1",
        role: "user",
        content: "Question",
        createdAt: new Date(),
        status: "complete",
      },
      {
        id: "assistant-1",
        role: "assistant",
        content: "Answer.",
        createdAt: new Date(),
        status: "complete",
      },
    ];
    const render = (isRunning: boolean) => (
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={isRunning}
        terminal={!isRunning}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />
    );

    const dom = mount(render(false));
    expect(dom.textContent).not.toContain("Working on it");

    rerender(render(true));
    expect(dom.textContent).not.toContain("Working on it");

    rerender(render(false));
    act(() => {
      vi.runOnlyPendingTimers();
    });
    expect(dom.textContent).not.toContain("Working on it");
  });

  it("shows 'Working on it...' when a running transition lasts past the delay", () => {
    vi.useFakeTimers();
    const messages: ChatMessage[] = [
      {
        id: "user-1",
        role: "user",
        content: "Question",
        createdAt: new Date(),
        status: "complete",
      },
      {
        id: "assistant-1",
        role: "assistant",
        content: "Answer.",
        createdAt: new Date(),
        status: "complete",
      },
    ];
    const render = (isRunning: boolean) => (
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={isRunning}
        terminal={!isRunning}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />
    );

    const dom = mount(render(false));
    rerender(render(true));
    act(() => {
      vi.advanceTimersByTime(250);
    });

    expect(dom.textContent).toContain("Working on it");
  });

  it("shows 'Working on it...' below complete preamble text while isRunning", () => {
    // The tail iteration emitted text-only content ("Let me write the
    // script:") and flipped to status="complete", but isRunning is
    // still true because the agent will call tools next. The Simple
    // mode group shimmer must surface below SimpleFinalAnswer so the
    // user sees the agent is still working — Expert mode appends an
    // equivalent thinking entry in the same window.
    const messages: ChatMessage[] = [
      {
        id: "user-1",
        role: "user",
        content: "create a powerpoint",
        createdAt: new Date(),
        status: "complete",
      },
      {
        id: "iter-0",
        role: "assistant",
        content: "Now I'll create the presentation. Let me write the script:",
        createdAt: new Date(),
        status: "complete",
        turnId: "t-1",
        iterationIndex: 0,
      },
    ];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={true}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expect(dom.textContent).toContain("Let me write the script:");
    expect(dom.textContent).toContain("Working on it");
  });

  it("shows 'Working on it...' alongside a mid-stream text tail", () => {
    // The reducer keeps the message in status="streaming" until the
    // closing llm.response lands, but llm.delta cadence can stop long
    // before that (the model is still reasoning / about to call a
    // tool). Showing the shimmer below the streamed text matches
    // Expert mode, which appends a thinking entry in the same window.
    const messages: ChatMessage[] = [
      {
        id: "user-1",
        role: "user",
        content: "hi",
        createdAt: new Date(),
        status: "complete",
      },
      {
        id: "iter-0",
        role: "assistant",
        content: "Half a sentence so f",
        createdAt: new Date(),
        status: "streaming",
        turnId: "t-1",
        iterationIndex: 0,
      },
    ];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={true}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    expect(dom.textContent).toContain("Working on it");
  });

  it("renders skill-only iterations so reasoning stays accessible", () => {
    // Previously these iterations were hidden outright, leaving Simple
    // mode showing only "Working on it..." while the model was producing
    // useful reasoning + a meaningful iteration summary. We now surface
    // them as a normal collapsible row so the user can read the
    // reasoning on demand — same access as Expert mode's "Thought
    // for a few seconds" entries.
    const messages: ChatMessage[] = [
      {
        id: "user-1",
        role: "user",
        content: "create a pptx",
        createdAt: new Date(),
        status: "complete",
      },
      {
        id: "iter-0",
        role: "assistant",
        content: "",
        reasoning: "Let me load the pptx skill first.",
        createdAt: new Date(),
        status: "streaming",
        turnId: "t-1",
        iterationIndex: 0,
        iterationSummary: {
          iterationIndex: 0,
          summary: "Loaded pptx skill",
          toolCallIds: ["c1"],
          startedAt: "",
          endedAt: "",
        },
        toolCalls: [
          { id: "c1", toolName: "skill_view", args: "{}", status: "complete", result: "{}" },
        ],
      },
    ];
    const dom = mount(
      <ChatThread
        sessionId="s-1"
        messages={messages}
        isRunning={true}
        terminal={false}
        onSend={noop}
        onStop={noop}
        viewMode="simple"
      />,
    );
    // The iteration summary label surfaces (collapsed row).
    expect(dom.textContent).toContain("Loaded pptx skill");
    // The shimmer also still appears below — the iteration is complete
    // but the session is still running.
    expect(dom.textContent).toContain("Working on it");
  });
});
