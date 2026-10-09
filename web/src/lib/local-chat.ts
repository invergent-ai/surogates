// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A chat on a folder of this computer, as the web client makes one in Surogate Desktop
// (desktop design, Section 8). Its one value import is a file of web/src named with its
// extension, and node strips its type-only import, so a node test runs it.

import { onDeviceOf } from "../api/device-requests.ts";
import type {
  DesktopBinding,
  DesktopBridge,
  DesktopDeviceState,
} from "./desktop-bridge-contract";

/** What the new chat's create sends the agent: the folder the user confirmed on this computer. */
export interface LocalExecution {
  kind: "device";
  device_id: string;
  folder: string;
  nonce: string; // names the confirmation: the token that goes with it never leaves the bridge
}

export const NO_FOLDER = "No folder was chosen for this chat";

// What Electron puts before the desktop's words: the name of the call it invoked.
const INVOKED = /^Error invoking remote method '[^']*': (?:Error: )?/;

/** Why a call failed, in the desktop's own words. */
export function saidBy(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(INVOKED, "");
}

// A refusal of the desktop's, said again in its own words.
const said = (error: unknown): never => {
  throw new Error(saidBy(error));
};

/**
 * The agent's /auth/config, as the page keeps it: local folders only where it says true. An older
 * server says nothing. Null where it could not be read: fetchAuthConfig's fallback names no agent,
 * and every server's answer does.
 */
export function desktopSessionsOf(config: {
  agent_id?: unknown;
  desktop_sessions?: unknown;
}): boolean | null {
  return config.agent_id === undefined
    ? null
    : config.desktop_sessions === true;
}

/** What the agent's /auth/config says; null until it has been read, and where it could not be. */
export interface LocalCapabilities {
  desktopSessions: boolean | null;
  multiSession: boolean | null;
}

/**
 * Where a new chat works, as the line under its composer says it. *device* is the desktop's,
 * null in a browser. The folder itself is shown only in the desktop's own sheet: the page
 * learns no path of this computer before its user confirms one.
 */
export function newChatPlace(
  device: DesktopDeviceState | null,
  agent: LocalCapabilities,
  choice: "last" | "pick",
): { local: boolean; text: string | null } {
  // A browser, or an agent that keeps one conversation: nothing to say.
  if (device === null || agent.multiSession === false) {
    return { local: false, text: null };
  }
  if (device.localFolders) {
    return {
      local: true,
      text:
        choice === "last"
          ? "Works in a folder on this computer: the one you used last, or a new one. You confirm it when you send."
          : "Works in a folder on this computer that you choose when you send.",
    };
  }
  // Nothing is said until the agent's config has been read.
  if (agent.desktopSessions === null) {
    return { local: false, text: null };
  }
  const why = cloudReason(device, agent.desktopSessions);
  return {
    local: false,
    text: `${why}, so this chat works in the cloud.${why === REVOKED ? " Restore it from Surogate's sidebar." : ""}`,
  };
}

const REVOKED = "Local access revoked";

/** Why a new chat in the desktop works in the cloud, once the agent's config has been read. */
function cloudReason(
  device: DesktopDeviceState,
  desktopSessions: boolean,
): string {
  if (!desktopSessions) {
    return "This server doesn't support local folders yet";
  }
  return device.device !== null
    ? REVOKED
    : "Surogate can't work on folders of this computer for this account";
}

/**
 * What a new chat needs of the agent: to make it, on a folder of this computer or in the cloud,
 * whether it hears this computer now, and its config read again.
 */
export interface NewChatApi<T> {
  create(execution?: LocalExecution): Promise<T>;
  online(deviceId: string): Promise<boolean>;
  capabilities(): Promise<LocalCapabilities>;
}

/**
 * A new chat, where the desktop says when its first message goes, not when the line under the
 * composer was drawn (*shown*): the computer may have been added since, as the first chat after
 * the first sign-in finds, or its access may have ended, when no chat is made until the line says
 * the cloud. On a folder of this computer it is made in
 * Section 8's order: the user confirms the folder in the desktop's own sheet, the chat is made
 * with it, and the desktop records the folder for that chat, settling once the agent has heard.
 * Only then may the first message and its attachments go. Anywhere else it is made in the cloud.
 */
