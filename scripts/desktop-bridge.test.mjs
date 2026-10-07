// The web client's side of Surogate Desktop (web/src/lib/desktop-bridge.ts), against a fake bridge.
import assert from "node:assert/strict";
import { test } from "node:test";

import { joinDesktop, leaveDesktop, signInFromDesktop } from "../web/src/lib/desktop-bridge.ts";
import { projectFixtures } from "../web/src/lib/projects.ts";

const ACCOUNT = { name: "Flavius", email: "f@example.com", userId: "u", orgId: "o" };

function bridge(code = "web-code") {
  const calls = [];
  return {
    calls,
    version: 1,
    getDevice: async () => ({ device: null, localFolders: true }),
    webSignIn: async () => (code === null ? null : { code }),
    setAccount: async (account) => calls.push(["setAccount", account]),
    registerProjects: async (source) => calls.push(["registerProjects", source]),
  };
}

// The project routes, as the page serves them: a stand-in the tests tell apart by its identity.
const PROJECTS = { list: async () => [] };

function api() {
  return {
    account: async () => ACCOUNT,
    projects: PROJECTS,
  };
}

// Every step of a join waits on a fake that answers at once: what is due has happened once the
// event loop has gone round a few times, with no clock involved.
async function settled() {
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setImmediate(resolve));
}

test("tells the desktop who is signed in, and serves the project routes", async () => {
  const desktop = bridge();
  joinDesktop(desktop, api());
  await settled();
  assert.deepEqual(desktop.calls, [["setAccount", ACCOUNT], ["registerProjects", PROJECTS]]);
});

test("a join ended and made again at once, as StrictMode makes it, tells the desktop no sign-out", async () => {
  const desktop = bridge();
  const first = joinDesktop(desktop, api());
  first();
  joinDesktop(desktop, api());
  await settled();
  assert.deepEqual(desktop.calls, [["setAccount", ACCOUNT], ["registerProjects", PROJECTS]]);
});

test("serves nothing once ended while the desktop is still told who is signed in", async () => {
  const desktop = bridge();
  const told = desktop.setAccount;
  let hear = () => {};
  // The desktop hears who is signed in only once the test lets it: the join ends meanwhile.
  desktop.setAccount = async (account) => {
    await told(account);
    await new Promise((resolve) => {
      hear = resolve;
    });
  };
  const stop = joinDesktop(desktop, api());
  await settled();
  stop();
  hear();
  await settled();
  assert.deepEqual(desktop.calls, [["setAccount", ACCOUNT]]);
});

test("tells the desktop that nobody is signed in on a page with no sign-in, as after an expired session", async () => {
  const desktop = bridge();
  leaveDesktop(desktop);
  await settled();
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

test("the fixtures hold a thread of every group, and every reason", () => {
  const { threads } = projectFixtures();
  const rows = Object.values(threads).flat();
  assert.deepEqual([...new Set(rows.map((row) => row.group))].sort(), ["idle", "resolved", "waiting", "working"]);
  assert.deepEqual([...new Set(rows.map((row) => row.reason))].sort(), ["approval", "computer", "failed", "question", null].sort());
});
