// One registered device: its link to the agent, and the runner that does the
// work the link brings.

import type { OperationJournal } from "./journal/journal.js";
import { DeviceLink, type LinkStatus } from "./link/client.js";
import { type Executor, OperationRunner } from "./operations/runner.js";
import { report } from "./report.js";

export interface DeviceOptions {
  url: string;
  token: string;
  journal: OperationJournal;
  executor: Executor;
  onStatus?: (status: LinkStatus) => void;
  // Something the app cannot recover from (a journal that fails, a journal that is
  // another device's): said first, then the link stops. Required, so a stop is never silent.
  onError: (error: unknown) => void;
  delay?: (attempt: number) => number;
}

export function connectDevice(options: DeviceOptions): { link: DeviceLink; runner: OperationRunner } {
  // The device cannot go on: say why, then stop the link, as when its own handler throws.
  const fail = (error: unknown): void => {
    report(options.onError, error);
    void link.stop();
  };
  // Opening the journal already answered what a crash cut off "interrupted".
  const runner = new OperationRunner(options.journal, options.executor, (frame) => link.send(frame), fail);
  const link: DeviceLink = new DeviceLink({
    url: options.url,
    token: options.token,
    delay: options.delay,
    openIds: () => runner.openIds(),
    handlers: {
      onWelcome: (welcome) => {
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
      onStatus: options.onStatus,
      onError: options.onError,
    },
  });
  return { link, runner };
}
