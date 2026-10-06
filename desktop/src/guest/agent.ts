// The guest agent, under tini (vm/init). It speaks the control protocol
// (control.ts) on the ai.surogate.control port, and runs each root's commands in
// that root's own namespaces (root.ts). Anything it does not catch ends it, and
// with it the guest.

import { createInterface } from "node:readline";

import { Control } from "./control.js";
import { findPort, openPort } from "./port.js";
import { enter, Roots, uidOf } from "./root.js";

const port = await openPort(await findPort("ai.surogate.control"));
const control = new Control((message) => void port.write(`${JSON.stringify(message)}\n`), new Roots({ start: enter, uid: uidOf }));
createInterface({ input: port.input, crlfDelay: Infinity }).on("line", (line) => control.receive(line));
control.hello();
