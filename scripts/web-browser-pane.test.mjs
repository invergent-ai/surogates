// The browser pane of a chat on a folder of the user's computer, as the web client draws it
// (web/src/features/chat/computer-browser.tsx), and the control route's posts as it makes them
// (web/src/api/sessions.ts): the modules themselves, with the app's sign-in, its chat and its
// buttons stood in for.
import assert from "node:assert/strict";
import { createRequire, register } from "node:module";
import { test } from "node:test";
import { pathToFileURL } from "node:url";

register("./web-src-hooks.mjs", import.meta.url, {
  data: {
    stubs: {
      "api/auth": ["authFetch"],
      "components/ui/button": ["Button"],
      "features/chat/browser-panes": ["browserPaneOf"],
      "features/chat/local-chat-bar": ["useLocalChat"],
    },
  },
});

const web = createRequire(new URL("../web/package.json", import.meta.url));
const { createElement } = await import(pathToFileURL(web.resolve("react")).href);
const { renderToStaticMarkup } = await import(pathToFileURL(web.resolve("react-dom/server")).href);
const src = (path) => import(new URL(`../web/src/${path}`, import.meta.url).href);
const { AGENT_GOES_ON, WRITE_TO_THE_AGENT, localChatOf } = await src("lib/local-chat.ts");
const { acquireBrowserControl, releaseBrowserControl } = await src("api/sessions.ts");
const { ComputerBrowser } = await src("features/chat/computer-browser.tsx");

const CONTROL = "/api/v1/sessions/s%2F1/browser/control";
const JSON_POST = { "Content-Type": "application/json" };

// The app's signed-in fetch: what was posted, each answered as *answers* says, in order.
function signedIn(...answers) {
  const posted = [];
  globalThis.webStubs = {
    "api/auth": {
      authFetch: async (path, { method, headers, body }) => {
        posted.push([path, method, headers, JSON.parse(body)]);
        const [status, answer] = answers[posted.length - 1];
        return { ok: status < 300, status, json: async () => answer };
      },
    },
  };
  return posted;
}

test("posts a release to the chat's control route, saying it is a hand back only where its user confirmed one", async () => {
  const goesOn = { outcome: "released", resumes: true };
  const forThePane = { outcome: "released", resumes: false };
  const posted = signedIn([200, goesOn], [200, forThePane], [200, forThePane]);

  // The answer is the server's own, whose `resumes` the pane reads.
  assert.deepEqual(await releaseBrowserControl("s/1", true), goesOn);
  assert.deepEqual(await releaseBrowserControl("s/1", false), forThePane);
  // A cloud chat's release, and the pane's own at a chat's opening, say nothing of a hand back.
  assert.deepEqual(await releaseBrowserControl("s/1"), forThePane);

  assert.deepEqual(posted, [
    [CONTROL, "POST", JSON_POST, { action: "release", handed_back: true }],
    [CONTROL, "POST", JSON_POST, { action: "release" }],
    [CONTROL, "POST", JSON_POST, { action: "release" }],
  ]);
});

test("fails a release, and a take-over, the server did not make, so the pane says the chat could not be told", async () => {
  const busy = { detail: "The chat is being told of its browser by another request. Post it again." };
  const posted = signedIn([503, busy], [503, busy]);

  await assert.rejects(releaseBrowserControl("s/1", true), { message: "Failed to release browser control" });
  await assert.rejects(acquireBrowserControl("s/1"), { message: "Failed to acquire browser control" });

  assert.deepEqual(posted.map(([, , , said]) => said), [{ action: "release", handed_back: true }, { action: "acquire" }]);
});

const NOTES = {
  execution: { kind: "device", device_id: "d-1", device_name: "Flavius's ThinkPad" },
  workspace_path: "/home/flavius/notes",
};
const DEVICES = [{ id: "d-1", name: "Flavius's ThinkPad", revoked_at: null }];

// The pane as the page draws it, in the desktop on the chat's computer: what its binding there reads
// of who holds the browser, and what the chat's pane keeps of its last press.
function drawn(takenOver, kept) {
  const state = { asking: false, failure: null, said: null, answers: 0, ...kept };
  const browser = { show: async () => {}, takeOver: async () => {}, handBack: async () => "confirmed" };
  globalThis.webStubs = {
    "components/ui/button": { Button: ({ children }) => createElement("button", null, children) },
    "features/chat/browser-panes": {
      browserPaneOf: () => ({ subscribe: () => () => {}, state: () => state, press: async () => {} }),
    },
    "features/chat/local-chat-bar": {
      useLocalChat: (sessionId) => ({
        chat: localChatOf(sessionId, NOTES, DEVICES, { folder: NOTES.workspace_path, mode: "ask", takenOver }),
        desktop: { browser },
        reread: () => {},
      }),
    },
  };
  const html = renderToStaticMarkup(createElement(ComputerBrowser, { sessionId: "s-1", available: true, readOnly: false }));
  const text = (tag) => html.match(new RegExp(`<${tag}[^>]*>(.*?)</${tag}>`))[1].replaceAll("&#x27;", "'");
  return {
    says: text("p"),
    output: text("output"),
    alert: text("span"),
    buttons: [...html.matchAll(/<button>(.*?)<\/button>/g)].map(([, label]) => label),
  };
}

test("draws what the pane said of a hand back under who holds the browser, and only while nobody does", () => {
  for (const said of [AGENT_GOES_ON, WRITE_TO_THE_AGENT]) {
    assert.deepEqual(drawn(false, { said }), {
      says: "The browser is open on this computer.",
      output: said,
      alert: "",
      buttons: ["Show browser", "Take over"],
    });
  }
  // Nothing said yet, or nothing to say of the last press.
  assert.equal(drawn(false, {}).output, "");
  // Taken over again from another chat, or from this one: the pane says who holds it, and nothing of
  // the hand back made before.
  assert.deepEqual(drawn("elsewhere", { said: AGENT_GOES_ON }), {
    says: "The browser is open on this computer, and you have it, taken over from another chat: the agent waits until you hand it back there.",
    output: "",
    alert: "",
    buttons: ["Show browser"],
  });
  assert.deepEqual(drawn(true, { said: AGENT_GOES_ON }).output, "");
  assert.deepEqual(drawn(true, { said: AGENT_GOES_ON }).buttons, ["Show browser", "Hand back"]);
  // What could not be told is said whoever holds it.
  const untold = "You have the browser, but the chat could not be told.";
  assert.deepEqual(drawn(true, { failure: untold }).alert, untold);
});
