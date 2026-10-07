// A chat on a folder of this computer, as the web client makes and shows one in Surogate Desktop
// (web/src/lib/local-chat.ts), against a fake bridge and a fake server.
import assert from "node:assert/strict";
import { test } from "node:test";

import { createChat, desktopSessionsOf, folderCalls, localChatOf, NO_FOLDER, newChatPlace, saidBy, switchMode } from "../web/src/lib/local-chat.ts";

const PREPARED = { folder: "/home/flavius/notes", mode: "ask", nonce: "n".repeat(43), token: "t".repeat(43) };
const THIS_COMPUTER = { device: { deviceId: "d-1", name: "Flavius's ThinkPad" }, localFolders: true };
const NONE = { device: null, localFolders: false };
const AGENT = { desktopSessions: true, multiSession: true };
const UNREAD = { desktopSessions: null, multiSession: null };

function bridge({ device = THIS_COMPUTER, prepared = PREPARED, bind = async () => {} } = {}) {
  const calls = [];
  const desktop = {
    calls,
    device,
    getDevice: async () => {
      calls.push(["getDevice"]);
      return desktop.device;
    },
    prepareFolder: async (choice) => {
      calls.push(["prepareFolder", choice]);
      return prepared;
    },
    bindSession: async (sessionId, token) => {
      calls.push(["bindSession", sessionId, token]);
      await bind();
    },
    cancelPrepared: async (token) => {
      calls.push(["cancelPrepared", token]);
    },
  };
  return desktop;
}

// The agent, as the page reaches it: what it was asked to make, whether it hears this computer now,
// and its config as a read of it again finds it.
function agent({ online = true, refuse = null, config = AGENT } = {}) {
  const made = [];
  const server = {
    made,
    reads: 0,
    create: async (execution) => {
      if (refuse) throw new Error(refuse);
      made.push(execution);
      return { id: "s-1" };
    },
    online: async (deviceId) => online && deviceId === "d-1",
    capabilities: async () => {
      server.reads += 1;
      return config;
    },
  };
  return server;
}

test("confirms the folder on this computer, makes the chat with it, then binds it, and never sends the server the token", async () => {
  const desktop = bridge();
  const server = agent();
  assert.deepEqual(await createChat(desktop, AGENT, "last", server), { id: "s-1" });
  assert.deepEqual(server.made, [{ kind: "device", device_id: "d-1", folder: "/home/flavius/notes", nonce: "n".repeat(43) }]);
  assert.deepEqual(desktop.calls, [["getDevice"], ["prepareFolder", "last"], ["bindSession", "s-1", "t".repeat(43)]]);
  assert.equal(JSON.stringify(server.made).includes("t".repeat(43)), false);
});

test("makes the first chat after this computer was added in its folder, though the line under the composer was drawn before", async () => {
  // A fresh install: the page drew the line before the app had added this computer to the agent.
  const desktop = bridge({ device: NONE });
  assert.equal(newChatPlace(await desktop.getDevice(), AGENT, "last").local, false);
  desktop.device = THIS_COMPUTER;
  const server = agent();
  await createChat(desktop, AGENT, "last", server);
  assert.deepEqual(server.made, [{ kind: "device", device_id: "d-1", folder: "/home/flavius/notes", nonce: "n".repeat(43) }]);
});

test("makes a chat in the cloud where this computer can work on no folder when it is sent, and in a browser", async () => {
  for (const device of [NONE, { device: THIS_COMPUTER.device, localFolders: false }]) {
    const desktop = bridge({ device });
    const server = agent();
    await createChat(desktop, AGENT, "last", server);
    assert.deepEqual([server.made, desktop.calls], [[undefined], [["getDevice"]]]);
  }
  const server = agent();
  await createChat(undefined, AGENT, "last", server);
  assert.deepEqual(server.made, [undefined]);
});

