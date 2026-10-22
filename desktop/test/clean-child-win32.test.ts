// A child the app starts on Windows, through surogate-helper.exe's spawn (src/clean-child.ts): its channel to its
// parent kept, as a file host's is; a program that is not there said as a shell says it; and the program taken along
// when the helper is killed, as a file host is stopped. On Windows alone, with the helper built (npm run build:win32);
// that the program holds no other handle of its parent's, the helper's own tests show (native/windows/helper).

import { once } from "node:events";

import { describe, expect, it } from "vitest";

import { spawnClean } from "../src/clean-child.js";

const ENV = { PATH: process.env.PATH ?? "" };

// Whether the process *pid* is there.
const alive = (pid: number): boolean => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};

describe.runIf(process.platform === "win32")("a child the app starts on Windows", () => {
  it("talks with the app over its channel, a fourth descriptor, as a file host does", async () => {
    const child = spawnClean(process.execPath, ["-e", "process.on('message', (said) => process.send({ heard: said, pid: process.pid }, () => process.exit(3)))"], {
      stdio: ["ignore", "pipe", "pipe", "ipc"], env: ENV,
    });
    const exited = once(child, "exit");
    child.send({ asked: "a b \"c\" \\d" });
    const [said] = (await once(child, "message")) as [{ heard: unknown; pid: number }];
    expect(said.heard).toEqual({ asked: "a b \"c\" \\d" });
    // The program is the helper's child, not the helper.
    expect(said.pid).not.toBe(child.pid);
    expect((await exited)[0]).toBe(3);
  });

  it("says a program that is not there as a shell says it, and ends 127", async () => {
    const child = spawnClean("C:\\no\\such\\node.exe", [], { stdio: ["ignore", "pipe", "pipe"], env: ENV });
    let stderr = "";
    child.stderr!.on("data", (chunk: Buffer) => (stderr += chunk.toString()));
    expect((await once(child, "exit"))[0]).toBe(127);
    expect(stderr).toMatch(/^C:\\no\\such\\node\.exe: /);
  });

  it("takes the program along when the helper is killed, as a file host's stop kills it", async () => {
    const child = spawnClean(process.execPath, ["-e", "process.send(process.pid); setInterval(() => {}, 1000)"], {
      stdio: ["ignore", "ignore", "ignore", "ipc"], env: ENV,
    });
    const [pid] = (await once(child, "message")) as [number];
    expect(alive(pid)).toBe(true);
    child.kill("SIGKILL");
    await once(child, "exit");
    await expect.poll(() => alive(pid), { timeout: 10_000 }).toBe(false);
  });
});
