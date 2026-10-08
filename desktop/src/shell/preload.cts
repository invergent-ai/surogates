// The bridge of the agent's web client (spec, Section 8), given as Claude Desktop gives
// claude.ai its own (mainView.js): only in the top frame, and only on the agent's origin,
// which the main process names in the view's arguments. Each call goes to the handlers
// registered on this view in the main process, which check it again.

import { contextBridge, ipcRenderer } from "electron";

import type { ProjectsSource } from "../../../web/src/lib/projects-contract.js";
import type { ToPage } from "./projects.js";

const PREFIX = "--surogate-origin=";
const origin = process.argv.find((arg) => arg.startsWith(PREFIX))?.slice(PREFIX.length);

if (origin !== undefined && window.top === window && location.origin === origin) {
  const call = (name: string) => (...args: unknown[]) => ipcRenderer.invoke(`desktop:${name}`, ...args);
  // Show folder only at its user's click: one heard here, in the preload's own world, where the
  // page's scripts cannot make a trusted one. A click by key or by assistive technology counts. It
  // allows one Show folder, for as long as Chromium's activation lasts.
  const CLICK_MS = 5_000;
  let clickedAt = -Infinity;
  window.addEventListener("click", (event) => {
    if (event.isTrusted) clickedAt = performance.now();
  }, true);
  // The projects source the page serves (Section 12) stays here, and is called for the main
  // process, which checks each answer.
  let projects: ProjectsSource | null = null;
  const subscriptions = new Map<number, () => void>();
  // Calls that reach this page before it serves its projects, as one sent while its load
  // commits does: answered once it serves them, or refused once it says it serves none. One
  // with under a second left by then would answer after the main process refused it: it never
  // runs. One whose deadline has passed goes from the hold as the next call comes.
  let early: Array<Extract<ToPage, { type: "call" }>> = [];
  // The page said it serves none (registerProjects(null)): a call is refused at once, not held.
  let servesNone = false;
  const NONE = "The agent's page serves no projects";
  const LAST_SECOND_MS = 1_000;
  // A source's methods, which must be its own: what the page hands over is a copy, and keeps no prototype.
  const METHODS = ["list", "get", "create", "update", "archive", "threads", "resolve", "reopen", "library", "routines", "subscribe"] as const;
  const answer = (id: number, outcome: { ok: unknown } | { error: string }) => ipcRenderer.send("desktop:projects-answer", id, outcome);
  const called = (source: ProjectsSource, message: Extract<ToPage, { type: "call" }>) => {
    const method = source[message.method] as (...args: unknown[]) => Promise<unknown>;
    Promise.resolve().then(() => method(...message.args)).then(
      (ok) => answer(message.id, { ok }),
      (error: unknown) => answer(message.id, { error: error instanceof Error ? error.message : String(error) }),
    ).catch(() => {
      // An answer no message can carry, as one holding a function, is said, not left to run out of time.
      answer(message.id, { error: "The agent's page answered with something it cannot send" });
    });
  };
  ipcRenderer.on("desktop:projects", (_event, message: ToPage) => {
    const source = projects;
    if (message.type === "unsubscribe") {
      subscriptions.get(message.id)?.();
      subscriptions.delete(message.id);
    } else if (!source) {
      // A subscription is made again once the page serves: the main process follows anew then.
      if (message.type !== "call") return;
      if (servesNone) answer(message.id, { error: NONE });
      else early = [...early.filter((held) => Date.now() < held.deadline), message];
    } else if (message.type === "subscribe") {
      subscriptions.set(message.id, source.subscribe(message.projectId, (threadId) => {
        ipcRenderer.send("desktop:projects-changed", message.id, threadId);
      }));
    } else {
      called(source, message);
    }
  });
  // What the user sent from the desktop's quick entry, for the new chat the shell opened: held until
  // the page listens, as when it comes while the chat's page still loads. A page that leaves that
  // chat first never hears it, and the desktop is told: the text was for that chat only.
  type QuickMessage = { id: string; text: string };
  let handed: QuickMessage | null = null;
  const quickListeners = new Set<(message: QuickMessage) => void>();
  const answerQuick = (id: unknown, refused: unknown) => ipcRenderer.send("desktop:quick-entry-answer", id, refused);
  const LEFT_CHAT = "The agent's page left the new chat before it heard the message, so nothing was sent.";
  ipcRenderer.on("desktop:quick-entry", (_event, message: unknown) => {
    const { id, text } = (message ?? {}) as { id?: unknown; text?: unknown };
    if (typeof id !== "string" || typeof text !== "string") return;
    // Come after the page left the new chat, it is for none the page shows.
    if (location.pathname !== "/chat") return answerQuick(id, LEFT_CHAT);
    if (quickListeners.size === 0) handed = { id, text };
    for (const listener of quickListeners) listener({ id, text });
  });
  // The Navigation API tells of a pushState too, which the TypeScript of this pin does not type.
  (window as unknown as { navigation: EventTarget }).navigation.addEventListener("currententrychange", () => {
    if (handed === null || location.pathname === "/chat") return;
    answerQuick(handed.id, LEFT_CHAT);
    handed = null;
  });
  contextBridge.exposeInMainWorld("surogateDesktop", {
    version: 1,
    getDevice: call("getDevice"),
    webSignIn: call("webSignIn"),
    signOut: call("signOut"),
    prepareFolder: call("prepareFolder"),
    bindSession: call("bindSession"),
    setMode: call("setMode"),
    requestFreeMode: call("requestFreeMode"),
    cancelPrepared: call("cancelPrepared"),
    getBinding: call("getBinding"),
    // Only at its user's click: the agent's page cannot open file manager windows by itself.
    revealFolder: (sessionId: unknown) => {
      const clicked = navigator.userActivation.isActive && performance.now() - clickedAt < CLICK_MS;
      clickedAt = -Infinity;
      return clicked
        ? ipcRenderer.invoke("desktop:revealFolder", sessionId)
        : Promise.reject(new Error("Surogate shows a chat's folder only when its user asks, with a click"));
    },
    onBindingChanged: (listener: (sessionId: string) => void) => {
      const relay = (_event: unknown, sessionId: unknown) => {
        if (typeof sessionId === "string") listener(sessionId);
      };
      ipcRenderer.on("desktop:binding-changed", relay);
      return () => ipcRenderer.off("desktop:binding-changed", relay);
    },
    onQuickEntry: (listener: (message: QuickMessage) => void) => {
      quickListeners.add(listener);
      const held = handed;
      handed = null;
      if (held !== null) listener(held);
      return () => {
        quickListeners.delete(listener);
      };
    },
    answerQuickEntry: answerQuick,
    getAppearance: call("getAppearance"),
    onAppearanceChanged: (listener: (appearance: unknown) => void) => {
      const relay = (_event: unknown, appearance: unknown) => listener(appearance);
      ipcRenderer.on("desktop:appearance", relay);
      return () => ipcRenderer.off("desktop:appearance", relay);
    },
    setAccount: call("setAccount"),
    registerProjects: (source: ProjectsSource | null) => {
      if (source && METHODS.some((name) => typeof source[name] !== "function")) {
        throw new Error("A projects source's methods must be its own properties, as an object literal's are");
      }
      for (const end of subscriptions.values()) end();
      subscriptions.clear();
      projects = source;
      servesNone = source === null;
      const held = early;
      early = [];
      for (const message of held) {
        if (message.deadline - Date.now() < LAST_SECOND_MS) continue;
        if (source) called(source, message);
        else answer(message.id, { error: NONE });
      }
      return ipcRenderer.invoke("desktop:registerProjects", source !== null);
    },
  });
}
