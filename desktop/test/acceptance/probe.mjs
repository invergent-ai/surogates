// The acceptance VMs' probe of a file tool's chain, run by an installed version's own Electron
// as its app's main: Electron under the app's AppArmor profile, the file host on the app's
// bin/node, srt, and the bwrap the app itself gives it. Its executor is the app's own
// (dist/shell/tools.js, which the app's main makes its own through): the probe names no bwrap. It
// writes one file in a folder of the home through the real file host, prints what each process
// ran as, and exits.
//
// With PROBE_ON_PATH=1 it is the control: the same chain with no bwrap named, as the app would be
// without its version's copy, so that the file host finds the system's on its PATH.
import { mkdtempSync, readdirSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import { app } from "electron";

import { BOOT_ID } from "./dist/binding/folder.js";
import { appTools } from "./dist/shell/tools.js";
import { VmExecutor } from "./dist/vm/executor.js";

const ROOT = "11111111-1111-4111-8111-111111111111";
const label = (pid) => readFileSync(`/proc/${pid}/attr/current`, "utf8").replace(/\0/g, "").trim();
// Each process under *pid*, with its program and label.
const tree = (pid) => readdirSync("/proc").filter((entry) => /^\d+$/.test(entry)).flatMap((entry) => {
  try {
    const parent = readFileSync(`/proc/${entry}/stat`, "utf8").split(") ")[1].split(" ")[1];
    return parent === String(pid) ? [{ pid: Number(entry), cmd: readFileSync(`/proc/${entry}/cmdline`, "utf8").split("\0")[0], label: label(entry) }, ...tree(entry)] : [];
  } catch {
    return [];
  }
});

app.whenReady().then(async () => {
  const folder = mkdtempSync(join(homedir(), "probe-"));
  const { dev, ino } = statSync(folder);
  const options = {
    bindingOf: () => ({ folder, dev, ino, boot: BOOT_ID }), dataDir: join(homedir(), ".probe-data"), cacheDir: join(homedir(), ".cache", "surogate"),
    env: { HOME: homedir(), LANG: "C.UTF-8" },
    // The probe runs no command, and its folder is no thread's: nothing of it reaches the VM.
    vm: {
      perform: () => Promise.reject(new Error("the probe runs no command")), teardown: () => Promise.resolve(), onProcesses: () => () => {}, onAsk: () => () => {},
      history: () => Promise.reject(new Error("the probe has no thread")), unplace: () => Promise.resolve(false),
    },
    user: "probe",
  };
  const tools = process.env.PROBE_ON_PATH === "1" ? new VmExecutor(options) : appTools(options);
  const key = join(folder, "probe.txt");
  const seen = [];
  const watch = setInterval(() => seen.push(...tree(process.pid)), 50);
  const outcome = await tools.run({
    id: "probe", sessionId: ROOT, callingSessionId: ROOT, invocationId: "call", ordinal: 1, kind: "write", args: { key, data: Buffer.from("written in srt\n").toString("base64") }, digest: "d",
  }, new AbortController().signal);
  clearInterval(watch);
  let written = null;
  try {
    written = readFileSync(key, "utf8");
  } catch {}
  const programs = Object.values(Object.fromEntries(seen.map((process) => [`${process.cmd} ${process.label}`, { cmd: process.cmd, label: process.label }])));
  console.log(JSON.stringify({ main: label(process.pid), outcome, written, programs }));
  await tools.stop();
  app.exit(0);
});
