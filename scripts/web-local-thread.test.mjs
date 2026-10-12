// A project's thread on a folder of this computer, as the web client starts one from its card in
// Surogate Desktop (web/src/lib/local-thread.ts), against a fake bridge and fake project routes.
import assert from "node:assert/strict";
import { test } from "node:test";

import { localThreads, NO_FOLDER, projectFolderLost, startLocalThread } from "../web/src/lib/local-thread.ts";

const PREPARED = { folder: "/home/flavius/Budget", mode: "ask", nonce: "n".repeat(43), token: "t".repeat(43) };
const CARD = { projectId: "p-1", proposalId: "pr-1", key: "2", title: "Check the totals" };
const ROW = { id: "t-1", title: "Check the totals", group: "working" };
const THIS_COMPUTER = { deviceId: "d-1", name: "Flavius's ThinkPad" };

function bridge({
  device = THIS_COMPUTER, localFolders = true, prepare = async () => PREPARED, bind = async () => {}, projectFolders = true,
} = {}) {
  const calls = [];
  return {
    calls,
    ...(projectFolders ? {
      prepareThreadFolder: async (masterSessionId) => {
        calls.push(["prepareThreadFolder", masterSessionId]);
        return prepare();
      },
    } : {}),
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
// A project in the cloud, unless *place* names the computer it was made on.
function routes({ refuse = null, online = true, place = { kind: "cloud" } } = {}) {
  const asked = [];
  return {
    asked,
    online: async (deviceId) => {
      asked.push(["online", deviceId]);
      return online;
    },
    get: async (projectId) => {
      asked.push(["get", projectId]);
      return { name: "Q3 report", masterSessionId: "m-1", place, folder: place.kind === "device" ? PREPARED.folder : null };
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
    ["get", "p-1"],
    ["online", "d-1"],
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
    assert.deepEqual([desktop.calls, agent.asked], [[["getDevice"]], [["get", "p-1"]]]);
  }
});

test("makes no thread when the user cancels the sheet", async () => {
  const agent = routes();
  await assert.rejects(startLocalThread(bridge({ prepare: async () => null }), agent, CARD), { message: NO_FOLDER });
  assert.deepEqual(agent.asked, [["get", "p-1"], ["online", "d-1"]]);
});

test("asks for no folder while the agent does not hear this computer, and says so", async () => {
  const desktop = bridge();
  const agent = routes({ online: false });
  await assert.rejects(startLocalThread(desktop, agent, CARD), {
    message: "Flavius's ThinkPad is not connected to the agent right now, so this thread was not started. Allow it again once it is.",
  });
  assert.deepEqual([desktop.calls, agent.asked], [[["getDevice"]], [["get", "p-1"], ["online", "d-1"]]]);
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

test("offers a thread on this computer only in Surogate Desktop, and says which computer it is", async () => {
  // In a browser there is no bridge: the card offers the cloud alone.
  assert.deepEqual(localThreads(undefined, routes()), {});
  const { startLocalThread: start, getLocalDeviceId } = localThreads(bridge(), routes());
  assert.deepEqual(await start(CARD), ROW);
  assert.equal(await getLocalDeviceId(), "d-1");
  assert.equal(await localThreads(bridge({ device: null, localFolders: false }), routes()).getLocalDeviceId(), null);
});

// A project made in Surogate Desktop on this computer: its threads work in copies of its folder here.
const ON_THIS_COMPUTER = { kind: "device", deviceId: "d-1", deviceName: "Flavius's ThinkPad", online: true };

test("starts a computer project's thread at once in a copy of the project's folder, asking for no folder", async () => {
  const desktop = bridge();
  const agent = routes({ place: ON_THIS_COMPUTER });
  assert.deepEqual(await startLocalThread(desktop, agent, CARD), ROW);
  // The project's folder, as its coordinator's binding holds it here: no sheet asks again.
  assert.deepEqual(desktop.calls, [["getDevice"], ["prepareThreadFolder", "m-1"], ["bindSession", "t-1", "t".repeat(43)]]);
  assert.deepEqual(agent.asked, [
    ["get", "p-1"],
    ["online", "d-1"],
    ["makeOnComputer", "p-1", "pr-1", "2", { kind: "device", device_id: "d-1", folder: "/home/flavius/Budget", nonce: "n".repeat(43) }],
    ["begin", "p-1", "t-1"],
  ]);
});

test("starts no computer project's thread anywhere but on its computer, and never offers the cloud", async () => {
  const elsewhere = { ...ON_THIS_COMPUTER, deviceId: "d-2", deviceName: "Flavius's desktop" };
  for (const [desktop, place, message] of [
    [bridge(), elsewhere, "This project works in a folder on Flavius's desktop: start this thread in Surogate Desktop there."],
    [bridge({ device: null, localFolders: false }), elsewhere,
      "Surogate can't work on folders of this computer for this account. Start this thread in Surogate Desktop on Flavius's desktop."],
    [bridge({ localFolders: false }), ON_THIS_COMPUTER,
      "Local access to this computer was revoked. Restore it from Surogate's sidebar, then start this thread again."],
    [bridge({ projectFolders: false }), ON_THIS_COMPUTER, "This Surogate Desktop cannot start a thread in its project's folder: update it."],
  ]) {
    const agent = routes({ place });
    await assert.rejects(startLocalThread(desktop, agent, CARD), { message });
    assert.equal(desktop.calls.some(([name]) => name === "prepareFolder" || name === "bindSession"), false);
    assert.equal(agent.asked.some(([name]) => name === "makeOnComputer"), false);
  }
});

test("says a computer project's thread is started again, never allowed, when its computer is away or its binding fails", async () => {
  await assert.rejects(startLocalThread(bridge(), routes({ place: ON_THIS_COMPUTER, online: false }), CARD), {
    message: "Flavius's ThinkPad is not connected to the agent right now, so this thread was not started. Start it again once it is.",
  });
  const refused = async () => {
    throw new Error("This computer could not record the folder for this chat");
  };
  await assert.rejects(startLocalThread(bridge({ bind: refused }), routes({ place: ON_THIS_COMPUTER }), CARD), {
    message: "Surogate could not set up this thread's folder on this computer: This computer could not record the folder for this chat. Start it again.",
  });
});

test("starts no thread of a computer project whose computer can no longer reach its folder, and says to use this computer for it", async () => {
  const lost = { ...ON_THIS_COMPUTER, deviceId: "d-0", online: false, lost: true };
  // On the computer it was made on too: there the folder could not be set up.
  for (const place of [lost, { ...ON_THIS_COMPUTER, lost: true }]) {
    const desktop = bridge();
    const agent = routes({ place });
    await assert.rejects(startLocalThread(desktop, agent, CARD), {
      message: "This project's folder can no longer be reached on Flavius's ThinkPad. Choose Use this computer for this project in its Overview, then start this thread.",
    });
    assert.deepEqual(desktop.calls, [["getDevice"]]);
    assert.equal(agent.asked.some(([name]) => name === "makeOnComputer"), false);
  }
});

test("says whether a computer project's folder can no longer be reached on its computer, as the agent says it", async () => {
  for (const [place, lost] of [
    [{ ...ON_THIS_COMPUTER, lost: true }, true], [{ ...ON_THIS_COMPUTER, lost: false }, false], [ON_THIS_COMPUTER, false],
    [{ kind: "cloud" }, false],
  ]) {
    const agent = routes({ place });
    assert.equal(await projectFolderLost(agent, "p-1"), lost, JSON.stringify(place));
    assert.deepEqual(agent.asked, [["get", "p-1"]]);
  }
});
