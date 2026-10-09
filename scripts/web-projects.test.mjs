// The web client's project routes (web/src/api/workstream-routes.ts), which are also the
// ProjectsSource it serves Surogate Desktop, and their helpers: a row's mapping
// (web/src/lib/projects-wire.ts) and the streams that open themselves again
// (web/src/lib/reopening-stream.ts). A project's stream runs over the SDK's own
// FetchSseEventStream, as web/src/api/workstreams.ts opens it.
import assert from "node:assert/strict";
import { test } from "node:test";

import { FetchSseEventStream } from "../sdk/agent-chat-react/src/runtime/fetch-sse-stream.ts";
import { workstreamRoutes } from "../web/src/api/workstream-routes.ts";
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
    files: [{ kind: "file", label: "notes.md", ref: "threads/tidy/notes.md", thread_id: "t-1", landing: "redoing" }],
    place: { kind: "device", device_id: "d-1", device_name: "thinkpad", online: false },
    created_at: "2026-10-07T10:00:00Z", updated_at: "2026-10-07T11:00:00Z", resolved_at: null,
  }), {
    id: "t-1", title: "Tidy the shared folder", group: "working", reason: "computer",
    statusLine: "Waiting for thinkpad", progress: { done: 1, total: 2 },
    files: [{ kind: "file", label: "notes.md", ref: "threads/tidy/notes.md", threadId: "t-1", landing: "redoing" }],
    place: { kind: "device", deviceId: "d-1", deviceName: "thinkpad", online: false },
    createdAt: "2026-10-07T10:00:00Z", updatedAt: "2026-10-07T11:00:00Z", resolvedAt: null,
  });
  assert.deepEqual(threadRowOf({
    id: "t-2", title: "Draft A", group: "idle", reason: null, status_line: null, progress: null, files: [],
    place: { kind: "cloud" }, created_at: "2026-10-07T10:00:00Z", updated_at: "2026-10-07T11:00:00Z",
    resolved_at: "2026-10-07T12:00:00Z",
  }).place, { kind: "cloud" });
  // A server from before file history sends a file with no mark: the page serves none.
  assert.deepEqual(threadRowOf({
    id: "t-3", title: "Draft B", group: "idle", reason: null, status_line: null, progress: null,
    files: [{ kind: "file", label: "b.md", ref: "b.md", thread_id: "t-3" }],
    place: { kind: "cloud" }, created_at: "2026-10-07T10:00:00Z", updated_at: "2026-10-07T11:00:00Z", resolved_at: null,
  }).files, [{ kind: "file", label: "b.md", ref: "b.md", threadId: "t-3", landing: null }]);
});

// The project routes over a fake fetch: *answer* gives each request's response, and every
// request is kept, as [method, url, body], and a POST or a PATCH with the type its body was sent as.
function routesOver(answer) {
  const asked = [];
  const fetchFn = async (input, init = {}) => {
    const method = init.method ?? "GET";
    const request = [method, String(input), init.body === undefined ? undefined : JSON.parse(init.body)];
    if (method === "POST" || method === "PATCH") request.push(init.headers?.["Content-Type"]);
    asked.push(request);
    return answer(String(input), init);
  };
  return { asked, routes: workstreamRoutes(fetchFn, (url, watched) => new FetchSseEventStream(url, { fetchFn: watched })) };
}

const PROJECT = {
  id: "p-1", name: "Quarterly report", icon: null, created_at: "2026-10-07T10:00:00Z", updated_at: "2026-10-07T11:00:00Z",
  waiting: 1, working: 2, goal: "Close Q3", instructions: "Write in French.", master_session_id: "m-1",
  coordinator_tier: null, thread_tier: "pro",
};
const ROW = {
  id: "t-1", title: "Draft A", group: "idle", reason: null, status_line: "Drafted the memo.", progress: null, files: [],
  place: { kind: "cloud" }, created_at: "2026-10-07T10:00:00Z", updated_at: "2026-10-07T11:00:00Z", resolved_at: null,
};

const FILE = {
  path: "threads/Draft A/A.docx", origin: "produced", thread_id: "t-1", size: 2048, updated_at: "2026-10-07T11:00:00Z",
  place: { kind: "cloud" },
};
const ROUTINE = {
  id: "r-1", name: "Weekly cash report", prompt: "Report the cash.", status: "active", kind: "cron",
  schedule_display: "Every Monday at 08:00", next_run_at: "2026-10-12T08:00:00Z", created_from_session_id: "m-1",
};

