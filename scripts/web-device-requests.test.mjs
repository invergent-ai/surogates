// A change to a local-folder chat's files is sent again, under one request id, until its computer answers it.
import assert from "node:assert/strict";
import { test } from "node:test";

import { RETRY_FIRST_MS, RETRY_MOST_MS, untilAnswered } from "../web/src/api/device-requests.ts";

// Each answer a status, or "drop": a fetch that rejects, as a network that drops does.
const answers = (...statuses) => {
  const sent = [];
  const send = async (requestId) => {
    sent.push(requestId);
    const status = statuses.shift();
    if (status === "drop") throw new TypeError("Failed to fetch");
    return new Response(null, { status });
  };
  return { send, sent };
};

test("sends a change once when it is answered", async () => {
  const { send, sent } = answers(201);
  const slept = [];
  const response = await untilAnswered(send, async (ms) => slept.push(ms));
  assert.equal(response.status, 201);
  assert.equal(sent.length, 1);
  assert.match(sent[0], /^[0-9a-f]{32}$/);
  assert.deepEqual(slept, []);
});

test("sends a change its computer has not answered again, under the same request id, backing off", async () => {
  const { send, sent } = answers(202, 202, 202, 202, 202, 202, 403);
  const slept = [];
  const response = await untilAnswered(send, async (ms) => slept.push(ms));
  assert.equal(response.status, 403);
  assert.equal(new Set(sent).size, 1);
  assert.deepEqual(slept, [RETRY_FIRST_MS, 2000, 4000, 8000, RETRY_MOST_MS, RETRY_MOST_MS]);
});

test("two changes have their own request ids", async () => {
  const first = answers(200);
  const second = answers(200);
  await untilAnswered(first.send, async () => {});
  await untilAnswered(second.send, async () => {});
  assert.notEqual(first.sent[0], second.sent[0]);
});

test("sends a change again under the same request id when its fetch fails, backing off", async () => {
  const { send, sent } = answers("drop", 202, "drop", 201);
  const slept = [];
  const response = await untilAnswered(send, async (ms) => slept.push(ms));
  assert.equal(response.status, 201);
  assert.equal(sent.length, 4);
  assert.equal(new Set(sent).size, 1);
  assert.deepEqual(slept, [RETRY_FIRST_MS, 2000, 4000]);
});
