import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useMissionEvents } from "../src/components/missions/use-mission-events";
import type {
  AgentChatAdapter,
  AgentChatMissionEvent,
  AgentChatMissionEventsPage,
} from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  root = null;
  container = null;
});

function makeEvent(id: number): AgentChatMissionEvent {
  return {
    id,
    sessionId: "coord-1",
    type: "iteration.summary",
    data: { summary: `event ${id}` },
    createdAt: "2026-06-11T12:00:00Z",
  };
}

function Probe({ adapter }: { adapter: AgentChatAdapter }) {
  const feed = useMissionEvents({
    adapter,
    missionId: "m1",
    missionStatus: "active",
    isTerminal: false,
    pollIntervalMs: 60_000,
  });
  return (
    <div>
      <span data-testid="supported">{String(feed.supported)}</span>
      <span data-testid="count">{feed.events.length}</span>
      <span data-testid="sessions">{Object.keys(feed.sessions).length}</span>
    </div>
  );
}

async function mount(adapter: AgentChatAdapter) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(<Probe adapter={adapter} />);
  });
  // Flush the async backfill loop.
  await act(async () => {
    await Promise.resolve();
  });
}

function text(testId: string): string {
  return (
    container!.querySelector(`[data-testid="${testId}"]`)!.textContent ?? ""
  );
}

describe("useMissionEvents", () => {
  it("backfills with the after_id cursor until a short page", async () => {
    const pageOne: AgentChatMissionEventsPage = {
      events: Array.from({ length: 500 }, (_, i) => makeEvent(i + 1)),
      sessions: {
        "coord-1": { taskId: null, agentDefName: null, kind: "coordinator" },
      },
    };
    const pageTwo: AgentChatMissionEventsPage = {
      events: [makeEvent(501)],
      sessions: {
        "tsess-1": {
          taskId: "T1",
          agentDefName: "claude-coder",
          kind: "task",
        },
      },
    };
    const listMissionEvents = vi
      .fn<
        (input: {
          missionId: string;
          afterId?: number;
          limit?: number;
        }) => Promise<AgentChatMissionEventsPage>
      >()
      .mockResolvedValueOnce(pageOne)
      .mockResolvedValueOnce(pageTwo);
    const adapter = { listMissionEvents } as unknown as AgentChatAdapter;

    await mount(adapter);

    expect(listMissionEvents).toHaveBeenCalledTimes(2);
    expect(listMissionEvents.mock.calls[0][0]).toMatchObject({
      missionId: "m1",
      afterId: undefined,
      limit: 500,
    });
    expect(listMissionEvents.mock.calls[1][0]).toMatchObject({
      afterId: 500,
      limit: 500,
    });
    expect(text("count")).toBe("501");
    // sessions maps merge across pages
    expect(text("sessions")).toBe("2");
  });

  it("reports unsupported (and fetches nothing) without the adapter method", async () => {
    const adapter = {} as AgentChatAdapter;
    await mount(adapter);
    expect(text("supported")).toBe("false");
    expect(text("count")).toBe("0");
  });

  it("keeps the last good feed when a fetch rejects", async () => {
    const listMissionEvents = vi.fn().mockRejectedValue(new Error("boom"));
    const adapter = { listMissionEvents } as unknown as AgentChatAdapter;
    await mount(adapter);
    expect(text("supported")).toBe("true");
    expect(text("count")).toBe("0");
  });
});