test("each project route is asked at its own path, and answers the shell's types", async () => {
  const { asked, routes } = routesOver((url, init) => {
    if (init.method === "DELETE") return new Response(null, { status: 204 });
    if (url.endsWith("/library")) return Response.json([FILE]);
    if (url.startsWith("/api/v1/scheduled-work")) return Response.json({ items: [ROUTINE, { ...ROUTINE, id: "r-2", name: null }], total: 2 });
    if (url.includes("/threads")) return Response.json(init.method === "POST" ? ROW : [ROW]);
    return Response.json(url === "/api/v1/workstreams" && !init.method ? [PROJECT] : PROJECT);
  });
  const summary = {
    id: "p-1", name: "Quarterly report", icon: null, createdAt: "2026-10-07T10:00:00Z", updatedAt: "2026-10-07T11:00:00Z",
    waiting: 1, working: 2,
  };
  const project = { ...summary, goal: "Close Q3", instructions: "Write in French.", masterSessionId: "m-1", coordinatorTier: null, threadTier: "pro" };
  assert.deepEqual(await routes.list(), [summary]);
  assert.deepEqual(await routes.get("p-1"), project);
  assert.deepEqual(await routes.create({ name: "Quarterly report", goal: "Close Q3" }), project);
  // Only the fields a change names, in the route's own names.
  assert.deepEqual(await routes.update("p-1", { name: "Q3", coordinatorTier: "pro", threadTier: null, secret: "x" }), project);
  assert.equal(await routes.archive("p-1"), undefined);
  assert.deepEqual((await routes.threads("p-1")).map((row) => row.statusLine), ["Drafted the memo."]);
  assert.equal((await routes.threads("p-1", "t-1"))[0].id, "t-1");
  assert.equal((await routes.resolve("p-1", "t-1")).id, "t-1");
  assert.equal((await routes.reopen("p-1", "t-1")).id, "t-1");
  assert.equal((await routes.start("p-1", "pr-1", "2")).id, "t-1");
  assert.deepEqual(await routes.library("p-1"), [{
    path: "threads/Draft A/A.docx", origin: "produced", threadId: "t-1", size: 2048, updatedAt: "2026-10-07T11:00:00Z",
    place: { kind: "cloud" },
  }]);
  // A schedule made without a name shows its schedule alone.
  assert.deepEqual(await routes.routines("p-1"), [
    { id: "r-1", name: "Weekly cash report", scheduleDisplay: "Every Monday at 08:00", nextRunAt: "2026-10-12T08:00:00Z", status: "active" },
    { id: "r-2", name: "", scheduleDisplay: "Every Monday at 08:00", nextRunAt: "2026-10-12T08:00:00Z", status: "active" },
  ]);
  assert.deepEqual(asked, [
    ["GET", "/api/v1/workstreams", undefined],
    ["GET", "/api/v1/workstreams/p-1", undefined],
    ["POST", "/api/v1/workstreams", { name: "Quarterly report", goal: "Close Q3" }, "application/json"],
    ["PATCH", "/api/v1/workstreams/p-1", { name: "Q3", coordinator_tier: "pro", thread_tier: null }, "application/json"],
    ["DELETE", "/api/v1/workstreams/p-1", undefined],
    ["GET", "/api/v1/workstreams/p-1/threads", undefined],
    ["GET", "/api/v1/workstreams/p-1/threads?thread_id=t-1", undefined],
    ["POST", "/api/v1/workstreams/p-1/threads/t-1/resolve", undefined, undefined],
    ["POST", "/api/v1/workstreams/p-1/threads/t-1/reopen", undefined, undefined],
    ["POST", "/api/v1/workstreams/p-1/threads", { proposal_id: "pr-1", key: "2" }, "application/json"],
    ["GET", "/api/v1/workstreams/p-1/library", undefined],
    // The routines are the master's schedules: the project is read for its master first.
    ["GET", "/api/v1/workstreams/p-1", undefined],
    ["GET", "/api/v1/scheduled-work?created_from_session_id=m-1&status=all&limit=200", undefined],
  ]);
});