export async function createChat<T extends { id: string }>(
  desktop:
    | Pick<
        DesktopBridge,
        "getDevice" | "prepareFolder" | "bindSession" | "cancelPrepared"
      >
    | undefined,
  agent: LocalCapabilities,
  choice: "last" | "pick",
  shown: { local: boolean },
  api: NewChatApi<T>,
): Promise<T> {
  if (!desktop) {
    return api.create();
  }
  // Unread, the line said nothing: a chat in the cloud would go there unsaid.
  const known =
    agent.desktopSessions === null ? await api.capabilities() : agent;
  if (known.desktopSessions === null) {
    throw new Error(
      "Surogate could not read the agent's settings, so this chat was not made. Send it again.",
    );
  }
  const state = await desktop.getDevice().catch(said);
  const device = state.device;
  if (!(device && newChatPlace(state, known, choice).local)) {
    if (!shown.local) {
      return api.create();
    }
    throw new Error(
      `${cloudReason(state, known.desktopSessions)}, so this chat was not made. Send it again to make it in the cloud.`,
    );
  }
  // Its binding reaches this computer through the agent: one the agent does not hear would wait in silence.
  if (!(await api.online(device.deviceId))) {
    throw new Error(
      `${device.name} is not connected to the agent right now, so this chat was not made. Send it again once it is.`,
    );
  }
  const prepared = await desktop.prepareFolder(choice).catch(said);
  if (!prepared) {
    throw new Error(NO_FOLDER);
  }
  let chat: T;
  try {
    chat = await api.create({
      kind: "device",
      device_id: device.deviceId,
      folder: prepared.folder,
      nonce: prepared.nonce,
    });
  } catch (error) {
    await desktop.cancelPrepared(prepared.token).catch(() => {
      // Unheard, it expires by itself.
    });
    throw error;
  }
  try {
    await desktop.bindSession(chat.id, prepared.token);
  } catch (error) {
    // Kept: a binding recorded here before the link went is finished at its next connection,
    // and deleting the chat could race it.
    throw new Error(
      `Surogate made this chat but could not set up its folder on this computer: ${saidBy(error)}. Start a new chat.`,
    );
  }
  return chat;
}

/** A computer of the user's, as GET /api/v1/devices lists it: what a chat's bar reads. */
export interface ListedDevice {
  id: string;
  name: string;
  revoked_at: string | null;
}

/** A chat on a folder of a computer of the user's, as its bar shows it. */
export interface LocalChat {
  root: string; // the chat its binding names: a sub-agent's chat works in its root's folder
  folder: string; // as the computer showed it to its user
  name: string; // the folder's own name
  computer: string;
  revoked: boolean; // the computer's access was revoked: it works on nothing until restored
  here: DesktopBinding | null; // on this computer: its folder opens, and its mode shows
}

/**
 * The chat's folder and computer, from its config (the server stamps both when it is made),
 * the user's computers as the agent lists them now (null until they come) and, in the desktop,
 * the binding this computer holds. Null for a chat in the cloud.
 */
export function localChatOf(
  sessionId: string,
  config: Record<string, unknown> | undefined,
  devices: ListedDevice[] | null,
  here: DesktopBinding | null,
): LocalChat | null {
  if (!onDeviceOf(config)) {
    return null;
  }
  const execution = config?.execution as {
    device_id?: unknown;
    device_name?: unknown;
  };
  const folder =
    typeof config?.workspace_path === "string" ? config.workspace_path : "";
  const device = devices?.find((row) => row.id === execution.device_id);
  const root = config?.sandbox_root_session_id;
  return {
    root: typeof root === "string" ? root : sessionId,
    folder,
    name: folder.split("/").filter(Boolean).at(-1) ?? folder,
    computer:
      device?.name ??
      (typeof execution.device_name === "string"
        ? execution.device_name
        : "your computer"),
    revoked: device !== undefined && device.revoked_at !== null,
    here,
  };
}

/** A chat's mode, as its bar switches it: the page makes a chat ask; only the desktop's own confirmation lets it work freely. */
export async function switchMode(
  desktop: Pick<DesktopBridge, "setMode" | "requestFreeMode">,
  sessionId: string,
  mode: "free" | "ask",
): Promise<void> {
  if (mode === "ask") {
    await desktop.setMode(sessionId, "ask");
  } else {
    await desktop.requestFreeMode(sessionId);
  }
}

