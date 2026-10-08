// A chat on a folder of this computer, as the web client makes and shows one in Surogate Desktop
// (web/src/lib/local-chat.ts), against a fake bridge and a fake server.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  actOnBrowser, browserPane, browserPanes, computerBrowser, createChat, desktopSessionsOf, folderCalls, localChatOf, NO_FOLDER, newChatPlace,
  saidBy, switchMode,
} from "../web/src/lib/local-chat.ts";

const PREPARED = { folder: "/home/flavius/notes", mode: "ask", nonce: "n".repeat(43), token: "t".repeat(43) };
const THIS_COMPUTER = { device: { deviceId: "d-1", name: "Flavius's ThinkPad" }, localFolders: true };
const NONE = { device: null, localFolders: false };
const AGENT = { desktopSessions: true, multiSession: true };
const UNREAD = { desktopSessions: null, multiSession: null };
// What the line under the composer showed when the message was sent.
const SHOWN_HERE = { local: true };
const SHOWN_CLOUD = { local: false };

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
  assert.deepEqual(await createChat(desktop, AGENT, "last", SHOWN_HERE, server), { id: "s-1" });
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
  await createChat(desktop, AGENT, "last", SHOWN_CLOUD, server);
  assert.deepEqual(server.made, [{ kind: "device", device_id: "d-1", folder: "/home/flavius/notes", nonce: "n".repeat(43) }]);
});

test("makes a chat in the cloud where this computer can work on no folder when it is sent, and in a browser", async () => {
  for (const device of [NONE, { device: THIS_COMPUTER.device, localFolders: false }]) {
    const desktop = bridge({ device });
    const server = agent();
    await createChat(desktop, AGENT, "last", SHOWN_CLOUD, server);
    assert.deepEqual([server.made, desktop.calls], [[undefined], [["getDevice"]]]);
  }
  const server = agent();
  await createChat(undefined, AGENT, "last", SHOWN_CLOUD, server);
  assert.deepEqual(server.made, [undefined]);
});

test("makes no chat in the cloud where the line under the composer said this computer, and says why", async () => {
  // Its access ended after the line was drawn: revoked, or the agent ended its token.
  for (const [device, why] of [
    [{ device: THIS_COMPUTER.device, localFolders: false }, "Local access revoked"],
    [NONE, "Surogate can't work on folders of this computer for this account"],
  ]) {
    const desktop = bridge({ device });
    const server = agent();
    await assert.rejects(createChat(desktop, AGENT, "last", SHOWN_HERE, server), {
      message: `${why}, so this chat was not made. Send it again to make it in the cloud.`,
    });
    assert.deepEqual([server.made, desktop.calls], [[], [["getDevice"]]]);
    // Sent again, under the line that now says the cloud: made there.
    await createChat(desktop, AGENT, "last", newChatPlace(device, AGENT, "last"), server);
    assert.deepEqual(server.made, [undefined]);
  }
});

test("asks for no folder while the agent does not hear this computer, and says so", async () => {
  const desktop = bridge();
  const server = agent({ online: false });
  await assert.rejects(createChat(desktop, AGENT, "last", SHOWN_HERE, server), {
    message: "Flavius's ThinkPad is not connected to the agent right now, so this chat was not made. Send it again once it is.",
  });
  assert.deepEqual([server.made, desktop.calls], [[], [["getDevice"]]]);
  // Nor while the agent's list of computers cannot be read.
  const unlisted = bridge();
  const failing = { ...agent(), online: async () => Promise.reject(new Error("Failed to list your computers")) };
  await assert.rejects(createChat(unlisted, AGENT, "last", SHOWN_HERE, failing), { message: "Failed to list your computers" });
  assert.deepEqual([failing.made, unlisted.calls], [[], [["getDevice"]]]);
});

test("says the desktop's refusals in its own words, and makes no chat", async () => {
  // As Electron rejects a call the main process refused: the call's name, then the desktop's words.
  const refused = (call, words) => async () => {
    throw new Error(`Error invoking remote method 'desktop:${call}': Error: ${words}`);
  };
  const desktop = bridge();
  const server = agent();
  desktop.prepareFolder = refused("prepareFolder", "Surogate is already asking");
  await assert.rejects(createChat(desktop, AGENT, "last", SHOWN_HERE, server), { message: "Surogate is already asking" });
  desktop.getDevice = refused("getDevice", "This computer is registered with the agent for another account");
  await assert.rejects(createChat(desktop, AGENT, "last", SHOWN_HERE, server), {
    message: "This computer is registered with the agent for another account",
  });
  assert.deepEqual(server.made, []);
});

