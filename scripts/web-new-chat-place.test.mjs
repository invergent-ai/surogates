// The line under a new chat's composer in Surogate Desktop, drawn in a DOM as the real component
// builds it: the folder menu that says where the chat works, and the sentence older desktops get.

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { after, before, test } from "node:test";
import { pathToFileURL } from "node:url";

const web = new URL("../web/", import.meta.url);
const fromWeb = createRequire(new URL("package.json", web));
const load = (from, name) => import(pathToFileURL(from.resolve(name)).href);

const NOTE = "You confirm the folder after your first message.";
const LOCAL = { local: true, text: "Works in" };

let vite;
let NewChatPlace;
let React;
let createRoot;
let window;

before(async () => {
  const dom = await load(fromWeb, "happy-dom");
  window = new dom.Window({ url: "https://agent.example/chat" });
  Object.assign(globalThis, { window, document: window.document, IS_REACT_ACT_ENVIRONMENT: true });
  // Radix reads the DOM's classes and observers as globals when it opens a menu.
  const globals = ["Element", "HTMLElement", "Node", "Event", "KeyboardEvent", "PointerEvent", "MutationObserver", "ResizeObserver", "CSS"];
  for (const name of globals) globalThis[name] = window[name];
  globalThis.getComputedStyle = window.getComputedStyle.bind(window);
  const { createServer } = await load(fromWeb, "vite");
  vite = await createServer({
    configFile: false,
    root: web.pathname,
    logLevel: "error",
    appType: "custom",
    server: { middlewareMode: true, hmr: false, ws: false },
    resolve: { dedupe: ["react", "react-dom"] },
    oxc: { jsx: { runtime: "automatic" } },
    plugins: [{
      name: "web-alias",
      enforce: "pre",
      resolveId(source, importer) {
        return source.startsWith("@/") ? this.resolve(new URL(`src/${source.slice(2)}`, web).pathname, importer) : null;
      },
    }],
  });
  ({ NewChatPlace } = await vite.ssrLoadModule("/src/features/chat/new-chat-place.tsx"));
  React = await load(fromWeb, "react");
  ({ createRoot } = await load(fromWeb, "react-dom/client"));
});

after(() => vite?.close());

/** The component drawn alone; *draw* draws it again with other props. */
async function drawn(props) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const act = (run) => React.act(async () => {
    await run();
  });
  const draw = (next) => act(() => root.render(React.createElement(NewChatPlace, next)));
  await draw(props);
  return { container, act, draw, unmount: () => act(() => root.unmount()) };
}

const trigger = (container) => container.querySelector("button");
const items = () => [...document.querySelectorAll('[role="menuitemradio"]')];

test("shows a new folder as chosen, with its icon", async () => {
  const view = await drawn({ place: LOCAL, choice: "new", menu: true, onChoice: () => {} });
  try {
    assert.match(trigger(view.container).textContent, /^a new folder$/);
    assert.ok(trigger(view.container).querySelector("svg.lucide-folder-plus"));
    assert.ok(view.container.textContent.includes(NOTE));
  } finally {
    await view.unmount();
  }
});

test("lists the three kinds of folder with their hints, the chosen one checked, and tells the page which was chosen", async () => {
  const chosen = [];
  const view = await drawn({ place: LOCAL, choice: "new", menu: true, onChoice: (c) => chosen.push(c) });
  try {
    await view.act(() =>
      trigger(view.container).dispatchEvent(
        new window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }),
      ));
    assert.deepEqual(items().map((item) => item.textContent), [
      "A new folderMade for this chat",
      "The last folderWhere your last chat worked",
      "Choose a folder…The folder window opens after you send",
    ]);
    assert.deepEqual(items().map((item) => item.getAttribute("aria-checked")), ["true", "false", "false"]);
    await view.act(() => items()[1].click());
    assert.deepEqual(chosen, ["last"]);
    await view.draw({ place: LOCAL, choice: "last", menu: true, onChoice: () => {} });
    assert.match(trigger(view.container).textContent, /^the last folder$/);
    assert.ok(trigger(view.container).querySelector("svg.lucide-history"));
  } finally {
    await view.unmount();
  }
});

test("shows today's line and link to a desktop that offers no new folder", async () => {
  const text = "Works in a folder on this computer: the one you used last, or a new one. You confirm it when you send.";
  const chosen = [];
  const view = await drawn({ place: { local: true, text }, choice: "last", menu: false, onChoice: (c) => chosen.push(c) });
  try {
    assert.ok(view.container.textContent.includes(text));
    assert.ok(!view.container.textContent.includes(NOTE));
    assert.equal(trigger(view.container).textContent, "Choose another folder");
    await view.act(() => trigger(view.container).click());
    assert.deepEqual(chosen, ["pick"]);
  } finally {
    await view.unmount();
  }
});
