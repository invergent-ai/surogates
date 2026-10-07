// One registered device: its link to the agent, and the runner that does the
// work the link brings.

import type { OperationJournal } from "./journal/journal.js";
import { DeviceLink, type LinkStatus } from "./link/client.js";
import type { Welcome } from "./link/protocol.js";
import { ACCESS_ENDED, type Executor, OperationRunner } from "./operations/runner.js";
import { report } from "./report.js";

// The computer's access to the agent ended: nothing local goes on for an agent that
// cannot hear it. update_required keeps access, and its results go out after the update.
const SUSPENDING: readonly LinkStatus[] = ["revoked", "unauthenticated", "superseded"];

// Every way the link ends but the app's own stop: the server answers the user's own requests
// "offline" from then on, so none still asked about may run later. The app's stop, at its quit,
// suspends what waits instead, to be asked again at the next launch, which the server then cancels.
const ENDING: readonly LinkStatus[] = ["offline", "update_required", ...SUSPENDING];

export interface DeviceOptions {
  url: string;
  token: string;
  journal: OperationJournal;
  executor: Executor;
  onStatus?: (status: LinkStatus) => void;
  // Who the server says this device is, at each connect, before the journal is claimed. A
  // throw refuses it: the link stops, as when any of its handlers throws, and says why.
  onWelcome?: (welcome: Welcome) => void;
  // Something the app cannot recover from (a journal that fails, a journal that is
  // another device's): said first, then the link stops. Required, so a stop is never silent.
  onError: (error: unknown) => void;
  delay?: (attempt: number) => number;
}

export function connectDevice(options: DeviceOptions): { link: DeviceLink; runner: OperationRunner } {
  // The device cannot go on: say why, then stop the link, as when its own handler throws.
  const fail = (error: unknown): void => {
    report(options.onError, error);
    // A stop of its own, but not a quit: the app goes on, and an allow must not run what the server gave up on.
    runner.disconnected();
    void link.stop();
  };
  // Opening the journal already answered what a crash cut off "interrupted".
  const runner = new OperationRunner(
    options.journal, options.executor, (frame, written) => link.send(frame, written), fail,
  );
  const link: DeviceLink = new DeviceLink({
    url: options.url,
    token: options.token,
    delay: options.delay,
    openIds: () => runner.openIds(),
    handlers: {
      onWelcome: (welcome) => {
        options.onWelcome?.(welcome);
        // One journal per device: another device's results would be refused and resent forever.
        if (!options.journal.claim(welcome.deviceId)) {
          fail(new Error(
            `This journal belongs to another device, not ${welcome.deviceId}: its results would be refused, so the link stopped`,
          ));
          return;
        }
        options.journal.prune();
        runner.connected();
      },
      onOperation: (operation) => runner.operation(operation),
      onCancel: (id) => runner.cancel(id),
      onAck: (id) => runner.acknowledged(id),
      onChunkAck: (id, seq) => runner.chunkAcked(id, seq),
      onUnwanted: (id) => runner.unwanted(id),
      onRejected: (id) => runner.rejected(id),
      onChunk: (id, seq, data) => runner.chunk(id, seq, data),
      onStatus: (status) => {
        if (ENDING.includes(status)) runner.disconnected();
        if (SUSPENDING.includes(status)) {
          // What runs is recorded first; then what no operation holds ends too.
          void runner.suspend(ACCESS_ENDED).then(() => options.executor.end?.())
            .catch((error: unknown) => report(options.onError, error));
        }
        options.onStatus?.(status);
      },
      onError: options.onError,
    },
  });
  return { link, runner };
}

/**
 * Revoke the device *token* belongs to, on a link of its own, as a sign-out made while the
 * agent could not be reached must: it connects, with the link's backoff, until the agent hears
 * the revoke frame. *done* settles once the agent has ended the token (revoked, or unknown).
 */
export function revokeDevice(url: string, token: string, delay?: (attempt: number) => number): { done: Promise<void>; stop(): Promise<void> } {
  const { promise: done, resolve } = Promise.withResolvers<void>();
  const link: DeviceLink = new DeviceLink({
    url,
    token,
    delay,
    openIds: () => [],
    handlers: {
      // Nothing is run for a device being revoked: what the agent sends behind the welcome is dropped with it.
      onWelcome: () => void link.revoke(),
      onOperation: () => {},
      onCancel: () => {},
      onAck: () => {},
      onStatus: (status) => {
        if (status === "revoked" || status === "unauthenticated") resolve();
      },
    },
  });
  link.start();
  return { done, stop: () => link.stop() };
}

// How long verifyDevice waits for a welcome, retries included.
export const VERIFY_TIMEOUT_MS = 15_000;

/**
 * Who *token* connects as, with no journal and nothing run: the link is opened, read
 * up to its welcome, and closed. The hello holds nothing open, and an operation sent
 * behind the welcome is left to the next connection. Rejects when the server ends
 * the link instead, or no welcome comes within *timeoutMs*.
 */
export function verifyDevice(url: string, token: string, timeoutMs = VERIFY_TIMEOUT_MS): Promise<Welcome> {
  const { promise, resolve, reject } = Promise.withResolvers<Welcome>();
  let settled = false;
  const settle = (outcome: () => void): void => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    void link.stop().then(outcome);
  };
  const link: DeviceLink = new DeviceLink({
    url,
    token,
    openIds: () => [],
    handlers: {
      onWelcome: (welcome) => settle(() => resolve(welcome)),
      onOperation: () => {},
      onCancel: () => {},
      onAck: () => {},
      onStatus: (status) => {
        if (status === "unauthenticated" || status === "revoked" || status === "superseded" || status === "update_required") {
          settle(() => reject(new Error(`The agent did not accept this computer's token (${status})`)));
        }
      },
    },
  });
  const timer = setTimeout(() => settle(() => reject(new Error("The agent did not answer in time"))), timeoutMs);
  link.start();
  return promise;
}
