// The acceptance VMs' probe of a command in the guest, run by an installed version's own Electron
// as its app's main. Its VM and its executor are the app's own (dist/shell/tools.js, which the
// app's main makes its own through): the VM's manager in a utility process, as the app's main
// starts it, and the guest's image downloaded from the base the app was installed from, as an
// installed app downloads it. It runs a command in a chat on a folder, and one in a project's
// thread on another, in the copy the guest's git makes of that folder, which is shared with the
// guest's root read-only. Then it prints what each came to, which program QEMU and each folder's
// daemon ran as, and what the guest's root found where a test's agent disk has it try that folder
// (acceptance.test.ts), and exits.
import { existsSync, mkdirSync, readdirSync, readFileSync, realpathSync, statSync, writeFileSync } from "node:fs";
import { homedir, userInfo } from "node:os";
import { join } from "node:path";

import { app, net, utilityProcess } from "electron";

import { BOOT_ID } from "./dist/binding/folder.js";
import { keyOf } from "./dist/history/place.js";
import { appTools, appVm } from "./dist/shell/tools.js";
import { ROOT_RECORD } from "./dist/shell/updates.js";
import { MANAGER, vmEnv, vmOptions } from "./dist/vm/client.js";
import { ImageDelivery, installBase, readManifest } from "./dist/vm/image.js";

const CHAT = "11111111-1111-4111-8111-111111111111";
const THREAD = "22222222-2222-4222-8222-222222222222";
const label = (pid) => readFileSync(`/proc/${pid}/attr/current`, "utf8").replace(/\0/g, "").trim();
// As it was run: a folder's daemon has its folder for its root, where its own program is not to be found.
const program = (pid) => readFileSync(`/proc/${pid}/cmdline`, "utf8").split("\0")[0];
// Each process under *pid*, with its program and label.
const tree = (pid) => readdirSync("/proc").filter((entry) => /^\d+$/.test(entry)).flatMap((entry) => {
  try {
    const parent = readFileSync(`/proc/${entry}/stat`, "utf8").split(") ")[1].split(" ")[1];
    return parent === String(pid) ? [{ program: program(entry), label: label(entry) }, ...tree(entry)] : [];
  } catch {
    return [];
  }
});
// The VM's manager in an Electron utility process, as the app's main starts it: what is sent before it has spawned waits.
const manager = () => {
  const child = utilityProcess.fork(MANAGER, [], { serviceName: "Surogate VM", stdio: "inherit" });
  const waiting = [];
  let spawned = false;
  let exited = false;
  child.once("spawn", () => {
    spawned = true;
    for (const message of waiting.splice(0)) child.postMessage(message);
  });
  child.once("exit", () => {
    exited = true;
  });
  return {
    send: (message) => {
      if (exited) return;
      if (spawned) child.postMessage(message);
      else waiting.push(message);
    },
    onMessage: (listener) => void child.on("message", listener),
    onExit: (listener) => (exited ? listener() : void child.once("exit", () => listener())),
    kill: () => void child.kill(),
  };
};
const folderOf = (name, files = {}) => {
  const folder = join(homedir(), name);
  mkdirSync(folder, { recursive: true });
  for (const [file, text] of Object.entries(files)) writeFileSync(join(folder, file), text);
  return realpathSync(folder);
};
const bound = (folder, history) => {
  const { dev, ino } = statSync(folder);
  return { folder, dev, ino, boot: BOOT_ID, ...(history === undefined ? {} : { history }) };
};

app.whenReady().then(async () => {
  const said = { main: label(process.pid), boot: readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim() };
  try {
    mkdirSync(join(homedir(), ".guest-probe-data"), { recursive: true });
    const data = realpathSync(join(homedir(), ".guest-probe-data"));
    const stop = new AbortController();
    const delivery = new ImageDelivery({
      manifest: readManifest(join(process.resourcesPath, "vm", "manifest.json")),
      base: () => installBase(ROOT_RECORD, true),
      images: join(data, "vm", "images"),
      fetch: (url, init) => net.fetch(url, { ...init, credentials: "omit", cache: "no-store" }),
      signal: stop.signal,
    });
    delivery.start();
    await delivery.wait(stop.signal);
    const { uid, gid, username } = userInfo();
    const options = vmOptions(data, { uid, gid, name: username, home: homedir() }, vmEnv(process.env, app.isPackaged), {
      image: delivery.folder, agentDisk: join(process.resourcesPath, "vm", "agent.img"),
    });
    const vm = appVm({ vm: options, spawn: manager });
    const chat = folderOf("chat");
    const project = folderOf("project", { "Report.txt": "the real report\n" });
    const bindings = new Map([[CHAT, bound(chat)], [THREAD, bound(project, THREAD)]]);
    const tools = appTools({
      bindingOf: (root) => bindings.get(root), dataDir: data, cacheDir: join(homedir(), ".cache", "surogate"), env: { HOME: homedir(), LANG: "C.UTF-8" }, vm, user: "probe",
    });
    let ordinal = 0;
    const run = (root, command) => tools.run({
      id: `run-${(ordinal += 1)}`, sessionId: root, callingSessionId: root, invocationId: "call", ordinal, kind: "run", args: { command, workdir: null, timeout: 60 }, digest: "d",
    }, new AbortController().signal);
    said.chat = await run(CHAT, "cat /proc/sys/kernel/random/boot_id && echo made in the guest > made.txt");
    said.thread = await run(THREAD, "cat Report.txt && echo \"the thread's\" > Report.txt && cat Report.txt");
    // While the thread's copy and its folder's place are in the guest: QEMU, and each folder's daemon with what it runs.
    const qemu = readFileSync(join(options.run, "qemu.pid"), "utf8").trim();
    said.qemu = { program: program(qemu), label: label(qemu), memlock: /^Max locked memory\s+(\S+)\s+(\S+)/m.exec(readFileSync(`/proc/${qemu}/limits`, "utf8"))?.slice(1) ?? null };
    said.daemons = readdirSync(options.run).filter((name) => /^vfs-\d+\.pid$/.test(name)).map((name) => {
      const pid = readFileSync(join(options.run, name), "utf8").trim();
      return { program: program(pid), label: label(pid), under: tree(pid) };
    });
    const found = join(data, "history", keyOf(project), "found");
    for (const end = Date.now() + 30_000; !existsSync(found) && Date.now() < end;) await new Promise((resolve) => setTimeout(resolve, 100));
    said.found = existsSync(found) ? readFileSync(found, "utf8") : null;
    await tools.stop();
    await vm.stop();
  } catch (error) {
    said.error = error instanceof Error ? error.stack : String(error);
  }
  console.log(JSON.stringify(said));
  app.exit(0);
});
