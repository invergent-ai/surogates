// The guest agent, under tini (vm/init). It speaks the control protocol
// (control.ts) on the ai.surogate.control port, runs each root's commands in that
// root's own namespaces (root.ts), and carries their connections to the host proxy
// on the ai.surogate.net port (network.ts). Anything it does not catch ends it, and
// with it the guest.

import { createInterface } from "node:readline";

import { Control } from "./control.js";
import { Network } from "./network.js";
import { findPort, openPort } from "./port.js";
import type { FromAgent } from "./protocol.js";
import { CGROUPS, contain, enter, killRoot, powerOff, Roots, uidOf, unmountShare } from "./root.js";

const port = await openPort(await findPort("ai.surogate.control"));
const network = new Network(await openPort(await findPort("ai.surogate.net")));
const say = (message: FromAgent) => void port.write(`${JSON.stringify(message)}\n`);
const roots = new Roots({
  start: enter, uid: uidOf, kill: killRoot, contain, cgroups: CGROUPS, unmount: unmountShare,
  tunnels: (root, uid) => network.listen(root, uid),
  lost: (root) => say({ type: "lost", root }),
  handles: (root, handles, live) => say({ type: "handles", root, handles, live }),
});
const control = new Control(say, roots, { powerOff });
createInterface({ input: port, crlfDelay: Infinity }).on("line", (line) => control.receive(line));
control.hello();
