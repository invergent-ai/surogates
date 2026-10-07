// The bridge Surogate Desktop gives the agent's web client (desktop design, Section 8).
// A declaration file, so desktop/ can import it from outside its rootDir. The page uses
// only the calls its `version` has; window.surogateDesktop is undefined in a browser.
// Version 1 stays open to new calls until the first desktop release.
//
// A call that needs this computer's device rejects while there is none, with a message the
// page can show: another account's, a revoked computer, or none registered yet. One about a
// chat rejects for a chat with no folder on this computer.

import type { ProjectsSource } from "./projects-contract.js";

export interface DesktopDevice {
  deviceId: string;
  name: string;
}

export interface DesktopDeviceState {
  device: DesktopDevice | null; // null until the app has added this computer to the agent
  localFolders: boolean; // the agent can bind a chat to a folder of this computer
}

export interface DesktopPreparedFolder {
  folder: string;
  mode: "free" | "ask";
  nonce: string;
  token: string; // for bindSession only: never sent to the server
}

export interface DesktopAppearance {
  theme: "light" | "dark"; // the theme in effect
  textSize: "small" | "medium" | "large";
  transcriptWidth: "narrow" | "medium" | "wide";
  motion: "system" | "reduced";
}

// The signed-in user, as GET /api/v1/auth/me gives them.
export interface DesktopAccount {
  name: string;
  email: string;
  userId: string;
  orgId: string;
}

export interface DesktopBridge {
  readonly version: 1;
  getDevice(): Promise<DesktopDeviceState>;
  // The app signs in itself, in the system browser: this is a one-time code for the page's own
  // session, which the page exchanges at POST /api/v1/auth/oauth/web-session. Null while nobody
  // is signed in to the app.
  webSignIn(): Promise<{ code: string } | null>;
  // Log out of the app, after its own confirmation: this computer's access to the agent ends, its
  // folders here are forgotten, and this page's session goes with the window's storage.
  signOut(): Promise<void>;
  // Null when the user cancels the sheet, or its page goes. Rejects with "Surogate is already
  // asking" while this window's last one is still open.
  prepareFolder(choice: "last" | "pick"): Promise<DesktopPreparedFolder | null>;
  bindSession(sessionId: string, token: string): Promise<void>;
  // From now on the chat asks before each command, file change and input. The page can
  // make a chat only safer: "Work freely" is the desktop's own to grant.
  setMode(sessionId: string, mode: "ask"): Promise<void>;
  // Opens the desktop's own confirmation; true once the chat works freely, false when the user
  // keeps it asking or nobody answers in time. Rejects with "Surogate is already asking" while
  // this window's last one is still open, and, once it was kept asking, with "The user chose to
  // keep this chat asking" for that chat until the page loads again: the switch can be refused.
  requestFreeMode(sessionId: string): Promise<boolean>;
  // Drops a folder confirmed with prepareFolder whose chat was never created.
  cancelPrepared(token: string): Promise<void>;
  getAppearance(): Promise<DesktopAppearance>;
  onAppearanceChanged(listener: (appearance: DesktopAppearance) => void): () => void;
  // Who is signed in; null once nobody is.
  setAccount(account: DesktopAccount | null): Promise<void>;
  // The page serves its agent's projects, and the shell calls them through the main process
  // (Section 12). Null withdraws them, as when the user signs out.
  registerProjects(source: ProjectsSource | null): Promise<void>;
}

declare global {
  interface Window {
    surogateDesktop?: DesktopBridge;
  }
}
