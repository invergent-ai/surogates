// One registered device: its link to the agent, and the runner that does the
// work the link brings.

import type { OperationJournal } from "./journal/journal.js";
import { DeviceLink, type LinkStatus } from "./link/client.js";
import { type Executor, OperationRunner } from "./operations/runner.js";

export interface DeviceOptions {
  url: string;
  token: string;
  journal: OperationJournal;
  executor: Executor;
  onStatus?: (status: LinkStatus) => void;
  // Something the app cannot recover from (a journal that fails): the link has stopped.
  onError?: (error: unknown) => void;
  delay?: (attempt: number) => number;
}

export function connectDevice(options: DeviceOptions): { link: DeviceLink; runner: OperationRunner } {
  // The runner failed after the executor answered: the link stops, as when its own handler throws.
  const fail = (error: unknown): void => {
    void link.stop();
    options.onError?.(error);
  };
  // Opening the journal already answered what a crash cut off "interrupted".
  const runner = new OperationRunner(options.journal, options.executor, (frame) => link.send(frame), fail);
  // Set while the welcomed device is not the journal's: stop() takes a moment, and
  // what the server sent right behind the welcome would still arrive and be applied.
  let refused = false;
  const link: DeviceLink = new DeviceLink({
    url: options.url,
    token: options.token,
    delay: options.delay,
    openIds: () => runner.openIds(),
    handlers: {
      onWelcome: (welcome) => {
        // One journal per device: another device's results would be refused and resent forever.
        refused = !options.journal.claim(welcome.deviceId);
        if (refused) {
          void link.stop();
          return;
        }
        options.journal.prune();
        runner.connected();
      },
      onOperation: (operation) => {
        if (!refused) runner.operation(operation);
      },
      onCancel: (id) => {
        if (!refused) runner.cancel(id);
      },
      onAck: (id) => {
        if (!refused) runner.acknowledged(id);
      },
      onStatus: options.onStatus,
      onError: options.onError,
    },
  });
  return { link, runner };
}
