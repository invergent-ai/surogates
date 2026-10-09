// The acceptance VMs' probe of an update, run by an installed version's own Electron as its
// app's main. With --bind it records a chat's binding in a device's journal, as the binder does.
// With --check it runs the app's own Updates as its main makes them for an installed app
// (installedUpdates, and a log): on Electron's net.fetch, against the release keys of the helper
// pkexec runs. With --update it also installs what the check found, through pkexec and that
// helper, and once that is installed it starts the app again from its launcher, as the restart
// does. With --changed too, one byte of the downloaded tarball is changed between the check and
// the install, as a program of the user's own could change it: the app's look at the click takes
// the file, and the helper's reading of it does not. Every run writes its version, that binding
// as its journal holds it, and what its updates came to, with their line and their log, to
// ~/probe-<version>.json, and exits.
import { closeSync, mkdirSync, openSync, readFileSync, readSync, statSync, writeFileSync, writeSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

import { app, net } from "electron";

import { BOOT_ID } from "./dist/binding/folder.js";
import { OperationJournal } from "./dist/journal/journal.js";
import { installedUpdates, updateLine, Updates } from "./dist/shell/updates.js";

const ROOT = "11111111-1111-4111-8111-111111111111";
const { version } = JSON.parse(readFileSync(join(import.meta.dirname, "package.json"), "utf8"));
const journal = join(homedir(), ".local", "share", "surogate", "devices", "probe", "journal.sqlite");
const asked = (flag) => process.argv.includes(flag);

app.whenReady().then(async () => {
  const said = { version };
  try {
    mkdirSync(dirname(journal), { recursive: true });
    const opened = new OperationJournal(journal);
    if (asked("--bind")) {
      const folder = join(homedir(), "bound");
      mkdirSync(folder, { recursive: true });
      const { dev, ino } = statSync(folder);
      opened.bindings.add({ root: ROOT, nonce: "probe", folder, dev, ino, boot: BOOT_ID, mode: "ask", boundAt: Date.now() });
    }
    said.binding = opened.bindings.get(ROOT) ?? null;
    opened.close();
    if (asked("--check") || asked("--update")) {
      // The app's log, as its main writes one: why a check took nothing, and all that a helper said.
      const logged = [];
      const updates = new Updates({
        ...installedUpdates(version, join(homedir(), ".cache", "surogate", "updates"),
          (url, init) => net.fetch(url, { ...init, credentials: "omit", cache: "no-store" }), new AbortController().signal),
        log: (words) => logged.push(String(words)),
      });
      await updates.check().catch((error) => logged.push(String(error)));
      said.found = updates.state.state;
      if (asked("--update")) {
        if (asked("--changed") && updates.state.state === "available") {
          const file = openSync(updates.state.files.tarball, "r+");
          const byte = Buffer.alloc(1);
          readSync(file, byte, 0, 1, 1000);
          byte[0] ^= 0xff;
          writeSync(file, byte, 0, 1, 1000);
          closeSync(file);
        }
        await updates.install();
        said.update = updates.state;
        if (updates.state.state === "installed") app.relaunch({ execPath: "/usr/local/bin/surogate", args: ["--password-store=basic"] });
      }
      said.line = updateLine(updates.state);
      said.logged = logged;
    }
  } catch (error) {
    said.error = String(error);
  }
  writeFileSync(join(homedir(), `probe-${version}.json`), JSON.stringify(said));
  app.exit(0);
});
