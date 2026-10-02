// A device for the server's cross-check: it connects like the app, answers
// `which`, and holds anything else until cancelled when --hold is given. With
// --folder it answers every operation through the real tool hosts instead, with
// that folder bound to every session. One JSON line per event on stdout; a link
// that stops itself says why as an "error" event.
// The journal's file stays locked while this runs, so each op_ack the server sends is
// said aloud as an "ack" event (one per frame, a repeat too): the cross-check reads
// the file only after it quits.

import { dirname, join } from "node:path";
import { parseArgs } from "node:util";

import { connectDevice } from "../device.js";
import { ToolHosts } from "../hosts/tool-hosts.js";
import { OperationJournal } from "../journal/journal.js";
import type { Operation, Outcome } from "../link/protocol.js";

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    token: { type: "string" },
    journal: { type: "string" },
    hold: { type: "boolean", default: false },
    folder: { type: "string" },
  },
});
if (!values.url || !values.token || !values.journal) {
  process.stderr.write("usage: echo-client --url URL --token TOKEN --journal PATH [--hold] [--folder PATH]\n");
  process.exit(2);
}

const say = (event: Record<string, unknown>) => process.stdout.write(`${JSON.stringify(event)}\n`);

class SpokenJournal extends OperationJournal {
  override acknowledge(id: string): void {
    super.acknowledge(id);
    say({ event: "ack", id });
  }
}

// With --folder, every session works on that folder through the real tool hosts.
const folder = values.folder;
const hosts = folder
  ? new ToolHosts({
    bindingOf: () => ({ folder }),
    dataDir: join(dirname(values.journal), "data"),
    env: { HOME: process.env.HOME ?? "", LANG: process.env.LANG ?? "C.UTF-8", PATH: process.env.PATH ?? "/usr/bin:/bin" },
  })
  : null;

const { link } = connectDevice({
  url: values.url,
  token: values.token,
  journal: new SpokenJournal(values.journal),
  onStatus: (status) => say({ event: "status", status }),
  onError: (error) => say({ event: "error", message: String(error) }),
  executor: hosts ?? {
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
  void link.stop().then(() => hosts?.stop()).then(() => process.exit(0));
});
