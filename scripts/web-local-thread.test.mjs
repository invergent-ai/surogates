// A project's thread on a folder of this computer, as the web client starts one from its card in
// Surogate Desktop (web/src/lib/local-thread.ts), against a fake bridge and fake project routes.
import assert from "node:assert/strict";
import { test } from "node:test";

import { localThreads, NO_FOLDER, startLocalThread } from "../web/src/lib/local-thread.ts";

const PREPARED = { folder: "/home/flavius/Budget", mode: "ask", nonce: "n".repeat(43), token: "t".repeat(43) };
const CARD = { projectId: "p-1", proposalId: "pr-1", key: "2", title: "Check the totals" };
const ROW = { id: "t-1", title: "Check the totals", group: "working" };
const THIS_COMPUTER = { deviceId: "d-1", name: "Flavius's ThinkPad" };

function bridge({ device = THIS_COMPUTER, localFolders = true, prepare = async () => PREPARED, bind = async () => {} } = {}) {
  const calls = [];
  return {
    calls,
    getDevice: async () => {
      calls.push(["getDevice"]);
      return { device, localFolders };
    },
    prepareFolder: async (choice, thread) => {
      calls.push(["prepareFolder", choice, thread]);
      return prepare();
    },
    bindSession: async (sessionId, token) => {
      calls.push(["bindSession", sessionId, token]);
      await bind();
    },
    cancelPrepared: async (token) => {
      calls.push(["cancelPrepared", token]);
    },
  };
}

// The agent, as the page reaches it: the project routes, and whether it hears a computer. What each was asked.
function routes({ refuse = null, online = true } = {}) {
  const asked = [];
  return {
    asked,
    online: async (deviceId) => {
      asked.push(["online", deviceId]);
      return online;
    },
    get: async (projectId) => {
      asked.push(["get", projectId]);
      return { name: "Q3 report" };
    },
    makeOnComputer: async (...args) => {
      asked.push(["makeOnComputer", ...args]);
      if (refuse) throw new Error(refuse);
      return "t-1";
    },
    begin: async (...args) => {
      asked.push(["begin", ...args]);
      return ROW;
    },
  };
}

test("confirms a folder on this computer for the project's thread, makes the thread there, binds it, then begins it", async () => {
  const desktop = bridge();
  const agent = routes();
  assert.deepEqual(await startLocalThread(desktop, agent, CARD), ROW);
  assert.deepEqual(desktop.calls, [
    ["getDevice"],
    // The sheet names the project and the thread.
    ["prepareFolder", "last", { project: "Q3 report", thread: "Check the totals" }],
    ["bindSession", "t-1", "t".repeat(43)],
  ]);
  assert.deepEqual(agent.asked, [
    ["online", "d-1"],
    ["get", "p-1"],
    ["makeOnComputer", "p-1", "pr-1", "2", { kind: "device", device_id: "d-1", folder: "/home/flavius/Budget", nonce: "n".repeat(43) }],
    ["begin", "p-1", "t-1"],
  ]);
  // The confirmation's token never leaves the bridge.
  assert.equal(JSON.stringify(agent.asked).includes("t".repeat(43)), false);
});

test("asks for no folder where this computer works on none for this account, and says what to do", async () => {
  for (const [desktop, message] of [
    [bridge({ device: null, localFolders: false }), "Surogate can't work on folders of this computer for this account. Run this thread in the cloud instead."],
    [bridge({ localFolders: false }), "Local access to this computer was revoked. Restore it from Surogate's sidebar, or run this thread in the cloud instead."],
  ]) {
    const agent = routes();
    await assert.rejects(startLocalThread(desktop, agent, CARD), { message });
    assert.deepEqual([desktop.calls, agent.asked], [[["getDevice"]], []]);
  }
});

test("makes no thread when the user cancels the sheet", async () => {
  const agent = routes();
  await assert.rejects(startLocalThread(bridge({ prepare: async () => null }), agent, CARD), { message: NO_FOLDER });
  assert.deepEqual(agent.asked, [["online", "d-1"], ["get", "p-1"]]);
});

test("asks for no folder while the agent does not hear this computer, and says so", async () => {
  const desktop = bridge();
  const agent = routes({ online: false });
  await assert.rejects(startLocalThread(desktop, agent, CARD), {
    message: "Flavius's ThinkPad is not connected to the agent right now, so this thread was not started. Allow it again once it is.",
  });
  assert.deepEqual([desktop.calls, agent.asked], [[["getDevice"]], [["online", "d-1"]]]);
});

test("lets the confirmation go when the thread cannot be made, and says why in the server's words", async () => {
  const desktop = bridge();
  await assert.rejects(startLocalThread(desktop, routes({ refuse: "Local access to this computer was revoked." }), CARD), {
    message: "Local access to this computer was revoked.",
  });
  assert.deepEqual(desktop.calls.slice(-1), [["cancelPrepared", "t".repeat(43)]]);
  assert.equal(desktop.calls.some(([name]) => name === "bindSession"), false);
});

test("says the desktop's refusals in its own words, without the name of the call", async () => {
  // As Electron rejects a call the main process refused: the call's name, then the desktop's words.
  const refused = (message) => async () => {
    throw new Error(`Error invoking remote method 'desktop:prepareFolder': Error: ${message}`);
  };
  await assert.rejects(startLocalThread(bridge({ prepare: refused("Surogate is already asking") }), routes(), CARD), {
    message: "Surogate is already asking",
  });
  const agent = routes();
  const desktop = bridge({ bind: refused("This folder's confirmation expired before its chat was created") });
  await assert.rejects(startLocalThread(desktop, agent, CARD), {
    message: "Surogate could not set up this thread's folder on this computer: This folder's confirmation expired before its chat was created. Allow it again.",
  });
  assert.equal(agent.asked.some(([name]) => name === "begin"), false);
  assert.equal(desktop.calls.some(([name]) => name === "cancelPrepared"), false);
});

test("offers a thread on this computer only in Surogate Desktop", async () => {
  // In a browser there is no bridge: the card offers the cloud alone.
  assert.equal(localThreads(undefined, routes()).startLocalThread, undefined);
  const { startLocalThread: start } = localThreads(bridge(), routes());
  assert.deepEqual(await start(CARD), ROW);
});
