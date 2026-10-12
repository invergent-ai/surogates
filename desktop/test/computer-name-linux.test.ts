// This computer's name on Linux, as the agent and Settings show it: its host name.

import { hostname } from "node:os";

import { expect, it } from "vitest";

import { computerName } from "../src/shell/computer-name-linux.js";

it("is the computer's host name", () => {
  expect(computerName()).toBe(hostname());
});
