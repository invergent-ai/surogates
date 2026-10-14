// This computer's name on Windows, as the agent and Settings show it: its host name, the device name Settings →
// System → About shows.

import { hostname } from "node:os";

import { expect, it } from "vitest";

import { importedOn } from "./platforms.js";

it("is the computer's host name", async () => {
  const { computerName } = await importedOn("win32", () => import("../src/shell/computer-name.js"));
  expect(computerName()).toBe(hostname());
});
