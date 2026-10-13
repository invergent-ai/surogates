// A child the app starts holds the descriptors it is meant to have and nothing else of the app's.
// The parent here is a bash that holds two files open without close-on-exec, as Chromium holds its
// own in the app's main process: a Node of its own marks all it inherits at its start, so no test
// can hold one open in Node itself. Through the real app, the E2E files list each child's. On macOS
// the child is started by the app's own surogate-spawn, which native/spawn/build.sh makes.

import { spawnSync } from "node:child_process";
import { once } from "node:events";
import { mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { cleanly, spawnClean } from "../src/clean-child.js";
import { importedOn } from "./platforms.js";

// *file* and *args*, run by a parent that holds descriptors 3, 7 and 8 open: a bash that reads no rc file, which with a
// socket for stdin, as spawnSync's pipes are on macOS, it would.
const held = (file: string, args: string[]) => spawnSync("/usr/bin/env", ["-i", "WORD=kept", "/bin/bash", "--norc", "--noprofile", "-c", 'exec 3</dev/null 7</dev/null 8</dev/null; exec "$@"', "bash", file, ...args], { encoding: "utf8" });
// A child that lists its own descriptors, by number: by its /proc entry on Linux, by /dev/fd on macOS.
const OPEN = process.platform === "linux" ? "/proc/$$/fd/$fd" : "/dev/fd/$fd";
const LISTS: [string, string[]] = ["/bin/bash", ["-c", `for fd in $(seq 0 40); do [ ! -e ${OPEN} ] || echo -n "$fd "; done`]];

describe("a child the app starts", () => {
  it("holds its three standard descriptors and nothing else, where one started as it is holds what its parent had open", () => {
    expect(held(...LISTS).stdout).toBe("0 1 2 3 7 8 ");
    expect(held(...cleanly(...LISTS)).stdout).toBe("0 1 2 ");
  });

  it("keeps as many descriptors as it is meant to have, a fourth for a channel among them", () => {
    expect(held(...cleanly(...LISTS, 3)).stdout).toBe("0 1 2 3 ");
    expect(held(...cleanly(...LISTS, 7)).stdout).toBe("0 1 2 3 7 ");
  });

  it("is the program itself: its arguments as they are, its exit and its process, and its environment to the name", () => {
    const itself = held("/bin/bash", ["-c", 'echo $$; exec "$@"', "bash", ...[cleanly("/bin/bash", ["-c", 'echo "$$ $0 $1 $2 $WORD"; exit 7', "a b", "c'd", "$(id)"])].flat(2)]);
    const [pid, said] = itself.stdout.trim().split("\n");
    expect([itself.status, said]).toEqual([7, `${pid} a b c'd $(id) kept`]);
    // What the parent named, and nothing a shell on the way would add (PWD, SHLVL).
    expect(spawnSync(...cleanly("/usr/bin/env", []), { encoding: "utf8", env: { WORD: "kept" } }).stdout).toBe("WORD=kept\n");
  });

  it("says so as a shell does, and ends 127, where the program is not there; and runs one by its whole path alone", () => {
    const gone = held(...cleanly("/nowhere/program", []));
    expect([gone.status, gone.stderr]).toEqual([127, "/nowhere/program: No such file or directory\n"]);
    expect(() => cleanly("pkexec", [])).toThrow("pkexec is not named by its whole path");
  });

  it("reads no shell's startup file, nor a BASH_ENV or an ENV, whatever the environment it is given names", () => {
    const home = realpathSync(mkdtempSync(join(tmpdir(), "clean-home-")));
    try {
      // Each says it was read, on stdout and on stderr: a shell that reads its rc files with a socket for stdin, as
      // spawnSync's pipes are on macOS, or a login shell, or one told of a file by BASH_ENV or ENV.
      for (const name of [".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshenv", ".zprofile", ".zshrc", ".zlogin", "env.sh"]) {
        writeFileSync(join(home, name), `echo ${name} was read; echo ${name} was read >&2\n`);
      }
      const env = { HOME: home, ZDOTDIR: home, BASH_ENV: join(home, "env.sh"), ENV: join(home, "env.sh"), WORD: "kept" };
      const ran = spawnSync(...cleanly("/bin/echo", ["only", "this"]), { encoding: "utf8", env });
      expect([ran.status, ran.stdout, ran.stderr]).toEqual([0, "only this\n", ""]);
      const gone = spawnSync(...cleanly("/nowhere/program", []), { encoding: "utf8", env });
      expect([gone.status, gone.stdout, gone.stderr]).toEqual([127, "", "/nowhere/program: No such file or directory\n"]);
    } finally {
      rmSync(home, { recursive: true, force: true });
    }
  });

  it("is the very process spawn gave", async () => {
    const child = spawnClean("/bin/bash", ["-c", "echo $$"], { env: {}, stdio: ["ignore", "pipe", "inherit"] });
    let said = "";
    child.stdout!.on("data", (chunk: Buffer) => {
      said += chunk.toString();
    });
    await once(child, "close");
    expect(Number(said.trim())).toBe(child.pid);
  });

  it("is started on macOS by the app's own surogate-spawn, beside its node, told the last descriptor to keep", async () => {
    const { cleanly: onMac } = await importedOn("darwin", () => import("../src/clean-child.js"));
    const spawn = fileURLToPath(new URL("../bin/surogate-spawn", import.meta.url));
    expect(onMac("/usr/bin/env", ["a b"], 3)).toEqual([spawn, ["3", "/usr/bin/env", "a b"]]);
    expect(onMac("/usr/bin/env", [])).toEqual([spawn, ["2", "/usr/bin/env"]]);
  });

  it("is refused, in words, on a system the app cannot start one cleanly on yet", async () => {
    const { cleanly: elsewhere } = await importedOn("win32", () => import("../src/clean-child.js"));
    expect(() => elsewhere("/usr/bin/env", [])).toThrow(new Error("Starting a program cleanly is not built for win32 yet"));
    expect(() => elsewhere("env", [])).toThrow("env is not named by its whole path");
  });
});

describe.runIf(process.platform === "darwin")("surogate-spawn, asked what it cannot do", () => {
  const spawn = fileURLToPath(new URL("../bin/surogate-spawn", import.meta.url));

  it("ends 126 and runs nothing on an ask it cannot read", () => {
    for (const args of [[], ["2"], ["x", "/usr/bin/true"], ["1", "/usr/bin/true"], ["65", "/usr/bin/true"], ["2", "true"]]) {
      expect(spawnSync(spawn, args, { encoding: "utf8" }).status, args.join(" ")).toBe(126);
    }
  });
});
