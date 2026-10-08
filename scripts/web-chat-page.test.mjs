// The web client's chat page itself, drawn in a DOM, with what it draws around the chat, its store
// and its route stood in for: what Surogate Desktop's quick entry hands it reaches the chat only once
// the page may send it, past the AI disclosure and the line under the composer, and the desktop
// hears what became of it. Vite, the web's own, loads the page as it builds it.

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { after, before, test } from "node:test";
import { pathToFileURL } from "node:url";

const web = new URL("../web/", import.meta.url);
const fromWeb = createRequire(new URL("package.json", web));
// The DOM the SDK's own tests draw in: the web depends on the SDK.
const fromSdk = createRequire(new URL("../sdk/agent-chat-react/package.json", import.meta.url));
const load = (from, name) => import(pathToFileURL(from.resolve(name)).href);

// What the page reads and calls, the test's own (globalThis.chatPage): the store, the route, the
// transparency read, and the chat and the disclosure, which keep the props they were drawn with.
const STUBS = {
  "@/stores/app-store": `
    import { useSyncExternalStore } from "react";
    const page = () => globalThis.chatPage;
    export const useAppStore = (pick) => useSyncExternalStore(page().subscribe, () => pick(page().store));
    useAppStore.getState = () => page().store;`,
  "@tanstack/react-router": `
    import { useSyncExternalStore } from "react";
    const page = () => globalThis.chatPage;
    export const useParams = () => useSyncExternalStore(page().subscribe, () => page().params);
    export const useNavigate = () => page().navigate;`,
  "@/api/transparency": "export const getTransparencyConfig = () => globalThis.chatPage.transparency.promise;",
  "@invergent/agent-chat-react": "export function AgentChat(props) { globalThis.chatPage.chat = props; return null; }",
  "@/components/transparency-banner": "export function TransparencyBanner(props) { globalThis.chatPage.banner = props; return null; }",
  "@/components/app-shell": "export const AppShell = ({ children }) => children;",
  "@/components/navbar": "export const SessionSidebar = () => null;",
  "@/stores/capabilities-slice": "export const slashCommandEnabled = () => true;",
  "@/api/devices": "export const listDevices = async () => [];",
  "@/api/sessions": "export const createSession = async () => ({ id: 's-made' }); export const confirmDisclosure = async () => {};",
  "./local-chat-bar": "export const LocalChatBar = () => null;",
  "./new-chat-place": "export const NewChatPlace = () => null;",
  "./surogates-web-chat-adapter": "export const surogatesWebChatAdapter = {}; export const toAgentChatSession = (session) => session;",
};

let vite;
let ChatPage;
let React;
let createRoot;

