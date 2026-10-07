// A device for the server's cross-check: it connects like the app, answers
// `which`, and holds anything else until cancelled when --hold is given. With
// --folder it answers every operation as the app does instead, through the
// VmExecutor: the file kinds in the chat's file host, the process kinds in the VM,
// with that folder bound to every session. With --confirm it binds chats as the app
// does: before it connects it confirms that folder, as a user accepting the sheet
// would, and says the sheet ("sheet") and the folder and nonce the page would send
// ("prepared"); then it answers a chat's bind operation against that confirmation,
// and runs the chat's other operations through the VmExecutor. With --ask
// WORD as well, those chats ask every time: each approval is said ("approval"),
// and denied when what it asks about names WORD, allowed otherwise. A command's
// connection to a host off the package list, other than this computer's own or one
// that cannot be looked up, asks in either mode and is said as an "approval" event;
// without --ask it is denied. One JSON line per event on stdout; a link that stops
// itself says why as an "error" event.
// The journal's file stays locked while this runs, so each op_ack the server sends is
// said aloud as an "ack" event (one per frame, a repeat too): the cross-check reads
// the file only after it quits.

import { statSync } from "node:fs";
import { userInfo } from "node:os";
import { dirname, join } from "node:path";
import { parseArgs } from "node:util";

import { Binder } from "../binding/binder.js";
import { BOOT_ID } from "../binding/folder.js";
import { connectDevice } from "../device.js";
import { appEnvironment } from "../hosts/environment.js";
import type { NetworkApprovals } from "../hosts/tool-hosts.js";
import { OperationJournal } from "../journal/journal.js";
import type { Operation, Outcome } from "../link/protocol.js";
import { VmClient, vmOptions } from "../vm/client.js";
import { VmExecutor } from "../vm/executor.js";

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    token: { type: "string" },
    journal: { type: "string" },
    hold: { type: "boolean", default: false },
    folder: { type: "string" },
    confirm: { type: "string" },
    ask: { type: "string" },
  },
});
// --folder binds every root by itself, so it cannot stand beside a confirmed folder; only a confirmed one asks.
const usage = (values.folder && values.confirm) || (values.ask !== undefined && !values.confirm);
if (!values.url || !values.token || !values.journal || usage) {
  process.stderr.write(
    "usage: echo-client --url URL --token TOKEN --journal PATH [--hold] [--folder PATH | --confirm PATH [--ask WORD]]\n",
  );
  process.exit(2);
}

const say = (event: Record<string, unknown>) => process.stdout.write(`${JSON.stringify(event)}\n`);
const sayError = (error: unknown) => say({ event: "error", message: String(error) });

class SpokenJournal extends OperationJournal {
  override acknowledge(id: string): void {
    super.acknowledge(id);
    say({ event: "ack", id });
  }
}

const journal = new SpokenJournal(values.journal);
const dataDir = join(dirname(values.journal), "data");
const env: Record<string, string> = values.folder || values.confirm ? await appEnvironment() : {};
// With --folder, every session works on that folder, as if each had been bound to it there.
let everyRoot: { folder: string; dev: number; ino: number; boot: string } | undefined;
if (values.folder) {
  const { dev, ino } = statSync(values.folder);
  everyRoot = { folder: values.folder, dev, ino, boot: BOOT_ID };
}
// The binder's approvals decide the network as they decide operations; a host asks
// them only once the binder exists. With --folder there is no binder, and every
// destination off the package list is refused.
const network: NetworkApprovals = {
  granted: (root) => binder?.approvals.granted(root) ?? [],
  askNetwork: (root, asked, signal) => binder?.approvals.askNetwork(root, asked, signal) ?? Promise.resolve("deny"),
};
const bindingOf = (root: string) => everyRoot ?? journal.bindings.get(root);
const host = userInfo();
const vm = values.folder || values.confirm ? new VmClient({ vm: vmOptions(dataDir, { uid: host.uid, gid: host.gid, name: host.username, home: env.HOME ?? host.homedir }) }) : null;
const hosts = vm ? new VmExecutor({ bindingOf, dataDir, env, network, vm }) : null;
const { confirm, ask } = values;
let refusal: string | null = null;
const binder = confirm && hosts
  ? new Binder({
    bindings: journal.bindings,
    prompts: {
      pickFolder: () => Promise.resolve(confirm),
      confirmFolder: (sheet) => {
        say({ event: "sheet", ...sheet });
        refusal = sheet.refusal;
        return Promise.resolve(refusal === null ? { mode: ask === undefined ? sheet.mode : "ask" } : null);
      },
    },
    guards: hosts.guards(),
    agent: "the cross-check",
    hosts,
    approvalPrompts: {
      approve: (request) => {
        say({ event: "approval", ...request });
        const named = request.kind === "command" ? request.command
          : request.kind === "change" ? request.path
          : request.kind === "input" ? request.data
          : request.host;
        // Without --ask only a network prompt comes, and is denied; any other is a fault, and fails loudly.
        return Promise.resolve(ask === undefined || named.includes(ask) ? "deny" : "allow");
      },
      confirmFreeMode: () => Promise.resolve(false),
    },
    onError: sayError,
  })
  : null;
if (binder) {
  const prepared = await binder.prepareFolder("pick", "cross-check", new AbortController().signal);
  if (!prepared) {
    say({ event: "error", message: `the folder was refused: ${refusal ?? "the sheet was not accepted"}` });
    process.exit(2);
  }
  say({ event: "prepared", folder: prepared.folder, nonce: prepared.nonce });
}

const { link } = connectDevice({
  url: values.url,
  token: values.token,
  journal,
  onStatus: (status) => say({ event: "status", status }),
  onError: sayError,
  executor: binder ?? hosts ?? {
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
  void link.stop().then(() => hosts?.stop()).then(() => vm?.stop()).then(() => process.exit(0));
});
