// The agent's AI disclosure setting, as the web client reads it (web/src/api/transparency.ts),
// against a fake fetch: a read that failed is marked so and asked again, so that nothing is sent for
// the user past a setting nobody read; one that came is the agent's, and is kept.
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { getTransparencyConfig } from "../web/src/api/transparency.ts";

const fetched = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = fetched;
});

test("marks a setting it could not read as unread, each time, and keeps the one it read", async () => {
  const asked = [];
  const answers = (answer) => {
    globalThis.fetch = async (url) => {
      asked.push(url);
      return answer();
    };
  };
  // The agent refuses the read, then cannot be reached: unread both times, and asked again.
  answers(() => new Response("{}", { status: 500 }));
  assert.deepEqual(await getTransparencyConfig(), { enabled: false, read: false });
  answers(() => {
    throw new TypeError("fetch failed");
  });
  assert.deepEqual(await getTransparencyConfig(), { enabled: false, read: false });
  // Read: the agent's setting, kept, and not asked for again.
  answers(() => Response.json({ enabled: true }));
  assert.deepEqual(await getTransparencyConfig(), { enabled: true });
  answers(() => {
    throw new Error("The setting was read already");
  });
  assert.deepEqual(await getTransparencyConfig(), { enabled: true });
  assert.deepEqual(asked, ["/api/v1/transparency", "/api/v1/transparency", "/api/v1/transparency"]);
});