export type FolderCalls = DesktopBridge &
  Required<
    Pick<DesktopBridge, "getBinding" | "revealFolder" | "onBindingChanged">
  >;

/**
 * This desktop, where it has the calls about a chat's folder: the bridge's version 1 grows,
 * and a desktop from before them answers none, so the page looks for each.
 */
export function folderCalls(
  desktop: DesktopBridge | undefined,
): FolderCalls | null {
  const present =
    desktop !== undefined &&
    typeof desktop.getBinding === "function" &&
    typeof desktop.revealFolder === "function" &&
    typeof desktop.onBindingChanged === "function";
  return present ? (desktop as FolderCalls) : null;
}

// As the desktop's browser tools say it (surogates/devices/browser.py).
const NO_BROWSER_HERE =
  "No supported browser on this computer. Install Google Chrome, Microsoft Edge, Brave or Vivaldi, or pick one in Settings → Browser. The Snap build of Chromium is not supported.";

export type BrowserAction = "show" | "takeOver" | "handBack" | "settings";

type BrowserCalls = Pick<DesktopBridge, "browser" | "openSettings">;

/**
 * Who holds the browser on this computer, as the chat's binding reads it, and what this chat may do
 * about it: take it over from nobody, hand it back where it is held from this chat or from one that
 * is gone, and neither where it is held from another chat, for which the desktop refuses both. While
 * the desktop asks its user about a hand back (*asking*), no second one is offered.
 */
function heldBrowser(
  takenOver: DesktopBinding["takenOver"] | undefined,
  asking: boolean,
): { text: string; drive: BrowserAction | null } {
  const open = "The browser is open on this computer";
  if (takenOver === "elsewhere") {
    return {
      text: `${open}, and you have it, taken over from another chat: the agent waits until you hand it back there.`,
      drive: null,
    };
  }
  // A desktop from before the take-over says nothing of it: nobody holds its browser.
  if (!(takenOver === true || takenOver === "orphaned")) {
    return { text: `${open}.`, drive: "takeOver" };
  }
  if (asking) {
    return {
      text: `${open}, and you have it. Surogate is asking you, in a window of its own, whether to hand it back.`,
      drive: null,
    };
  }
  return {
    text:
      takenOver === true
        ? `${open}, and you have it: the agent waits until you hand it back.`
        : `${open}, and you have it, though the chat it was taken over from is gone: hand it back here, then write to the agent to go on.`,
    drive: "handBack",
  };
}

/**
 * What a local-folder chat's browser pane says, and the buttons it offers (Section 5): Show browser
 * and Take over or Hand back where the browser is open, or Settings → Browser where there is none,
 * each only in the desktop on the computer the chat is bound to, and only where that desktop has
 * the call. Elsewhere the pane says where the browser is, and nothing more. *available* is false
 * where the computer has no supported browser; a chat the page only reads (*readOnly*) is neither
 * taken over nor handed back from here. *asking* is true while the desktop asks its user whether to
 * hand the browser back.
 */
export function computerBrowser(
  chat: LocalChat,
  browser: { available: boolean; readOnly: boolean },
  desktop: BrowserCalls | null,
  asking = false,
): { text: string; actions: BrowserAction[] } {
  const here = desktop !== null ? chat.here : null;
  if (!browser.available) {
    if (!here) {
      return {
        text: `No supported browser on ${chat.computer}. Install Google Chrome, Microsoft Edge, Brave or Vivaldi there, or pick one in Surogate's Settings → Browser on it.`,
        actions: [],
      };
    }
    return {
      text: NO_BROWSER_HERE,
      actions: desktop?.openSettings ? ["settings"] : [],
    };
  }
  if (!here) {
    return { text: `The browser is open on ${chat.computer}.`, actions: [] };
  }
  const { text, drive } = heldBrowser(here.takenOver, asking);
  const driven = browser.readOnly || drive === null ? [] : [drive];
  return {
    text,
    actions: desktop?.browser ? ["show", ...driven] : [],
  };
}

/** What the pane's presses tell the chat's server: that its user took the browser over, and that they handed it back. */
export interface BrowserTelling {
  taken(): Promise<unknown>;
  /**
   * Told once the desktop answered a hand back true. *confirmed* where its user confirmed, in the
   * desktop's own window, handing back the browser this chat held; not where the chat it was held
   * from is gone, nor where nobody held it. Resolves whether the agent goes on by itself.
   */
  handedBack(confirmed: boolean): Promise<boolean>;
}

