// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A change to a local-folder chat's files (an upload, a delete) that the computer
// it is on has not answered yet is answered 202: its user may still be asked to
// allow it there. The same request, sent again under its request id, joins it,
// so it runs once and is answered as it ended (surogates/api/routes/workspace.py).
// A fetch that fails (a network that drops) is sent again the same way: the
// change may already be waiting on the computer, and a new id would be a second
// change. A cloud chat's change is sent once, as before: its storage keeps no
// request to join, so sending it again would do it twice.

export const RETRY_FIRST_MS = 1_000;
export const RETRY_MOST_MS = 10_000;
// Past the ten minutes the app's prompt waits for its user, so a change its user
// answers in time is never given up on; and well within the hour the server keeps
// a finished request's rows, so a send never comes after they are gone.
export const GIVE_UP_MS = 11 * 60_000;
export const NOT_FINISHED =
  "This change did not finish on the computer this chat's folder is on. It may still be waiting there: check the folder before you try again.";

export interface UntilAnsweredOptions {
  /** A local-folder chat's change: sent again until it is answered. */
  onDevice: boolean;
  /** Stops the sending: the promise rejects with the signal's reason. */
  signal?: AbortSignal;
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
  now?: () => number;
}

const pause = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      reject(signal.reason);
    }, { once: true });
  });

/** Sends a change under one new request id until it is answered with anything but 202. */
export async function untilAnswered(
  send: (requestId: string) => Promise<Response>,
  { onDevice, signal, sleep = pause, now = Date.now }: UntilAnsweredOptions,
): Promise<Response> {
  const requestId = crypto.randomUUID().replaceAll("-", "");
  if (!onDevice) return send(requestId);
  const started = now();
  for (let wait = RETRY_FIRST_MS; ; wait = Math.min(wait * 2, RETRY_MOST_MS)) {
    signal?.throwIfAborted();
    try {
      const response = await send(requestId);
      if (response.status !== 202) return response;
    } catch (error) {
      // Stopped: said as it was. Otherwise not answered at all: asked again below, under the same id.
      if (signal?.aborted) throw error;
    }
    if (now() - started >= GIVE_UP_MS) throw new Error(NOT_FINISHED);
    await sleep(wait, signal);
  }
}
