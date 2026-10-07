// A change to a local-folder chat's files is sent again, under one request id, until its computer answers it.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  GIVE_UP_MS, NOT_FINISHED, onDeviceOf, RETRY_FIRST_MS, RETRY_MOST_MS, untilAnswered,
} from "../web/src/api/device-requests.ts";

// Each answer a status, [status, body], or "drop": a fetch that rejects, as a network that drops does.
// What each send named in place of its body is in changes: null when it carried its body.
const answers = (...statuses) => {
  const sent = [];
  const changes = [];
  const send = async (requestId, change) => {
    sent.push(requestId);
    changes.push(change);
    const answer = statuses.shift();
    if (answer === "drop") throw new TypeError("Failed to fetch");
    const [status, body] = Array.isArray(answer) ? answer : [answer, null];
    return new Response(body === null ? null : JSON.stringify(body), { status });
  };
  return { send, sent, changes };
};

test("sends a change once when it is answered", async () => {
  const { send, sent } = answers(201);
  const slept = [];
  const response = await untilAnswered(send, { onDevice: true, sleep: async (ms) => slept.push(ms) });
  assert.equal(response.status, 201);
  assert.equal(sent.length, 1);
  assert.match(sent[0], /^[0-9a-f]{32}$/);
  assert.deepEqual(slept, []);
});

test("sends a change its computer has not answered again, under the same request id, backing off", async () => {
  const { send, sent } = answers(202, 202, 202, 202, 202, 202, 403);
  const slept = [];
  const response = await untilAnswered(send, { onDevice: true, sleep: async (ms) => slept.push(ms) });
  assert.equal(response.status, 403);
  assert.equal(new Set(sent).size, 1);
  assert.deepEqual(slept, [RETRY_FIRST_MS, 2000, 4000, 8000, RETRY_MOST_MS, RETRY_MOST_MS]);
});

test("two changes have their own request ids", async () => {
  const first = answers(200);
  const second = answers(200);
  await untilAnswered(first.send, { onDevice: true, sleep: async () => {} });
  await untilAnswered(second.send, { onDevice: true, sleep: async () => {} });
  assert.notEqual(first.sent[0], second.sent[0]);
});

test("sends a change again under the same request id when its fetch fails, backing off", async () => {
  const { send, sent } = answers("drop", 202, "drop", 201);
  const slept = [];
  const response = await untilAnswered(send, { onDevice: true, sleep: async (ms) => slept.push(ms) });
  assert.equal(response.status, 201);
  assert.equal(sent.length, 4);
  assert.equal(new Set(sent).size, 1);
  assert.deepEqual(slept, [RETRY_FIRST_MS, 2000, 4000]);
});

// A clock the loop's own sleeps move on, as the eleven minutes pass.
const clock = () => {
  let at = 0;
  return { now: () => at, sleep: async (ms) => { at += ms; } };
};

test("gives up past eleven minutes in all, and says the change did not finish", async () => {
  for (const status of [202, "drop"]) {
    const { send, sent } = answers(...Array(200).fill(status));
    const time = clock();
    await assert.rejects(untilAnswered(send, { onDevice: true, ...time }), { message: NOT_FINISHED });
    assert.ok(time.now() >= GIVE_UP_MS && time.now() < GIVE_UP_MS + RETRY_MOST_MS, String(time.now()));
    assert.ok(sent.length < 80, String(sent.length));
  }
});

test("stops when its signal aborts, and sends nothing more", async () => {
  const { send, sent } = answers(202, 202, 202, 202);
  const stop = new AbortController();
  const sleep = async () => { if (sent.length === 2) stop.abort(); };
  await assert.rejects(untilAnswered(send, { onDevice: true, signal: stop.signal, sleep }), { name: "AbortError" });
  assert.equal(sent.length, 2);
});

test("a cloud chat's change is sent once, as before, whatever comes of it", async () => {
  for (const status of [202, 404, "drop"]) {
    const { send, sent } = answers(status, 201);
    const slept = [];
    const sending = untilAnswered(send, { onDevice: false, sleep: async (ms) => slept.push(ms) });
    if (status === "drop") await assert.rejects(sending, TypeError);
    // Kept on a computer this client did not know of: never read as a finished change.
    else if (status === 202) await assert.rejects(sending, { message: NOT_FINISHED });
    else assert.equal((await sending).status, status);
    assert.deepEqual([sent.length, slept], [1, []]);
  }
});

test("knows a local-folder chat by where its config says it works", () => {
  assert.equal(onDeviceOf({ execution: { kind: "device", device_id: "d" } }), true);
  for (const config of [{ execution: { kind: "cloud" } }, { execution: "device" }, {}, null, undefined]) {
    assert.equal(onDeviceOf(config), false, JSON.stringify(config));
  }
});

test("sends a change the server says it holds again by its digest, and whole when the server holds none", async () => {
  const held = [202, { request_id: "r", message: "waiting", change: "c".repeat(64) }];
  const { send, changes } = answers(held, held, "drop", 428, 201);
  const slept = [];
  const response = await untilAnswered(send, { onDevice: true, sleep: async (ms) => slept.push(ms) });
  assert.equal(response.status, 201);
  // Whole first; then by its digest, a dropped fetch too; whole again, at once, once the server holds none.
  assert.deepEqual(changes, [null, "c".repeat(64), "c".repeat(64), "c".repeat(64), null]);
  assert.deepEqual(slept, [RETRY_FIRST_MS, 2000, 4000]);
});

test("a server that holds none of a change sent whole is answered as it said", async () => {
  const { send, changes } = answers(428);
  assert.equal((await untilAnswered(send, { onDevice: true, sleep: async () => {} })).status, 428);
  assert.deepEqual(changes, [null]);
});

test("sends a change told to wait its turn again, under the same request id, as a 202", async () => {
  const { send, sent } = answers(202, 429, 429, 201);
  const slept = [];
  const response = await untilAnswered(send, { onDevice: true, sleep: async (ms) => slept.push(ms) });
  assert.equal(response.status, 201);
  assert.equal(new Set(sent).size, 1);
  assert.deepEqual(slept, [RETRY_FIRST_MS, 2000, 4000]);
});

test("hands its signal to a local-folder chat's sends only: a cloud chat's one fetch has none, as before", async () => {
  const stop = new AbortController();
  const signals = [];
  const send = async (_requestId, _change, signal) => {
    signals.push(signal);
    return new Response(null, { status: 201 });
  };
  await untilAnswered(send, { onDevice: true, signal: stop.signal, sleep: async () => {} });
  await untilAnswered(send, { onDevice: false, signal: stop.signal, sleep: async () => {} });
  assert.deepEqual(signals, [stop.signal, undefined]);
});