test("asks for no folder while the agent does not hear this computer, and says so", async () => {
  const desktop = bridge();
  const server = agent({ online: false });
  await assert.rejects(createChat(desktop, AGENT, "last", server), {
    message: "Flavius's ThinkPad is not connected to the agent right now, so this chat was not made. Send it again once it is.",
  });
  assert.deepEqual([server.made, desktop.calls], [[], [["getDevice"]]]);
});

test("lets the confirmation go when the chat cannot be made, and says why in the server's words", async () => {
  const desktop = bridge();
  await assert.rejects(createChat(desktop, AGENT, "pick", agent({ refuse: "Local access to this computer was revoked." })), {
    message: "Local access to this computer was revoked.",
  });
  assert.deepEqual(desktop.calls.slice(-1), [["cancelPrepared", "t".repeat(43)]]);
  assert.equal(desktop.calls.some(([name]) => name === "bindSession"), false);
});

test("reads the agent's config again at the first message when it could not be read, and makes no chat while it still cannot be", async () => {
  const read = agent();
  await createChat(bridge(), UNREAD, "last", read);
  assert.deepEqual([read.reads, read.made], [1, [{ kind: "device", device_id: "d-1", folder: "/home/flavius/notes", nonce: "n".repeat(43) }]]);
  const desktop = bridge();
  const unread = agent({ config: UNREAD });
  await assert.rejects(createChat(desktop, UNREAD, "last", unread), {
    message: "Surogate could not read the agent's settings, so this chat was not made. Send it again.",
  });
  assert.deepEqual([unread.made, desktop.calls], [[], []]);
  // A browser offers no folder: its chat is made in the cloud, the config read or not.
  const browser = agent({ config: UNREAD });
  await createChat(undefined, UNREAD, "last", browser);
  assert.deepEqual([browser.reads, browser.made], [0, [undefined]]);
});

test("makes no chat when the user cancels the folder sheet", async () => {
  const server = agent();
  await assert.rejects(createChat(bridge({ prepared: null }), AGENT, "last", server), { message: NO_FOLDER });
  assert.deepEqual(server.made, []);
});

test("says plainly that the chat was made when its folder could not be set up, and keeps it", async () => {
  // As Electron rejects a call the main process refused: the call's name, then the desktop's words.
  const desktop = bridge({
    bind: async () => {
      throw new Error("Error invoking remote method 'desktop:bindSession': Error: This folder's confirmation expired before its chat was created");
    },
  });
  await assert.rejects(createChat(desktop, AGENT, "last", agent()), {
    message: "Surogate made this chat but could not set up its folder on this computer: This folder's confirmation expired before its chat was created. Start a new chat.",
  });
  assert.equal(desktop.calls.some(([name]) => name === "cancelPrepared"), false);
  assert.equal(saidBy(new Error("The folder /home/f/notes is not there")), "The folder /home/f/notes is not there");
});

test("reads local folders as the agent's config says them: only true is true, an older server says nothing, and a failed read is unread", () => {
  assert.deepEqual(
    [{ desktop_sessions: true }, { desktop_sessions: false }, {}, { desktop_sessions: "yes" }]
      .map((config) => desktopSessionsOf({ agent_id: "a-1", ...config })),
    [true, false, false, false],
  );
  // What fetchAuthConfig answers when the read fails: every server's answer names its agent.
  assert.equal(desktopSessionsOf({ self_registration_enabled: false, firebase: null }), null);
});

