// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A project's thread on a folder of this computer, as the web client starts one from its card in
// Surogate Desktop (desktop design, Section 12). Every import here is a file of web/src named with
// its extension, so a node test runs it.

import type { WorkstreamRoutes } from "../api/workstream-routes.ts";
import type { DesktopBridge } from "./desktop-bridge-contract";
import { saidBy } from "./local-chat.ts";
import type { ThreadRow } from "./projects-contract";

export const NO_FOLDER = "No folder was chosen for this thread";

type LocalDesktop = Pick<DesktopBridge, "getDevice" | "prepareFolder" | "bindSession" | "cancelPrepared">;

/** What a thread on this computer needs of the agent: the project routes, and whether it hears this computer now. */
export type LocalThreadApi = Pick<WorkstreamRoutes, "get" | "makeOnComputer" | "begin"> & {
  online(deviceId: string): Promise<boolean>;
};

/** The proposal's card the thread is started from. */
export interface LocalCard {
  projectId: string;
  proposalId: string;
  key: string;
  title: string;
}

const fromDesktop = <T>(call: Promise<T>): Promise<T> =>
  call.catch((error: unknown) => {
    throw new Error(saidBy(error));
  });

/** The adapter's startLocalThread, in Surogate Desktop only: a browser has no folder of this computer to offer. */
export function localThreads(
  desktop: LocalDesktop | undefined,
  api: LocalThreadApi,
): { startLocalThread?(card: LocalCard): Promise<ThreadRow> } {
  return desktop ? { startLocalThread: (card) => startLocalThread(desktop, api, card) } : {};
}

/**
 * Start the thread a proposal's card names in a folder of this computer, in Section 12's order:
 * the user confirms the folder in the desktop's own sheet, which names the project and the thread;
 * the thread is made with it; the desktop records the folder for that thread, settling once the
 * agent has heard; only then does the thread begin. The confirmation's token never leaves the bridge.
 */
export async function startLocalThread(desktop: LocalDesktop, api: LocalThreadApi, card: LocalCard): Promise<ThreadRow> {
  const { device, localFolders } = await fromDesktop(desktop.getDevice());
  if (!device) {
    throw new Error("Surogate can't work on folders of this computer for this account. Run this thread in the cloud instead.");
  }
  if (!localFolders) {
    throw new Error("Local access to this computer was revoked. Restore it from Surogate's sidebar, or run this thread in the cloud instead.");
  }
  // Its binding reaches this computer through the agent: one the agent does not hear would wait in silence.
  if (!(await api.online(device.deviceId))) {
    throw new Error(`${device.name} is not connected to the agent right now, so this thread was not started. Allow it again once it is.`);
  }
  const { name } = await api.get(card.projectId);
  const prepared = await fromDesktop(desktop.prepareFolder("last", { project: name, thread: card.title }));
  if (!prepared) throw new Error(NO_FOLDER);
  let threadId: string;
  try {
    threadId = await api.makeOnComputer(card.projectId, card.proposalId, card.key, {
      kind: "device", device_id: device.deviceId, folder: prepared.folder, nonce: prepared.nonce,
    });
  } catch (error) {
    await desktop.cancelPrepared(prepared.token).catch(() => {});
    throw error;
  }
  try {
    await desktop.bindSession(threadId, prepared.token);
  } catch (error) {
    // Made: a binding recorded here before the link went is finished at its next connection.
    throw new Error(`Surogate made this thread but could not set up its folder on this computer: ${saidBy(error)}. Allow it again.`);
  }
  return api.begin(card.projectId, threadId);
}
