// The preload of the desktop's own pages, loaded from the app's files. Each call goes to
// the handlers the main process registered on that page's own webContents, and to no
// other page's.

import { contextBridge, ipcRenderer } from "electron";

const listen = (channel: string) => (listener: () => void) => {
  const relay = () => listener();
  ipcRenderer.on(channel, relay);
  return () => ipcRenderer.off(channel, relay);
};

if (location.protocol === "file:") {
  contextBridge.exposeInMainWorld("surogateShell", {
    state: () => ipcRenderer.invoke("shell:state"),
    connect: (address: string) => ipcRenderer.invoke("shell:connect", address),
    signIn: () => ipcRenderer.invoke("shell:sign-in"),
    signOut: () => ipcRenderer.invoke("shell:sign-out"),
    remove: () => ipcRenderer.invoke("shell:remove"),
    restore: () => ipcRenderer.invoke("shell:restore"),
    go: (path: string) => ipcRenderer.invoke("shell:go", path),
    projects: () => ipcRenderer.invoke("shell:projects"),
    project: (id: string) => ipcRenderer.invoke("shell:project", id),
    thread: (id: string) => ipcRenderer.invoke("shell:thread", id),
    back: () => ipcRenderer.invoke("shell:back"),
    forward: () => ipcRenderer.invoke("shell:forward"),
    reload: () => ipcRenderer.invoke("shell:reload"),
    place: (hole: { x: number; y: number; width: number; height: number }) => ipcRenderer.invoke("shell:place", hole),
    menu: (which: "app" | "project") => ipcRenderer.invoke("shell:menu", which),
    settings: () => ipcRenderer.invoke("shell:settings"),
    link: (which: string) => ipcRenderer.invoke("shell:link", which),
    onChanged: listen("shell:changed"),
  });
  contextBridge.exposeInMainWorld("surogateSettings", {
    state: () => ipcRenderer.invoke("settings:state"),
    set: (key: string, value: string) => ipcRenderer.invoke("settings:set", key, value),
    link: (which: string) => ipcRenderer.invoke("settings:link", which),
    close: () => ipcRenderer.invoke("settings:close"),
    onChanged: listen("settings:changed"),
  });
  contextBridge.exposeInMainWorld("surogatePrompt", {
    state: () => ipcRenderer.invoke("prompt:state"),
    answer: (button: string, choice: string | null) => ipcRenderer.invoke("prompt:answer", button, choice),
    onChanged: listen("prompt:changed"),
    onArmed: (listener: (armed: boolean) => void) => {
      const relay = (_event: unknown, armed: unknown) => listener(armed === true);
      ipcRenderer.on("prompt:armed", relay);
      return () => ipcRenderer.off("prompt:armed", relay);
    },
  });
}
