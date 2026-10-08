// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A change to a local-folder chat's files (an upload, a delete) that the computer
// it is on has not answered yet is answered 202: its user may still be asked to
// allow it there. The same request, sent again under its request id, joins it,
// so it runs once and is answered as it ended (surogates/api/routes/workspace.py).
// A fetch that fails (a network that drops) is sent again the same way: the
// change may already be waiting on the computer, and a new id would be a second
// change. An upload's 202 names its change, a digest of what it does: sent again
// by that digest alone, its file does not cross again, and a 428 says the server
// holds no such change, so it is sent whole. A 429 says the chat has its fill of
// changes waiting on the computer: it is sent again the same way until one of
// them is done and it is let in. A cloud chat's change is sent once, as before:
// its storage keeps no request to join, so sending it again would do it twice.
//
// A read (the file tree) whose computer is offline is answered 503
// device_offline, and read again until the computer is back. A change answered
// so is not sent again: the server cancelled it before it answered, and one its
// user allowed just before may still land, so a second send could do it twice.

export const RETRY_FIRST_MS = 1_000;
export const RETRY_MOST_MS = 10_000;
// Past the ten minutes the app's prompt waits for its user, so a change its user
// answers in time is never given up on; and well within the hour the server keeps
// a finished request's rows, so a send never comes after they are gone.
export const GIVE_UP_MS = 11 * 60_000;
export const NOT_FINISHED =
  "This change did not finish on the computer this chat's folder is on. It may still be waiting there: check the folder before you try again.";

/** Whether a session's config says it works on a folder of its user's computer (surogates/devices/binding.py). */
export function onDeviceOf(config: Record<string, unknown> | null | undefined): boolean {
  const execution = config?.execution;
  return typeof execution === "object" && execution !== null && (execution as { kind?: unknown }).kind === "device";
}

/** The computer a local-folder chat works on, by the name the server stamped in its config. */
export function computerOf(config: Record<string, unknown> | null | undefined): string {
  const name = onDeviceOf(config) ? (config?.execution as { device_name?: unknown }).device_name : undefined;
  return typeof name === "string" ? name : "your computer";
}

/**
 * What the file panel says of a refusal from the computer a local-folder chat's folder is on
 * (surogates/api/routes/workspace.py's details), as the spec's Section 8 words it: a computer
 * whose access ended, or one that is offline. Undefined for any other: the server's own words do.
 * *computer* names the computer, asked only for the refusal that names it.
 */
export async function refusalOf(detail: unknown, computer: () => Promise<string>): Promise<string | undefined> {
  const code = typeof detail === "object" && detail !== null ? (detail as { error?: unknown }).error : undefined;
  if (code === "device_revoked") {
    return "Local access revoked";
  }
  if (code === "device_offline") {
    return `${await computer()} is offline. Try again once it is back.`;
  }
  return undefined;
}

export interface UntilAnsweredOptions {
  /** A local-folder chat's change: sent again until it is answered. */
  onDevice: boolean;
  /**
   * Stops a local-folder chat's sending: the promise rejects with the signal's reason. A
   * cloud chat's one send never sees it, so closing the panel never cuts an upload short.
   */
  signal?: AbortSignal;
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
  now?: () => number;
  /** Told of each wait before the change is sent again: for its user on the computer (202), or for its turn there (429). */
  onWaiting?: (status: 202 | 429) => void;
}

const pause = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      reject(signal.reason);
    }, { once: true });
  });

// The change a 202 names, if it names one.
async function changeOf(response: Response): Promise<string | null> {
  const body = (await response.json().catch(() => null)) as { change?: unknown } | null;
  return typeof body?.change === "string" ? body.change : null;
}

/**
 * Sends a change under one new request id until it is answered with anything but 202 or 429.
 * *send* gets the change to name in place of the body, or null to send it whole, and the
 * signal its fetch stops with: none for a cloud chat's, which runs as it always did.
 */
export async function untilAnswered(
  send: (requestId: string, change: string | null, signal: AbortSignal | undefined) => Promise<Response>,
  { onDevice, signal, sleep = pause, now = Date.now, onWaiting }: UntilAnsweredOptions,
): Promise<Response> {
  const requestId = crypto.randomUUID().replaceAll("-", "");
  if (!onDevice) {
    const response = await send(requestId, null, undefined);
    // Kept on a computer this client did not know the chat was on: not a change that finished.
    if (response.status === 202) throw new Error(NOT_FINISHED);
    return response;
  }
  const started = now();
  let change: string | null = null;
  for (let wait = RETRY_FIRST_MS; ; ) {
    signal?.throwIfAborted();
    try {
      const response = await send(requestId, change, signal);
      if (response.status === 428 && change !== null) {
        // The server holds no such change: sent whole, at once.
        change = null;
        continue;
      }
      // Waiting on the computer (202), or waiting its turn there (429): asked again below.
      if (response.status !== 202 && response.status !== 429) return response;
      onWaiting?.(response.status as 202 | 429);
      change = (await changeOf(response)) ?? change;
    } catch (error) {
      // Stopped: said as it was. Otherwise not answered at all: asked again below, under the same id.
      if (signal?.aborted) throw error;
    }
    if (now() - started >= GIVE_UP_MS) throw new Error(NOT_FINISHED);
    await sleep(wait, signal);
    wait = Math.min(wait * 2, RETRY_MOST_MS);
  }
}

/**
 * Reads with *read* until its computer answers: while the server says it is offline (503
 * device_offline), *onWaiting* is told, and it is read again every RETRY_MOST_MS. *signal* stops
 * it, with the signal's reason: the panel closed, or reads something else.
 */
export async function untilOnline(
  read: (signal: AbortSignal | undefined) => Promise<Response>,
  { signal, sleep = pause, onWaiting }: Pick<UntilAnsweredOptions, "signal" | "sleep"> & { onWaiting?: () => void },
): Promise<Response> {
  for (;;) {
    signal?.throwIfAborted();
    const response = await read(signal);
    if (response.status !== 503) {
      return response;
    }
    const body = (await response.clone().json().catch(() => null)) as { detail?: { error?: unknown } } | null;
    if (body?.detail?.error !== "device_offline") {
      return response;
    }
    onWaiting?.();
    await sleep(RETRY_MOST_MS, signal);
  }
}
