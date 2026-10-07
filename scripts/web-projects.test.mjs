// The web client's project helpers, which the desktop's ProjectsSource reuses: a row's mapping
// (web/src/lib/projects-wire.ts) and the streams that open themselves again
// (web/src/lib/reopening-stream.ts). A project's stream runs over the SDK's own
// FetchSseEventStream, as web/src/api/workstreams.ts opens it.
import assert from "node:assert/strict";
import { test } from "node:test";

import { FetchSseEventStream } from "../sdk/agent-chat-react/src/runtime/fetch-sse-stream.ts";
import { threadRowOf } from "../web/src/lib/projects-wire.ts";
import {
  INBOX_REOPENING,
  projectReopening,
  projectStream,
  reopeningStream,
} from "../web/src/lib/reopening-stream.ts";

class Connection {
  listeners = new Map();
  onerror = null;
  closed = false;

  addEventListener(type, listener) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close() {
    this.closed = true;
  }

  emit(type, data = "{}") {
    for (const listener of this.listeners.get(type) ?? []) listener({ data });
  }
}

function opened(reopening) {
  const connections = [];
  const stream = reopeningStream(() => {
    connections.push(new Connection());
    return connections.at(-1);
  }, reopening);
  const surfaced = [];
  stream.onerror = () => surfaced.push("onerror");
  return { stream, connections, surfaced };
}

test("a project's stream opens again after four failures, sooner first, and is live again", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  // With no random part, each wait is the whole of its delay.
  const { stream, connections, surfaced } = opened(projectReopening(() => false, () => 0));
  const heard = [];
  stream.addEventListener("ready", (event) => heard.push(event.data));
  for (const [failed, wait] of [3_000, 6_000, 12_000, 24_000].entries()) {
    const failing = connections.at(-1);
    failing.onerror();
    assert.equal(failing.closed, true);
    t.mock.timers.tick(wait - 1);
    assert.equal(connections.length, failed + 1, "not before its wait");
    t.mock.timers.tick(1);
    assert.equal(connections.length, failed + 2);
  }
  connections.at(-1).emit("ready");
  assert.deepEqual(heard, ["{}"]);
  assert.deepEqual(surfaced, []);
  // Heard from again, it counts its failures from the first.
  connections.at(-1).onerror();
  t.mock.timers.tick(3_000);
  assert.equal(connections.length, 6);
});

test("a project's stream waits at most a minute between tries", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { connections } = opened(projectReopening(() => false));
  for (let failed = 0; failed < 10; failed++) {
    connections.at(-1).onerror();
    t.mock.timers.tick(60_000);
  }
  assert.equal(connections.length, 11);
});

test("a project's stream waits between half and all of each delay, at random", () => {
  const delays = [3_000, 6_000, 12_000, 24_000, 48_000, 60_000, 60_000];
  for (const [failed, delay] of delays.entries()) {
    assert.equal(projectReopening(() => false, () => 0).delayMs(failed + 1), delay);
    assert.equal(projectReopening(() => false, () => 1).delayMs(failed + 1), delay / 2);
    for (let i = 0; i < 100; i++) {
      const wait = projectReopening(() => false).delayMs(failed + 1);
      assert.ok(wait >= delay / 2 && wait <= delay, `${wait} is not within ${delay / 2}..${delay}`);
    }
  }
});

// Node's globalThis has no addEventListener; a browser's window does.
function withOnline(t) {
  const target = new EventTarget();
  globalThis.addEventListener = target.addEventListener.bind(target);
  globalThis.removeEventListener = target.removeEventListener.bind(target);
  t.after(() => {
    globalThis.addEventListener = undefined;
    globalThis.removeEventListener = undefined;
  });
  return () => target.dispatchEvent(new Event("online"));
}

test("a stream waiting to open again opens at once when the network comes back", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const online = withOnline(t);
  const { stream, connections } = opened(projectReopening(() => false, () => 0));
  // Open, it leaves the network to its watchdog.
  online();
  assert.equal(connections.length, 1);
  connections[0].onerror();
  online();
  assert.equal(connections.length, 2);
  // Its wait is over: neither the wait's end nor the network opens it again.
  t.mock.timers.tick(3_000);
  online();
  assert.equal(connections.length, 2);
  // Closed while it waits, it opens no more.
  connections[1].onerror();
  stream.close();
  online();
  t.mock.timers.tick(60_000);
  assert.equal(connections.length, 2);
});

