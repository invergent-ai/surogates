// What an installed app runs as root at its user's click: pkexec's whole command, the three
// paths and one PATH, started as a program and by no shell. Nothing is started here: spawn is the
// test's own, which writes down what it was asked for and answers as a helper would.

import { EventEmitter } from "node:events";

import { describe, expect, it, vi } from "vitest";

import { installedUpdates } from "../src/shell/updates.js";

const asked = vi.hoisted(() => ({
  spawned: [] as unknown[][],
  // How the next program started ends: its exit code, its signal, and what it says first.
  ends: { code: 0 as number | null, signal: null as string | null, says: "" },
}));

vi.mock("node:child_process", async (original) => {
  const real = await original<typeof import("node:child_process")>();
  return {
    ...real,
    spawn: (...args: unknown[]) => {
      asked.spawned.push(args);
      const child = Object.assign(new EventEmitter(), { stderr: new EventEmitter() });
      setImmediate(() => {
        if (asked.ends.says) child.stderr.emit("data", Buffer.from(asked.ends.says));
        child.emit("close", asked.ends.code, asked.ends.signal);
      });
      return child;
    },
  };
});

const { signal } = new AbortController();
const installed = () => installedUpdates("1.2.3", "/home/user/.cache/surogate/updates", fetch, signal);

describe("an installed app's root helper", () => {
  it("is pkexec, by its whole path and with no agent of its own, on the helper the install script's action names, then --apply and the three paths, each an argument as it is", async () => {
    // A cache home as a user may name one: no word of it is a shell's to read.
    const folder = "/home/user/my cache; $(touch /tmp/x) `id` 'q' \"d\"\n--help/surogate/updates/1.2.4";
    const files = { manifest: `${folder}/manifest.json`, signature: `${folder}/manifest.json.sig`, tarball: `${folder}/release.tar.gz` };
    asked.ends = { code: 0, signal: null, says: "" };
    expect(await installed().apply(files)).toEqual({ code: 0, said: "" });
    expect(asked.spawned).toEqual([[
      "/usr/bin/pkexec",
      ["--disable-internal-agent", "/opt/surogate/bin/surogate-apply-update", "--apply", files.manifest, files.signature, files.tarball],
      // No shell, nothing on its input or from its output but what it says of a failure, and of the app's environment a PATH alone.
      { stdio: ["ignore", "ignore", "pipe"], env: { PATH: "/usr/bin:/bin" } },
    ]]);
  });

  it("answers what pkexec answers: its exit code, and the last 4000 characters of what was said", async () => {
    const files = { manifest: "/c/m.json", signature: "/c/m.json.sig", tarball: "/c/r.tar.gz" };
    asked.ends = { code: 127, signal: null, says: "Error executing command as another user: Not authorized\n" };
    expect(await installed().apply(files)).toEqual({ code: 127, said: "Error executing command as another user: Not authorized" });
    // A helper that says more than anyone would read: the end of it is kept, where its last line is.
    asked.ends = { code: 1, signal: null, says: `${"x".repeat(10_000)}\nSurogate Desktop: the release's archive could not be unpacked\n` };
    const { code, said } = await installed().apply(files);
    expect([code, said.length, said.split("\n").at(-1)]).toEqual([1, 3999, "Surogate Desktop: the release's archive could not be unpacked"]);
    asked.ends = { code: null, signal: "SIGKILL", says: "" };
    expect(await installed().apply(files)).toEqual({ code: null, said: "its helper was stopped by SIGKILL" });
  });
});