before(async () => {
  const { Window } = await load(fromSdk, "happy-dom");
  const window = new Window({ url: "https://agent.example/chat" });
  Object.assign(globalThis, { window, document: window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const { createServer } = await load(fromWeb, "vite");
  vite = await createServer({
    configFile: false,
    root: web.pathname,
    logLevel: "error",
    appType: "custom",
    server: { middlewareMode: true, hmr: false, ws: false },
    ssr: { noExternal: ["@tanstack/react-router", "@invergent/agent-chat-react"] },
    oxc: { jsx: { runtime: "automatic" } },
    plugins: [{
      name: "chat-page-stubs",
      enforce: "pre",
      // The web's own "@/" is resolved here, so that a stub stands in before it.
      resolveId(source, importer) {
        if (source in STUBS) return `\0stub:${source}`;
        return source.startsWith("@/") ? this.resolve(new URL(`src/${source.slice(2)}`, web).pathname, importer) : null;
      },
      load: (id) => (id.startsWith("\0stub:") ? STUBS[id.slice("\0stub:".length)] : null),
    }],
  });
  ({ ChatPage } = await vite.ssrLoadModule("/src/features/chat/chat-page.tsx"));
  React = await load(fromWeb, "react");
  ({ createRoot } = await load(fromWeb, "react-dom/client"));
});

after(() => vite?.close());

const HANDED = { id: "q-1", text: "Draft the March invoices" };

/**
 * The chat page at /chat in Surogate Desktop, as a fresh load draws it: the store's reads not in,
 * the transparency read and this computer's state answered when the test says.
 */
async function opened() {
  const listeners = new Set();
  const page = {
    store: {
      sessionsLoading: true, sessions: [], activeSessionId: null, multiSession: null, desktopSessions: null,
      slashCommands: null, browserEnabled: null,
      fetchSessions: async () => {}, fetchUser: async () => {}, fetchCapabilities: async () => {},
      setActiveSession: (id) => change({ activeSessionId: id }), upsertSession: () => {}, setToolCheckpoint: () => {},
    },
    params: {},
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    navigate: async ({ params }) => {
      page.params = params ?? {};
      for (const listener of listeners) listener();
    },
    transparency: Promise.withResolvers(),
    device: Promise.withResolvers(),
    answers: [],
  };
  const change = (store) => {
    page.store = { ...page.store, ...store };
    for (const listener of listeners) listener();
  };
  globalThis.chatPage = page;
  let hear;
  window.surogateDesktop = {
    version: 1,
    getDevice: () => page.device.promise,
    onQuickEntry: (listener) => {
      hear = listener;
      return () => {};
    },
    answerQuickEntry: (id, refused) => page.answers.push([id, refused]),
  };
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const act = (run) => React.act(async () => {
    await run();
  });
  await act(() => root.render(React.createElement(ChatPage)));
  return {
    page,
    act,
    change: (store) => act(() => change(store)),
    hand: (message = HANDED) => {
      assert.equal(typeof hear, "function", "The page listens for what quick entry hands it");
      return act(() => hear(message));
    },
    given: () => page.chat?.firstMessage ?? null,
    unmount: () => act(() => root.unmount()),
  };
}

const LOCAL = { device: { deviceId: "d-1", name: "Laptop" }, localFolders: true };

// What the web client's own read of the transparency setting answers once the agent refuses it: its
// module itself, which the page's stand-in for it hands on.
async function unreadSetting() {
  const { getTransparencyConfig } = await vite.ssrLoadModule("/src/api/transparency.ts");
  const fetched = globalThis.fetch;
  globalThis.fetch = async () => new Response("{}", { status: 500 });
  try {
    return await getTransparencyConfig();
  } finally {
    globalThis.fetch = fetched;
  }
}

test("sends what quick entry handed past the AI disclosure only once it is read and accepted, and tells the desktop it went", async () => {
  const { page, act, change, hand, given, unmount } = await opened();
  await hand();
  await change({ sessionsLoading: false, multiSession: true, desktopSessions: true });
  await act(() => page.device.resolve(LOCAL));
  // The transparency setting is not read yet: nothing goes.
  assert.equal(given(), null);
  await act(() => page.transparency.resolve({ enabled: true }));
  // Read, and not accepted yet: the disclosure shows, and still nothing goes.
  assert.equal(typeof page.banner?.onConfirmed, "function");
  assert.equal(given(), null);
  await act(() => page.banner.onConfirmed());
  assert.deepEqual(given(), HANDED);
  // The chat says it sent it: the desktop hears so, and the page has nothing left to give.
  await act(() => page.chat.onFirstMessageSent(HANDED.id, null));
  assert.deepEqual(page.answers, [[HANDED.id, null]]);
  assert.equal(given(), null);
  await unmount();
  assert.deepEqual(page.answers, [[HANDED.id, null]]);
});

test("waits for the line under the composer, so the chat is made where it says", async () => {
  const { page, act, change, hand, given, unmount } = await opened();
  await hand();
  await act(() => page.transparency.resolve({ enabled: false }));
  await change({ sessionsLoading: false, multiSession: true });
  // This computer's state is not read yet: the line says nothing, and nothing goes.
  assert.equal(given(), null);
  await act(() => page.device.resolve({ device: { deviceId: "d-1", name: "Laptop" }, localFolders: false }));
  // Nor while the agent's settings are unread: the line would say why the chat works in the cloud.
  assert.equal(given(), null);
  await change({ desktopSessions: true });
  assert.deepEqual(given(), HANDED);
  await unmount();
});

test("never sends it once the disclosure is declined, or unreadable, nor after another chat was shown, and tells the desktop why", async () => {
  const declined = await opened();
  await declined.hand();
  await declined.change({ sessionsLoading: false, multiSession: true });
  await declined.act(() => declined.page.device.resolve(LOCAL));
  await declined.act(() => declined.page.transparency.resolve({ enabled: true }));
  await declined.act(() => declined.page.banner.onDeclined());
  assert.deepEqual(declined.page.answers, [[HANDED.id, "You declined the agent's AI disclosure, so nothing was sent."]]);
  assert.equal(declined.given(), null);
  await declined.unmount();

  const unread = await opened();
  await unread.hand();
  const setting = await unreadSetting();
  await unread.act(() => unread.page.transparency.resolve(setting));
  assert.deepEqual(unread.page.answers, [[HANDED.id, "Surogate could not read the agent's AI disclosure setting, so nothing was sent."]]);
  await unread.unmount();

  // Left under the disclosure while the user opens another chat, then a New chat: it is not sent there.
  const left = await opened();
  await left.hand();
  await left.change({ sessionsLoading: false, multiSession: true, desktopSessions: true });
  await left.act(() => left.page.device.resolve(LOCAL));
  await left.act(() => left.page.transparency.resolve({ enabled: true }));
  await left.act(() => left.page.navigate({ to: "/chat/$sessionId", params: { sessionId: "s-1" } }));
  assert.deepEqual(left.page.answers, [[HANDED.id, "Another chat opened before this one was made, so nothing was sent."]]);
  await left.act(() => left.page.navigate({ to: "/chat" }));
  await left.act(() => left.page.banner.onConfirmed());
  assert.equal(left.given(), null);
  await left.unmount();
  assert.equal(left.page.answers.length, 1);
});

test("on an agent of one conversation, tells the desktop why it sent nothing, whether its conversation or the disclosure's read comes first", async () => {
  const refused = async ({ page, given, unmount }) => {
    assert.deepEqual(page.answers, [[HANDED.id, "This agent keeps one conversation, and quick entry starts new chats, so nothing was sent. Write to it in Surogate's window."]]);
    assert.equal(given(), null);
    await unmount();
  };
  // The session list first: the page moves to the one conversation before the disclosure is read.
  const pinned = await opened();
  await pinned.hand();
  await pinned.change({ sessionsLoading: false, multiSession: false, sessions: [{ id: "s-1", status: "idle" }] });
  assert.deepEqual(pinned.page.params, { sessionId: "s-1" });
  await pinned.act(() => pinned.page.transparency.resolve({ enabled: false }));
  await refused(pinned);
  // The disclosure's read first, and this computer's state, which has no local folders for such an
  // agent: the line waits for the agent's settings, which come with the session list still on its way.
  const read = await opened();
  await read.hand();
  await read.act(() => read.page.transparency.resolve({ enabled: false }));
  await read.act(() => read.page.device.resolve({ device: { deviceId: "d-1", name: "Laptop" }, localFolders: false }));
  assert.equal(read.given(), null);
  await read.change({ multiSession: false, desktopSessions: true });
  await refused(read);
});

test("tells the desktop when the page leaves the new chat before it could send it", async () => {
  const { page, hand, unmount } = await opened();
  await hand();
  await unmount();
  assert.deepEqual(page.answers, [[HANDED.id, "The agent's page left the new chat before it was made, so nothing was sent."]]);
});

test("hands the chat the same message and the same callback across a redraw, so the chat sends it once and its answer reaches the desktop", async () => {
  const { page, act, change, hand, given, unmount } = await opened();
  await hand();
  await change({ sessionsLoading: false, multiSession: true, desktopSessions: true });
  await act(() => page.device.resolve(LOCAL));
  await act(() => page.transparency.resolve({ enabled: false }));
  const drawn = page.chat;
  assert.deepEqual(drawn.firstMessage, HANDED);
  // A redraw of the page: the chat's first-message effect must not run again, for a send or an answer.
  await change({ slashCommands: [] });
  assert.notEqual(page.chat, drawn);
  assert.equal(page.chat.firstMessage, drawn.firstMessage);
  assert.equal(page.chat.onFirstMessageSent, drawn.onFirstMessageSent);
  // The callback the chat held before the redraw still answers the desktop, once.
  await act(() => drawn.onFirstMessageSent(HANDED.id, null));
  assert.deepEqual(page.answers, [[HANDED.id, null]]);
  assert.equal(given(), null);
  await unmount();
  assert.deepEqual(page.answers, [[HANDED.id, null]]);
});
