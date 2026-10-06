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
    go: (path: string) => ipcRenderer.invoke("shell:go", path),
    projects: () => ipcRenderer.invoke("shell:projects"),
    project: (id: string) => ipcRenderer.invoke("shell:project", id),
    back: () => ipcRenderer.invoke("shell:back"),
    forward: () => ipcRenderer.invoke("shell:forward"),
    reload: () => ipcRenderer.invoke("shell:reload"),
    place: (hole: { x: number; y: number; width: number; height: number }) => ipcRenderer.invoke("shell:place", hole),
    menu: (which: "app" | "project") => ipcRenderer.invoke("shell:menu", which),
    onChanged: listen("shell:changed"),
  });
}
