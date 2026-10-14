// The data root's state schema at the app's start (src/shell/state-schema.ts): an older app refuses newer data, in words
// that name the version to install, on macOS and Windows; Linux's app records nothing.

import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { newerData, RECORDS_STATE, STATE_RECORD } from "../src/shell/state-schema.js";

let root: string;
beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "state-schema-"));
});
afterEach(() => rmSync(root, { recursive: true, force: true }));

const record = () => JSON.parse(readFileSync(join(root, STATE_RECORD), "utf8")) as unknown;

describe("the state schema at the app's start", () => {
  it("is recorded by the first app, raised by a newer schema's, and kept by one of the same schema", () => {
    expect(newerData(root, 2, "1.0.0", true)).toBeNull();
    expect(record()).toEqual({ schema: 2, version: "1.0.0" });
    expect(newerData(root, 2, "1.0.5", true)).toBeNull();
    expect(record()).toEqual({ schema: 2, version: "1.0.0" });
    expect(newerData(root, 3, "1.1.0", true)).toBeNull();
    expect(record()).toEqual({ schema: 3, version: "1.1.0" });
  });

  it("refuses an older app's start on newer data, naming the version that changed it, and changes nothing", () => {
    newerData(root, 3, "1.1.0", true);
    expect(newerData(root, 2, "1.0.5", true)).toEqual({
      message: "This Surogate is older than the data it would open",
      detail: "Surogate 1.0.5 cannot open this computer's Surogate data, which Surogate 1.1.0 changed. Install Surogate 1.1.0 or later.",
    });
    expect(record()).toEqual({ schema: 3, version: "1.1.0" });
  });

  it("records afresh a record that is not one", () => {
    for (const text of ["not json", "{\"schema\":\"3\",\"version\":\"1.1.0\"}", "{\"schema\":0,\"version\":\"1.1.0\"}", "[3]"]) {
      writeFileSync(join(root, STATE_RECORD), text);
      expect(newerData(root, 2, "1.0.0", true)).toBeNull();
      expect(record()).toEqual({ schema: 2, version: "1.0.0" });
    }
  });

  it("reads and writes nothing where this OS's app records nothing, as Linux's", () => {
    expect(newerData(root, 2, "1.0.0", false)).toBeNull();
    expect(existsSync(join(root, STATE_RECORD))).toBe(false);
    expect(RECORDS_STATE).toBe(process.platform === "darwin" || process.platform === "win32");
  });
});
