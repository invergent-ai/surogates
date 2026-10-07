// Section 11's list of what went with srt around commands, and what stays: srt wraps the file helper alone.

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, it } from "vitest";

import * as processes from "../src/guest/processes.js";
import * as record from "../src/hosts/folder-record.js";

const SRC = fileURLToPath(new URL("../src", import.meta.url));

it("wraps the file helper alone in srt, and keeps none of the command layer srt wrapped", () => {
  const files = readdirSync(SRC, { recursive: true, encoding: "utf8" }).filter((name) => name.endsWith(".ts"));
  const using = files.filter((name) => readFileSync(join(SRC, name), "utf8").includes("@anthropic-ai/sandbox-runtime"));
  expect(using.sort()).toEqual(["hosts/host.ts", "hosts/policy.ts"]);
  expect(readFileSync(join(SRC, "hosts", "host.ts"), "utf8").match(/wrapWithSandboxArgv\(/g)).toHaveLength(1);
  for (const gone of ["run.ts", "session-runner.ts", "restarts.ts", "environment.ts"]) expect(existsSync(join(SRC, "hosts", gone))).toBe(false);
  expect(Object.keys(record).filter((name) => ["PLACEHOLDERS", "presentIn", "removePlaceholders"].includes(name))).toEqual([]);
  expect(Object.keys(processes).filter((name) => ["RESTARTED", "restartNotice"].includes(name))).toEqual([]);
  expect(["restart", "takeNotice"].filter((name) => name in processes.Processes.prototype)).toEqual([]);
  expect(readFileSync(join(SRC, "guest", "runner.ts"), "utf8")).not.toContain("SUROGATE_PROCESS");
});