test("lets the confirmation go when the chat cannot be made, and says why in the server's words", async () => {
  const desktop = bridge();
  await assert.rejects(createChat(desktop, AGENT, "pick", SHOWN_HERE, agent({ refuse: "Local access to this computer was revoked." })), {
    message: "Local access to this computer was revoked.",
  });
  assert.deepEqual(desktop.calls.slice(-1), [["cancelPrepared", "t".repeat(43)]]);
  assert.equal(desktop.calls.some(([name]) => name === "bindSession"), false);
});

test("reads the agent's config again at the first message when it could not be read, and makes no chat while it still cannot be", async () => {
  const read = agent();
  await createChat(bridge(), UNREAD, "last", SHOWN_CLOUD, read);
  assert.deepEqual([read.reads, read.made], [1, [{ kind: "device", device_id: "d-1", folder: "/home/flavius/notes", nonce: "n".repeat(43) }]]);
  const desktop = bridge();
  const unread = agent({ config: UNREAD });
  await assert.rejects(createChat(desktop, UNREAD, "last", SHOWN_CLOUD, unread), {
    message: "Surogate could not read the agent's settings, so this chat was not made. Send it again.",
  });
  assert.deepEqual([unread.made, desktop.calls], [[], []]);
  // A browser offers no folder: its chat is made in the cloud, the config read or not.
  const browser = agent({ config: UNREAD });
  await createChat(undefined, UNREAD, "last", SHOWN_CLOUD, browser);
  assert.deepEqual([browser.reads, browser.made], [0, [undefined]]);
});

test("makes no chat when the user cancels the folder sheet", async () => {
  const server = agent();
  await assert.rejects(createChat(bridge({ prepared: null }), AGENT, "last", SHOWN_HERE, server), { message: NO_FOLDER });
  assert.deepEqual(server.made, []);
});