test("a project's stream stops when the project is gone, and says so", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let gone = false;
  const { connections, surfaced } = opened(projectReopening(() => gone));
  gone = true;
  connections[0].onerror();
  t.mock.timers.tick(120_000);
  assert.equal(connections.length, 1);
  assert.deepEqual(surfaced, ["onerror"]);
});

test("the inbox's stream gives up after three failures in a row, as its hook expects", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { connections, surfaced } = opened(INBOX_REOPENING);
  for (let failed = 0; failed < 4; failed++) {
    connections.at(-1).onerror();
    t.mock.timers.tick(3_000);
  }
  assert.equal(connections.length, 4);
  assert.deepEqual(surfaced, ["onerror"]);
});

// The answers settle over the event loop, which the mocked timers leave alone.
async function settled() {
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setImmediate(resolve));
}

function followed(t, answer) {
  const fetches = [];
  const stream = projectStream(
    (fetchFn) => new FetchSseEventStream("/api/v1/workstreams/p-1/stream", { fetchFn }),
    async (input) => {
      fetches.push(String(input));
      return answer();
    },
  );
  const surfaced = [];
  stream.onerror = () => surfaced.push("onerror");
  t.after(() => stream.close());
  return { fetches, surfaced };
}

test("a project's stream ends when its own route says the project is gone", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { fetches, surfaced } = followed(t, () => Response.json({ detail: "No such project." }, { status: 404 }));
  await settled();
  assert.deepEqual(surfaced, ["onerror"]);
  t.mock.timers.tick(120_000);
  await settled();
  assert.deepEqual(fetches, ["/api/v1/workstreams/p-1/stream"]);
});

for (const [failure, answer] of [
  // Traefik answers so for a Service with no ready pod.
  ["an ingress's 404", () => new Response("404 page not found\n", { status: 404, headers: { "content-type": "text/plain" } })],
  // An API pod that does not have the route yet.
  ["another route's 404", () => Response.json({ detail: "Not Found" }, { status: 404 })],
  ["a 502", () => new Response("Bad Gateway", { status: 502 })],
  ["a 503", () => new Response("Service Unavailable", { status: 503 })],
  ["a fetch that throws", () => {
    throw new TypeError("fetch failed");
  }],
]) {
  test(`a project's stream tries again after ${failure}`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    t.mock.method(console, "error", () => {});
    const { fetches, surfaced } = followed(t, answer);
    await settled();
    assert.equal(fetches.length, 1);
    t.mock.timers.tick(3_000);
    await settled();
    assert.equal(fetches.length, 2);
    assert.deepEqual(surfaced, []);
  });
}

test("a closed stream opens no more", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { stream, connections } = opened(projectReopening(() => false));
  connections[0].onerror();
  stream.close();
  t.mock.timers.tick(60_000);
  assert.equal(connections.length, 1);
});

test("a row maps to the shell's ThreadRow field by field", () => {
  assert.deepEqual(threadRowOf({
    id: "t-1", title: "Tidy the shared folder", group: "working", reason: "computer",
    status_line: "Waiting for thinkpad", progress: { done: 1, total: 2 },
    files: [{ kind: "file", label: "notes.md", ref: "threads/tidy/notes.md", thread_id: "t-1" }],
    place: { kind: "device", device_id: "d-1", device_name: "thinkpad", online: false },
    created_at: "2026-10-07T10:00:00Z", updated_at: "2026-10-07T11:00:00Z", resolved_at: null,
  }), {
    id: "t-1", title: "Tidy the shared folder", group: "working", reason: "computer",
    statusLine: "Waiting for thinkpad", progress: { done: 1, total: 2 },
    files: [{ kind: "file", label: "notes.md", ref: "threads/tidy/notes.md", threadId: "t-1" }],
    place: { kind: "device", deviceId: "d-1", deviceName: "thinkpad", online: false },
    createdAt: "2026-10-07T10:00:00Z", updatedAt: "2026-10-07T11:00:00Z", resolvedAt: null,
  });
  assert.deepEqual(threadRowOf({
    id: "t-2", title: "Draft A", group: "idle", reason: null, status_line: null, progress: null, files: [],
    place: { kind: "cloud" }, created_at: "2026-10-07T10:00:00Z", updated_at: "2026-10-07T11:00:00Z",
    resolved_at: "2026-10-07T12:00:00Z",
  }).place, { kind: "cloud" });
});
