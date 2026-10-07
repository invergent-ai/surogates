// The guest agent, under tini (vm/init). It speaks the control protocol
// (control.ts) on the ai.surogate.control port, runs each root's commands in that
// root's own namespaces (root.ts), and carries their connections to the host proxy
// on the ai.surogate.net port (network.ts). Anything it does not catch ends it, and
// with it the guest.

import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";

import { Control } from "./control.js";
import { Network } from "./network.js";
import { findPort, openPort } from "./port.js";
import type { FromAgent } from "./protocol.js";
import { CGROUPS, contain, enter, flushRoot, killRoot, powerOff, Roots, setClock, uidOf, unmountShare } from "./root.js";

// Anything the agent does not catch ends it at once, and with it tini and the guest
// (vm/init). An exit would wait for each read of its ports in flight, which never returns,
// and the guest would hang until the host's keepalive gave up on it, 30 s later.
function die(said: unknown): never {
  console.error(said);
  process.kill(process.pid, "SIGKILL");
  throw new Error("unreachable");
}
process.on("uncaughtException", die);

// The guest-kernel rule's outcome, which vm/init writes once it has loaded beside the boot:
// "attached: …" or "failed: …". A load that never ends is a failure too, inside the host's
// 15 s for the hello.
const RULE = "/run/surogate/rule";
const RULE_MS = 12_000;
async function rule(): Promise<string> {
  for (const deadline = performance.now() + RULE_MS; ;) {
    try {
      return readFileSync(RULE, "utf8").trim();
    } catch {
      // Not written yet.
    }
    if (performance.now() > deadline) return "failed: the protected-names rule did not finish loading";
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}
// No hello, and so no command, before the rule is attached; one that did not attach fails the boot.
const loaded = await rule();
if (!loaded.startsWith("attached: ")) die(`surogate: ${loaded.replace(/^failed: /, "")}`);
console.log(`surogate: ${loaded.slice("attached: ".length)}`);

const port = await openPort(await findPort("ai.surogate.control"));
const network = new Network(await openPort(await findPort("ai.surogate.net")));
const say = (message: FromAgent) => void port.write(`${JSON.stringify(message)}\n`);
const roots = new Roots({
  start: enter, uid: uidOf, kill: killRoot, contain, cgroups: CGROUPS, unmount: unmountShare, flush: flushRoot,
  tunnels: (root, uid) => network.listen(root, uid),
  lost: (root) => say({ type: "lost", root }),
  handles: (root, handles, live) => say({ type: "handles", root, handles, live }),
  // Past two of the host's pings unheard, it is asleep or gone (vm/manager.ts, PING_MS).
  hostSilenceMs: 25_000,
});
const control = new Control(say, roots, { setClock, powerOff, woke: (ms) => roots.woke(ms), heard: () => roots.heard() });
createInterface({ input: port, crlfDelay: Infinity }).on("line", (line) => control.receive(line));
control.hello();