test("says plainly that the chat was made when its folder could not be set up, and keeps it", async () => {
  // As Electron rejects a call the main process refused: the call's name, then the desktop's words.
  const desktop = bridge({
    bind: async () => {
      throw new Error("Error invoking remote method 'desktop:bindSession': Error: This folder's confirmation expired before its chat was created");
    },
  });
  await assert.rejects(createChat(desktop, AGENT, "last", SHOWN_HERE, agent()), {
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

test("says where a local-folder chat's browser is, with its buttons only in the desktop on the computer it is bound to", () => {
  const config = { execution: EXECUTION, workspace_path: "/home/flavius/notes" };
  const here = (takenOver) => localChatOf("s-1", config, DEVICES, { folder: "/home/flavius/notes", mode: "ask", takenOver });
  const elsewhere = localChatOf("s-1", config, DEVICES, null);
  const desktop = { browser: { show: async () => {}, takeOver: async () => {}, handBack: async () => true }, openSettings: async () => {} };
  const open = { available: true, readOnly: false };
  const none = { available: false, readOnly: false };
  assert.deepEqual(computerBrowser(here(false), open, desktop), { text: "The browser is open on this computer.", actions: ["show", "takeOver"] });
  assert.deepEqual(computerBrowser(here(true), open, desktop), {
    text: "The browser is open on this computer, and you have it: the agent waits until you hand it back.", actions: ["show", "handBack"],
  });
  assert.deepEqual(computerBrowser(here(false), none, desktop), {
    text: "No supported browser on this computer. Install Google Chrome, Microsoft Edge, Brave or Vivaldi, or pick one in Settings → Browser. The Snap build of Chromium is not supported.",
    actions: ["settings"],
  });
  // Elsewhere, and in a desktop that has not the calls: the message only.
  assert.deepEqual(computerBrowser(elsewhere, open, desktop), { text: "The browser is open on Flavius's ThinkPad.", actions: [] });
  assert.deepEqual(computerBrowser(elsewhere, none, null), {
    text: "No supported browser on Flavius's ThinkPad. Install Google Chrome, Microsoft Edge, Brave or Vivaldi there, or pick one in Surogate's Settings → Browser on it.",
    actions: [],
  });
  assert.deepEqual(computerBrowser(here(false), open, {}), { text: "The browser is open on this computer.", actions: [] });
  assert.deepEqual(computerBrowser(here(false), none, {}).actions, []);
  // A chat the page only reads: its browser is shown, never taken over or handed back from here.
  const watched = { available: true, readOnly: true };
  assert.deepEqual(computerBrowser(here(false), watched, desktop).actions, ["show"]);
  assert.deepEqual(computerBrowser(here(true), watched, desktop).actions, ["show"]);
});

// A chat bound on this computer, whose browser is open there, as its pane reads it from the desktop.
const NOTES = { execution: EXECUTION, workspace_path: "/home/flavius/notes" };
const bound = (takenOver) => localChatOf("s-1", NOTES, DEVICES, { folder: "/home/flavius/notes", mode: "ask", takenOver });
const OPEN = { available: true, readOnly: false };
const ASKING = "The browser is open on this computer, and you have it. Surogate is asking you, in a window of its own, whether to hand it back.";
const UNTOLD_TAKEN = "You have the browser, but the chat could not be told.";
const UNTOLD_HANDED_BACK = "The browser is the agent's again, but the agent could not be told: write to it to go on.";

// The desktop's browser calls and the chat's control route, as the pane reaches each: what each was asked, in
// order, answered as *answers* says then.
function browserDesk() {
  const did = [];
  const answers = { takeOver: async () => {}, handBack: async () => true, acquire: async () => {}, release: async () => {} };
  const desktop = {
    browser: {
      show: async (id) => void did.push(["show", id]),
      takeOver: (id) => {
        did.push(["takeOver", id]);
        return answers.takeOver();
      },
      handBack: (id) => {
        did.push(["handBack", id]);
        return answers.handBack();
      },
    },
    openSettings: async (section) => void did.push(["settings", section]),
  };
  const posts = {
    acquire: () => {
      did.push(["post", "acquire"]);
      return answers.acquire();
    },
    release: () => {
      did.push(["post", "release"]);
      return answers.release();
    },
  };
  return { did, answers, desktop, posts };
}

// An answer that comes when the test says, as the desktop's does once its user has answered.
function later() {
  let resolve;
  let reject;
  const promise = new Promise((done, failed) => {
    resolve = done;
    reject = failed;
  });
  return { promise, resolve, reject };
}

const offline = async () => Promise.reject(new Error("offline"));
// As Electron rejects a call the desktop refused: the call's name, then the desktop's words.
const refused = (call, words) => async () => Promise.reject(new Error(`Error invoking remote method 'desktop:${call}': Error: ${words}`));

test("says who holds the browser on this computer, and offers Take over or Hand back only where this chat can", () => {
  const { desktop } = browserDesk();
  // Held from this chat, which hands it back; by nobody, so this chat may take it over.
  assert.deepEqual(computerBrowser(bound(true), OPEN, desktop).actions, ["show", "handBack"]);
  assert.deepEqual(computerBrowser(bound(false), OPEN, desktop).actions, ["show", "takeOver"]);
  // Held from a chat that is gone: there is no chat to hand it back from, so this one does.
  assert.deepEqual(computerBrowser(bound("orphaned"), OPEN, desktop), {
    text: "The browser is open on this computer, and you have it, though the chat it was taken over from is gone: the agent waits until you hand it back here.",
    actions: ["show", "handBack"],
  });
  // Held from another chat: the desktop refuses both here, so neither is offered.
  assert.deepEqual(computerBrowser(bound("elsewhere"), OPEN, desktop), {
    text: "The browser is open on this computer, and you have it, taken over from another chat: the agent waits until you hand it back there.",
    actions: ["show"],
  });
  // A chat the page only reads is shown its browser, whoever holds it.
  for (const takenOver of [true, false, "orphaned", "elsewhere"]) {
    assert.deepEqual(computerBrowser(bound(takenOver), { available: true, readOnly: true }, desktop).actions, ["show"]);
    // Nor has a desktop without the calls a button for any.
    assert.deepEqual(computerBrowser(bound(takenOver), OPEN, {}).actions, []);
    // And with no browser there, who holds it is nothing to say.
    assert.deepEqual(computerBrowser(bound(takenOver), { available: false, readOnly: false }, desktop).actions, ["settings"]);
  }
  // A desktop from before the take-over says nothing of it: nobody holds its browser.
  assert.deepEqual(computerBrowser(bound(undefined), OPEN, desktop), { text: "The browser is open on this computer.", actions: ["show", "takeOver"] });
});

test("tells the server of a take-over, and of a hand back only once its user confirmed it in the desktop", async () => {
  const did = [];
  let handed = false;
  const desktop = {
    browser: {
      show: async (id) => void did.push(["show", id]),
      takeOver: async (id) => void did.push(["takeOver", id]),
      handBack: async (id) => {
        did.push(["handBack", id]);
        return handed;
      },
    },
    openSettings: async (section) => void did.push(["settings", section]),
  };
  const server = { taken: async () => void did.push(["server", "taken"]), handedBack: async () => void did.push(["server", "handed back"]) };
  await actOnBrowser("takeOver", "root-1", desktop, server);
  // Kept in the desktop's own box: the server hears nothing, and the agent is not woken.
  await actOnBrowser("handBack", "root-1", desktop, server);
  handed = true;
  await actOnBrowser("handBack", "root-1", desktop, server);
  await actOnBrowser("show", "root-1", desktop, server);
  await actOnBrowser("settings", "root-1", desktop, server);
  assert.deepEqual(did, [
    ["takeOver", "root-1"], ["server", "taken"], ["handBack", "root-1"], ["handBack", "root-1"], ["server", "handed back"],
    ["show", "root-1"], ["settings", "browser"],
  ]);
  // A take-over the desktop refused is not told.
  const refusing = { browser: { ...desktop.browser, takeOver: async () => Promise.reject(new Error("This chat has no folder on this computer")) } };
  await assert.rejects(actOnBrowser("takeOver", "root-1", refusing, server), /This chat has no folder on this computer/);
  assert.equal(did.length, 7);
  // A server that cannot be told: the browser is whose the desktop made it, and the pane says what is missing.
  const away = { taken: async () => Promise.reject(new Error("offline")), handedBack: async () => Promise.reject(new Error("offline")) };
  await assert.rejects(actOnBrowser("takeOver", "root-1", desktop, away), { message: "You have the browser, but the chat could not be told." });
  await assert.rejects(actOnBrowser("handBack", "root-1", desktop, away), {
    message: "The browser is the agent's again, but the agent could not be told: write to it to go on.",
  });
});

test("asks the desktop once for each press, the first thing the press does", async () => {
  const { did, desktop, posts } = browserDesk();
  const pane = browserPane(posts);
  const asked = { show: ["show", "root-1"], takeOver: ["takeOver", "root-1"], handBack: ["handBack", "root-1"], settings: ["settings", "browser"] };
  for (const [action, call] of Object.entries(asked)) {
    const before = did.length;
    const pressed = pane.press(action, "root-1", desktop);
    // Asked before the press returns, so before anything was waited for: the desktop lets one call through for a
    // click, and only while the click is its page's newest.
    assert.deepEqual(did.slice(before), [call]);
    await pressed;
  }
  assert.deepEqual(did.filter(([what]) => what !== "post"), Object.values(asked));
  // And as the pane's button calls it, with no pane between.
  const alone = browserDesk();
  const acted = actOnBrowser("handBack", "root-1", alone.desktop, { taken: async () => {}, handedBack: async () => {} });
  assert.deepEqual(alone.did, [["handBack", "root-1"]]);
  await acted;
});

test("shows that the desktop is asking while a hand back waits for its user, and offers no second one meanwhile", async () => {
  const { did, answers, desktop, posts } = browserDesk();
  const answer = later();
  answers.handBack = () => answer.promise;
  const pane = browserPane(posts);
  let drawn = 0;
  const stop = pane.subscribe(() => {
    drawn += 1;
  });
  assert.deepEqual(pane.state(), { asking: false, failure: null, answers: 0 });
  const pressed = pane.press("handBack", "root-1", desktop);
  // At once, for the pane to draw: the desktop's own window can take two seconds to show, and its user minutes to answer.
  assert.deepEqual([pane.state(), drawn], [{ asking: true, failure: null, answers: 0 }, 1]);
  for (const takenOver of [true, "orphaned"]) {
    assert.deepEqual(computerBrowser(bound(takenOver), OPEN, desktop, pane.state().asking), { text: ASKING, actions: ["show"] });
  }
  // A press that reached it all the same asks the desktop nothing: it would be refused, "Surogate is already asking".
  await pane.press("handBack", "root-1", desktop);
  assert.deepEqual(did, [["handBack", "root-1"]]);
  // The browser is shown meanwhile, and the pane goes on waiting.
  await pane.press("show", "root-1", desktop);
  assert.deepEqual([did, pane.state().asking], [[["handBack", "root-1"], ["show", "root-1"]], true]);
  answer.resolve(false);
  await pressed;
  // Kept: Hand back is offered again, and the server was told nothing.
  assert.deepEqual(pane.state(), { asking: false, failure: null, answers: 1 });
  assert.deepEqual(computerBrowser(bound(true), OPEN, desktop, pane.state().asking).actions, ["show", "handBack"]);
  assert.deepEqual(did, [["handBack", "root-1"], ["show", "root-1"]]);
  // A pane that is drawn no more hears no more.
  stop();
  const heard = drawn;
  await pane.press("show", "root-1", desktop);
  assert.equal(drawn, heard);
});

test("tells the server of a hand back only when the desktop answers true, and has the binding read again whatever it answers", async () => {
  const { did, answers, desktop, posts } = browserDesk();
  const pane = browserPane(posts);
  await pane.press("takeOver", "root-1", desktop);
  assert.deepEqual([did, pane.state()], [[["takeOver", "root-1"], ["post", "acquire"]], { asking: false, failure: null, answers: 1 }]);
  // Each a hand back that released nothing: its user kept the browser, or closed the window, or nobody answered in
  // time, or it was taken over from another chat meanwhile; the desktop was asking already; it is another chat's.
  const kept = [
    [async () => false, null],
    [refused("handBack", "Surogate is already asking"), "Surogate is already asking"],
    [
      refused("handBack", "The agent's browser on this computer is taken over from another chat, and is handed back there"),
      "The agent's browser on this computer is taken over from another chat, and is handed back there",
    ],
  ];
  for (const [index, [answer, failure]] of kept.entries()) {
    answers.handBack = answer;
    await pane.press("handBack", "root-1", desktop);
    // The pane reads the binding again at each answer, and draws what it says.
    assert.deepEqual(pane.state(), { asking: false, failure, answers: index + 2 });
  }
  assert.deepEqual(did.filter(([what]) => what === "post"), [["post", "acquire"]]);
  // Handed back: only now is the chat told, and its agent woken.
  answers.handBack = async () => true;
  await pane.press("handBack", "root-1", desktop);
  assert.deepEqual(did.filter(([what]) => what === "post"), [["post", "acquire"], ["post", "release"]]);
  assert.deepEqual(pane.state(), { asking: false, failure: null, answers: 5 });
  // Showing the browser, or opening Settings, changes nothing the binding says.
  await pane.press("show", "root-1", desktop);
  await pane.press("settings", "root-1", desktop);
  assert.equal(pane.state().answers, 5);
});

test("has the binding read again as soon as the desktop has answered, before the server has", async () => {
  const { answers, desktop, posts } = browserDesk();
  const heard = later();
  const told = later();
  answers.acquire = () => heard.promise;
  answers.release = () => told.promise;
  const pane = browserPane(posts);
  const took = pane.press("takeOver", "root-1", desktop);
  await new Promise((resolve) => setImmediate(resolve));
  // Taken over in the desktop, and the server still being told: the pane draws who holds the browser now.
  assert.deepEqual(pane.state(), { asking: false, failure: null, answers: 1 });
  heard.resolve();
  await took;
  assert.deepEqual(pane.state(), { asking: false, failure: null, answers: 1 });
  const pressed = pane.press("handBack", "root-1", desktop);
  await new Promise((resolve) => setImmediate(resolve));
  // Handed back in the desktop, and the server still being told: the pane waits for its user no more.
  assert.deepEqual(pane.state(), { asking: false, failure: null, answers: 2 });
  told.resolve();
  await pressed;
  assert.deepEqual(pane.state(), { asking: false, failure: null, answers: 2 });
});

test("sends a take-over the server never heard of again before its hand back, so the chat is told both, in order", async () => {
  const { did, answers, desktop, posts } = browserDesk();
  const pane = browserPane(posts);
  answers.acquire = offline;
  await pane.press("takeOver", "root-1", desktop);
  assert.deepEqual([did, pane.state().failure], [[["takeOver", "root-1"], ["post", "acquire"]], UNTOLD_TAKEN]);
  answers.acquire = async () => {};
  await pane.press("handBack", "root-1", desktop);
  assert.deepEqual(did.slice(2), [["handBack", "root-1"], ["post", "acquire"], ["post", "release"]]);
  assert.equal(pane.state().failure, null);
  // One the server did hear of is not sent again.
  const heard = browserDesk();
  const told = browserPane(heard.posts);
  await told.press("takeOver", "root-1", heard.desktop);
  await told.press("handBack", "root-1", heard.desktop);
  assert.deepEqual(heard.did, [["takeOver", "root-1"], ["post", "acquire"], ["handBack", "root-1"], ["post", "release"]]);
  // A page loaded again while its user held the browser does not know what the server heard: it sends both, and
  // the server tells the chat of a take-over only once.
  const reloaded = browserDesk();
  await browserPane(reloaded.posts).press("handBack", "root-1", reloaded.desktop);
  assert.deepEqual(reloaded.did, [["handBack", "root-1"], ["post", "acquire"], ["post", "release"]]);
});

test("says what the server could not be told of a take-over and of a hand back", async () => {
  // The take-over that could not be sent again: the hand back alone would be told to nobody, so it is not sent.
  const unsent = browserDesk();
  unsent.answers.acquire = offline;
  const first = browserPane(unsent.posts);
  await first.press("takeOver", "root-1", unsent.desktop);
  assert.equal(first.state().failure, UNTOLD_TAKEN);
  await first.press("handBack", "root-1", unsent.desktop);
  assert.deepEqual(unsent.did, [["takeOver", "root-1"], ["post", "acquire"], ["handBack", "root-1"], ["post", "acquire"]]);
  assert.equal(first.state().failure, UNTOLD_HANDED_BACK);
  // The hand back itself that could not be told.
  const untold = browserDesk();
  untold.answers.release = offline;
  const second = browserPane(untold.posts);
  await second.press("takeOver", "root-1", untold.desktop);
  await second.press("handBack", "root-1", untold.desktop);
  assert.deepEqual(untold.did.slice(2), [["handBack", "root-1"], ["post", "release"]]);
  assert.equal(second.state().failure, UNTOLD_HANDED_BACK);
  // What was said stays until the next press, which starts clean.
  untold.answers.release = async () => {};
  await second.press("show", "root-1", untold.desktop);
  assert.equal(second.state().failure, null);
  // A desktop's own refusal is said in its words.
  const refusing = browserDesk();
  refusing.answers.takeOver = refused("takeOver", "This chat has no folder on this computer");
  const third = browserPane(refusing.posts);
  await third.press("takeOver", "root-1", refusing.desktop);
  assert.deepEqual([refusing.did, third.state().failure], [[["takeOver", "root-1"]], "This chat has no folder on this computer"]);
});

test("tells the server once at a chat's load that nobody holds its browser, wherever the computer says so", async () => {
  const { did, answers, desktop, posts } = browserDesk();
  const pane = browserPane(posts);
  // Held, from this chat or another or one that is gone; a chat with no folder here; a desktop that says nothing of it.
  for (const takenOver of [true, "elsewhere", "orphaned", undefined]) {
    await pane.loaded(takenOver);
  }
  assert.deepEqual(did, []);
  // Nobody holds it: the app may have ended while its user did, and the chat still says they do. Once, though the
  // chat's bar and its pane each read the binding.
  await Promise.all([pane.loaded(false), pane.loaded(false)]);
  await pane.loaded(false);
  assert.deepEqual(did, [["post", "release"]]);
  // A take-over told from here, and handed back from another chat of the same folder: told at this one's next load.
  await pane.press("takeOver", "root-1", desktop);
  await pane.loaded(false);
  await pane.loaded(false);
  assert.deepEqual(did.slice(1), [["takeOver", "root-1"], ["post", "acquire"], ["post", "release"]]);
  // One that could not be sent says nothing, as nothing its user did is untold, and is sent at the next load.
  const away = browserDesk();
  away.answers.release = offline;
  const unheard = browserPane(away.posts);
  await unheard.loaded(false);
  assert.deepEqual([away.did, unheard.state()], [[["post", "release"]], { asking: false, failure: null, answers: 0 }]);
  away.answers.release = async () => {};
  await unheard.loaded(false);
  await unheard.loaded(false);
  assert.deepEqual(away.did, [["post", "release"], ["post", "release"]]);
  // A take-over that could not be told may have arrived all the same: handed back from another chat of its
  // folder, it is told at this one's next load too.
  const lost = browserDesk();
  const unsure = browserPane(lost.posts);
  await unsure.loaded(false);
  lost.answers.acquire = offline;
  await unsure.press("takeOver", "root-1", lost.desktop);
  await unsure.loaded(false);
  assert.deepEqual(lost.did, [["post", "release"], ["takeOver", "root-1"], ["post", "acquire"], ["post", "release"]]);
  // Nor is a hand back whose own telling failed left there: the next load tells it.
  answers.release = offline;
  await pane.press("takeOver", "root-1", desktop);
  await pane.press("handBack", "root-1", desktop);
  assert.equal(pane.state().failure, UNTOLD_HANDED_BACK);
  answers.release = async () => {};
  const before = did.length;
  await pane.loaded(false);
  assert.deepEqual(did.slice(before), [["post", "release"]]);
});

test("tells the server of a take-over only after what it was told at load, each in its turn", async () => {
  const { did, answers, desktop, posts } = browserDesk();
  const first = later();
  answers.release = () => first.promise;
  const pane = browserPane(posts);
  const loaded = pane.loaded(false);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(did, [["post", "release"]]);
  const pressed = pane.press("takeOver", "root-1", desktop);
  await new Promise((resolve) => setImmediate(resolve));
  // The desktop was asked at the press; the server is not told of the take-over while the load's release is on
  // its way, which would arrive after it and tell the chat its user had handed the browser back.
  assert.deepEqual(did, [["post", "release"], ["takeOver", "root-1"]]);
  first.resolve();
  await Promise.all([loaded, pressed]);
  assert.deepEqual(did, [["post", "release"], ["takeOver", "root-1"], ["post", "acquire"]]);
  // And the hand back's two after the take-over's one, though that one is still on its way.
  const slow = browserDesk();
  const taken = later();
  slow.answers.acquire = () => taken.promise;
  const other = browserPane(slow.posts);
  const took = other.press("takeOver", "root-1", slow.desktop);
  const handed = other.press("handBack", "root-1", slow.desktop);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(slow.did, [["takeOver", "root-1"], ["handBack", "root-1"], ["post", "acquire"]]);
  taken.resolve();
  await Promise.all([took, handed]);
  assert.deepEqual(slow.did.slice(3), [["post", "release"]]);
});

test("asks the desktop about a sub-agent's chat by its root, and tells the server of the chat its pane is drawn in", async () => {
  const asked = [];
  const told = [];
  const desktop = { browser: { show: async () => {}, takeOver: async (id) => void asked.push(id), handBack: async () => true } };
  const paneOf = browserPanes((sessionId) => ({
    acquire: async () => void told.push([sessionId, "acquire"]),
    release: async () => void told.push([sessionId, "release"]),
  }));
  // A sub-agent's chat works in its root's folder: the desktop's binding, and its browser calls, name the root.
  const helper = localChatOf("helper", { ...NOTES, sandbox_root_session_id: "s-1" }, DEVICES, { folder: "/home/flavius/notes", mode: "ask", takenOver: false });
  assert.deepEqual(computerBrowser(helper, OPEN, desktop).actions, ["show", "takeOver"]);
  await paneOf("helper").loaded(helper.here.takenOver);
  await paneOf("helper").press("takeOver", helper.root, desktop);
  // The chat that says it, and whose agent a hand back wakes, is the one its user is in.
  assert.deepEqual([asked, told], [["s-1"], [["helper", "release"], ["helper", "acquire"]]]);
  // One pane for a chat while the page lives, so what it was told outlasts each drawing of it; and one for each chat.
  assert.equal(paneOf("helper"), paneOf("helper"));
  assert.notEqual(paneOf("helper"), paneOf("s-1"));
  await paneOf("s-1").press("handBack", "s-1", desktop);
  assert.deepEqual(told.slice(2), [["s-1", "acquire"], ["s-1", "release"]]);
});