// What the pane says once the browser is handed back. Whether the agent goes on is the server's
// answer: it gives the chat's agent a turn only for a hand back its user confirmed, and only where
// the chat can take one then (no turn under way, not stopped, its user's limit not spent).
export const AGENT_GOES_ON =
  "The browser is the agent's again, and the agent goes on.";
export const WRITE_TO_THE_AGENT =
  "The browser is the agent's again. The agent does not go on by itself: write to it to go on.";

/**
 * What a button of the pane does, when pressed. The desktop's call is made in the same turn as the
 * press, before anything is awaited: the desktop lets one call through for a click of its user's.
 * The pause is the desktop's; the server is told of a take-over, and of a hand back only once the
 * desktop answered true (POST …/browser/control). *held* is who held the browser when the button
 * was pressed, as the chat's binding read it: only a hand back of the browser this chat held is
 * its user's confirmed one, for which the server gives the agent a turn where it can. Resolves
 * with what the pane says of a hand back made, whether the agent goes on or is to be written to;
 * rejects in the desktop's words, or with what the server could not be told.
 */
export async function actOnBrowser(
  action: BrowserAction,
  root: string,
  desktop: BrowserCalls,
  server: BrowserTelling,
  held?: DesktopBinding["takenOver"],
): Promise<string | null> {
  if (action === "settings") {
    await desktop.openSettings?.("browser");
    return null;
  }
  const calls = desktop.browser;
  if (!calls) {
    return null;
  }
  if (action === "show") {
    await calls.show(root);
    return null;
  }
  if (action === "takeOver") {
    await calls.takeOver(root);
    await server.taken().catch(() => {
      throw new Error("You have the browser, but the chat could not be told.");
    });
    return null;
  }
  if (!(await calls.handBack(root))) {
    return null;
  }
  const goesOn = await server.handedBack(held === true).catch(() => {
    throw new Error(
      "The browser is the agent's again, but the agent could not be told: write to it to go on.",
    );
  });
  return goesOn ? AGENT_GOES_ON : WRITE_TO_THE_AGENT;
}

/**
 * What a release posts to a chat's control route. A hand back its user confirmed in the desktop
 * says so; any other release is posted as it always was, and as a cloud chat's is.
 */
export function browserRelease(handedBack: boolean): {
  action: "release";
  handed_back?: true;
} {
  return handedBack
    ? { action: "release", handed_back: true }
    : { action: "release" };
}

/** The chat's control route, as the pane posts to it (POST …/browser/control). */
export interface BrowserPosts {
  acquire(): Promise<unknown>;
  /**
   * *handedBack* says the release is its user's hand back, confirmed in the desktop, of the browser
   * this chat held. The answer's `resumes` says whether the agent goes on by itself.
   */
  release(handedBack: boolean): Promise<unknown>;
}

/** What a chat's pane shows beside where its browser is. */
export interface BrowserPaneState {
  asking: boolean; // the desktop is asking its user whether to hand the browser back
  failure: string | null; // what the desktop refused, in its words, or what the server could not be told
  said: string | null; // once the browser is handed back: whether the agent goes on, or is to be written to
  answers: number; // the take-overs and hand backs the desktop has answered: the binding is read again at each
}

/**
 * A chat's browser pane, for as long as its page lives: what it shows beside where the browser is,
 * and what the chat's server has been told. The pane itself is drawn anew each time it is opened,
 * and a hand back, which waits for its user in the desktop, can outlast a drawing of it.
 */
export interface BrowserPane {
  state(): BrowserPaneState;
  /** Hears each change of the state; the function returned stops it. */
  subscribe(listener: () => void): () => void;
  /**
   * A button pressed: called first thing in its click's own handler, and the desktop is asked before
   * it returns. Settles once all the press does is over, the server told too, and never rejects.
   */
  press(
    action: BrowserAction,
    root: string,
    desktop: BrowserCalls,
    held?: DesktopBinding["takenOver"],
  ): Promise<void>;
  /** What the computer says of who holds the browser, when the chat is opened. Never rejects. */
  loaded(takenOver: DesktopBinding["takenOver"] | undefined): Promise<void>;
}

