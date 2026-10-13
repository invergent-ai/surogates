// The guest agent, under tini (vm/init). It speaks the control protocol
// (control.ts) on the ai.surogate.control port, runs each root's commands in that
// root's own namespaces (root.ts), carries their connections to the host proxy
// on the ai.surogate.net port (network.ts), and the browser's connections to a
// root's own servers from the ai.surogate.inbound port (inbound.ts), each on the
// transport the kernel's command line names (port.ts). Anything it does not catch
// ends it, and with it the guest.

import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";

import { Control } from "./control.js";
import { Inbound } from "./inbound.js";
import { Network } from "./network.js";
import { Places } from "./places.js";
import { openPortOn, transportOf } from "./port.js";
import { type FromAgent, INBOUND_PORT } from "./protocol.js";
import {
  admit, BOUNDS, CGROUPS, contain, enter, flushRoot, killRoot, powerOff, Roots, SCOPE, scopeOf, SESSIONS, setClock, uidOf, unmountShare,
} from "./root.js";
import { trust } from "./trust.js";

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
// 15 s for the hello, or 120 s emulated.
const RULE = "/run/surogate/rule";
const RULE_MS = BOUNDS.ruleMs;
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
// The one uid the rule judges besides the roots' own, which vm/init set with it: every root runs as it on
// Apple's share, which maps no owner (root.ts, sessionUid). None on Linux's.
const scope = scopeOf(readFileSync(SCOPE, "utf8"));

const transport = transportOf(readFileSync("/proc/cmdline", "utf8"));
const port = await openPortOn(transport, "ai.surogate.control");
// A control port that ends is a host that has gone, as a vsock one's does: the guest goes with it.
port.once("end", () => die("surogate: the host's control port closed"));
const network = new Network(await openPortOn(transport, "ai.surogate.net"));
const say = (message: FromAgent) => void port.write(`${JSON.stringify(message)}\n`);
const roots = new Roots({
  start: (root, place, share, user) => enter(root, place, share, user, scope), uid: (root) => uidOf(root, SESSIONS, scope),
  kill: killRoot, contain, admit, cgroups: CGROUPS, unmount: unmountShare, flush: flushRoot,
  tunnels: (root, uid) => network.listen(root, uid),
  arrivals: (root, id) => network.arrival(root, id),
  lost: (root) => say({ type: "lost", root }),
  handles: (root, handles, live) => say({ type: "handles", root, handles, live }),
  // Past two of the host's pings unheard, it is asleep or gone (vm/manager.ts, PING_MS).
  hostSilenceMs: 25_000,
});
new Inbound(await openPortOn(transport, INBOUND_PORT), (root, to, first) => roots.reach(root, to, first));
const places = new Places();
// A store that could not be made leaves each root the image's, with the public roots alone: the console says why.
const trusted = (certificates: string[]) => trust(certificates).then(() => {}, (error: unknown) => {
  console.error(`surogate: this computer's sandbox does not trust your company's certificate authority: ${error instanceof Error ? error.message : String(error)}`);
});
const control = new Control(say, roots, { setClock, powerOff, woke: (ms) => roots.woke(ms), heard: () => roots.heard(), trust: trusted }, places);
createInterface({ input: port, crlfDelay: Infinity }).on("line", (line) => control.receive(line));
control.hello();
