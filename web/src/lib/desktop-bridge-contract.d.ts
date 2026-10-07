// The bridge Surogate Desktop gives the agent's web client (desktop design, Section 8).
// A declaration file, so desktop/ can import it from outside its rootDir. The page uses
// only the calls its `version` has; window.surogateDesktop is undefined in a browser.

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
  prepareFolder(choice: "last" | "pick"): Promise<DesktopPreparedFolder | null>;
  bindSession(sessionId: string, token: string): Promise<void>;
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
