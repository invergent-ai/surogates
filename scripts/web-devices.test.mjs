// The user's computers and desktop sign-ins at an agent, as the web client's Settings → Devices
// and its "computer added" notice show them (web/src/lib/devices.ts).
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  addedNotice, addedNotices, deviceHistory, deviceState, dismissedNotices, dismissNotice, signInsFrom,
} from "../web/src/lib/devices.ts";

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

test("tells of each computer added or reauthorized in the last 7 days that can still work, until it is dismissed here", () => {
  const devices = [
    { ...DEVICE, id: "new", name: "New laptop", created_at: "2026-10-06T12:00:00Z" },
    { ...DEVICE, id: "old", name: "Old desktop", created_at: "2026-09-01T12:00:00Z" },
    { ...DEVICE, id: "back", name: "Restored laptop", created_at: "2026-09-01T12:00:00Z", reauthorized_at: "2026-10-05T12:00:00Z" },
    { ...DEVICE, id: "gone", name: "Revoked laptop", created_at: "2026-10-06T12:00:00Z", revoked_at: "2026-10-06T13:00:00Z" },
  ];
  assert.deepEqual(addedNotices(devices, NOW, new Set()), [
    { key: "new@2026-10-06T12:00:00Z", name: "New laptop" },
    { key: "back@2026-10-05T12:00:00Z", name: "Restored laptop" },
  ]);
  // Dismissed here, it is told no more; a dismissal of an earlier time does not hide a later reauthorization.
  const dismissed = new Set(["new@2026-10-06T12:00:00Z", "back@2026-09-01T12:00:00Z"]);
  assert.deepEqual(addedNotices(devices, NOW, dismissed).map((notice) => notice.name), ["Restored laptop"]);
  // Seven days on, it goes by itself.
  assert.deepEqual(addedNotices(devices, new Date("2026-10-13T12:00:01Z"), new Set()), []);
  assert.equal(addedNotice("New laptop", "acme.surogate.ai"), "New laptop can now work on folders of your computer through acme.surogate.ai");
});

test("keeps what this browser dismissed, only while it is told, and dismisses for the page alone where nothing can be kept", () => {
  const kept = new Map();
  const storage = { getItem: (key) => kept.get(key) ?? null, setItem: (key, value) => kept.set(key, value) };
  const told = ["new@2026-10-06T12:00:00Z", "back@2026-10-05T12:00:00Z"];
  dismissNotice(storage, "new@2026-10-06T12:00:00Z", told);
  dismissNotice(storage, "back@2026-10-05T12:00:00Z", told);
  assert.deepEqual([...dismissedNotices(storage)], told);
  // A notice no longer told, as one past its 7 days, is kept no more: the list never grows past what is told.
  dismissNotice(storage, "later@2026-10-07T11:00:00Z", ["back@2026-10-05T12:00:00Z", "later@2026-10-07T11:00:00Z"]);
  assert.deepEqual([...dismissedNotices(storage)], ["back@2026-10-05T12:00:00Z", "later@2026-10-07T11:00:00Z"]);
  kept.set("surogate:computers-told", "not json");
  assert.deepEqual([...dismissedNotices(storage)], []);
  const blocked = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
  dismissNotice(blocked, "new@2026-10-06T12:00:00Z", told);
  assert.deepEqual([...dismissedNotices(blocked)], []);
  assert.deepEqual([...dismissedNotices(null)], []);
});
