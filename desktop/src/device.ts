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
  delay?: (attempt: number) => number;
}

export function connectDevice(options: DeviceOptions): { link: DeviceLink; runner: OperationRunner } {
  // Opening the journal already answered what a crash cut off "interrupted".
  const runner = new OperationRunner(options.journal, options.executor, (frame) => link.send(frame));
  // Set while the welcomed device is not the journal's: stop() takes a moment, and
  // an operation sent right behind the welcome would still arrive and run.
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
      onCancel: (id) => runner.cancel(id),
      onAck: (id) => runner.acknowledged(id),
      onStatus: options.onStatus,
    },
  });
  return { link, runner };
}
