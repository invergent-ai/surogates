/**
 * TurnSummaryCard — per-turn recap + curated artifact list rendered
 * below the final assistant text in Simple mode.
 *
 * Verifies:
 * - hidden when both recap and artifacts are empty
 * - recap text rendering
 * - file artifacts dispatch onFileSelect
 * - url artifacts render as external anchors with noopener
 * - command artifacts dispatch onCommandSelect when wired, else
 *   render as plain text
 * - artifact artifacts resolve against the session's artifact.created
 *   system messages and render the existing ArtifactBlock when a
 *   match is found, falling back to plain text otherwise.
 */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentChatAdapterProvider, NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { TurnSummaryCard } from "../src/components/chat/turn-summary-card";
import { TooltipProvider } from "../src/components/ui/tooltip";
import type {
  AgentChatAdapter,
  AgentChatTurnSummary,
  ChatMessage,
} from "../src/types";

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
    getArtifact: vi.fn().mockResolvedValue({
      meta: {
        artifact_id: "a-1",
        session_id: "s-1",
        name: "Report",
        kind: "markdown",
        version: 1,
        size: 12,
        created_at: "2026-01-01T00:00:00Z",
      },
      kind: "markdown",
      spec: { content: "" },
    }),
  } as unknown as AgentChatAdapter;
}

function summaryWith(
  over: Partial<AgentChatTurnSummary> = {},
): AgentChatTurnSummary {
  return {
    turnId: "t-1",
    recap: "Reworked the hero around brain/hands.",
    artifacts: [],
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


describe("TurnSummaryCard", () => {
  it("renders nothing when artifacts is empty (even if recap has text)", () => {
    const dom = mount(
      <TurnSummaryCard
        summary={summaryWith()}
        sessionId="s-1"
        messages={[]}
      />,
    );
    expect(dom.textContent ?? "").toBe("");
  });

  it("renders the recap text when at least one artifact is present", () => {
    const dom = mount(
      <TurnSummaryCard
        summary={summaryWith({
          artifacts: [
            { kind: "url", label: "example.com", ref: "https://example.com" },
          ],
        })}
        sessionId="s-1"
        messages={[]}
      />,
    );
    expect(dom.textContent).toMatch(/Reworked the hero/);
  });

  it("renders file artifacts as WorkspaceFileCard with Download button", () => {
    // File artifacts no longer route through onFileSelect — they
    // render as the Claude-style WorkspaceFileCard. The download
    // anchor carries the workspace download URL and the filename.
    const dom = mount(
      <TurnSummaryCard
        summary={summaryWith({
          recap: "",
          artifacts: [
            { kind: "file", label: "landing.html", ref: "landing.html" },
          ],
        })}
        sessionId="s-1"
        messages={[]}
      />,
    );
    expect(dom.textContent).toContain("landing.html");
    expect(dom.textContent).toContain("HTML");
    const downloadAnchor = Array.from(dom.querySelectorAll("a"))
      .find((a) => a.textContent?.includes("Download"));
    expect(downloadAnchor).toBeDefined();
    expect(downloadAnchor!.getAttribute("download")).toBe("landing.html");
  });

  it("renders url artifacts as external anchors with noopener", () => {
    const dom = mount(
      <TurnSummaryCard
        summary={summaryWith({
          recap: "",
          artifacts: [
            { kind: "url", label: "example.com", ref: "https://example.com" },
          ],
        })}
        sessionId="s-1"
        messages={[]}
      />,
    );
    const anchor = dom.querySelector("a");
    expect(anchor).not.toBeNull();
    expect(anchor!.getAttribute("href")).toBe("https://example.com");
    expect(anchor!.getAttribute("target")).toBe("_blank");
    expect(anchor!.getAttribute("rel")).toContain("noopener");
  });

  it("dispatches onCommandSelect for command artifacts when wired", () => {
    const onCommandSelect = vi.fn();
    const dom = mount(
      <TurnSummaryCard
        summary={summaryWith({
          recap: "",
          artifacts: [{ kind: "command", label: "ls -la", ref: "tc-1" }],
        })}
        sessionId="s-1"
        messages={[]}
        onCommandSelect={onCommandSelect}
      />,
    );
    const btn = Array.from(dom.querySelectorAll("button")).find(
      (b) => b.textContent === "ls -la",
    );
    expect(btn).toBeDefined();
    act(() => btn!.click());
    expect(onCommandSelect).toHaveBeenCalledWith("tc-1");
  });

  it("falls back to plain text for command artifacts when no resolver is wired", () => {
    const dom = mount(
      <TurnSummaryCard
        summary={summaryWith({
          recap: "",
          artifacts: [{ kind: "command", label: "ls -la", ref: "tc-1" }],
        })}
        sessionId="s-1"
        messages={[]}
      />,
    );
    expect(dom.textContent).toContain("ls -la");
    expect(dom.querySelector("a")).toBeNull();
    expect(dom.querySelector("button")).toBeNull();
  });

  it("resolves artifact refs against artifact.created system messages and renders ArtifactBlock", () => {
    const messages: ChatMessage[] = [
      {
        id: "m1",
        role: "system",
        content: "Report",
        createdAt: new Date(),
        status: "complete",
        systemKind: "artifact",
        systemMeta: {
          artifact_id: "a-1",
          name: "Report",
          kind: "markdown",
          version: 3,
        },
      },
    ];
    const dom = mount(
      <TurnSummaryCard
        summary={summaryWith({
          recap: "",
          artifacts: [
            { kind: "artifact", label: "Report", ref: "a-1" },
          ],
        })}
        sessionId="s-1"
        messages={messages}
      />,
    );
    // ArtifactBlock renders a header with the artifact name + version.
    expect(dom.textContent).toContain("Report");
    // The plain-text fallback path would render JUST the label; verify
    // we got the rich block (it includes additional chrome).
    expect(dom.querySelector("button, a, [role='button']")).not.toBeNull();
  });

  it("falls back to plain text when the artifact ref cannot be resolved", () => {
    const dom = mount(
      <TurnSummaryCard
        summary={summaryWith({
          recap: "",
          artifacts: [
            { kind: "artifact", label: "Stale", ref: "a-missing" },
          ],
        })}
        sessionId="s-1"
        messages={[]}
      />,
    );
    expect(dom.textContent).toContain("Stale");
    // No interactive control because the resolver failed.
    expect(dom.querySelector("button, a")).toBeNull();
  });
});
