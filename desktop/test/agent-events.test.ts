import { describe, expect, it, vi } from "vitest";

import { type Api, Burst, followChat, followInbox, followTurns, type InboxItem, restOf } from "../src/shell/agent-events.js";

// The agent's routes as the app reads them: each stream opened is kept, for the test to send on or end.
class FakeAgent {
  readonly streams: Array<{ path: string; send(text: string): void; end(): void; signal: AbortSignal }> = [];
  readonly items = new Map<number, Record<string, unknown>>();
  readonly titles = new Map<string, string>();
  readonly statuses = new Map<string, string | 404>();
  readonly trees = new Map<string, string[]>(); // each chat's tree's statuses, the chat's own first
  status = 200; // what the next stream opens with
  itemFailure: number | "throw" | null = null; // how the next item's read fails: an HTTP status, or no answer at all
  readonly api: Api = async (path, init) => {
    const item = /^\/api\/v1\/inbox\/(\d+)\?agent_id=a$/.exec(path);
    if (item) {
      const failure = this.itemFailure;
      this.itemFailure = null;
      if (failure === "throw") throw new TypeError("fetch failed");
      if (failure !== null) return new Response(null, { status: failure });
      const found = this.items.get(Number(item[1]));
      return found ? Response.json(found) : new Response(null, { status: 404 });
    }
    const tree = /^\/api\/v1\/sessions\/([^/?]+)\/tree\?agent_id=a$/.exec(path);
    if (tree) {
      const statuses = this.trees.get(tree[1]!);
      if (statuses === undefined) return new Response(null, { status: 404 });
      return Response.json({ nodes: statuses.map((status, i) => ({ id: `${tree[1]}-${i}`, status })), total: statuses.length });
    }
    const chat = /^\/api\/v1\/sessions\/([^/?]+)\?agent_id=a$/.exec(path);
    if (chat) {
      if (this.statuses.get(chat[1]!) === 404) return new Response(null, { status: 404 });
      return Response.json({ id: chat[1], title: this.titles.get(chat[1]!) ?? null, status: this.statuses.get(chat[1]!) ?? "active" });
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

  it("tells, once it is back, an item whose read failed, and never one the agent did not have", async () => {
    const agent = new FakeAgent();
    agent.pending(2, "input_required", "Which report?");
    agent.pending(3, "governance_gate", "Delete the old drafts?");
    const { told, stop } = following(agent);
    await until(() => agent.streams.length === 1);
    agent.streams[0]!.send('event: snapshot\r\ndata: {"unread_ids": []}\r\n\r\n');
    // As the agent restarts: one read answers 503, one gets no answer, and one is of an item it does not have.
    agent.itemFailure = 503;
    agent.streams[0]!.send('event: item\r\ndata: {"item_id": 2}\r\n\r\n');
    await settle();
    agent.itemFailure = "throw";
    agent.streams[0]!.send('event: item\r\ndata: {"item_id": 3}\r\n\r\n');
    await settle();
    agent.streams[0]!.send('event: item\r\ndata: {"item_id": 4}\r\n\r\n');
    await settle();
    expect(told).toEqual([]);
    agent.pending(4, "input_required", "Made after its read");
    agent.streams[0]!.end();
    await until(() => agent.streams.length === 2);
    agent.streams[1]!.send('event: snapshot\r\ndata: {"unread_ids": [2, 3, 4]}\r\n\r\n');
    await settle();
    expect(told.map((item) => item.id)).toEqual([2, 3]);
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

  it("backs off a stream that keeps closing as it opens", async () => {
    const agent = new FakeAgent();
    const waits: number[] = [];
    const stop = followInbox({
      api: agent.api, agentId: "a", onError: () => {}, onItem: () => {}, delayMs: (attempt) => {
        waits.push(attempt);
        return 0;
      },
    });
    for (let n = 1; n <= 3; n++) {
      await until(() => agent.streams.length === n);
      agent.streams[n - 1]!.end();
    }
    await until(() => waits.length === 3);
    expect(waits).toEqual([0, 1, 2]);
    stop();
  });

  it("starts its backoff over once a stream has held for 30 s", async () => {
    // Only the clock is the test's: the streams' own timers run as they do.
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      const agent = new FakeAgent();
      const waits: number[] = [];
      const stop = followInbox({
        api: agent.api, agentId: "a", onError: () => {}, onItem: () => {}, delayMs: (attempt) => {
          waits.push(attempt);
          return 0;
        },
      });
      await until(() => agent.streams.length === 1);
      agent.streams[0]!.end();
      await until(() => agent.streams.length === 2);
      vi.setSystemTime(Date.now() + 30_000);
      agent.streams[1]!.end();
      await until(() => agent.streams.length === 3);
      agent.streams[2]!.end();
      await until(() => waits.length === 3);
      expect(waits).toEqual([0, 0, 1]);
      stop();
    } finally {
      vi.useRealTimers();
    }
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

describe("the chat the window shows, followed", () => {
  const CHAT = "7d2e0f8a-2b3c-4d5e-9f60-718293a4b5c6";
  const following = (agent: FakeAgent, ended: string[]) => followChat({
    api: agent.api, agentId: "a", onError: () => {}, delayMs: () => 0, sessionId: CHAT, onTurnEnd: (title) => ended.push(title),
  });

  it("tells the chat's title at the end of each turn, from its newest event on, and takes up where it was whenever its stream closes", async () => {
    const agent = new FakeAgent();
    agent.titles.set(CHAT, "Quarterly report");
    const ended: string[] = [];
    const { stop } = following(agent, ended);
    await until(() => agent.streams.length === 1);
    agent.streams[0]!.send("id: 5\r\nevent: stream.start\r\ndata: {}\r\n\r\n");
    // As the agent ends a stream at its longest: the follow takes up after the start's cursor.
    agent.streams[0]!.send('event: stream.timeout\r\ndata: {"reason": "max_duration_exceeded"}\r\n\r\n');
    agent.streams[0]!.end();
    await until(() => agent.streams.length === 2);
    agent.streams[1]!.send("id: 7\r\nevent: llm.response\r\ndata: {}\r\n\r\nid: 8\r\nevent: session.complete\r\ndata: {}\r\n\r\n");
    await until(() => ended.length === 1);
    // A chat between turns, as an agent without watch tells it, is no end of the follow.
    agent.streams[1]!.send('event: session.done\r\ndata: {"reason": "completed", "status": "completed"}\r\n\r\n');
    agent.streams[1]!.end();
    await until(() => agent.streams.length === 3);
    agent.titles.delete(CHAT);
    agent.streams[2]!.send("id: 12\r\nevent: session.complete\r\ndata: {}\r\n\r\n");
    await until(() => ended.length === 2);
    expect(agent.streams.map((stream) => stream.path)).toEqual([
      `/api/v1/sessions/${CHAT}/events?after=-1&watch=1`,
      `/api/v1/sessions/${CHAT}/events?after=5&watch=1`,
      `/api/v1/sessions/${CHAT}/events?after=8&watch=1`,
    ]);
    expect(ended).toEqual(["Quarterly report", "A chat"]);
    stop();
  });

  it("tells nothing of a turn that failed or was stopped", async () => {
    const agent = new FakeAgent();
    agent.titles.set(CHAT, "Quarterly report");
    const ended: string[] = [];
    const { stop } = following(agent, ended);
    await until(() => agent.streams.length === 1);
    agent.streams[0]!.send(["session.pause", "session.fail", "session.complete"]
      .map((type, at) => `id: ${at + 3}\r\nevent: ${type}\r\ndata: {}\r\n\r\n`).join(""));
    await until(() => ended.length === 1);
    await settle();
    expect(ended).toEqual(["Quarterly report"]);
    stop();
  });

  it("says it has started only once the chat's stream has, whatever was refused before", async () => {
    const agent = new FakeAgent();
    agent.status = 503;
    const chat = following(agent, []);
    expect(chat.started()).toBe(false);
    // Refused, then opened again: still nothing heard of the chat.
    await until(() => agent.streams.length === 1);
    expect(chat.started()).toBe(false);
    agent.streams[0]!.send("id: 5\r\nevent: stream.start\r\ndata: {}\r\n\r\n");
    await until(() => chat.started());
    chat.stop();
  });

  it("follows a chat archived, or gone, no more", async () => {
    for (const reason of ["archived", "session_not_found"]) {
      const agent = new FakeAgent();
      following(agent, []);
      await until(() => agent.streams.length === 1);
      agent.streams[0]!.send(`event: session.done\r\ndata: {"reason": "${reason}"}\r\n\r\n`);
      agent.streams[0]!.end();
      await settle();
      expect([agent.streams.length, agent.streams[0]!.signal.aborted], reason).toEqual([1, true]);
    }
  });

  it("follows no more, and says nothing of, a chat the agent does not have when its stream opens", async () => {
    const agent = new FakeAgent();
    agent.status = 404;
    const errors: string[] = [];
    followChat({ api: agent.api, agentId: "a", onError: (error) => errors.push(String(error)), delayMs: () => 0, sessionId: CHAT, onTurnEnd: () => {} });
    await settle();
    expect([agent.streams.length, errors]).toEqual([0, []]);
  });
});

describe("a chat's turns, followed", () => {
  it("tells each turn's end of a chat at once, asking nothing more of the agent", async () => {
    const agent = new FakeAgent();
    const paths: string[] = [];
    const api: Api = (path, init) => {
      paths.push(path);
      return agent.api(path, init);
    };
    let ended = 0;
    const { stop } = followTurns({ api, onError: () => {}, delayMs: () => 0, sessionId: "chat-1", onTurnEnd: () => ended++ });
    await until(() => agent.streams.length === 1);
    agent.streams[0]!.send("id: 3\r\nevent: session.complete\r\ndata: {}\r\n\r\n");
    await until(() => ended === 1);
    stop();
    expect(paths).toEqual([expect.stringMatching(/^\/api\/v1\/sessions\/chat-1\/events\?/)]);
    expect(agent.streams[0]!.signal.aborted).toBe(true);
  });

  it("tells a turn that failed or was stopped as an end too, apart from one done", async () => {
    const agent = new FakeAgent();
    const ended: boolean[] = [];
    const { stop } = followTurns({ api: agent.api, onError: () => {}, delayMs: () => 0, sessionId: "chat-1", onTurnEnd: (done) => ended.push(done) });
    await until(() => agent.streams.length === 1);
    agent.streams[0]!.send(([
      ["session.pause", `{"reason": "interrupted", "turn_ended": true}`], ["session.fail", "{}"], ["session.stopped", "{}"],
      ["llm.response", "{}"], ["session.complete", "{}"],
    ] as const).map(([type, data], at) => `id: ${at + 3}\r\nevent: ${type}\r\ndata: ${data}\r\n\r\n`).join(""));
    await until(() => ended.length === 4);
    expect(ended).toEqual([false, false, false, true]);
    stop();
  });

  it("tells each turn's start, a message, a wake or a resume, and no other event of a turn as one", async () => {
    const agent = new FakeAgent();
    const told: string[] = [];
    const { stop } = followTurns({
      api: agent.api, onError: () => {}, delayMs: () => 0, sessionId: "chat-1",
      onTurnEnd: () => told.push("end"), onTurnStart: () => told.push("start"),
    });
    await until(() => agent.streams.length === 1);
    agent.streams[0]!.send(([
      "user.message", "harness.wake", "llm.request", "tool.call", "session.complete", "session.title_updated", "session.resume",
      "coordinator.message", "device.waiting", "session.pause",
    ] as const).map((type, at) => `id: ${at + 3}\r\nevent: ${type}\r\ndata: {}\r\n\r\n`).join(""));
    await until(() => told.length === 5);
    await settle();
    expect(told).toEqual(["start", "start", "end", "start", "start"]);
    stop();
  });

  it("says a chat is at rest between its turns, done or failed, in a turn while active, and nothing of one it cannot tell", async () => {
    const agent = new FakeAgent();
    agent.trees.set("done", ["completed"]).set("failed", ["failed"]).set("busy", ["active"]).set("paused", ["paused"]);
    const said = await Promise.all(["done", "failed", "busy", "paused", "gone"].map((id) => restOf(agent.api, "a", id)));
    expect(said).toEqual([true, true, false, undefined, undefined]);
  });

  it("says a chat whose turn is done is in a turn while a helper under it still works, and not at rest while one has not started", async () => {
    const agent = new FakeAgent();
    agent.trees.set("helping", ["completed", "active"]).set("helped", ["completed", "completed", "failed"]).set("starting", ["completed", "pending"]);
    const said = await Promise.all(["helping", "helped", "starting"].map((id) => restOf(agent.api, "a", id)));
    expect(said).toEqual([false, true, undefined]);
  });

  it("says nothing of a chat whose tree is listed in full to the server's cap, as some of it may be left out", async () => {
    const agent = new FakeAgent();
    agent.trees.set("big", Array<string>(200).fill("completed"));
    expect(await restOf(agent.api, "a", "big")).toBeUndefined();
  });

  it("tells a Stop's end only once its turn has stopped, not at the pause the Stop starts with", async () => {
    const agent = new FakeAgent();
    const ended: boolean[] = [];
    const { stop } = followTurns({ api: agent.api, onError: () => {}, delayMs: () => 0, sessionId: "chat-1", onTurnEnd: (done) => ended.push(done) });
    await until(() => agent.streams.length === 1);
    // The pause route's own, while the turn may still undo what it did on this computer.
    agent.streams[0]!.send("id: 3\r\nevent: session.pause\r\ndata: {}\r\n\r\n");
    await settle();
    expect(ended).toEqual([]);
    agent.streams[0]!.send(`id: 4\r\nevent: session.pause\r\ndata: {"reason": "interrupted", "turn_ended": true}\r\n\r\n`);
    await until(() => ended.length === 1);
    expect(ended).toEqual([false]);
    stop();
  });

  it("stops by itself, telling nothing more and opening no stream again, once the sign-in it was started under has ended", async () => {
    const agent = new FakeAgent();
    let ended = 0;
    let signedIn = true;
    followTurns({ api: agent.api, onError: () => {}, delayMs: () => 0, sessionId: "chat-1", onTurnEnd: () => ended++, live: () => signedIn });
    await until(() => agent.streams.length === 1);
    signedIn = false;
    agent.streams[0]!.send("id: 3\r\nevent: session.complete\r\ndata: {}\r\n\r\n");
    await until(() => agent.streams[0]!.signal.aborted);
    await new Promise((done) => setTimeout(done, 50));
    expect([ended, agent.streams.length]).toEqual([0, 1]);
  });

  it("asks nothing of the agent with a sign-in that ended while its stream was down", async () => {
    const agent = new FakeAgent();
    let signedIn = true;
    const paths: string[] = [];
    const api: Api = (path, init) => {
      paths.push(path);
      return agent.api(path, init);
    };
    followTurns({ api, onError: () => {}, delayMs: () => 0, sessionId: "chat-1", onTurnEnd: () => {}, live: () => signedIn });
    await until(() => agent.streams.length === 1);
    signedIn = false;
    agent.streams[0]!.end();
    await new Promise((done) => setTimeout(done, 50));
    expect(paths).toHaveLength(1);
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
