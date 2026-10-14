// The plain parts of browser push in the web client (web/src/lib/push-offer.ts).
import assert from "node:assert/strict";
import { test } from "node:test";

import { attentionTitle, dismissOffer, DISMISS_DAYS, keyBytes, offerDismissed } from "../web/src/lib/push-offer.ts";

function memory() {
  const items = new Map();
  return { getItem: (k) => items.get(k) ?? null, setItem: (k, v) => items.set(k, String(v)) };
}

test("not now keeps the offer away for a while, then it comes back", () => {
  const store = memory();
  const day = 24 * 3600 * 1000;
  assert.equal(offerDismissed(1_000 * day, store), false);
  dismissOffer(1_000 * day, store);
  assert.equal(offerDismissed(1_000 * day + day, store), true);
  assert.equal(offerDismissed(1_000 * day + (DISMISS_DAYS + 1) * day, store), false);
});

test("the server's base64url key becomes the 65 bytes of its point", () => {
  const key = "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8";
  const bytes = keyBytes(key);
  assert.equal(bytes.length, 65);
  assert.equal(bytes[0], 4);
});

test("the title flashes only while messages wait", () => {
  assert.equal(attentionTitle("Chat", 0, true), "Chat");
  assert.equal(attentionTitle("Chat", 2, false), "Chat");
  assert.equal(attentionTitle("Chat", 2, true), "(2) ● New message");
});
