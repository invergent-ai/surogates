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
  const answer = (id: number, outcome: { ok: unknown } | { error: string }) => ipcRenderer.send("desktop:projects-answer", id, outcome);
  ipcRenderer.on("desktop:projects", (_event, message: ToPage) => {
    const source = projects;
    if (message.type === "unsubscribe") {
      subscriptions.get(message.id)?.();
      subscriptions.delete(message.id);
    } else if (!source) {
      if (message.type === "call") answer(message.id, { error: "The agent's page serves no projects" });
    } else if (message.type === "subscribe") {
      subscriptions.set(message.id, source.subscribe(message.projectId, (threadId) => {
        ipcRenderer.send("desktop:projects-changed", message.id, threadId);
      }));
    } else {
      const method = source[message.method] as (...args: unknown[]) => Promise<unknown>;
      Promise.resolve().then(() => method(...message.args)).then(
        (ok) => answer(message.id, { ok }),
        (error: unknown) => answer(message.id, { error: error instanceof Error ? error.message : String(error) }),
      );
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
      for (const end of subscriptions.values()) end();
      subscriptions.clear();
      projects = source;
      return ipcRenderer.invoke("desktop:registerProjects", source !== null);
    },
  });
}
