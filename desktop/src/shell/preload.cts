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
    );
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
