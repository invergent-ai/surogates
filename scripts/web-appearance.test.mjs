// How Surogate Desktop's Settings shape the web client's transcript (web/src/lib/appearance.ts),
// against a fake bridge and a stand-in for the document's root.
import assert from "node:assert/strict";
import { test } from "node:test";

import { applyTranscript, followTranscript } from "../web/src/lib/appearance.ts";

const LARGE = { theme: "dark", textSize: "large", transcriptWidth: "wide", motion: "reduced" };
const MEDIUM = { theme: "light", textSize: "medium", transcriptWidth: "medium", motion: "system" };

const root = () => ({ dataset: {} });

// A bridge whose getAppearance answers only once the test calls answer(), and which tells each change.
function bridge(now = LARGE) {
  const listeners = new Set();
  let answer;
  const asked = new Promise((resolve) => {
    answer = () => resolve(now);
  });
  return {
    answer,
    change: (appearance) => {
      for (const listener of listeners) listener(appearance);
    },
    listening: () => listeners.size,
    getAppearance: () => asked,
    onAppearanceChanged: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

const settled = () => new Promise((resolve) => setImmediate(resolve));

test("sets the transcript's text size, width and motion on the root, and no theme", () => {
  const found = root();
  applyTranscript(found, LARGE);
  assert.deepEqual(found.dataset, { textSize: "large", transcriptWidth: "wide", motion: "reduced" });
});

test("drops a value the desktop does not offer, and one it no longer sends", () => {
  const found = root();
  applyTranscript(found, LARGE);
  applyTranscript(found, { textSize: "huge", transcriptWidth: "medium" });
  assert.deepEqual(found.dataset, { transcriptWidth: "medium" });
});

test("follows the desktop: its settings as they are, then each change, until stopped", async () => {
  const desktop = bridge();
  const found = root();
  const stop = followTranscript(desktop, found);
  desktop.answer();
  await settled();
  assert.equal(found.dataset.textSize, "large");
  desktop.change(MEDIUM);
  assert.deepEqual(found.dataset, { textSize: "medium", transcriptWidth: "medium", motion: "system" });
  stop();
  assert.equal(desktop.listening(), 0);
  desktop.change(LARGE);
  assert.equal(found.dataset.textSize, "medium");
});

test("keeps a change heard while the first read was on its way over that read's older answer", async () => {
  const desktop = bridge(LARGE);
  const found = root();
  followTranscript(desktop, found);
  desktop.change(MEDIUM);
  desktop.answer();
  await settled();
  assert.equal(found.dataset.textSize, "medium");
});

test("says so, and keeps the web client's own look, when the desktop cannot say how it looks", async () => {
  const found = root();
  const warned = [];
  const warn = console.warn;
  console.warn = (...args) => warned.push(args);
  try {
    followTranscript({ getAppearance: async () => { throw new Error("gone"); }, onAppearanceChanged: () => () => {} }, found);
    await settled();
  } finally {
    console.warn = warn;
  }
  assert.deepEqual(found.dataset, {});
  assert.equal(warned.length, 1);
});
