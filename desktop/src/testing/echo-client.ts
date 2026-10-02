// A device for the server's cross-check: it connects like the app, answers
// `which`, and holds anything else until cancelled when --hold is given. One
// JSON line per event on stdout; a link that stops itself says why as an "error" event.
// The journal's file stays locked while this runs, so an acknowledgement is said
// aloud as an "ack" event: the cross-check reads the file only after it quits.

import { parseArgs } from "node:util";

import { connectDevice } from "../device.js";
import { OperationJournal } from "../journal/journal.js";
import type { Operation, Outcome } from "../link/protocol.js";

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    token: { type: "string" },
    journal: { type: "string" },
    hold: { type: "boolean", default: false },
  },
});
if (!values.url || !values.token || !values.journal) {
  process.stderr.write("usage: echo-client --url URL --token TOKEN --journal PATH [--hold]\n");
  process.exit(2);
}

const say = (event: Record<string, unknown>) => process.stdout.write(`${JSON.stringify(event)}\n`);

class SpokenJournal extends OperationJournal {
  override acknowledge(id: string): void {
    super.acknowledge(id);
    say({ event: "ack", id });
  }
}

const { link } = connectDevice({
  url: values.url,
  token: values.token,
  journal: new SpokenJournal(values.journal),
  onStatus: (status) => say({ event: "status", status }),
  onError: (error) => say({ event: "error", message: String(error) }),
  executor: {
    run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
      say({ event: "op", id: operation.id, kind: operation.kind });
      if (operation.kind === "which") {
        return Promise.resolve({ ok: `/usr/bin/${String(operation.args.name)}` });
      }
      if (!values.hold) return Promise.resolve({ ok: null });
      return new Promise((resolve) => {
        signal.addEventListener("abort", () => {
          say({ event: "cancel", id: operation.id });
          resolve({ error: { type: "cancelled", message: "stopped" } });
        });
      });
    },
  },
});
link.start();
process.on("SIGTERM", () => {
  void link.stop().then(() => process.exit(0));
});
