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
    read: (id: string | null) => ipcRenderer.invoke("shell:read", id),
    focusPane: () => ipcRenderer.invoke("shell:focus-pane"),
    resolve: (id: string) => ipcRenderer.invoke("shell:resolve", id),
    reopen: (id: string) => ipcRenderer.invoke("shell:reopen", id),
    history: (path: string) => ipcRenderer.invoke("shell:history", path),
    closeHistory: () => ipcRenderer.invoke("shell:history-close"),
    openVersion: (id: string) => ipcRenderer.invoke("shell:history-open", id),
    back: () => ipcRenderer.invoke("shell:back"),
    forward: () => ipcRenderer.invoke("shell:forward"),
    reload: () => ipcRenderer.invoke("shell:reload"),
    place: (hole: { x: number; y: number; width: number; height: number }) => ipcRenderer.invoke("shell:place", hole),
    placePane: (hole: { x: number; y: number; width: number; height: number }) => ipcRenderer.invoke("shell:place-pane", hole),
    menu: (which: "app" | "project") => ipcRenderer.invoke("shell:menu", which),
    settings: () => ipcRenderer.invoke("shell:settings"),
    newProject: () => ipcRenderer.invoke("shell:new-project"),
    projectSettings: () => ipcRenderer.invoke("shell:project-settings"),
    quitNow: () => ipcRenderer.invoke("shell:quit-now"),
    link: (which: string) => ipcRenderer.invoke("shell:link", which),
    sandbox: (action: string) => ipcRenderer.invoke("shell:sandbox", action),
    update: () => ipcRenderer.invoke("shell:update"),
    onChanged: listen("shell:changed"),
    // Where the keyboard comes back to from the pane's transcript: its head's Open or Back.
    onPaneLeft: (listener: (to: string) => void) => {
      const relay = (_event: unknown, to: unknown) => listener(String(to));
      ipcRenderer.on("shell:pane-left", relay);
      return () => ipcRenderer.off("shell:pane-left", relay);
    },
  });
  contextBridge.exposeInMainWorld("surogateProject", {
    state: () => ipcRenderer.invoke("project:state"),
    save: (fields: unknown) => ipcRenderer.invoke("project:save", fields),
    archive: () => ipcRenderer.invoke("project:archive"),
    close: () => ipcRenderer.invoke("project:close"),
  });
  contextBridge.exposeInMainWorld("surogateSettings", {
    state: () => ipcRenderer.invoke("settings:state"),
    folders: () => ipcRenderer.invoke("settings:folders"),
    takeBack: (root: string, host: string) => ipcRenderer.invoke("settings:take-back", root, host),
    takeBrowserBack: (root: string) => ipcRenderer.invoke("settings:take-back-browser", root),
    handBrowserBack: () => ipcRenderer.invoke("settings:hand-back-browser"),
    takePortBack: (root: string, port: number) => ipcRenderer.invoke("settings:take-back-port", root, port),
    stop: (root: string, id: string) => ipcRenderer.invoke("settings:stop", root, id),
    set: (key: string, value: string) => ipcRenderer.invoke("settings:set", key, value),
    link: (which: string) => ipcRenderer.invoke("settings:link", which),
    sandbox: (action: string) => ipcRenderer.invoke("settings:sandbox", action),
    close: () => ipcRenderer.invoke("settings:close"),
    onChanged: listen("settings:changed"),
    // A section to show while it is open, as when the agent's page opens Browser.
    onShow: (listener: (section: string) => void) => {
      const relay = (_event: unknown, section: unknown) => listener(String(section));
      ipcRenderer.on("settings:show", relay);
      return () => ipcRenderer.off("settings:show", relay);
    },
  });
  contextBridge.exposeInMainWorld("surogateAbout", {
    state: () => ipcRenderer.invoke("about:state"),
    copy: () => ipcRenderer.invoke("about:copy"),
    documentation: () => ipcRenderer.invoke("about:documentation"),
    close: () => ipcRenderer.invoke("about:close"),
  });
  contextBridge.exposeInMainWorld("surogateQuick", {
    send: (text: string) => ipcRenderer.invoke("quick:send", text),
    dismiss: () => ipcRenderer.invoke("quick:dismiss"),
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
