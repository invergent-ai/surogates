// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A change to a local-folder chat's files (an upload, a delete) that the computer
// it is on has not answered yet is answered 202: its user may still be asked to
// allow it there. The same request, sent again under its request id, joins it,
// so it runs once and is answered as it ended (surogates/api/routes/workspace.py).
// A fetch that fails (a network that drops) is sent again the same way: the
// change may already be waiting on the computer, and a new id would be a second
// change.

export const RETRY_FIRST_MS = 1_000;
export const RETRY_MOST_MS = 10_000;

const pause = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** Sends a change under one new request id until it is answered with anything but 202. */
export async function untilAnswered(
  send: (requestId: string) => Promise<Response>,
  sleep: (ms: number) => Promise<void> = pause,
): Promise<Response> {
  const requestId = crypto.randomUUID().replaceAll("-", "");
  for (let wait = RETRY_FIRST_MS; ; wait = Math.min(wait * 2, RETRY_MOST_MS)) {
    try {
      const response = await send(requestId);
      if (response.status !== 202) return response;
    } catch {
      // Not answered at all: asked again below, under the same id.
    }
    await sleep(wait);
  }
}