/**
 * The pane of one chat, which posts to that chat's control route (*posts*).
 *
 * The server tells the chat of a take-over once, and of a hand back only after one. So each is
 * posted in its turn, the next once the last was answered:
 * - a take-over the desktop made (acquire);
 * - a hand back the desktop answered true (release), with the take-over posted again before it
 *   unless this page saw the server answer it: one that never arrived, one made before this page
 *   loaded, or one made from another chat. The server tells a chat no second time of a take-over
 *   it knows, so the chat is told both, in order. The release says it is a hand back only where
 *   its user confirmed one of the browser this chat held: the server then gives the agent a turn
 *   where it can, answers whether it did, and the pane says that. Made for a chat that is gone,
 *   it is a release like the next, and the agent is to be written to;
 * - at the chat's load, where the computer says nobody holds the browser, a release, once: the app
 *   may have ended while its user held it, and the chat would still say they do. Nobody handed
 *   anything back, so it is no hand back, and wakes nobody. The server passes over a release with
 *   no take-over standing.
 */
export function browserPane(posts: BrowserPosts): BrowserPane {
  let state: BrowserPaneState = {
    asking: false,
    failure: null,
    said: null,
    answers: 0,
  };
  const listeners = new Set<() => void>();
  const set = (change: Partial<BrowserPaneState>) => {
    state = { ...state, ...change };
    for (const listener of [...listeners]) {
      listener();
    }
  };

  // What the server was last told, as far as this page knows: a post that failed may have arrived.
  let told: "unknown" | "taken" | "free" = "unknown";
  let line: Promise<unknown> = Promise.resolve();
  const inTurn = <T>(post: () => Promise<T>): Promise<T> => {
    const posted = line.then(post);
    line = posted.catch(() => {
      // Said by whoever posted it: the next is posted all the same.
    });
    return posted;
  };
  const post = async (action: "acquire" | "release", handedBack = false) => {
    told = "unknown";
    const answer =
      action === "acquire"
        ? await posts.acquire()
        : await posts.release(handedBack);
    told = action === "acquire" ? "taken" : "free";
    return answer;
  };
  const telling: BrowserTelling = {
    taken: () => inTurn(() => post("acquire")),
    handedBack: (confirmed) =>
      inTurn(async () => {
        if (told !== "taken") {
          await post("acquire");
        }
        const answer = await post("release", confirmed);
        // Only the server's own yes: an answer that says nothing of it is no turn given.
        return (answer as { resumes?: unknown } | null)?.resumes === true;
      }),
  };

  return {
    state: () => state,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    press: (action, root, desktop, held) => {
      const handingBack = action === "handBack";
      if (handingBack && state.asking) {
        return Promise.resolve();
      }
      // The desktop has answered a take-over or a hand back, whatever the server has yet to hear.
      let waiting = handingBack || action === "takeOver";
      const answered = () => {
        if (waiting) {
          waiting = false;
          set({
            asking: handingBack ? false : state.asking,
            answers: state.answers + 1,
          });
        }
      };
      const acted = actOnBrowser(
        action,
        root,
        desktop,
        {
          taken: () => {
            answered();
            return telling.taken();
          },
          handedBack: (confirmed) => {
            answered();
            return telling.handedBack(confirmed);
          },
        },
        held,
      );
      // What was said of the last press stays until the next, which starts clean.
      set({ asking: handingBack || state.asking, failure: null, said: null });
      return acted.then(
        (said) => {
          answered();
          if (said !== null) {
            set({ said });
          }
        },
        (error: unknown) => {
          answered();
          set({ failure: saidBy(error) });
        },
      );
    },
    loaded: (takenOver) => {
      if (takenOver !== false) {
        return Promise.resolve();
      }
      return inTurn(async () => {
        if (told !== "free") {
          await post("release");
        }
      }).catch(() => {
        // Nothing its user did is untold: it is posted again when the chat is next opened.
      });
    },
  };
}

/** Each chat's pane, one for a chat while the page lives: *postsOf* gives the chat's own control route. */
export function browserPanes(
  postsOf: (sessionId: string) => BrowserPosts,
): (sessionId: string) => BrowserPane {
  const panes = new Map<string, BrowserPane>();
  return (sessionId) => {
    const pane = panes.get(sessionId) ?? browserPane(postsOf(sessionId));
    panes.set(sessionId, pane);
    return pane;
  };
}
