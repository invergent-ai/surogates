// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A chat on a folder of this computer, as the web client makes one in Surogate Desktop
// (desktop design, Section 8). Every import here is a file of web/src named with its extension,
// so a node test runs it.

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
