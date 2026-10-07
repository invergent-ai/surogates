import { describe, expect, it } from "vitest";

import { type Api, Burst, followInbox, type InboxItem } from "../src/shell/agent-events.js";

// The agent's routes as the app reads them: each stream opened is kept, for the test to send on or end.
class FakeAgent {
  readonly streams: Array<{ path: string; send(text: string): void; end(): void; signal: AbortSignal }> = [];
  readonly items = new Map<number, Record<string, unknown>>();
  status = 200; // what the next stream opens with
  readonly api: Api = async (path, init) => {
    const item = /^\/api\/v1\/inbox\/(\d+)\?agent_id=a$/.exec(path);
    if (item) {
      const found = this.items.get(Number(item[1]));
      return found ? Response.json(found) : new Response(null, { status: 404 });
    }
    if (this.status !== 200) {
      const status = this.status;
      this.status = 200;
      return new Response(null, { status });
    }
    const signal = init!.signal!;
    let open!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start: (controller) => {
        open = controller;
      },
    });
    signal.addEventListener("abort", () => open.error(signal.reason));
    this.streams.push({ path, send: (text) => open.enqueue(new TextEncoder().encode(text)), end: () => open.close(), signal });
    return new Response(body);
  };

  pending(id: number, kind: string, title: string, sessionId = "s-1"): void {
    this.items.set(id, { id, kind, title, status: "pending", session_id: sessionId });
  }
}

async function until(done: () => boolean): Promise<void> {
  for (let tries = 0; !done(); tries++) {
    if (tries > 200) throw new Error("Never happened");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

function following(agent: FakeAgent, silenceMs = 10_000) {
  const told: InboxItem[] = [];
  const errors: string[] = [];
  const stop = followInbox({
    api: agent.api, agentId: "a", onError: (error) => errors.push(String(error)), delayMs: () => 0, silenceMs, onItem: (item) => told.push(item),
  });
  return { told, errors, stop };
}

describe("the inbox, followed", () => {
  it("tells each item that comes, once, as the agent has it, and not what the inbox held when it opened", async () => {
    const agent = new FakeAgent();
    agent.pending(1, "input_required", "Already there");
    agent.pending(2, "input_required", "Which report?");
    agent.items.set(3, { id: 3, kind: "task_complete", title: "Answered", status: "responded", session_id: "s-1" });
    const { told, stop } = following(agent);
    await until(() => agent.streams.length === 1);
    expect(agent.streams[0]!.path).toBe("/api/v1/inbox/stream?agent_id=a");
    agent.streams[0]!.send('event: snapshot\r\ndata: {"unread_ids": [1]}\r\n\r\n');
    for (const id of [2, 2, 3]) agent.streams[0]!.send(`event: item\r\ndata: {"item_id": ${id}, "kind": "input_required"}\r\n\r\n`);
    await settle();
    expect(told).toEqual([{ id: 2, kind: "input_required", title: "Which report?", sessionId: "s-1" }]);
    stop();
  });

  it("tells, once it is back, what came while the agent was out of reach", async () => {
    const agent = new FakeAgent();
    agent.pending(1, "task_complete", "Draft");
    const { told, errors, stop } = following(agent);
    await until(() => agent.streams.length === 1);
    agent.streams[0]!.send('event: snapshot\r\ndata: {"unread_ids": [1]}\r\n\r\n');
    agent.streams[0]!.end();
    agent.pending(4, "governance_gate", "Delete the old drafts?");
    await until(() => agent.streams.length === 2);
    agent.streams[1]!.send('event: snapshot\r\ndata: {"unread_ids": [1, 4]}\r\n\r\n');
    await settle();
    expect(told.map((item) => item.id)).toEqual([4]);
    expect(errors).toEqual([]);
    stop();
  });

  it("opens a stream again that stays silent past the agent's pings, and says why one did not open", async () => {
    const agent = new FakeAgent();
    agent.status = 503;
    const { errors, stop } = following(agent, 50);
    await until(() => agent.streams.length === 1);
    expect(errors).toEqual(["Error: The agent did not open /api/v1/inbox/stream?agent_id=a (HTTP 503)"]);
    agent.streams[0]!.send(": ping\r\n\r\n");
    await until(() => agent.streams.length === 2);
    expect(agent.streams[0]!.signal.aborted).toBe(true);
    expect(errors).toHaveLength(1);
    stop();
  });

  it("stops for good", async () => {
    const agent = new FakeAgent();
    const { stop } = following(agent);
    await until(() => agent.streams.length === 1);
    stop();
    await settle();
    expect([agent.streams.length, agent.streams[0]!.signal.aborted]).toEqual([1, true]);
  });
});

describe("a burst of inbox items", () => {
  it("holds the items that come within its window of the one before, and starts again after a pause", () => {
    let now = 0;
    const burst = new Burst(5_000, () => now);
    const item = (id: number): InboxItem => ({ id, kind: "input_required", title: `Question ${id}`, sessionId: `s-${id}` });
    expect(burst.add(item(1)).map((each) => each.id)).toEqual([1]);
    now = 4_000;
    expect(burst.add(item(2)).map((each) => each.id)).toEqual([1, 2]);
    // Within five seconds of the one before, however long the burst has run.
    now = 8_500;
    expect(burst.add(item(3)).map((each) => each.id)).toEqual([1, 2, 3]);
    now = 13_501;
    expect(burst.add(item(4)).map((each) => each.id)).toEqual([4]);
  });
});
