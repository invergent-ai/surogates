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

// A chat's folder on this computer, as its binding holds it.
export interface DesktopBinding {
  folder: string; // as the folder sheet showed it, and the agent was told
  mode: "free" | "ask";
  // Where the agent's browser on this computer is held, as this chat reads it. While it is held from any
  // chat, the agent's browser calls wait in every chat here.
  //   true         its user holds it, taken over from this chat, which hands it back.
  //   false        nobody holds it: the agent drives it, and this chat may take it over.
  //   "elsewhere"  held from another chat that is here. This chat can neither take it over nor hand it
  //                back, as takeOver and handBack say: it is handed back there.
  //   "orphaned"   held from a chat that is gone (deleted, or its folder forgotten here), which can hand
  //                nothing back: every chat reads this then, and any of them hands it back, as handBack says.
  // Only true says this chat holds it, not any value but false: for "elsewhere" the desktop refuses both
  // takeOver and handBack, so a page offers neither. A chat the browser is not held from reads a change
  // at its next getBinding: onBindingChanged names the chat it is held from.
  takenOver: boolean | "orphaned" | "elsewhere";
}

// The agent's browser on this computer, for a chat bound here (Section 5): each call takes the chat's
// id as getBinding does (a sub-agent's chat is its root's), and rejects as getBinding does, and for a
// chat with no folder on this computer. A take-over and a hand back are told by onBindingChanged, for
// the chat the browser is held from. The browser is one for every chat of the agent's on this computer:
// held from one chat, it is held for all, and only that chat hands it back.
export interface DesktopBrowser {
  // The chat's newest page, brought to the front. Rejects with "Surogate shows the agent's browser
  // only when its user asks, with a click" but at a click of its user's, once for each, within 5 s
  // of it, as revealFolder does; and when the chat has no page open.
  show(sessionId: string): Promise<void>;
  // The user drives the browser from now on, held from this chat: the agent's browser calls in every
  // chat on this computer answer that the user has taken control, and their browser questions waiting
  // here go. It needs no click, since it only makes the chats safer; the chat's page comes to the
  // front only at a click of its user's, as show's does. Rejects with "The agent's browser on this
  // computer is taken over from another chat, and is handed back there" while another chat's
  // take-over stands (takenOver reads "elsewhere"), which this one does not end.
  takeOver(sessionId: string): Promise<void>;
  // The desktop asks its user, in a prompt window of its own, whose Hand back takes no press for a
  // moment after it opens; true once the agent drives the browser again, in every chat here; false
  // when the user keeps it, nobody answers in time, or the page that asked loads again meanwhile, which
  // closes the prompt. A page cannot hand it back on its own, nor ask to: rejects with "Surogate
  // hands the agent's browser back only when its user asks, with a click" but at a click of its
  // user's, once for each, within 5 s of it, as show does, whatever its user chose before; and with
  // "Surogate is already asking" while this window's last one is still open. Only the chat the browser
  // is held from hands it back, while that chat is here: from another chat (takenOver reads "elsewhere")
  // it rejects with "The agent's browser on this computer is taken over from another chat, and is handed
  // back there", and nothing is asked. Once the chat it was held from is gone (takenOver reads "orphaned"),
  // any chat hands it back, at its user's click and the desktop's prompt as any. True, with no prompt,
  // where nobody holds the browser.
  handBack(sessionId: string): Promise<boolean>;
}

// The project's thread a folder is asked for (Section 12): the sheet names both. The page's own
// words, which the desktop shows as text.
export interface DesktopThreadLabel {
  project: string; // at most 256 UTF-16 units, as the project's name
  thread: string; // at most 256, as the thread's title
}

export interface DesktopAppearance {
  theme: "light" | "dark"; // the theme in effect: it drives the page's prefers-color-scheme, which the web client follows
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
  // asking" while this window's last one is still open. *thread*, for a project's thread, is
  // named on the sheet.
  prepareFolder(choice: "last" | "pick", thread?: DesktopThreadLabel | null): Promise<DesktopPreparedFolder | null>;
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
  // The three calls about a chat's folder came after version 1's first desktops, which have none of
  // them: the page looks for each before it calls it.
  // The chat's folder on this computer, and whether it asks; null for a chat with no folder here.
  // Rejects as every call about a chat does: on a page of another account's, or of nobody's, with
  // "This computer is registered with the agent for another account", and while this computer is
  // not registered with the agent, or its access was revoked.
  getBinding?(sessionId: string): Promise<DesktopBinding | null>;
  // Shows the chat's folder in the file manager, selected in its parent. Rejects as getBinding
  // does; with "This chat has no folder on this computer" for a chat getBinding answers null for;
  // with "Surogate shows a chat's folder only when its user asks, with a click" but at a
  // click of its user's, once for each, within 5 s of it; with "Surogate is still showing a folder"
  // while this window's last one is being shown; with "Surogate is still looking for …" while a
  // look at this folder, or at two others, has not returned; and once the folder is not there,
  // does not answer within 5 s, or was replaced after it was confirmed for the chat.
  revealFolder?(sessionId: string): Promise<void>;
  // The browser's calls and Settings came after version 1's first desktops too: the page looks for each.
  browser?: DesktopBrowser;
  // Opens the desktop's own Settings on *section*: Browser, as the browser pane's no-browser message
  // offers. Rejects with "Surogate opens its Settings only when its user asks, with a click" but at
  // a click of its user's, as revealFolder does, and with "Surogate has a project's dialog open:
  // close it to open Settings" while one is over the window.
  openSettings?(section: "browser"): Promise<void>;
  // Hears each chat bound on this computer, each change of a chat's mode, and each chat's folder
  // forgotten here, as for a deleted chat, by the chat's id.
  onBindingChanged?(listener: (sessionId: string) => void): () => void;
  getAppearance(): Promise<DesktopAppearance>;
  onAppearanceChanged(listener: (appearance: DesktopAppearance) => void): () => void;
  // Who is signed in; null once nobody is.
  setAccount(account: DesktopAccount | null): Promise<void>;
  // The page serves its agent's projects, and the shell calls them through the main process
  // (Section 12). Null withdraws them, as when the user signs out. The desktop is handed a copy,
  // which keeps the source's own methods, bound to it, and leaves any prototype behind: a
  // source's methods are its own properties, as an object literal's are, or it is refused.
  registerProjects(source: ProjectsSource | null): Promise<void>;
}

declare global {
  interface Window {
    surogateDesktop?: DesktopBridge;
  }
}