test("a project route puts each id in its path as one segment, so no id reaches another route", async () => {
  const { asked, routes } = routesOver((url, init) => {
    if (init.method === "DELETE") return new Response(null, { status: 204 });
    if (url.endsWith("/library")) return Response.json([]);
    if (url.includes("/threads")) return Response.json(init.method === "POST" ? ROW : []);
    return Response.json(PROJECT);
  });
  await routes.get("../devices");
  await routes.update("p/1", { name: "Q3" });
  await routes.archive("p?1");
  await routes.threads("p#1");
  await routes.threads("p 1", "t&thread_id=2");
  await routes.resolve("p%1", "t/../2");
  await routes.reopen("p1", "t?2");
  await routes.library("p/1");
  await routes.start("p/1", "pr-1", "2");
  assert.deepEqual(asked.map(([method, url]) => [method, url]), [
    ["GET", "/api/v1/workstreams/..%2Fdevices"],
    ["PATCH", "/api/v1/workstreams/p%2F1"],
    ["DELETE", "/api/v1/workstreams/p%3F1"],
    ["GET", "/api/v1/workstreams/p%231/threads"],
    ["GET", "/api/v1/workstreams/p%201/threads?thread_id=t%26thread_id%3D2"],
    ["POST", "/api/v1/workstreams/p%251/threads/t%2F..%2F2/resolve"],
    ["POST", "/api/v1/workstreams/p1/threads/t%3F2/reopen"],
    ["GET", "/api/v1/workstreams/p%2F1/library"],
    ["POST", "/api/v1/workstreams/p%2F1/threads"],
  ]);
  // A dot segment the URL would resolve away is no id at all.
  await assert.rejects(routes.archive(".."), { message: "No such project." });
  await assert.rejects(routes.resolve("p1", "."), { message: "No such thread." });
  assert.equal(asked.length, 9);
});

test("a project route whose answer is not JSON, or not of its shape, says the route's own words, and never succeeds hollow", async () => {
  const words = {
    list: "Failed to fetch the projects",
    get: "Failed to fetch the project",
    create: "The project could not be created.",
    update: "The project could not be changed.",
    threads: "Failed to fetch the project's threads",
    resolve: "The thread could not be resolved.",
    reopen: "The thread could not be reopened.",
    library: "Failed to fetch the project's Library",
    start: "The thread could not be started.",
  };
  for (const body of ["<!doctype html><p>Sign in</p>", "{}", "[{}]", "null", '[{"id": "t-1"}]', '{"id": "t-1"}']) {
    const { routes } = routesOver(() => new Response(body, { headers: { "content-type": "application/json" } }));
    const calls = {
      list: () => routes.list(),
      get: () => routes.get("p-1"),
      create: () => routes.create({ name: "Q3" }),
      update: () => routes.update("p-1", { name: "Q3" }),
      threads: () => routes.threads("p-1", "t-1"),
      resolve: () => routes.resolve("p-1", "t-1"),
      reopen: () => routes.reopen("p-1", "t-1"),
      library: () => routes.library("p-1"),
      start: () => routes.start("p-1", "pr-1", "2"),
    };
    for (const [name, call] of Object.entries(calls)) {
      await assert.rejects(call(), { message: words[name] }, `${name} answered ${body}`);
    }
  }
  // The routines read the project first: a schedule list that is not one is the routines' own failure.
  for (const body of ["<!doctype html>", "{}", '{"items": {}}', '{"items": [{}]}']) {
    const { routes } = routesOver((url) => url.startsWith("/api/v1/scheduled-work")
      ? new Response(body, { headers: { "content-type": "application/json" } })
      : Response.json(PROJECT));
    await assert.rejects(routes.routines("p-1"), { message: "Failed to fetch the project's routines" }, `routines answered ${body}`);
  }
});

