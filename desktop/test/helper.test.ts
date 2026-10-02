import { spawn } from "node:child_process";
import { mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

const HELPER = fileURLToPath(new URL("../dist/files/helper.js", import.meta.url));

let base = "";
beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "helper-")));
  writeFileSync(join(base, "a.txt"), "alpha\n");
});
afterEach(() => rmSync(base, { recursive: true, force: true }));

// The helper outside any sandbox: what it says to a line it cannot act on.
async function lines(input: string[], count: number): Promise<unknown[]> {
  const child = spawn(process.execPath, [HELPER], {
    env: { SUROGATE_FOLDER: base, HOME: base, PATH: "/usr/bin:/bin" },
    stdio: ["pipe", "pipe", "inherit"],
  });
  const heard: unknown[] = [];
  const done = new Promise<void>((resolve) => {
    createInterface({ input: child.stdout }).on("line", (line) => {
      heard.push(JSON.parse(line));
      if (heard.length === count) resolve();
    });
  });
  for (const line of input) child.stdin.write(`${line}\n`);
  await done;
  child.kill("SIGKILL");
  return heard;
}

describe("the file helper", () => {
  it("says {ready: true} first, then answers a request", async () => {
    const heard = await lines([JSON.stringify({ id: "1", kind: "stat", args: { key: `${base}/a.txt` } })], 2);
    expect(heard[0]).toEqual({ ready: true });
    expect(heard[1]).toMatchObject({ id: "1", outcome: { ok: expect.anything() } });
  });

  it("ignores a line that is not a request, and keeps answering", async () => {
    const heard = await lines(
      ["null", "5", "[]", "not json", JSON.stringify({ id: "1", kind: "stat", args: { key: `${base}/a.txt` } })],
      2,
    );
    expect(heard[1]).toMatchObject({ id: "1", outcome: { ok: expect.anything() } });
  });

  it("answers a request with an id and no usable kind or args, instead of staying silent", async () => {
    const malformed = { error: { type: "value", message: "malformed request" } };
    const heard = await lines(
      [
        JSON.stringify({ id: "1", kind: 5, args: {} }),
        JSON.stringify({ id: "2", kind: "stat", args: null }),
        JSON.stringify({ id: "3", kind: "stat" }),
      ],
      4,
    );
    expect(heard.slice(1)).toEqual([
      { id: "1", outcome: malformed },
      { id: "2", outcome: malformed },
      { id: "3", outcome: malformed },
    ]);
  });
});
