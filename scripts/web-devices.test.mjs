// The user's computers and desktop sign-ins at an agent, as the web client's Settings → Devices
// and its "computer added" notice show them (web/src/lib/devices.ts).
import assert from "node:assert/strict";
import { test } from "node:test";

import { deviceHistory, deviceState, signInsFrom } from "../web/src/lib/devices.ts";

const NOW = new Date("2026-10-07T12:00:00Z");
const DEVICE = {
  id: "d-1", name: "Flavius's ThinkPad", created_at: "2026-10-04T12:00:00Z", last_seen_at: "2026-10-07T09:00:00Z",
  revoked_at: null, reauthorized_at: null, online: false,
};

test("says how each computer is now: revoked, online, last seen, or never connected", () => {
  assert.equal(deviceState({ ...DEVICE, revoked_at: "2026-10-06T12:00:00Z", online: true }, NOW), "Revoked 1 day ago");
  assert.equal(deviceState({ ...DEVICE, online: true }, NOW), "Online");
  assert.equal(deviceState(DEVICE, NOW), "Last seen about 3 hours ago");
  assert.equal(deviceState({ ...DEVICE, last_seen_at: null }, NOW), "Never connected");
});

test("says when each computer was added, and when it was reauthorized", () => {
  assert.equal(deviceHistory(DEVICE, NOW), "Added 3 days ago");
  assert.equal(deviceHistory({ ...DEVICE, reauthorized_at: "2026-10-07T11:00:00Z" }, NOW), "Added 3 days ago, reauthorized about 1 hour ago");
});

const answer = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

test("lists the desktop sign-ins, or asks for a recent sign-in, as the agent does to show them", async () => {
  const signIns = [{ id: "s-1", created_at: "2026-10-07T10:00:00Z", last_used_at: "2026-10-07T11:00:00Z", device_id: "d-1", device_name: "Flavius's ThinkPad" }];
  assert.deepEqual(await signInsFrom(answer(200, signIns)), signIns);
  const stale = { detail: { code: "recent_sign_in_required", message: "Sign in again: adding or restoring a computer needs a sign-in from the last 10 minutes." } };
  assert.equal(await signInsFrom(answer(403, stale)), "sign-in-again");
  await assert.rejects(signInsFrom(answer(403, { detail: "Devices belong to a user account." })), { message: "Devices belong to a user account." });
  await assert.rejects(signInsFrom(new Response("<html>", { status: 502 })), { message: "Failed to list your desktop sign-ins" });
});