test("a thread on the user's computer is made from its card with the folder confirmed there, and begun at its own route", async () => {
  const { asked, routes } = routesOver((url) => Response.json(url.endsWith("/start") ? ROW : { thread_id: "t-1" }, { status: 201 }));
  const folder = { kind: "device", device_id: "d-1", folder: "/home/flavius/Budget", nonce: "n".repeat(43) };
  assert.equal(await routes.makeOnComputer("p/1", "pr-1", "2", folder), "t-1");
  assert.equal((await routes.begin("p-1", "t/1")).id, "t-1");
  assert.deepEqual(asked, [
    ["POST", "/api/v1/workstreams/p%2F1/threads", { proposal_id: "pr-1", key: "2", execution: folder }, "application/json"],
    ["POST", "/api/v1/workstreams/p-1/threads/t%2F1/start", undefined, undefined],
  ]);
  // An answer of another shape is the route's own failure, never a thread with no id.
  for (const body of ["{}", '{"thread_id": 7}', "[]"]) {
    const { routes: hollow } = routesOver(() => new Response(body, { headers: { "content-type": "application/json" } }));
    await assert.rejects(hollow.makeOnComputer("p-1", "pr-1", "2", folder), { message: "The thread could not be started." });
    await assert.rejects(hollow.begin("p-1", "t-1"), { message: "The thread could not be started." });
  }
  // A folder the server refuses is said in words.
  const { routes: refusing } = routesOver(() => Response.json({
    detail: [{ type: "string_pattern_mismatch", loc: ["body", "execution", "nonce"], msg: "String should match pattern '^[A-Za-z0-9_-]{16,128}$'" }],
  }, { status: 422 }));
  await assert.rejects(refusing.makeOnComputer("p-1", "pr-1", "2", folder), {
    message: "nonce: String should match pattern '^[A-Za-z0-9_-]{16,128}$'",
  });
});

test("a project route that refuses a field says why in words, never as the route's raw detail", async () => {
  const { routes } = routesOver(() => Response.json({
    detail: [
      { type: "value_error", loc: ["body", "name"], msg: "Value error, must not be blank", input: "\u001f" },
      { type: "string_pattern_mismatch", loc: ["body", "goal"], msg: "String should match pattern '^[^\\x00]*$'", input: "\u0000" },
    ],
  }, { status: 422 }));
  await assert.rejects(routes.create({ name: "\u001f", goal: "\u0000" }), {
    message: "name: must not be blank. goal: String should match pattern '^[^\\x00]*$'",
  });
});

test("a project route that refuses says the route's own words", async () => {
  const { routes } = routesOver((url) => url.endsWith("/workstreams")
    ? Response.json({ detail: "This agent keeps a single conversation, so it has no projects." }, { status: 409 })
    : new Response("Bad Gateway", { status: 502 }));
  await assert.rejects(routes.create({ name: "Q3" }), /This agent keeps a single conversation, so it has no projects\./);
  await assert.rejects(routes.get("p-1"), /Failed to fetch the project/);
});

const EVENTS = 'event: ready\ndata: {}\n\nevent: change\ndata: {"thread_id": "t-1", "type": "session.complete"}\n\n'
  + 'event: change\ndata: {"thread_id": null, "type": "worker.spawned"}\n\nevent: change\ndata: not json\n\n'
  // A landing another lock holder finished: a change of its thread's row, as any other is.
  + 'event: change\ndata: {"thread_id": "t-2", "type": "history.landed"}\n\n';

test("a project is followed at its own stream, over the fetch it was given: ready and each change", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { asked, routes } = routesOver(() => new Response(EVENTS, { headers: { "content-type": "text/event-stream" } }));
  const heard = [];
  const stop = routes.subscribe("p-1", (threadId) => heard.push(threadId));
  t.after(stop);
  await settled();
  assert.deepEqual(heard, [null, "t-1", null, null, "t-2"]);
  assert.deepEqual(asked, [["GET", "/api/v1/workstreams/p-1/stream", undefined]]);
});

test("a project gone from its stream is a project-wide change, heard once", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { asked, routes } = routesOver(() => Response.json({ detail: "No such project." }, { status: 404 }));
  const heard = [];
  t.after(routes.subscribe("p-1", (threadId) => heard.push(threadId)));
  await settled();
  t.mock.timers.tick(120_000);
  await settled();
  assert.deepEqual(heard, [null]);
  assert.equal(asked.length, 1);
});

test("a stream closed while it waits to open again stops listening for the network", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const target = new EventTarget();
  const listening = new Set();
  globalThis.addEventListener = (type, listener) => {
    listening.add(listener);
    target.addEventListener(type, listener);
  };
  globalThis.removeEventListener = (type, listener) => {
    listening.delete(listener);
    target.removeEventListener(type, listener);
  };
  t.after(() => {
    globalThis.addEventListener = undefined;
    globalThis.removeEventListener = undefined;
  });
  const { stream, connections } = opened(projectReopening(() => false));
  connections[0].onerror();
  assert.equal(listening.size, 1);
  stream.close();
  assert.equal(listening.size, 0);
});
