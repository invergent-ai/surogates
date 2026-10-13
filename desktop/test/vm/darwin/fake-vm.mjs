// A stand-in for surogate-vm (native/macos), for the macOS backend's tests on any OS: it speaks
// surogate-vm's line protocol (native/macos/Sources/SurogateVM/Protocol.swift), makes its three
// sockets in the runtime folder, and runs the guest agent's own control (dist/guest/control.js) on
// the control socket, over roots of its own that run nothing. It boots no VM. What it does besides
// is told by its environment, as a test's wrapper sets it (vm-darwin.test.ts):
//   FAKE_VM_BOOT=fail     the boot fails, saying why
//   FAKE_VM_SHARE=refuse  each share is refused; =silent, never answered
//   FAKE_VM_UNSHARE=silent each unshare is never answered
//   FAKE_VM_KILL=ignore   a kill line is not heeded
//   FAKE_VM_STOP=error    once started, the VM stops by itself for an error
//   FAKE_VM_SUPPORTED=0   check says this Mac cannot run it
//   FAKE_VM_UID=<uid>     its agent gives each root that uid, not 10000, the first a root has
//   FAKE_VM_POWEROFF=ignore its agent's power-off never ends the VM
//   FAKE_VM_LOG=<file>    each line read is written there
// Node, and no Apple framework: so it runs where the tests do, on Linux and on a Mac.

import { execFileSync } from "node:child_process";
import { appendFileSync, chmodSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { join } from "node:path";
import { createInterface } from "node:readline";

const { Control } = await import(new URL("../../../dist/guest/control.js", import.meta.url).href);

const env = process.env;
const say = (line) => process.stdout.write(`${JSON.stringify(line)}\n`);
const [command, run, ...rest] = process.argv.slice(2);

// A process's command line, as the OS shows it: /proc on Linux, ps(1) on macOS.
function argsOf(pid) {
  try {
    return existsSync(`/proc/${pid}/cmdline`) ? readFileSync(`/proc/${pid}/cmdline`, "utf8").split("\0") : execFileSync("/bin/ps", ["-o", "command=", "-p", String(pid)], { encoding: "utf8" }).trim().split(" ");
  } catch {
    return [];
  }
}

if (command === "check") {
  say(env.FAKE_VM_SUPPORTED === "0" ? { supported: false, reason: "the fake cannot" } : { supported: true });
  process.exit(0);
}

if (command === "sweep") {
  try {
    const pid = Number(readFileSync(join(run, "vm.pid"), "utf8").trim());
    const args = argsOf(pid);
    if (pid > 1 && args.includes("run") && args.includes(run)) process.kill(pid, "SIGKILL");
  } catch {
    // No pidfile, or its process gone.
  }
  rmSync(run, { recursive: true, force: true });
  mkdirSync(run, { recursive: true, mode: 0o700 });
  chmodSync(run, 0o700);
  process.exit(0);
}

if (command !== "run" || rest[0] !== "--data" || rest[2] !== "--image" || rest[4] !== "--bundle") {
  process.stderr.write("usage: fake-vm run <runtime folder> --data <data> --image <image folder> --bundle <app folder>\n");
  process.exit(2);
}

writeFileSync(join(run, "vm.pid"), `${process.pid}\n`, { mode: 0o600 });
const names = ["control.sock", "net.sock", "inbound.sock"];
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
    server.listen(join(run, name), () => {
      chmodSync(join(run, name), 0o600);
      if ((listening += 1) === names.length) say({ type: "listening" });
    });
  }
});

let state = "waiting";
let made = 0;
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
  const powerOff = env.FAKE_VM_POWEROFF === "ignore" ? () => new Promise(() => {}) : async () => stop("guest");
  const machine = { setClock: async () => {}, woke: () => {}, heard: () => {}, trust: async () => {}, powerOff };
  const agent = new Control((message) => control.write(`${JSON.stringify(message)}\n`), roots, machine);
  createInterface({ input: control }).on("line", (text) => agent.receive(text)).on("error", () => {});
  state = "running";
  say({ type: "started" });
  agent.hello();
  if (env.FAKE_VM_STOP === "error") setTimeout(() => stop("error", "the hypervisor failed"), 200);
}

let pending = null;
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
    pending = line;
    return boot();
  }
  if (line.type === "share") {
    if (state !== "running") return say({ type: "refused", id: line.id, why: "the guest is not running" });
    if (env.FAKE_VM_SHARE === "silent") return;
    if (env.FAKE_VM_SHARE === "refuse") return say({ type: "refused", id: line.id, why: "the fake refuses every folder" });
    made += 1;
    return say(pending.layout === "single" ? { type: "shared", id: line.id, tag: `s${made}` } : { type: "shared", id: line.id, tag: "surogate", name: line.name });
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
