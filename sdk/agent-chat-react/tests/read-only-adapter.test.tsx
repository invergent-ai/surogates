import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentChatAdapterProvider } from "../src/adapter-context";
import { AgentChat } from "../src/agent-chat";
import { ArtifactImage } from "../src/components/chat/artifacts/artifact-media";
import { ReadOnlyChatError, readOnlyAdapter } from "../src/read-only-adapter";
import type {
  AgentChatAdapter,
  AgentChatEventStream,
  AgentChatEventType,
  AgentChatSession,
  AgentChatSseMessageEvent,
} from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

class FakeEventStream implements AgentChatEventStream {
  onerror: (() => void) | null = null;
  readonly listeners = new Map<
    AgentChatEventType,
    Array<(event: AgentChatSseMessageEvent) => void>
  >();

  addEventListener(
    type: AgentChatEventType,
    listener: (event: AgentChatSseMessageEvent) => void,
  ): void {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  close(): void {}

  emit(type: AgentChatEventType, eventId: number, data: Record<string, unknown>) {
    const event = {
      data: JSON.stringify(data),
      lastEventId: String(eventId),
    };
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

async function getSession(input: { sessionId: string }): Promise<AgentChatSession> {
  return { id: input.sessionId, status: "active" };
}

/** The adapter over a source that has only the two reads every source has. */
function watching(stream = new FakeEventStream()) {
  return readOnlyAdapter({ getSession, openEventStream: () => stream });
}

/**
 * Watches a turn that wrote a file and ended, with no composer, and answers what the render threw
 * past the chat, if anything.
 */
async function watchTurnThatWroteAFile(adapter: AgentChatAdapter, stream: FakeEventStream): Promise<unknown[]> {
  const thrown: unknown[] = [];
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container, { onUncaughtError: (error) => thrown.push(error) });
  await act(async () => {
    root?.render(<AgentChat adapter={adapter} sessionId="s-1" hideComposer />);
    await Promise.resolve();
  });
  await act(async () => {
    stream.emit("user.message", 1, { content: "Write the report" });
    stream.emit("llm.request", 2, {});
    stream.emit("tool.call", 3, { tool_call_id: "w-1", name: "write_file", arguments: { path: "reports/Summary.docx", content: "Both payslips match." } });
    stream.emit("tool.result", 4, { tool_call_id: "w-1", name: "write_file", content: "{}" });
    stream.emit("llm.response", 5, { message: { content: "The report is written." } });
    stream.emit("session.done", 6, {});
    await Promise.resolve();
  });
  return thrown;
}

async function refusal(call: () => Promise<unknown>): Promise<unknown> {
  try {
    await call();
  } catch (error) {
    return error;
  }
  throw new Error("the call was not refused");
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

describe("readOnlyAdapter", () => {
  it("the source's reads are the adapter's", async () => {
    const stream = new FakeEventStream();
    const page = { events: [{ id: 3, type: "user.message", data: { content: "Check the payslips" } }], hasMore: false };
    const adapter = readOnlyAdapter({
      getSession,
      openEventStream: () => stream,
      pollEvents: async () => page,
    });
    await expect(adapter.getSession({ sessionId: "s-1" })).resolves.toEqual({ id: "s-1", status: "active" });
    expect(adapter.openEventStream({ sessionId: "s-1", after: 0 })).toBe(stream);
    await expect(adapter.pollEvents?.({ sessionId: "s-1", after: 0 })).resolves.toBe(page);
  });

  it("every write refuses as read-only", async () => {
    const adapter = watching();
    const writes: Array<[string, () => Promise<unknown>]> = [
      ["sendMessage", () => adapter.sendMessage({ sessionId: "s-1", content: "Approve it" })],
      ["createSession", () => adapter.createSession({})],
      ["pauseSession", () => adapter.pauseSession({ sessionId: "s-1" })],
      ["retrySession", () => adapter.retrySession({ sessionId: "s-1" })],
      ["submitAskUserQuestionResponse", () => adapter.submitAskUserQuestionResponse({ sessionId: "s-1", toolCallId: "ask-1", responses: [] })],
      ["uploadWorkspaceFile", () => adapter.uploadWorkspaceFile({ sessionId: "s-1", file: new File(["x"], "x.txt") })],
      ["deleteWorkspaceFile", () => adapter.deleteWorkspaceFile({ sessionId: "s-1", path: "x.txt" })],
      ["acquireBrowserControl", () => adapter.acquireBrowserControl("s-1")],
      ["releaseBrowserControl", () => adapter.releaseBrowserControl("s-1")],
    ];
    for (const [name, write] of writes) {
      const error = await refusal(write);
      expect(error, name).toBeInstanceOf(ReadOnlyChatError);
      expect((error as Error).message, name).toBe("This chat is read-only.");
    }
    // What a chat draws as it goes asks for no write: a URL it cannot have is none.
    expect(adapter.browserShellUrl("s-1")).toBe("");
    expect(adapter.getWorkspaceDownloadUrl({ sessionId: "s-1", path: "reports/Summary.docx" })).toBe("");
  });

  it("a source's download URL is the adapter's", async () => {
    const stream = new FakeEventStream();
    const adapter = readOnlyAdapter({
      getSession,
      openEventStream: () => stream,
      getWorkspaceDownloadUrl: ({ sessionId, path }) => `/files/${sessionId}/${path}`,
    });
    expect(await watchTurnThatWroteAFile(adapter, stream)).toEqual([]);
    const download = container!.querySelector<HTMLAnchorElement>("a[download]");
    expect(download?.textContent).toBe("Download");
    expect(download?.getAttribute("href")).toBe("/files/s-1/reports/Summary.docx");
  });

  it("a watched turn that wrote a file shows without a download", async () => {
    const stream = new FakeEventStream();
    expect(await watchTurnThatWroteAFile(watching(stream), stream)).toEqual([]);
    expect(container!.textContent).toContain("Write the report");
    expect(container!.textContent).toContain("The report is written.");
    expect(container!.textContent).toContain("Summary");
    expect(container!.querySelector("a[download]")).toBeNull();
    expect(container!.textContent).not.toContain("Download");
  });

  it("a watched image with no download URL shows its file's name, and no image", () => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root?.render(
        <AgentChatAdapterProvider value={{ adapter: watching(), sessionId: "s-1" }}>
          <ArtifactImage sessionId="s-1" spec={{ path: "charts/payslips.png", caption: "Both payslips" }} />
        </AgentChatAdapterProvider>,
      );
    });
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toBe("payslips.png");
  });

  it("a write the source carries is not passed through", async () => {
    const sendMessage = vi.fn(async () => ({ eventId: 9, status: "accepted" }));
    const source = { getSession, openEventStream: () => new FakeEventStream(), sendMessage };
    const adapter = readOnlyAdapter(source);
    await expect(adapter.sendMessage({ sessionId: "s-1", content: "Approve it" })).rejects.toBeInstanceOf(ReadOnlyChatError);
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("features the source lacks stay off", async () => {
    const adapter = watching();
    expect(adapter.defineOutcome).toBeUndefined();
    expect(adapter.stopSession).toBeUndefined();
    expect(adapter.listMissions).toBeUndefined();
    expect(adapter.listSlashCommands).toBeUndefined();
    expect(adapter.getWorkspaceEditorConfig).toBeUndefined();
    await expect(adapter.getBrowserState("s-1")).resolves.toBeNull();
  });

  it("a session watched through a read-only adapter shows with no composer", async () => {
    vi.useFakeTimers();
    const rejections: unknown[] = [];
    const onRejection = (reason: unknown) => {
      rejections.push(reason);
    };
    process.on("unhandledRejection", onRejection);
    try {
      const stream = new FakeEventStream();
      container = document.createElement("div");
      document.body.appendChild(container);
      root = createRoot(container);
      act(() => {
        root?.render(<AgentChat adapter={watching(stream)} sessionId="s-1" hideComposer />);
      });
      act(() => {
        stream.emit("user.message", 1, { content: "Check the payslips" });
        stream.emit("llm.response", 2, { message: { content: "Both payslips match." } });
      });
      // Past the chat's first reconcile, which polls for what the stream missed: this source has no poll.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(4500);
      });
      vi.useRealTimers();
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(container.textContent).toContain("Check the payslips");
      expect(container.textContent).toContain("Both payslips match.");
      expect(container.querySelector("textarea")).toBeNull();
      expect(rejections).toEqual([]);
    } finally {
      process.off("unhandledRejection", onRejection);
    }
  });
});
