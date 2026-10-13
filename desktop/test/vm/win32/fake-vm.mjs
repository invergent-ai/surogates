// A stand-in for surogate-vm.exe (native/windows/vm), for the Windows backend's tests on any OS: it speaks the VM
// helper's line protocol (src/vm/vm-helper.ts) on its stdin and stdout, serves the three ports the backend reaches,
// and runs the guest agent's own control (dist/guest/control.js) on the control port, over roots of its own that run
// nothing. It asks no service and boots no VM. Where the backend names the ports as pipes (on Windows) it serves pipes;
// elsewhere it serves Unix sockets, so the backend's driver is exercised on Linux too. What it does besides is told by
// its environment, as a test's wrapper sets it (vm-win32.test.ts):
//   FAKE_VM_BOOT=fail      the boot fails, saying why
//   FAKE_VM_SHARE=refuse   each share is refused; =silent, never answered
//   FAKE_VM_UNSHARE=silent each unshare is never answered
//   FAKE_VM_KILL=ignore    a kill line is not heeded
//   FAKE_VM_STOP=error     once started, the VM stops by itself for an error
//   FAKE_VM_UID=<uid>      its agent gives each root that uid, not the kernel command line's surogate.uid= (none here)
//   FAKE_VM_LOG=<file>     each line read is written there
// Node, and no Windows API: so it runs where the tests do, on Linux and on the Windows runner.

import { appendFileSync } from "node:fs";
import { createServer } from "node:net";
import { createInterface } from "node:readline";

const { Control } = await import(new URL("../../../dist/guest/control.js", import.meta.url).href);
// The backend names the three ports (dist/vm/win32.js, addressesWin32), the same on this OS as the backend uses them.
const { addressesWin32 } = await import(new URL("../../../dist/vm/win32.js", import.meta.url).href);

const env = process.env;
const say = (line) => process.stdout.write(`${JSON.stringify(line)}\n`);
const [command, run] = process.argv.slice(2);

if (command === "check") {
  const answer = env.FAKE_VM_CHECK ? JSON.parse(env.FAKE_VM_CHECK) : { virtualMachinePlatform: true, service: "present", hostCompute: "ok" };
  process.stdout.write(`${JSON.stringify(answer)}\n`);
  process.exit(0);
}

if (command !== "run" || !run) {
  process.stderr.write("usage: fake-vm run <runtime folder> --data <data> --control <pipe> --net <pipe> --inbound <pipe>\n");
  process.exit(2);
}

const { control: controlName, net: netName, inbound: inboundName } = addressesWin32(run);
const names = [controlName, netName, inboundName];
const connected = await new Promise((resolve) => {
  const sockets = [];
  let listening = 0;
  for (const [n, name] of names.entries()) {
    const server = createServer((socket) => {
      // The manager's end may go first, as at its kill: the port's end, never the VM's.
      socket.on("error", () => {});
      server.close();
      sockets[n] = socket;
      if (sockets.filter(Boolean).length === names.length) resolve(sockets);
    });
    server.listen(name, () => {
      // "listening" once all three ports are listening: the backend connects only then.
      if ((listening += 1) === names.length) say({ type: "listening" });
    });
  }
});

let state = "waiting";
const stop = (reason, why) => {
  say(why === undefined ? { type: "stopped", reason } : { type: "stopped", reason, why });
  process.exit(0);
};

function boot() {
  if (env.FAKE_VM_BOOT === "fail") {
    say({ type: "failed", why: "the fake was told to fail" });
    process.exit(1);
  }
  const uid = Number(env.FAKE_VM_UID ?? 10_000);
  const [control] = connected;
  const roots = {
    uid: () => uid,
    setup: async () => {},
    teardown: async () => {},
    perform: async (root, kind, args) => ({ ok: { output: `${kind} in ${root}: ${args.command ?? ""}`, returncode: 0, timed_out: false } }),
  };
  const machine = { setClock: async () => {}, woke: () => {}, heard: () => {}, trust: async () => {}, powerOff: async () => stop("guest") };
  const agent = new Control((message) => control.write(`${JSON.stringify(message)}\n`), roots, machine);
  createInterface({ input: control }).on("line", (text) => agent.receive(text)).on("error", () => {});
  state = "running";
  say({ type: "started" });
  agent.hello();
  if (env.FAKE_VM_STOP === "error") setTimeout(() => stop("error", "the hypervisor failed"), 200);
}

createInterface({ input: process.stdin }).on("line", (text) => {
  if (env.FAKE_VM_LOG) appendFileSync(env.FAKE_VM_LOG, `${text}\n`);
  let line;
  try {
    line = JSON.parse(text);
  } catch {
    return say({ type: "refused", why: "a line that is not a JSON object of a type, its fields each of theirs" });
  }
  if (line.type === "boot") {
    if (state !== "waiting") return say({ type: "refused", why: "a boot after the first" });
    return boot();
  }
  if (line.type === "share") {
    if (state !== "running") return say({ type: "refused", id: line.id, why: "the guest is not running" });
    if (env.FAKE_VM_SHARE === "silent") return;
    if (env.FAKE_VM_SHARE === "refuse") return say({ type: "refused", id: line.id, why: "the fake refuses every folder" });
    return say({ type: "shared", id: line.id, tag: line.name });
  }
  if (line.type === "unshare") {
    if (env.FAKE_VM_UNSHARE === "silent") return;
    return say({ type: "unshared", id: line.id });
  }
  if (line.type === "kill") {
    if (env.FAKE_VM_KILL === "ignore") return;
    return stop("killed");
  }
  say({ type: "refused", id: line.id, why: "a line of no type surogate-vm takes" });
}).on("close", () => {
  if (env.FAKE_VM_KILL !== "ignore") stop("killed");
});
