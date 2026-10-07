// The web client's project helpers, which the desktop's ProjectsSource reuses: a row's mapping
// (web/src/lib/projects-wire.ts) and the streams that open themselves again
// (web/src/lib/reopening-stream.ts).
import assert from "node:assert/strict";
import { test } from "node:test";

import { threadRowOf } from "../web/src/lib/projects-wire.ts";
import { INBOX_REOPENING, projectReopening, reopeningStream } from "../web/src/lib/reopening-stream.ts";

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
  const { stream, connections, surfaced } = opened(projectReopening(() => false));
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
