// The web client's side of Surogate Desktop (web/src/lib/desktop-bridge.ts), against a fake bridge.
import assert from "node:assert/strict";
import { test } from "node:test";

import { joinDesktop, leaveDesktop, sessionProjects, signInFromDesktop } from "../web/src/lib/desktop-bridge.ts";
import { projectFixtures } from "../web/src/lib/projects.ts";

const ACCOUNT = { name: "Flavius", email: "f@example.com", userId: "u", orgId: "o" };
const MASTER = "0b6f3c1e-8a2d-4c5e-9f10-1a2b3c4d5e6f";

function bridge(code = "web-code") {
  const calls = [];
  return {
    calls,
    version: 1,
    getDevice: async () => ({ device: null, localFolders: true }),
    webSignIn: async () => (code === null ? null : { code }),
    setAccount: async (account) => calls.push(["setAccount", account]),
    registerProjects: async (source) => calls.push(["registerProjects", source === null ? null : typeof source.list]),
  };
}

function api(sessions = []) {
  return {
    account: async () => ACCOUNT,
    sessions: async () => sessions,
  };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 50));

test("tells the desktop who is signed in and serves its projects; stopping withdraws both", async () => {
  const desktop = bridge();
  const stop = joinDesktop(desktop, api());
  await settle();
  assert.deepEqual(desktop.calls, [["setAccount", ACCOUNT], ["registerProjects", "function"]]);
  stop();
  await settle();
  assert.deepEqual(desktop.calls.slice(2), [["setAccount", null], ["registerProjects", null]]);
});

test("serves nothing once stopped while the desktop is still told who is signed in", async () => {
  const desktop = bridge();
  const told = desktop.setAccount;
  // The desktop takes 20 ms to hear who is signed in: the user signs out meanwhile.
  desktop.setAccount = async (account) => {
    await told(account);
    await new Promise((resolve) => setTimeout(resolve, 20));
  };
  const stop = joinDesktop(desktop, api());
  await new Promise((resolve) => setTimeout(resolve, 5));
  stop();
  await settle();
  assert.deepEqual(desktop.calls, [["setAccount", ACCOUNT], ["setAccount", null], ["registerProjects", null]]);
});

test("tells the desktop that nobody is signed in on a page with no sign-in, as after an expired session", async () => {
  const desktop = bridge();
  leaveDesktop(desktop);
  await settle();
  assert.deepEqual(desktop.calls, [["setAccount", null], ["registerProjects", null]]);
});

test("signs the page in with the app's sign-in: its one-time code, exchanged for the page's own session", async () => {
  const exchanged = [];
  const stored = [];
  const signedIn = await signInFromDesktop(
    bridge("web-code"),
    async (code) => {
      exchanged.push(code);
      return { access_token: "at", refresh_token: "rt" };
    },
    (access, refresh) => stored.push([access, refresh]),
  );
  assert.equal(signedIn, true);
  assert.deepEqual([exchanged, stored], [["web-code"], [["at", "rt"]]]);
});

test("signs nothing in while nobody is signed in to the app", async () => {
  const stored = [];
  assert.equal(await signInFromDesktop(bridge(null), async () => assert.fail("no code to exchange"), (...tokens) => stored.push(tokens)), false);
  assert.deepEqual(stored, []);
});

test("serves each root web session as a project, with every session under it as a thread, whatever its channel", async () => {
  const now = Date.parse("2026-10-20T12:00:00Z");
  const row = (id, parentId, channel, status, updatedAt, title = `Session ${id[0]}`) =>
    ({ id, parentId, channel, title, status, createdAt: "2026-10-01T00:00:00", updatedAt });
  const source = sessionProjects(api([
    row(MASTER, null, "web", "completed", "2026-10-01T00:00:00", "Quarterly report"),
    row("1c7f4d2f-9b3e-4d6f-8a21-2b3c4d5e6f70", MASTER, "worker", "failed", "2026-10-20T10:00:00"),
    row("2d8a5e3a-ac4f-4e7a-9b32-3c4d5e6f7081", "1c7f4d2f-9b3e-4d6f-8a21-2b3c4d5e6f70", "delegation", "active", "2026-10-20T11:00:00"),
    row("3e9b6f4b-bd5a-4f8b-8c43-4d5e6f708192", MASTER, "task", "completed", "2026-10-19T12:00:00"),
    row("4fac7a5c-ce6b-4a9c-9d54-5e6f708192a3", MASTER, "scheduled", "paused", "2026-10-01T12:00:00"),
    // A Slack conversation is not a project.
    row("5abd8b6d-df7c-4bad-8e65-6f708192a3b4", null, "slack", "active", "2026-10-20T11:30:00"),
  ]), () => now);
  assert.deepEqual(await source.list(), [{
    id: MASTER, name: "Quarterly report", icon: null, createdAt: "2026-10-01T00:00:00.000Z", updatedAt: "2026-10-20T11:00:00.000Z",
    waiting: 1, working: 1,
  }]);
  assert.equal((await source.get(MASTER)).masterSessionId, MASTER);
  // Last active first; a thread's own children are the project's threads too; times go out as UTC with Z.
  assert.deepEqual((await source.threads(MASTER)).map(({ title, group, reason, statusLine, updatedAt }) =>
    [title, group, reason, statusLine, updatedAt]), [
    ["Session 2", "working", null, null, "2026-10-20T11:00:00.000Z"],
    ["Session 1", "waiting", "failed", "Failed", "2026-10-20T10:00:00.000Z"],
    ["Session 3", "idle", null, null, "2026-10-19T12:00:00.000Z"],
    // Not active for a week: resolved, as Section 12 resolves threads.
    ["Session 4", "resolved", null, "Paused", "2026-10-01T12:00:00.000Z"],
  ]);
  assert.deepEqual([await source.library(MASTER), await source.routines(MASTER)], [[], []]);
  await assert.rejects(source.get("5abd8b6d-df7c-4bad-8e65-6f708192a3b4"), /No such project/);
  await assert.rejects(source.create({ name: "New" }), /This agent's server keeps no projects yet/);
});

test("the fixtures hold a thread of every group, and every reason", () => {
  const { threads } = projectFixtures();
  const rows = Object.values(threads).flat();
  assert.deepEqual([...new Set(rows.map((row) => row.group))].sort(), ["idle", "resolved", "waiting", "working"]);
  assert.deepEqual([...new Set(rows.map((row) => row.reason))].sort(), ["approval", "computer", "failed", "question", null].sort());
});
