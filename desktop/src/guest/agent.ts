// The guest agent, under tini (vm/init). It speaks the control protocol
// (control.ts) on the ai.surogate.control port, and runs each root's commands in
// that root's own namespaces (root.ts). Anything it does not catch ends it, and
// with it the guest.

import { createInterface } from "node:readline";

import { Control } from "./control.js";
import { findPort, openPort } from "./port.js";
import type { FromAgent } from "./protocol.js";
import { CGROUPS, contain, enter, killRoot, Roots, uidOf } from "./root.js";

const port = await openPort(await findPort("ai.surogate.control"));
const say = (message: FromAgent) => void port.write(`${JSON.stringify(message)}\n`);
const roots = new Roots({
  start: enter, uid: uidOf, kill: killRoot, contain, cgroups: CGROUPS,
  lost: (root) => say({ type: "lost", root }),
  handles: (root, handles, live) => say({ type: "handles", root, handles, live }),
});
const control = new Control(say, roots);
createInterface({ input: port.input, crlfDelay: Infinity }).on("line", (line) => control.receive(line));
control.hello();