test("says where a new chat works: on a folder of this computer, or in the cloud and why", () => {
  const older = { desktopSessions: desktopSessionsOf({ agent_id: "a-1" }), multiSession: true };
  const cases = [
    [null, AGENT, "last", { local: false, text: null }],
    [THIS_COMPUTER, AGENT, "last", { local: true, text: "Works in a folder on this computer: the one you used last, or a new one. You confirm it when you send." }],
    [THIS_COMPUTER, AGENT, "pick", { local: true, text: "Works in a folder on this computer that you choose when you send." }],
    // An older server, which says nothing of local folders: no computer can be added to it.
    [NONE, older, "last", { local: false, text: "This server doesn't support local folders yet, so this chat works in the cloud." }],
    // Before the agent's config is read, nothing is said yet.
    [NONE, { desktopSessions: null, multiSession: null }, "last", { local: false, text: null }],
    [{ device: THIS_COMPUTER.device, localFolders: false }, AGENT, "last",
      { local: false, text: "Local access revoked, so this chat works in the cloud. Restore it from Surogate's sidebar." }],
    [NONE, AGENT, "last",
      { local: false, text: "Surogate can't work on folders of this computer for this account, so this chat works in the cloud." }],
    // One conversation, in the cloud: there is no new chat to place.
    [NONE, { desktopSessions: true, multiSession: false }, "last", { local: false, text: null }],
  ];
  for (const [device, capabilities, choice, place] of cases) {
    assert.deepEqual(newChatPlace(device, capabilities, choice), place);
  }
});

const EXECUTION = { kind: "device", device_id: "d-1", device_name: "Flavius's ThinkPad" };
const DEVICES = [{ id: "d-1", name: "Flavius's ThinkPad", revoked_at: null }, { id: "d-2", name: "Office iMac", revoked_at: "2026-10-01T09:00:00Z" }];

test("shows a chat on a folder of a computer: the folder's name, the computer, and, here, how it asks", () => {
  const config = { execution: EXECUTION, workspace_path: "/home/flavius/Documente/Lucrări — 2026/" };
  assert.deepEqual(localChatOf("s-1", config, DEVICES, null), {
    root: "s-1", folder: "/home/flavius/Documente/Lucrări — 2026/", name: "Lucrări — 2026", computer: "Flavius's ThinkPad", revoked: false, here: null,
  });
  const here = { folder: "/home/flavius/Documente/Lucrări — 2026", mode: "ask" };
  assert.deepEqual(localChatOf("s-1", config, DEVICES, here).here, here);
  // A sub-agent's chat works in its root's folder: the binding is the root's.
  assert.equal(localChatOf("helper", { ...config, sandbox_root_session_id: "s-1" }, DEVICES, null).root, "s-1");
  // A revoked computer, named as the agent knows it now; and before its list has come.
  const revoked = { execution: { kind: "device", device_id: "d-2", device_name: "iMac" }, workspace_path: "/Users/f/notes" };
  assert.deepEqual([localChatOf("s-2", revoked, DEVICES, null).computer, localChatOf("s-2", revoked, DEVICES, null).revoked], ["Office iMac", true]);
  assert.deepEqual([localChatOf("s-2", revoked, null, null).computer, localChatOf("s-2", revoked, null, null).revoked], ["iMac", false]);
  // A chat in the cloud has none.
  assert.equal(localChatOf("s-3", {}, DEVICES, null), null);
  assert.equal(localChatOf("s-3", { execution: { kind: "cloud" } }, DEVICES, null), null);
});

test("lets the page make a chat ask every time, and only ask the desktop to let it work freely", async () => {
  const calls = [];
  const desktop = {
    setMode: async (...args) => calls.push(["setMode", ...args]),
    requestFreeMode: async (...args) => {
      calls.push(["requestFreeMode", ...args]);
      return false;
    },
  };
  await switchMode(desktop, "s-1", "ask");
  await switchMode(desktop, "s-1", "free");
  assert.deepEqual(calls, [["setMode", "s-1", "ask"], ["requestFreeMode", "s-1"]]);
});

test("uses a chat's folder calls only where this desktop has them", () => {
  const full = { getBinding: async () => null, revealFolder: async () => {}, onBindingChanged: () => () => {} };
  assert.equal(folderCalls(full), full);
  assert.equal(folderCalls({ getBinding: async () => null }), null);
  assert.equal(folderCalls(undefined), null);
});
